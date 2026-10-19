"""Native desktop interface for the video archive service."""

import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config import CameraSource, Settings
from desktop_support import app_data_dir, find_ffmpeg_pair, install_ffmpeg
from main import ServiceRuntime


LOG = logging.getLogger("video_archive.desktop")


def default_config():
    root = app_data_dir()
    return {
        "mode": "none",
        "cameras": [],
        "segment_seconds": 3600,
        "recordings_dir": str(root / "recordings"),
        "temp_dir": str(root / "temp"),
        "inbox_dir": str(root / "inbox"),
    }


def config_path() -> Path:
    return app_data_dir() / "settings.json"


def load_config():
    defaults = default_config()
    try:
        with config_path().open("r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            defaults.update(saved)
    except (OSError, ValueError, UnicodeError):
        LOG.warning("Не удалось прочитать настройки; использованы значения по умолчанию")
    if not isinstance(defaults.get("cameras"), list):
        defaults["cameras"] = []
    return defaults


def save_config(config):
    destination = config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(destination))


def make_settings(config, tools=None):
    mode = config.get("mode", "none")
    cameras = tuple(
        CameraSource(camera_id=item["id"], url=item["url"])
        for item in config.get("cameras", [])
    ) if mode == "cameras" else ()
    source_type = "none" if mode == "cameras" else mode
    ffmpeg, ffprobe = tools or ("ffmpeg", "ffprobe")
    return Settings(
        host="127.0.0.1",
        port=0,
        recordings_dir=Path(config["recordings_dir"]).expanduser().resolve(),
        temp_dir=Path(config["temp_dir"]).expanduser().resolve(),
        inbox_dir=Path(config["inbox_dir"]).expanduser().resolve(),
        segment_seconds=float(config.get("segment_seconds", 3600)),
        source_type=source_type,
        cameras=cameras,
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
    )


class WorkerSignals(QObject):
    progress = Signal(int, int)
    ready = Signal(str, str)
    failed = Signal(str)


class FfmpegBootstrap(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = WorkerSignals()

    def run(self):
        try:
            tools = find_ffmpeg_pair()
            if tools is None:
                tools = install_ffmpeg(self.signals.progress.emit)
            self.signals.ready.emit(*tools)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class CameraDialog(QDialog):
    def __init__(self, parent=None, camera=None):
        super().__init__(parent)
        self.setWindowTitle("Камера")
        self.setMinimumWidth(460)
        layout = QFormLayout(self)
        self.camera_id = QLineEdit(camera.get("id", "") if camera else "")
        self.url = QLineEdit(camera.get("url", "") if camera else "")
        self.url.setPlaceholderText("rtsp://пользователь:пароль@адрес/путь")
        layout.addRow("ID источника", self.camera_id)
        layout.addRow("Адрес RTSP/HTTP", self.url)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def value(self):
        return {"id": self.camera_id.text().strip(), "url": self.url.text().strip()}


class DesktopWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Видеоархив")
        self.resize(1260, 860)
        self.setMinimumSize(1020, 700)
        self.config = load_config()
        self.tools = find_ffmpeg_pair()
        self.runtime = None
        self.api = QNetworkAccessManager(self)
        self.live_reply = None
        self.live_buffer = bytearray()
        self.recordings = []
        self.bootstrap_worker = None
        self._build_ui()
        self._load_form()
        self._start_runtime()
        self._poll = QTimer(self)
        self._poll.timeout.connect(self.refresh_state)
        self._poll.start(2500)
        self._recording_refresh = QTimer(self)
        self._recording_refresh.timeout.connect(self.refresh_recordings)
        self._recording_refresh.start(15000)
        self.refresh_state()
        self.refresh_recordings()
        if self.tools is None:
            self._bootstrap_ffmpeg()
        else:
            self._set_tool_status("FFmpeg готов")

    def _build_ui(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f5f3ee; color: #23313a; font-family: "Segoe UI"; font-size: 13px; }
            QWidget#appRoot { background: #f5f3ee; }
            QFrame#appHeader, QFrame#mediaCard, QFrame#liveCard, QFrame#settingsCard { background: #ffffff; border: 1px solid #e7e1d7; border-radius: 14px; }
            QFrame#brandMark { background: #203640; border: 0; border-radius: 11px; }
            QLabel#brandInitials { color: #d7b47c; font-size: 16px; font-weight: 800; }
            QLabel#brandTitle { color: #1e3039; font-size: 20px; font-weight: 700; }
            QLabel#brandSubtitle { color: #7b8587; font-size: 11px; }
            QLabel#statusBadge { color: #345a49; background: #e9f1eb; border: 1px solid #dce9df; border-radius: 12px; padding: 7px 11px; font-weight: 600; }
            QLabel#statusBadge[state="idle"] { color: #66747a; background: #f0efeb; border-color: #e4e1d9; }
            QLabel#statusBadge[state="warning"] { color: #80622d; background: #f4ecdb; border-color: #ebdfc6; }
            QLabel#statusBadge[state="error"] { color: #8f4540; background: #f5e7e5; border-color: #edd6d3; }
            QLabel#toolStatus { color: #657278; font-size: 12px; }
            QTabWidget::pane { border: 0; background: transparent; top: 1px; }
            QTabBar::tab { background: transparent; color: #778187; padding: 12px 17px; margin-right: 8px; border: 0; border-bottom: 2px solid transparent; font-weight: 600; }
            QTabBar::tab:selected { color: #203640; border-bottom: 2px solid #b38a4c; }
            QTabBar::tab:hover:!selected { color: #455963; background: #eeece6; border-radius: 8px; }
            QLineEdit, QComboBox, QSpinBox, QTableWidget { background: #ffffff; color: #263740; border: 1px solid #deded8; border-radius: 8px; padding: 8px 10px; selection-background-color: #d8c29d; }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #b38a4c; }
            QTableWidget { alternate-background-color: #faf9f6; gridline-color: #f0eee8; }
            QTableWidget::item { padding: 7px; border: 0; }
            QTableWidget::item:selected { background: #eee7d9; color: #203640; }
            QPushButton { background: #203640; color: #ffffff; border: 0; border-radius: 8px; padding: 10px 15px; font-weight: 600; }
            QPushButton:hover { background: #314b55; }
            QPushButton:pressed { background: #172a32; }
            QPushButton:disabled { background: #c7cecc; color: #f9faf9; }
            QPushButton#recordButton { background: #a47d46; padding-left: 18px; padding-right: 18px; }
            QPushButton#recordButton:hover { background: #916d3d; }
            QPushButton#recordButton[recording="true"] { background: #a94f48; }
            QPushButton#recordButton[recording="true"]:hover { background: #913e39; }
            QPushButton#secondaryButton { color: #40535a; background: #f4f3ef; border: 1px solid #e4e1d9; }
            QPushButton#secondaryButton:hover { background: #ebe8e0; }
            QHeaderView::section { background: #f5f3ee; color: #66747a; border: 0; border-bottom: 1px solid #e7e1d7; padding: 10px 8px; font-weight: 650; }
            QProgressBar { background: #ebe8e0; border: 0; border-radius: 4px; text-align: center; color: #203640; }
            QProgressBar::chunk { background: #b38a4c; border-radius: 4px; }
            QSlider::groove:horizontal { height: 4px; background: #e4e1d9; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #b38a4c; border-radius: 2px; }
            QSlider::handle:horizontal { background: #ffffff; border: 2px solid #b38a4c; width: 12px; height: 12px; margin: -6px 0; border-radius: 7px; }
        """)
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(12)
        header_frame = QFrame()
        header_frame.setObjectName("appHeader")
        header_frame.setMinimumHeight(78)
        header = QHBoxLayout(header_frame)
        header.setContentsMargins(17, 13, 17, 13)
        header.setSpacing(13)
        brand_mark = QFrame()
        brand_mark.setObjectName("brandMark")
        brand_mark.setFixedSize(44, 44)
        mark_layout = QVBoxLayout(brand_mark)
        mark_layout.setContentsMargins(0, 0, 0, 0)
        mark = QLabel("VA")
        mark.setObjectName("brandInitials")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark_layout.addWidget(mark)
        header.addWidget(brand_mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(2)
        brand = QLabel("Видеоархив")
        brand.setObjectName("brandTitle")
        subtitle = QLabel("МОНИТОРИНГ И ЛОКАЛЬНЫЙ АРХИВ")
        subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(brand)
        brand_text.addWidget(subtitle)
        header.addLayout(brand_text)
        header.addStretch(1)
        self.status_label = QLabel("Подключение…")
        self.status_label.setObjectName("statusBadge")
        self.status_label.setProperty("state", "idle")
        header.addWidget(self.status_label)
        self.record_button = QPushButton("Начать запись")
        self.record_button.setObjectName("recordButton")
        self.record_button.setProperty("recording", False)
        self.record_button.clicked.connect(self.toggle_recording)
        header.addWidget(self.record_button)
        outer.addWidget(header_frame)

        tool_line = QHBoxLayout()
        self.tool_status = QLabel("Проверка FFmpeg…")
        self.tool_status.setObjectName("toolStatus")
        tool_line.addWidget(self.tool_status)
        tool_line.addStretch(1)
        self.tool_progress = QProgressBar()
        self.tool_progress.setRange(0, 100)
        self.tool_progress.setFixedWidth(220)
        self.tool_progress.setMaximumHeight(7)
        self.tool_progress.hide()
        tool_line.addWidget(self.tool_progress)
        outer.addLayout(tool_line)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self._build_live_tab()
        self._build_archive_tab()
        self._build_settings_tab()

    def _build_live_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(7, 13, 7, 7)
        layout.setSpacing(12)
        self._section_heading(layout, "Прямой эфир", "Выберите источник, чтобы открыть просмотр в приложении.")
        controls = QHBoxLayout()
        controls.setSpacing(9)
        self.live_sources = QComboBox()
        self.live_sources.setMinimumWidth(260)
        controls.addWidget(self.live_sources)
        refresh = QPushButton("Обновить")
        refresh.setObjectName("secondaryButton")
        refresh.clicked.connect(self.refresh_sources)
        controls.addWidget(refresh)
        self.live_button = QPushButton("Открыть эфир")
        self.live_button.clicked.connect(self.toggle_live)
        controls.addWidget(self.live_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        preview = QFrame()
        preview.setObjectName("liveCard")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(12, 12, 12, 12)
        self.live_view = QLabel("Выберите камеру и нажмите «Открыть эфир»")
        self.live_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_view.setMinimumSize(600, 360)
        self.live_view.setStyleSheet("background: #18272e; color: #d9e1dc; border-radius: 10px;")
        preview_layout.addWidget(self.live_view, 1)
        self.live_status = QLabel("Прямой просмотр не запущен")
        self.live_status.setObjectName("toolStatus")
        preview_layout.addWidget(self.live_status)
        layout.addWidget(preview, 1)
        self.tabs.addTab(page, "Прямой эфир")

    def _build_archive_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(7, 13, 7, 7)
        layout.setSpacing(12)
        self._section_heading(layout, "Архив записей", "Поиск и воспроизведение готовых видеофайлов.")
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по камере или времени")
        self.search.textChanged.connect(self._populate_recordings)
        controls.addWidget(self.search, 1)
        refresh = QPushButton("Обновить список")
        refresh.setObjectName("secondaryButton")
        refresh.clicked.connect(self.refresh_recordings)
        controls.addWidget(refresh)
        layout.addLayout(controls)
        table_frame = QFrame()
        table_frame.setObjectName("mediaCard")
        table_layout = QVBoxLayout(table_frame)
        table_layout.setContentsMargins(8, 8, 8, 8)
        self.recording_table = QTableWidget(0, 4)
        self.recording_table.setHorizontalHeaderLabels(["Источник", "Время", "Длительность", "Размер"])
        self.recording_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.recording_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.recording_table.setAlternatingRowColors(True)
        self.recording_table.verticalHeader().hide()
        self.recording_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.recording_table.doubleClicked.connect(self.play_selected)
        table_layout.addWidget(self.recording_table)
        layout.addWidget(table_frame, 1)
        player_frame = QFrame()
        player_frame.setObjectName("mediaCard")
        player_layout = QVBoxLayout(player_frame)
        player_layout.setContentsMargins(12, 12, 12, 10)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(210)
        self.video_widget.setStyleSheet("background: #18272e; border-radius: 10px;")
        player_layout.addWidget(self.video_widget, 1)
        playback = QHBoxLayout()
        self.play_button = QPushButton("Воспроизвести")
        self.play_button.setObjectName("secondaryButton")
        self.play_button.clicked.connect(self._toggle_playback)
        playback.addWidget(self.play_button)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self._seek)
        playback.addWidget(self.position_slider, 1)
        self.playback_time = QLabel("00:00 / 00:00")
        self.playback_time.setStyleSheet("color: #66747a; font-size: 12px;")
        playback.addWidget(self.playback_time)
        player_layout.addLayout(playback)
        layout.addWidget(player_frame, 1)
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.errorOccurred.connect(self._player_error)
        self.tabs.addTab(page, "Архив")

    @staticmethod
    def _section_heading(parent_layout, title, description):
        heading = QVBoxLayout()
        heading.setSpacing(3)
        title_label = QLabel(title)
        title_label.setStyleSheet("color: #203640; font-size: 18px; font-weight: 700;")
        description_label = QLabel(description)
        description_label.setStyleSheet("color: #7b8587; font-size: 12px;")
        heading.addWidget(title_label)
        heading.addWidget(description_label)
        parent_layout.addLayout(heading)

    def _build_settings_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(7, 13, 7, 7)
        layout.setSpacing(10)
        self._section_heading(layout, "Источники и настройки", "Добавьте камеры и задайте параметры архивации.")
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        self.mode = QComboBox()
        self.mode.addItem("Сетевые камеры", "cameras")
        self.mode.addItem("Тестовый источник", "synthetic")
        self.mode.addItem("Импорт готовых файлов", "inbox")
        self.mode.addItem("Только просмотр архива", "none")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Режим", self.mode)
        self.segment_seconds = QSpinBox()
        self.segment_seconds.setRange(1, 86400)
        self.segment_seconds.setSuffix(" сек")
        form.addRow("Длина сегмента", self.segment_seconds)
        layout.addLayout(form)
        camera_note = QLabel("Адреса с логином и паролем сохраняются локально в файле настроек.")
        camera_note.setStyleSheet("color: #7b8587; font-size: 12px;")
        layout.addWidget(camera_note)
        self.camera_table = QTableWidget(0, 2)
        self.camera_table.setHorizontalHeaderLabels(["ID", "Адрес потока"])
        self.camera_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.camera_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.camera_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.camera_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.camera_table.setAlternatingRowColors(True)
        self.camera_table.verticalHeader().hide()
        layout.addWidget(self.camera_table, 1)
        camera_buttons = QHBoxLayout()
        add_camera = QPushButton("Добавить камеру")
        add_camera.setObjectName("secondaryButton")
        add_camera.clicked.connect(self.add_camera)
        edit_camera = QPushButton("Изменить")
        edit_camera.setObjectName("secondaryButton")
        edit_camera.clicked.connect(self.edit_camera)
        remove_camera = QPushButton("Удалить")
        remove_camera.setObjectName("secondaryButton")
        remove_camera.clicked.connect(self.remove_camera)
        camera_buttons.addWidget(add_camera)
        camera_buttons.addWidget(edit_camera)
        camera_buttons.addWidget(remove_camera)
        camera_buttons.addStretch(1)
        layout.addLayout(camera_buttons)

        self.recordings_path = self._path_row(layout, "Каталог архива")
        self.temp_path = self._path_row(layout, "Временный каталог")
        self.inbox_path = self._path_row(layout, "Входящие MP4")
        self.apply_button = QPushButton("Сохранить и применить")
        self.apply_button.setStyleSheet("background: #a47d46; padding: 11px 18px;")
        self.apply_button.clicked.connect(self.apply_settings)
        layout.addWidget(self.apply_button)
        self.tabs.addTab(page, "Источники и настройки")

    def _path_row(self, parent_layout, title):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        field = QLineEdit()
        button = QPushButton("Выбрать…")
        button.setObjectName("secondaryButton")
        button.clicked.connect(lambda _=False, target=field: self._browse_directory(target))
        layout.addWidget(field, 1)
        layout.addWidget(button)
        parent_layout.addWidget(QLabel(title))
        parent_layout.addWidget(row)
        return field

    def _browse_directory(self, field):
        chosen = QFileDialog.getExistingDirectory(self, "Выберите каталог", field.text())
        if chosen:
            field.setText(chosen)

    def _load_form(self):
        self._set_mode(self.config.get("mode", "none"))
        self.segment_seconds.setValue(int(self.config.get("segment_seconds", 3600)))
        self.recordings_path.setText(self.config.get("recordings_dir", ""))
        self.temp_path.setText(self.config.get("temp_dir", ""))
        self.inbox_path.setText(self.config.get("inbox_dir", ""))
        self._populate_cameras()
        self._mode_changed()

    def _set_mode(self, mode):
        index = self.mode.findData(mode)
        self.mode.setCurrentIndex(index if index >= 0 else self.mode.findData("none"))

    def _populate_cameras(self):
        cameras = self.config.get("cameras", [])
        self.camera_table.setRowCount(len(cameras))
        for row, camera in enumerate(cameras):
            self.camera_table.setItem(row, 0, QTableWidgetItem(camera.get("id", "")))
            item = QTableWidgetItem(self._hide_url_password(camera.get("url", "")))
            item.setData(Qt.ItemDataRole.UserRole, camera.get("url", ""))
            self.camera_table.setItem(row, 1, item)

    @staticmethod
    def _hide_url_password(url):
        if "@" not in url or "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        credentials, host = rest.rsplit("@", 1)
        if ":" in credentials:
            user = credentials.split(":", 1)[0]
            credentials = user + ":••••"
        return scheme + "://" + credentials + "@" + host

    def _mode_changed(self, *_):
        enabled = self.mode.currentData() == "cameras"
        self.camera_table.setEnabled(enabled)
        for widget in (self.segment_seconds,):
            widget.setEnabled(self.mode.currentData() != "none")

    def _form_config(self):
        cameras = []
        for row in range(self.camera_table.rowCount()):
            id_item = self.camera_table.item(row, 0)
            url_item = self.camera_table.item(row, 1)
            cameras.append({
                "id": id_item.text() if id_item else "",
                "url": url_item.data(Qt.ItemDataRole.UserRole) if url_item else "",
            })
        config = {
            "mode": self.mode.currentData(),
            "cameras": cameras,
            "segment_seconds": self.segment_seconds.value(),
            "recordings_dir": self.recordings_path.text().strip(),
            "temp_dir": self.temp_path.text().strip(),
            "inbox_dir": self.inbox_path.text().strip(),
        }
        for key in ("recordings_dir", "temp_dir", "inbox_dir"):
            if not config[key]:
                raise ValueError("Укажите все каталоги хранения")
        if config["mode"] == "cameras":
            if not cameras:
                raise ValueError("Добавьте хотя бы одну камеру")
            Settings._parse_cameras(json.dumps(cameras))
        return config

    def add_camera(self):
        dialog = CameraDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._insert_camera(dialog.value())

    def edit_camera(self):
        row = self.camera_table.currentRow()
        if row < 0:
            return
        url_item = self.camera_table.item(row, 1)
        dialog = CameraDialog(self, {
            "id": self.camera_table.item(row, 0).text(),
            "url": url_item.data(Qt.ItemDataRole.UserRole),
        })
        if dialog.exec() == QDialog.DialogCode.Accepted:
            camera = dialog.value()
            try:
                Settings._parse_cameras(json.dumps([camera]))
            except ValueError as exc:
                QMessageBox.warning(self, "Проверьте камеру", str(exc))
                return
            self._set_camera_row(row, camera)

    def _insert_camera(self, camera):
        try:
            existing = [
                {"id": self.camera_table.item(row, 0).text(),
                 "url": self.camera_table.item(row, 1).data(Qt.ItemDataRole.UserRole)}
                for row in range(self.camera_table.rowCount())
            ]
            Settings._parse_cameras(json.dumps(existing + [camera]))
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте камеру", str(exc))
            return
        row = self.camera_table.rowCount()
        self.camera_table.insertRow(row)
        self._set_camera_row(row, camera)

    def _set_camera_row(self, row, camera):
        self.camera_table.setItem(row, 0, QTableWidgetItem(camera["id"]))
        item = QTableWidgetItem(self._hide_url_password(camera["url"]))
        item.setData(Qt.ItemDataRole.UserRole, camera["url"])
        self.camera_table.setItem(row, 1, item)

    def remove_camera(self):
        row = self.camera_table.currentRow()
        if row >= 0:
            self.camera_table.removeRow(row)

    def apply_settings(self):
        try:
            config = self._form_config()
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Настройки не сохранены", str(exc))
            return False
        was_recording = bool(self.runtime and self.runtime.is_recording)
        if was_recording and config != self.config:
            answer = QMessageBox.question(
                self,
                "Перезапустить запись?",
                "Изменение настроек остановит текущий сегмент и применит новую конфигурацию. Продолжить?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        try:
            save_config(config)
            self.config = config
            self._restart_runtime(start_archive=was_recording)
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось применить настройки", str(exc))
            return False
        self.statusBar().showMessage("Настройки сохранены", 4000)
        return True

    def _settings(self):
        return make_settings(self.config, self.tools)

    def _start_runtime(self):
        self.runtime = ServiceRuntime(self._settings())
        self.runtime.start(start_archive=False)

    def _restart_runtime(self, start_archive=False):
        self.stop_live()
        if self.runtime:
            self.runtime.stop()
        self._start_runtime()
        if start_archive and self.config.get("mode") != "none":
            self.runtime.start_archive()
        self.refresh_state()
        self.refresh_sources()
        self.refresh_recordings()

    def _bootstrap_ffmpeg(self):
        self.tool_status.setText("FFmpeg отсутствует — загружаю и проверяю автоматически…")
        self.tool_progress.show()
        worker = FfmpegBootstrap()
        worker.signals.progress.connect(self._download_progress)
        worker.signals.ready.connect(self._ffmpeg_ready)
        worker.signals.failed.connect(self._ffmpeg_failed)
        self.bootstrap_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _download_progress(self, received, total):
        self.tool_progress.setRange(0, 100 if total else 0)
        if total:
            self.tool_progress.setValue(min(100, int(received * 100 / total)))
            self.tool_status.setText(
                "Загружено FFmpeg: {:.1f} / {:.1f} МБ".format(received / 1048576, total / 1048576)
            )

    def _ffmpeg_ready(self, ffmpeg, ffprobe):
        self.tools = (ffmpeg, ffprobe)
        self.tool_progress.hide()
        self._set_tool_status("FFmpeg и ffprobe установлены")
        try:
            self._restart_runtime(start_archive=False)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка запуска внутреннего сервиса", str(exc))
        self.bootstrap_worker = None

    def _ffmpeg_failed(self, message):
        self.tool_progress.hide()
        self._set_tool_status("FFmpeg не установлен")
        QMessageBox.critical(
            self,
            "Не удалось загрузить FFmpeg",
            message + "\n\nПроверьте подключение к интернету и повторите запуск приложения.",
        )
        self.bootstrap_worker = None

    def _set_tool_status(self, message):
        self.tool_status.setText(message)
        self.record_button.setEnabled(self.tools is not None or self.config.get("mode") in ("none", "inbox"))

    def _base_url(self):
        return "http://127.0.0.1:{}".format(self.runtime.port)

    def _get(self, path, callback):
        request = QNetworkRequest(QUrl(self._base_url() + path))
        reply = self.api.get(request)
        reply.finished.connect(lambda current=reply: callback(current))

    @staticmethod
    def _json_reply(reply):
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                return None
            return json.loads(bytes(reply.readAll()).decode("utf-8"))
        except (ValueError, UnicodeError):
            return None
        finally:
            reply.deleteLater()

    def refresh_state(self):
        if not self.runtime:
            return
        self._get("/api/health", self._health_received)

    def _health_received(self, reply):
        data = self._json_reply(reply)
        if not data:
            self.status_label.setText("Внутренний сервис недоступен")
            self.status_label.setProperty("state", "error")
            self._refresh_style(self.status_label)
            return
        archive = data.get("archive", {})
        state = archive.get("state", "unknown")
        names = {"running": "идёт запись", "degraded": "есть проблемы с источником", "disabled": "запись выключена", "stopped": "остановлено"}
        self.status_label.setText("Сервис: " + names.get(state, state))
        self.status_label.setProperty(
            "state", "active" if state == "running" else "warning" if state == "degraded" else "idle"
        )
        self._refresh_style(self.status_label)
        self._set_recording_button(self.runtime.is_recording)
        self.record_button.setEnabled(
            self.runtime.is_recording or self.tools is not None or self.config.get("mode") in ("none", "inbox")
        )
        if archive.get("last_error"):
            self.statusBar().showMessage(archive["last_error"], 8000)

    def refresh_sources(self):
        self._get("/api/live/sources", self._sources_received)

    def _sources_received(self, reply):
        data = self._json_reply(reply)
        if data is None:
            return
        current = self.live_sources.currentData()
        self.live_sources.clear()
        for source in data.get("sources", []):
            self.live_sources.addItem(source.get("label", source["id"]), source["id"])
        if current is not None:
            index = self.live_sources.findData(current)
            if index >= 0:
                self.live_sources.setCurrentIndex(index)

    def toggle_live(self):
        if self.live_reply:
            self.stop_live()
            return
        source_id = self.live_sources.currentData()
        if source_id is None:
            self.live_status.setText("Нет источника для просмотра. Добавьте камеру или выберите тестовый источник.")
            return
        encoded = quote(str(source_id), safe="")
        request = QNetworkRequest(QUrl(self._base_url() + "/api/live.mjpeg?source_id=" + encoded))
        self.live_buffer.clear()
        self.live_view.setText("Подключаюсь к источнику…")
        reply = self.api.get(request)
        self.live_reply = reply
        reply.readyRead.connect(self._live_data)
        reply.finished.connect(self._live_finished)
        self.live_button.setText("Остановить эфир")
        self.live_status.setText("Подключение…")

    def _live_data(self):
        if not self.live_reply:
            return
        self.live_buffer.extend(bytes(self.live_reply.readAll()))
        while True:
            start = self.live_buffer.find(b"\xff\xd8")
            if start < 0:
                if len(self.live_buffer) > 1:
                    del self.live_buffer[:-1]
                return
            end = self.live_buffer.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del self.live_buffer[:start]
                return
            jpeg = bytes(self.live_buffer[start:end + 2])
            del self.live_buffer[:end + 2]
            image = QImage.fromData(jpeg, "JPEG")
            if image.isNull():
                continue
            self._show_live_image(image)
            self.live_status.setText("Прямой эфир подключён")

    def _show_live_image(self, image):
        self.live_image = image
        self.live_view.setPixmap(QPixmap.fromImage(image).scaled(
            self.live_view.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _live_finished(self):
        reply = self.live_reply
        self.live_reply = None
        if reply:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                error = reply.errorString()
                self.live_status.setText("Поток завершился: " + error)
            reply.deleteLater()
        self.live_button.setText("Открыть эфир")

    def stop_live(self):
        reply = self.live_reply
        if reply:
            self.live_reply = None
            reply.abort()
            reply.deleteLater()
        self.live_buffer.clear()
        self.live_view.clear()
        self.live_view.setText("Прямой просмотр не запущен")
        self.live_status.setText("Прямой просмотр не запущен")
        self.live_button.setText("Открыть эфир")

    def refresh_recordings(self):
        if self.runtime:
            self._get("/api/videos", self._recordings_received)

    def _recordings_received(self, reply):
        data = self._json_reply(reply)
        if data is None:
            return
        self.recordings = data.get("videos", [])
        self._populate_recordings()

    def _populate_recordings(self, *_):
        query = self.search.text().casefold() if hasattr(self, "search") else ""
        rows = [item for item in self.recordings
                if query in (item.get("camera_id") or "").casefold()
                or query in item.get("started_at", "").casefold()
                or query in item.get("name", "").casefold()]
        self.recording_table.setRowCount(len(rows))
        for row, video in enumerate(rows):
            camera = video.get("camera_id") or "Без ID"
            started = video.get("started_at", "")
            duration = video.get("duration_seconds")
            duration_text = self._format_ms(int(duration * 1000)) if duration is not None else "—"
            size = self._format_size(video.get("size_bytes", 0))
            values = (camera, started.replace("T", " "), duration_text, size)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, video.get("name"))
                self.recording_table.setItem(row, column, cell)

    def play_selected(self, *_):
        row = self.recording_table.currentRow()
        if row < 0:
            return
        name_item = self.recording_table.item(row, 0)
        name = name_item.data(Qt.ItemDataRole.UserRole) if name_item else None
        if not name:
            return
        path = self.config["recordings_dir"]
        video_path = Path(path) / name
        if not video_path.is_file():
            QMessageBox.warning(self, "Видео не найдено", "Файл записи уже недоступен в каталоге архива.")
            return
        self.player.setSource(QUrl.fromLocalFile(str(video_path.resolve())))
        self.player.play()
        self.play_button.setText("Пауза")

    def _toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif not self.player.source().isEmpty():
            self.player.play()
        else:
            self.play_selected()

    def _playback_changed(self, state):
        self.play_button.setText(
            "Пауза" if state == QMediaPlayer.PlaybackState.PlayingState else "Воспроизвести"
        )

    def _player_error(self, error, message):
        if error != QMediaPlayer.Error.NoError:
            self.statusBar().showMessage("Ошибка воспроизведения: " + message, 10000)

    def _seek(self, position):
        self.player.setPosition(position)

    def _position_changed(self, position):
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(position)
        self._update_playback_time(position, self.player.duration())

    def _duration_changed(self, duration):
        self.position_slider.setRange(0, max(0, duration))
        self._update_playback_time(self.player.position(), duration)

    def _update_playback_time(self, position, duration):
        self.playback_time.setText("{} / {}".format(self._format_ms(position), self._format_ms(duration)))

    @staticmethod
    def _format_ms(milliseconds):
        seconds = max(0, milliseconds // 1000)
        return "{:02d}:{:02d}:{:02d}".format(seconds // 3600, (seconds // 60) % 60, seconds % 60)

    @staticmethod
    def _format_size(size):
        value = float(size)
        for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
            if value < 1024 or unit == "ТБ":
                return "{:.1f} {}".format(value, unit)
            value /= 1024

    def toggle_recording(self):
        if self.runtime and self.runtime.is_recording:
            self.runtime.stop_archive()
            self._set_recording_button(False)
            self.refresh_state()
            return
        if self.config.get("mode") == "none":
            QMessageBox.information(self, "Источник не выбран", "Выберите камеры, тестовый источник или импорт готовых файлов в настройках.")
            self.tabs.setCurrentIndex(2)
            return
        try:
            form_config = self._form_config()
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))
            self.tabs.setCurrentIndex(2)
            return
        if form_config != self.config:
            if not self.apply_settings():
                return
        if self.tools is None and self.config.get("mode") in ("cameras", "synthetic"):
            QMessageBox.information(self, "FFmpeg ещё загружается", "Дождитесь завершения установки FFmpeg.")
            return
        try:
            self.runtime.start_archive()
            self._set_recording_button(True)
            self.refresh_state()
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось начать запись", str(exc))

    @staticmethod
    def _refresh_style(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _set_recording_button(self, active):
        self.record_button.setText("Остановить запись" if active else "Начать запись")
        self.record_button.setProperty("recording", bool(active))
        self._refresh_style(self.record_button)

    def closeEvent(self, event):
        self.stop_live()
        self.player.stop()
        if self.runtime:
            self.runtime.stop()
        event.accept()


def main():
    data = app_data_dir()
    log_path = data / "desktop.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Видеоархив")
    app.setOrganizationName("VideoArchiveService")
    window = DesktopWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
