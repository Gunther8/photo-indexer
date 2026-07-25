from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QCheckBox, QSpinBox, QDateEdit, QGroupBox,
    QPushButton, QScrollArea, QFrame,
)
from PyQt6.QtCore import pyqtSignal, QDate, Qt

from core.database import get_db


class SearchPanel(QWidget):
    filters_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaximumWidth(220)
        self._db = get_db()
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setSpacing(6)

        # Media type
        grp = QGroupBox("类型")
        fl = QVBoxLayout(grp)
        self._type_combo = QComboBox()
        self._type_combo.addItems(["全部", "照片", "视频"])
        self._type_combo.currentIndexChanged.connect(self._emit)
        fl.addWidget(self._type_combo)
        layout.addWidget(grp)

        # Date range
        grp2 = QGroupBox("拍摄时间")
        dl = QVBoxLayout(grp2)
        dl.addWidget(QLabel("从"))
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDate(QDate(2000, 1, 1))
        self._date_from.setSpecialValueText("不限")
        self._date_from.dateChanged.connect(self._emit)
        dl.addWidget(self._date_from)
        dl.addWidget(QLabel("到"))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDate(QDate.currentDate())
        self._date_to.dateChanged.connect(self._emit)
        dl.addWidget(self._date_to)
        layout.addWidget(grp2)

        # Location
        grp3 = QGroupBox("地点")
        ll = QVBoxLayout(grp3)
        self._country_combo = QComboBox()
        self._country_combo.addItem("全部国家")
        self._country_combo.currentIndexChanged.connect(self._emit)
        ll.addWidget(QLabel("国家/地区"))
        ll.addWidget(self._country_combo)

        self._province_combo = QComboBox()
        self._province_combo.addItem("全部省份")
        self._province_combo.currentIndexChanged.connect(self._emit)
        ll.addWidget(QLabel("省份/州"))
        ll.addWidget(self._province_combo)

        self._city_combo = QComboBox()
        self._city_combo.addItem("全部城市")
        self._city_combo.currentIndexChanged.connect(self._emit)
        ll.addWidget(QLabel("城市"))
        ll.addWidget(self._city_combo)
        layout.addWidget(grp3)

        # Camera
        grp4 = QGroupBox("设备")
        cl = QVBoxLayout(grp4)
        self._camera_combo = QComboBox()
        self._camera_combo.addItem("全部设备")
        self._camera_combo.currentIndexChanged.connect(self._emit)
        cl.addWidget(self._camera_combo)
        layout.addWidget(grp4)

        # Scene
        grp5 = QGroupBox("场景")
        sl = QVBoxLayout(grp5)
        self._weather_combo = QComboBox()
        self._weather_combo.addItems(["全部天气", "晴天", "阴天", "雨天", "雪天", "黄昏", "夜晚"])
        self._weather_combo.currentIndexChanged.connect(self._emit)
        sl.addWidget(QLabel("天气"))
        sl.addWidget(self._weather_combo)

        self._season_combo = QComboBox()
        self._season_combo.addItems(["全部季节", "春", "夏", "秋", "冬", "不确定"])
        self._season_combo.currentIndexChanged.connect(self._emit)
        sl.addWidget(QLabel("季节"))
        sl.addWidget(self._season_combo)

        self._mood_combo = QComboBox()
        self._mood_combo.addItems(["全部情绪", "壮阔", "温馨", "孤独", "热闹", "神秘", "其他"])
        self._mood_combo.currentIndexChanged.connect(self._emit)
        sl.addWidget(QLabel("情绪"))
        sl.addWidget(self._mood_combo)
        layout.addWidget(grp5)

        # Creative use
        grp6 = QGroupBox("创作用途")
        crv = QVBoxLayout(grp6)
        self._chk_cover = QCheckBox("适合封面")
        self._chk_cover.stateChanged.connect(self._emit)
        self._chk_broll = QCheckBox("适合B-roll")
        self._chk_broll.stateChanged.connect(self._emit)
        self._chk_caption = QCheckBox("适合配文")
        self._chk_caption.stateChanged.connect(self._emit)
        self._chk_person = QCheckBox("包含人物")
        self._chk_person.stateChanged.connect(self._emit)
        crv.addWidget(self._chk_cover)
        crv.addWidget(self._chk_broll)
        crv.addWidget(self._chk_caption)
        crv.addWidget(self._chk_person)
        layout.addWidget(grp6)

        # Quality
        grp7 = QGroupBox("质量")
        ql = QVBoxLayout(grp7)
        ql.addWidget(QLabel("最低质量分"))
        self._quality_spin = QSpinBox()
        self._quality_spin.setRange(1, 5)
        self._quality_spin.setValue(1)
        self._quality_spin.valueChanged.connect(self._emit)
        ql.addWidget(self._quality_spin)
        ql.addWidget(QLabel("最低视觉冲击力"))
        self._impact_spin = QSpinBox()
        self._impact_spin.setRange(1, 5)
        self._impact_spin.setValue(1)
        self._impact_spin.valueChanged.connect(self._emit)
        ql.addWidget(self._impact_spin)
        layout.addWidget(grp7)

        reset_btn = QPushButton("重置所有筛选")
        reset_btn.clicked.connect(self._reset)
        layout.addWidget(reset_btn)
        layout.addStretch()

    def populate_combos(self):
        """Load distinct values from DB into combo boxes."""
        for field, combo, placeholder in [
            ("geo_country", self._country_combo, "全部国家"),
            ("geo_province", self._province_combo, "全部省份"),
            ("geo_city", self._city_combo, "全部城市"),
            ("camera_model", self._camera_combo, "全部设备"),
        ]:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(placeholder)
            for val in self._db.get_filter_options(field):
                combo.addItem(val)
            combo.blockSignals(False)

    def _emit(self):
        self.filters_changed.emit(self.current_filters())

    def current_filters(self) -> dict:
        f: dict = {}

        t = self._type_combo.currentIndex()
        if t == 1:
            f["media_type"] = "photo"
        elif t == 2:
            f["media_type"] = "video"

        date_from = self._date_from.date()
        date_to = self._date_to.date()
        if date_from != QDate(2000, 1, 1):
            f["date_from"] = date_from.toString("yyyy-MM-dd")
        if date_to != QDate.currentDate():
            f["date_to"] = date_to.toString("yyyy-MM-dd") + " 23:59:59"

        if self._country_combo.currentIndex() > 0:
            f["geo_country"] = self._country_combo.currentText()
        if self._province_combo.currentIndex() > 0:
            f["geo_province"] = self._province_combo.currentText()
        if self._city_combo.currentIndex() > 0:
            f["geo_city"] = self._city_combo.currentText()
        if self._camera_combo.currentIndex() > 0:
            f["camera_model"] = self._camera_combo.currentText()

        if self._weather_combo.currentIndex() > 0:
            f["weather"] = self._weather_combo.currentText()
        if self._season_combo.currentIndex() > 0:
            f["season"] = self._season_combo.currentText()
        if self._mood_combo.currentIndex() > 0:
            f["mood"] = self._mood_combo.currentText()

        if self._chk_cover.isChecked():
            f["is_cover_worthy"] = True
        if self._chk_broll.isChecked():
            f["is_broll"] = True
        if self._chk_person.isChecked():
            f["has_person"] = True

        if self._quality_spin.value() > 1:
            f["quality_score_min"] = self._quality_spin.value()
        if self._impact_spin.value() > 1:
            f["visual_impact_min"] = self._impact_spin.value()

        return f

    def _reset(self):
        self._type_combo.setCurrentIndex(0)
        self._date_from.setDate(QDate(2000, 1, 1))
        self._date_to.setDate(QDate.currentDate())
        for combo in (self._country_combo, self._province_combo, self._city_combo,
                      self._camera_combo, self._weather_combo, self._season_combo,
                      self._mood_combo):
            combo.setCurrentIndex(0)
        for chk in (self._chk_cover, self._chk_broll, self._chk_caption, self._chk_person):
            chk.setChecked(False)
        self._quality_spin.setValue(1)
        self._impact_spin.setValue(1)
        self._emit()
