import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QFrame, QTextEdit,
    QGridLayout, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap, QFont

from config import get_config
from core.database import get_db


def _tag_label(text: str, color: str = "#e8f4fd", text_color: str = "#1a5276") -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"background:{color}; color:{text_color}; border-radius:3px; padding:2px 6px; font-size:11px;"
    )
    return lbl


class TagFlow(QWidget):
    """A simple flow layout for tag chips."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._layout.addStretch()

    def set_tags(self, tags: list[str], color="#e8f4fd", text_color="#1a5276"):
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for tag in tags:
            lbl = _tag_label(tag, color, text_color)
            self._layout.insertWidget(self._layout.count() - 1, lbl)


class DetailPanel(QWidget):
    download_requested = pyqtSignal(int, bool)  # media_id, is_raw

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg = get_config()
        self._db = get_db()
        self._current_media: Optional[dict] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        inner = QVBoxLayout(container)
        inner.setSpacing(8)

        # Thumbnail
        self._thumb = QLabel("选择一张图片查看详情")
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setFixedHeight(220)
        self._thumb.setStyleSheet("background:#f5f5f5; border-radius:4px;")
        inner.addWidget(self._thumb)

        # Description
        self._desc = QTextEdit()
        self._desc.setReadOnly(True)
        self._desc.setMaximumHeight(100)
        self._desc.setPlaceholderText("AI描述")
        inner.addWidget(self._desc)

        # Meta grid
        grid = QGridLayout()
        grid.setSpacing(4)
        self._meta_labels: dict[str, QLabel] = {}
        for i, (key, label) in enumerate([
            ("taken_at", "拍摄时间"),
            ("camera", "设备"),
            ("location", "位置"),
            ("weather", "天气"),
            ("season", "季节"),
            ("mood", "情绪"),
            ("quality_score", "质量"),
            ("visual_impact", "视觉冲击力"),
        ]):
            grid.addWidget(QLabel(f"<b>{label}</b>"), i, 0, Qt.AlignmentFlag.AlignTop)
            val = QLabel("—")
            val.setWordWrap(True)
            val.setStyleSheet("color: #333;")
            self._meta_labels[key] = val
            grid.addWidget(val, i, 1)
        inner.addLayout(grid)

        # Tags
        inner.addWidget(QLabel("<b>场景标签</b>"))
        self._scene_flow = TagFlow()
        inner.addWidget(self._scene_flow)

        inner.addWidget(QLabel("<b>物体清单</b>"))
        self._objects_flow = TagFlow()
        inner.addWidget(self._objects_flow)

        # Creative flags
        inner.addWidget(QLabel("<b>创作标记</b>"))
        self._creative_row = QHBoxLayout()
        inner.addLayout(self._creative_row)

        inner.addStretch()

        # Buttons
        btn_row = QHBoxLayout()
        self._dl_btn = QPushButton("下载原图")
        self._dl_raw_btn = QPushButton("下载RAW")
        self._copy_path_btn = QPushButton("复制路径")
        self._dl_btn.clicked.connect(lambda: self._download(False))
        self._dl_raw_btn.clicked.connect(lambda: self._download(True))
        self._copy_path_btn.clicked.connect(self._copy_path)
        btn_row.addWidget(self._dl_btn)
        btn_row.addWidget(self._dl_raw_btn)
        btn_row.addWidget(self._copy_path_btn)
        layout.addLayout(btn_row)

        self._set_buttons_enabled(False)

    def show_media(self, media_id: int):
        media = self._db.get_media_by_id(media_id)
        if not media:
            return
        self._current_media = media

        # Thumbnail from cache
        cache = media.get("thumbnail_cache")
        if cache and Path(cache).exists():
            pix = QPixmap(cache)
            if not pix.isNull():
                self._thumb.setPixmap(
                    pix.scaled(300, 220,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
                )
        else:
            self._thumb.setText(media["filename"])

        self._desc.setPlainText(media.get("description") or "")

        # Camera
        make = media.get("camera_make") or ""
        model = media.get("camera_model") or ""
        camera_str = f"{make} {model}".strip() or "—"

        # Location
        parts = filter(None, [
            media.get("geo_district"),
            media.get("geo_city"),
            media.get("geo_province"),
            media.get("geo_country"),
        ])
        loc_str = " · ".join(parts) or "—"

        self._meta_labels["taken_at"].setText(media.get("taken_at") or "—")
        self._meta_labels["camera"].setText(camera_str)
        self._meta_labels["location"].setText(loc_str)
        self._meta_labels["weather"].setText(media.get("weather") or "—")
        self._meta_labels["season"].setText(media.get("season") or "—")
        self._meta_labels["mood"].setText(media.get("mood") or "—")
        self._meta_labels["quality_score"].setText("⭐" * (media.get("quality_score") or 0))
        self._meta_labels["visual_impact"].setText("⚡" * (media.get("visual_impact") or 0))

        # Tags
        scene_tags = json.loads(media.get("scene_tags") or "[]")
        self._scene_flow.set_tags(scene_tags)

        objects = json.loads(media.get("objects") or "[]")
        self._objects_flow.set_tags(objects, "#fef9e7", "#7d6608")

        # Creative flags
        while self._creative_row.count():
            item = self._creative_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        flags = []
        if media.get("is_cover_worthy"):
            flags.append(("封面", "#e8f8f5", "#1e8449"))
        if media.get("is_caption_worthy"):
            flags.append(("配文", "#f9ebea", "#922b21"))
        if media.get("is_broll"):
            flags.append(("B-roll", "#eaf2ff", "#1a5276"))
        if media.get("has_person"):
            flags.append((f"人物×{media.get('person_count', 1)}", "#fdf2f8", "#76448a"))
        if media.get("is_vertical_comp"):
            flags.append(("竖构图", "#fef5e7", "#935116"))
        for text, bg, fg in flags:
            self._creative_row.addWidget(_tag_label(text, bg, fg))
        self._creative_row.addStretch()

        self._dl_raw_btn.setEnabled(bool(media.get("has_raw_pair")))
        self._set_buttons_enabled(True)

    def _set_buttons_enabled(self, enabled: bool):
        self._dl_btn.setEnabled(enabled)
        self._copy_path_btn.setEnabled(enabled)

    def _download(self, is_raw: bool):
        if self._current_media:
            self.download_requested.emit(self._current_media["id"], is_raw)

    def _copy_path(self):
        if self._current_media:
            QApplication.clipboard().setText(self._current_media["pan_path"])
