import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QStatusBar, QSplitter,
    QProgressBar, QLabel, QMenuBar, QFileDialog,
    QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QAction

from config import get_config
from core.database import get_db
from core.baidu_pan import BaiduPan
from core.indexer import Indexer
from ui.search_panel import SearchPanel
from ui.thumbnail_grid import ThumbnailGrid
from ui.detail_panel import DetailPanel
from ui.settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal()
    error = pyqtSignal(str)


class IndexWorker(QThread):
    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode
        self.signals = WorkerSignals()

    def run(self):
        try:
            indexer = Indexer()
            if self.mode == "scan":
                count = indexer.scan(self.signals.progress.emit)
                self.signals.progress.emit(0, 0, f"扫描完成，发现 {count} 个新文件")
            elif self.mode == "process":
                indexer.process_pending(self.signals.progress.emit)
            elif self.mode == "check_batch":
                indexer.check_batch_jobs(self.signals.progress.emit)
            self.signals.finished.emit()
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            self.signals.error.emit(str(e))


class DownloadWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, media_id: int, is_raw: bool):
        super().__init__()
        self.media_id = media_id
        self.is_raw = is_raw

    def run(self):
        try:
            db = get_db()
            pan = BaiduPan()
            cfg = get_config()

            media = db.get_media_by_id(self.media_id)
            if not media:
                self.error.emit("找不到媒体记录")
                return

            if self.is_raw:
                pan_path = media.get("raw_pan_path")
                fs_id = None  # Need to look up by path
                if not pan_path:
                    self.error.emit("该文件没有对应的RAW文件")
                    return
            else:
                fs_id = media.get("pan_fs_id")

            dl_dir = Path(cfg.get("download_dir"))
            dl_dir.mkdir(parents=True, exist_ok=True)
            dest = str(dl_dir / media["filename"])

            if fs_id:
                pan.download_file(fs_id, dest)
            self.finished.emit(dest)
        except Exception as e:
            self.error.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._cfg = get_config()
        self._db = get_db()
        self._pan = BaiduPan()
        self._worker: Optional[QThread] = None
        self._current_filters: dict = {}
        self._current_query: str = ""

        self.setWindowTitle("百度网盘照片索引器")
        self.resize(1200, 800)
        self._build_ui()
        self._build_menu()
        self._refresh_results()

        # 每2分钟自动检查一次批处理结果
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._auto_check_batch)
        self._poll_timer.start(2 * 60 * 1000)
        self._update_batch_label()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Search bar
        search_row = QHBoxLayout()
        search_row.setContentsMargins(8, 6, 8, 6)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索描述、标签、地点…（回车搜索）")
        self._search_input.returnPressed.connect(self._on_search)
        self._search_input.setMinimumHeight(32)
        search_btn = QPushButton("搜索")
        search_btn.setFixedHeight(32)
        search_btn.clicked.connect(self._on_search)
        clear_btn = QPushButton("清空")
        clear_btn.setFixedHeight(32)
        clear_btn.clicked.connect(self._clear_search)
        search_row.addWidget(self._search_input)
        search_row.addWidget(search_btn)
        search_row.addWidget(clear_btn)
        root.addLayout(search_row)

        # Main splitter
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(main_splitter)

        # Left: filter panel
        self._filter_panel = SearchPanel()
        self._filter_panel.filters_changed.connect(self._on_filters_changed)
        main_splitter.addWidget(self._filter_panel)

        # Center + right vertical splitter
        right_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.addWidget(right_splitter)

        self._grid = ThumbnailGrid()
        self._grid.media_selected.connect(self._on_media_selected)
        right_splitter.addWidget(self._grid)

        self._detail = DetailPanel()
        self._detail.download_requested.connect(self._on_download)
        right_splitter.addWidget(self._detail)

        main_splitter.setSizes([220, 980])
        right_splitter.setSizes([460, 280])

        # Status bar
        status = QStatusBar()
        self.setStatusBar(status)
        self._progress_bar = QProgressBar()
        self._progress_bar.setMaximumWidth(200)
        self._progress_bar.setVisible(False)
        self._status_label = QLabel("就绪")
        self._batch_label = QLabel("")
        self._batch_label.setStyleSheet("color: #888;")
        status.addWidget(self._status_label, 1)
        status.addPermanentWidget(self._batch_label)
        status.addPermanentWidget(self._progress_bar)

    def _build_menu(self):
        mb = QMenuBar()
        self.setMenuBar(mb)

        file_menu = mb.addMenu("文件")
        exit_act = QAction("退出", self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        index_menu = mb.addMenu("索引")
        scan_act = QAction("扫描网盘新文件", self)
        scan_act.triggered.connect(lambda: self._start_worker("scan"))
        index_menu.addAction(scan_act)

        process_act = QAction("处理待分析文件", self)
        process_act.triggered.connect(lambda: self._start_worker("process"))
        index_menu.addAction(process_act)

        batch_act = QAction("检查批处理任务", self)
        batch_act.triggered.connect(lambda: self._start_worker("check_batch"))
        index_menu.addAction(batch_act)

        settings_act = QAction("设置", self)
        settings_act.triggered.connect(self._open_settings)
        mb.addMenu("设置").addAction(settings_act)

    def _on_search(self):
        self._current_query = self._search_input.text().strip()
        self._refresh_results()

    def _clear_search(self):
        self._search_input.clear()
        self._current_query = ""
        self._refresh_results()

    def _on_filters_changed(self, filters: dict):
        self._current_filters = filters
        self._refresh_results()

    def _refresh_results(self):
        results = self._db.search(
            query=self._current_query,
            filters=self._current_filters,
            limit=200,
        )
        self._grid.load_results(results)
        counts = self._db.count_by_status()
        done = counts.get("done", 0)
        pending = counts.get("pending", 0) + counts.get("processing", 0)
        total = sum(counts.values())
        self._status_label.setText(
            f"显示 {len(results)} 条 | 已分析: {done} | 待处理: {pending} | 总计: {total}"
        )

    def _on_media_selected(self, media_id: int):
        self._detail.show_media(media_id)

        # Async load thumbnail URL if not cached
        media = self._db.get_media_by_id(media_id)
        if media and not media.get("thumbnail_cache"):
            try:
                url = self._pan.get_thumbnail_url(media["pan_path"])
                if url:
                    self._grid.load_thumbnail_from_url(media_id, url)
            except Exception:
                pass

    def _on_download(self, media_id: int, is_raw: bool):
        worker = DownloadWorker(media_id, is_raw)
        worker.finished.connect(lambda path: QMessageBox.information(
            self, "下载完成", f"文件已保存到:\n{path}"
        ))
        worker.error.connect(lambda err: QMessageBox.critical(self, "下载失败", err))
        worker.start()

    def _start_worker(self, mode: str):
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "提示", "当前有任务正在运行，请等待完成")
            return
        self._worker = IndexWorker(mode)
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.finished.connect(self._on_worker_done)
        self._worker.signals.error.connect(
            lambda err: QMessageBox.critical(self, "错误", err)
        )
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._worker.start()

    def _on_progress(self, current: int, total: int, msg: str):
        self._status_label.setText(msg)
        if total > 0:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(current)

    def _on_worker_done(self):
        self._progress_bar.setVisible(False)
        self._filter_panel.populate_combos()
        self._refresh_results()
        self._update_batch_label()

    def _update_batch_label(self):
        jobs = self._db.get_pending_batch_jobs()
        if jobs:
            self._batch_label.setText(f"AI批处理任务: {len(jobs)} 个待完成")
        else:
            self._batch_label.setText("")

    def _auto_check_batch(self):
        """后台静默检查批处理结果，有任务时才启动worker"""
        jobs = self._db.get_pending_batch_jobs()
        if not jobs:
            self._update_batch_label()
            return
        if self._worker and self._worker.isRunning():
            return  # 主任务运行中，跳过本次轮询
        logger.info(f"自动轮询: 检查 {len(jobs)} 个批处理任务...")
        self._worker = IndexWorker("check_batch")
        self._worker.signals.progress.connect(self._on_batch_poll_progress)
        self._worker.signals.finished.connect(self._on_batch_poll_done)
        self._worker.signals.error.connect(lambda e: logger.error(f"批处理轮询失败: {e}"))
        self._worker.start()

    def _on_batch_poll_progress(self, current: int, total: int, msg: str):
        self._batch_label.setText(msg)

    def _on_batch_poll_done(self):
        self._update_batch_label()
        self._refresh_results()  # 有新结果时刷新搜索列表

    def _open_settings(self):
        dlg = SettingsDialog(self)
        dlg.exec()
        self._filter_panel.populate_combos()
