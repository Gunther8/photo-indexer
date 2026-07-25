import os
import logging
from pathlib import Path
from typing import Optional

import requests
from PyQt6.QtWidgets import (
    QWidget, QGridLayout, QLabel, QScrollArea,
    QFrame, QSizePolicy, QVBoxLayout,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QThreadPool, QRunnable, QObject
from PyQt6.QtGui import QPixmap, QImage

from config import get_config
from core.database import get_db

logger = logging.getLogger(__name__)

THUMB_SIZE = 160
COLS = 4


class ThumbnailSignals(QObject):
    loaded = pyqtSignal(int, QPixmap)  # media_id, pixmap


class ThumbnailLoader(QRunnable):
    def __init__(self, media_id: int, url: str, cache_path: str, signals: ThumbnailSignals):
        super().__init__()
        self.media_id = media_id
        self.url = url
        self.cache_path = cache_path
        self.signals = signals

    def run(self):
        try:
            # Check local cache first
            if self.cache_path and os.path.exists(self.cache_path):
                pixmap = QPixmap(self.cache_path)
                if not pixmap.isNull():
                    self.signals.loaded.emit(self.media_id, pixmap)
                    return

            if not self.url:
                return

            resp = requests.get(self.url, timeout=15)
            resp.raise_for_status()

            # Save to cache
            if self.cache_path:
                Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.cache_path, "wb") as f:
                    f.write(resp.content)

            pixmap = QPixmap()
            pixmap.loadFromData(resp.content)
            if not pixmap.isNull():
                self.signals.loaded.emit(self.media_id, pixmap)

        except Exception as e:
            logger.debug(f"Thumbnail load failed for media {self.media_id}: {e}")


class ThumbnailCell(QFrame):
    clicked = pyqtSignal(int)  # media_id

    def __init__(self, media_id: int, filename: str, parent=None):
        super().__init__(parent)
        self.media_id = media_id
        self.setFixedSize(THUMB_SIZE + 8, THUMB_SIZE + 28)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        self._img_label = QLabel()
        self._img_label.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setText("⏳")
        layout.addWidget(self._img_label)

        name_label = QLabel(filename[:20] + "…" if len(filename) > 20 else filename)
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setStyleSheet("font-size: 10px; color: #666;")
        layout.addWidget(name_label)

    def set_pixmap(self, pixmap: QPixmap):
        scaled = pixmap.scaled(
            THUMB_SIZE, THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._img_label.setPixmap(scaled)

    def set_selected(self, selected: bool):
        self.setStyleSheet(
            "QFrame { border: 2px solid #0078d4; border-radius: 4px; }"
            if selected else ""
        )

    def mousePressEvent(self, event):
        self.clicked.emit(self.media_id)


class ThumbnailGrid(QWidget):
    media_selected = pyqtSignal(int)  # media_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = get_config()
        self._db = get_db()
        self._pool = QThreadPool.globalInstance()
        self._pool.setMaxThreadCount(4)

        self._cells: dict[int, ThumbnailCell] = {}
        self._selected_id: Optional[int] = None
        self._media_items: list[dict] = []

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        scroll.setWidget(container)
        self._grid = QGridLayout(container)
        self._grid.setSpacing(6)
        self._grid.setContentsMargins(6, 6, 6, 6)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def load_results(self, media_items: list[dict]):
        self._media_items = media_items
        self._clear_grid()
        self._cells.clear()
        self._selected_id = None

        for idx, item in enumerate(media_items):
            media_id = item["id"]
            cell = ThumbnailCell(media_id, item["filename"])
            cell.clicked.connect(self._on_cell_click)
            self._cells[media_id] = cell

            row, col = divmod(idx, COLS)
            self._grid.addWidget(cell, row, col)

            self._start_thumb_load(item)

    def _clear_grid(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _start_thumb_load(self, item: dict):
        media_id = item["id"]
        cache_path = item.get("thumbnail_cache") or self._default_cache_path(media_id)
        pan_path = item.get("pan_path", "")

        signals = ThumbnailSignals()
        signals.loaded.connect(self._on_thumb_loaded)

        # We don't request a new thumbnail URL here — that would require the BaiduPan object.
        # Instead, load from cache only in the background thread; the main window
        # can pre-populate thumbnail_cache via baidu_pan.get_thumbnail_url when needed.
        loader = ThumbnailLoader(media_id, "", cache_path, signals)
        self._pool.start(loader)

    def _default_cache_path(self, media_id: int) -> str:
        cache_dir = self._cfg.get("thumbnail_cache_dir")
        return str(Path(cache_dir) / f"{media_id}.jpg")

    def _on_thumb_loaded(self, media_id: int, pixmap: QPixmap):
        cell = self._cells.get(media_id)
        if cell:
            cell.set_pixmap(pixmap)
            # Persist cache path to DB if not already set
            item = next((m for m in self._media_items if m["id"] == media_id), None)
            if item and not item.get("thumbnail_cache"):
                cache_path = self._default_cache_path(media_id)
                self._db.update_media(media_id, {"thumbnail_cache": cache_path})

    def _on_cell_click(self, media_id: int):
        if self._selected_id is not None and self._selected_id in self._cells:
            self._cells[self._selected_id].set_selected(False)
        self._selected_id = media_id
        self._cells[media_id].set_selected(True)
        self.media_selected.emit(media_id)

    def load_thumbnail_from_url(self, media_id: int, url: str):
        """Called by main window to inject a live thumbnail URL."""
        item = next((m for m in self._media_items if m["id"] == media_id), None)
        cache_path = self._default_cache_path(media_id)
        signals = ThumbnailSignals()
        signals.loaded.connect(self._on_thumb_loaded)
        loader = ThumbnailLoader(media_id, url, cache_path, signals)
        self._pool.start(loader)
