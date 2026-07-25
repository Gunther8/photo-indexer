import webbrowser
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QLabel, QGroupBox,
    QComboBox, QFileDialog, QMessageBox, QTabWidget, QWidget,
    QSpinBox, QTextEdit,
)
from PyQt6.QtCore import Qt

from config import get_config
from core.baidu_pan import BaiduPan


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        self._cfg = get_config()
        self._pan = BaiduPan()
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._build_api_tab(), "API 配置")
        tabs.addTab(self._build_baidu_tab(), "百度网盘授权")
        tabs.addTab(self._build_paths_tab(), "路径与参数")

        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存")
        cancel_btn = QPushButton("取消")
        save_btn.clicked.connect(self._save)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _build_api_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)
        form.setVerticalSpacing(10)

        self._qwen_key = QLineEdit()
        self._qwen_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._qwen_key.setPlaceholderText("sk-...")
        form.addRow("阿里云百炼 API Key:", self._qwen_key)

        self._amap_key = QLineEdit()
        self._amap_key.setPlaceholderText("高德地图 Web API Key")
        form.addRow("高德地图 API Key:", self._amap_key)

        return w

    def _build_baidu_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)

        grp = QGroupBox("百度网盘应用凭证")
        form = QFormLayout(grp)
        self._bdk_key = QLineEdit()
        self._bdk_secret = QLineEdit()
        self._bdk_secret.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("App Key:", self._bdk_key)
        form.addRow("Secret Key:", self._bdk_secret)
        layout.addWidget(grp)

        grp2 = QGroupBox("OAuth 授权")
        auth_layout = QVBoxLayout(grp2)

        open_btn = QPushButton("1. 打开授权页面（浏览器）")
        open_btn.clicked.connect(self._open_auth_url)
        auth_layout.addWidget(open_btn)

        auth_layout.addWidget(QLabel("2. 复制授权码并粘贴到下方："))
        code_row = QHBoxLayout()
        self._auth_code = QLineEdit()
        self._auth_code.setPlaceholderText("粘贴授权码")
        exchange_btn = QPushButton("获取 Token")
        exchange_btn.clicked.connect(self._exchange_code)
        code_row.addWidget(self._auth_code)
        code_row.addWidget(exchange_btn)
        auth_layout.addLayout(code_row)

        self._auth_status = QLabel("未授权")
        self._auth_status.setStyleSheet("color: gray;")
        auth_layout.addWidget(self._auth_status)

        layout.addWidget(grp2)
        layout.addStretch()
        return w

    def _build_paths_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)
        form.setVerticalSpacing(10)

        self._scan_path = QLineEdit()
        self._scan_path.setPlaceholderText("/")
        form.addRow("网盘扫描路径:", self._scan_path)

        thumb_row = QHBoxLayout()
        self._thumb_dir = QLineEdit()
        thumb_browse = QPushButton("浏览")
        thumb_browse.clicked.connect(lambda: self._browse_dir(self._thumb_dir))
        thumb_row.addWidget(self._thumb_dir)
        thumb_row.addWidget(thumb_browse)
        form.addRow("缩略图缓存目录:", thumb_row)

        dl_row = QHBoxLayout()
        self._download_dir = QLineEdit()
        dl_browse = QPushButton("浏览")
        dl_browse.clicked.connect(lambda: self._browse_dir(self._download_dir))
        dl_row.addWidget(self._download_dir)
        dl_row.addWidget(dl_browse)
        form.addRow("默认下载目录:", dl_row)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["batch（批量，省50%费用）", "realtime（实时）"])
        form.addRow("分析模式:", self._mode_combo)

        # 文件大小过滤
        self._max_video_mb = QSpinBox()
        self._max_video_mb.setRange(0, 100000)
        self._max_video_mb.setSuffix(" MB")
        self._max_video_mb.setSpecialValueText("不限制")
        self._max_video_mb.setToolTip("超过此大小的视频跳过，0=不限制")
        form.addRow("视频最大体积:", self._max_video_mb)

        self._max_photo_mb = QSpinBox()
        self._max_photo_mb.setRange(0, 10000)
        self._max_photo_mb.setSuffix(" MB")
        self._max_photo_mb.setSpecialValueText("不限制")
        self._max_photo_mb.setToolTip("超过此大小的图片跳过，0=不限制")
        form.addRow("图片最大体积:", self._max_photo_mb)

        # 排除路径
        self._exclude_paths = QTextEdit()
        self._exclude_paths.setMaximumHeight(80)
        self._exclude_paths.setPlaceholderText("每行一个路径前缀，例如：\n/网课/\n/晚会/\n/录屏/")
        form.addRow("排除路径（每行一个）:", self._exclude_paths)

        # 并发下载数
        self._download_workers = QSpinBox()
        self._download_workers.setRange(1, 10)
        self._download_workers.setValue(3)
        self._download_workers.setToolTip("同时下载的文件数，网络好可调高（建议3-5）")
        form.addRow("并发下载数:", self._download_workers)

        # 代理
        self._http_proxy = QLineEdit()
        self._http_proxy.setPlaceholderText("http://127.0.0.1:7890  （留空表示不使用代理）")
        self._http_proxy.setToolTip("在印尼等海外地区下载百度网盘文件需要设置国内代理")
        form.addRow("HTTP 代理:", self._http_proxy)

        return w

    def _browse_dir(self, target: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "选择目录", target.text())
        if d:
            target.setText(d)

    def _load_values(self):
        self._qwen_key.setText(self._cfg.get("qwen_api_key", ""))
        self._amap_key.setText(self._cfg.get("amap_api_key", ""))
        self._bdk_key.setText(self._cfg.get("baidu_app_key", ""))
        self._bdk_secret.setText(self._cfg.get("baidu_app_secret", ""))
        self._scan_path.setText(self._cfg.get("scan_path", "/"))
        self._thumb_dir.setText(self._cfg.get("thumbnail_cache_dir", ""))
        self._download_dir.setText(self._cfg.get("download_dir", ""))
        mode = self._cfg.get("analysis_mode", "batch")
        self._mode_combo.setCurrentIndex(0 if mode == "batch" else 1)
        self._max_video_mb.setValue(self._cfg.get("max_video_size_mb", 500))
        self._max_photo_mb.setValue(self._cfg.get("max_photo_size_mb", 50))
        exclude = self._cfg.get("exclude_paths", [])
        self._exclude_paths.setPlainText("\n".join(exclude))
        self._download_workers.setValue(self._cfg.get("download_workers", 3))
        self._http_proxy.setText(self._cfg.get("http_proxy", ""))

        if self._pan.is_authorized():
            self._auth_status.setText("已授权")
            self._auth_status.setStyleSheet("color: green;")

    def _open_auth_url(self):
        self._cfg.update({
            "baidu_app_key": self._bdk_key.text().strip(),
            "baidu_app_secret": self._bdk_secret.text().strip(),
        })
        url = self._pan.get_auth_url()
        webbrowser.open(url)

    def _exchange_code(self):
        code = self._auth_code.text().strip()
        if not code:
            QMessageBox.warning(self, "错误", "请先粘贴授权码")
            return
        self._cfg.update({
            "baidu_app_key": self._bdk_key.text().strip(),
            "baidu_app_secret": self._bdk_secret.text().strip(),
        })
        try:
            self._pan.exchange_code(code)
            self._auth_status.setText("授权成功")
            self._auth_status.setStyleSheet("color: green;")
            QMessageBox.information(self, "成功", "百度网盘授权成功！")
        except Exception as e:
            QMessageBox.critical(self, "授权失败", str(e))

    def _save(self):
        mode = "batch" if self._mode_combo.currentIndex() == 0 else "realtime"
        exclude_raw = self._exclude_paths.toPlainText().strip()
        exclude_list = [p.strip() for p in exclude_raw.splitlines() if p.strip()]
        self._cfg.update({
            "qwen_api_key": self._qwen_key.text().strip(),
            "amap_api_key": self._amap_key.text().strip(),
            "baidu_app_key": self._bdk_key.text().strip(),
            "baidu_app_secret": self._bdk_secret.text().strip(),
            "scan_path": self._scan_path.text().strip() or "/",
            "thumbnail_cache_dir": self._thumb_dir.text().strip(),
            "download_dir": self._download_dir.text().strip(),
            "analysis_mode": mode,
            "max_video_size_mb": self._max_video_mb.value(),
            "max_photo_size_mb": self._max_photo_mb.value(),
            "exclude_paths": exclude_list,
            "download_workers": self._download_workers.value(),
            "http_proxy": self._http_proxy.text().strip(),
        })
        self.accept()
