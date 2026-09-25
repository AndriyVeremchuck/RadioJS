#!/usr/bin/env python3
import sys
import os
import json
import math
import re
import urllib.parse
import logging
from logging.handlers import RotatingFileHandler
import traceback
import shutil
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, QSize, QTimer, QRectF, QPointF, QEvent, QObject, pyqtSignal, QEventLoop
from PyQt6.QtGui import (
    QIcon, QPixmap, QFont, QColor, QPainter, QPen, QBrush,
    QConicalGradient, QPalette, QShortcut, QKeySequence
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QCheckBox, QLineEdit, QScrollArea, QGridLayout,
    QSlider, QMessageBox, QFrame, QFileDialog, QDialog, QSizePolicy, QProgressBar,
    QComboBox, QMenu
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaMetaData

# Determine base paths
APP_DIR = Path(__file__).resolve().parent
JSON_PATH = APP_DIR / "stations.json"
ALT_JSON_PATH = Path("/home/dusha/RadioJS/stations.json")
DELETED_JSON_PATH = APP_DIR / "station_delete.json"
ALT_DELETED_JSON_PATH = Path("/home/dusha/RadioJS/station_delete.json")
LOG_PATH = APP_DIR / "radio_manager.log"
APP_STATE_PATH = APP_DIR / "app_state.json"
ALT_APP_STATE_PATH = Path("/home/dusha/RadioJS/app_state.json")

# Configure logging
logger = logging.getLogger("RadioManager")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    log_formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-7s] [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. Console stream handler (sys.stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_formatter)
    logger.addHandler(console_handler)

    # 2. Rotating file handler (up to 10MB, 3 backups, UTF-8)
    try:
        file_handler = RotatingFileHandler(
            str(LOG_PATH),
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(log_formatter)
        logger.addHandler(file_handler)
    except Exception as _log_init_err:
        sys.stderr.write(f"Не вдалося ініціалізувати лог-файл {LOG_PATH}: {_log_init_err}\n")


def global_exception_hook(exctype, value, tb):
    """Глобальний перехоплювач непередбачених помилок для запобігання раптовому падінню додатку."""
    err_lines = traceback.format_exception(exctype, value, tb)
    err_str = "".join(err_lines)
    logger.critical(f"НЕПЕРЕХОПЛЕНИЙ КРИТИЧНИЙ ВИНЯТОК:\n{err_str}")

    app = QApplication.instance()
    if app:
        try:
            msg_box = QMessageBox()
            msg_box.setIcon(QMessageBox.Icon.Critical)
            msg_box.setWindowTitle("Критична помилка додатку")
            msg_box.setText("Сталася несподівана помилка в роботі додатку!")
            msg_box.setInformativeText(f"{value}\n\nПовний лог записано у файл:\n{LOG_PATH}")
            msg_box.setDetailedText(err_str)
            msg_box.setStyleSheet("""
                QMessageBox { background-color: #1e1f22; }
                QLabel { color: #ffffff; font-size: 17px; }
                QPushButton { background-color: #2b2d30; color: #ffffff; font-size: 17px; padding: 8px 20px; border-radius: 6px; }
            """)
            msg_box.exec()
        except Exception:
            pass

    # Також друкуємо в оригінальний excepthook
    sys.__excepthook__(exctype, value, tb)


sys.excepthook = global_exception_hook


def normalize_stream_url(url: str) -> str:
    """Нормалізує URL аудіопотоку для виявлення та видалення дублікатів."""
    if not url:
        return ""
    u = url.strip()
    try:
        p = urllib.parse.urlparse(u)
        path = p.path.rstrip("/")
        if path.endswith("/;"):
            path = path[:-2]
        elif path.endswith(";"):
            path = path[:-1]
        
        q_pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        filtered_q = [(k, v) for k, v in q_pairs if k.lower() not in ("n", "t", "_", "ref", "nocache", "type")]
        query = urllib.parse.urlencode(filtered_q)
        netloc = p.netloc.lower()
        q_str = "?" + query if query else ""
        return netloc + path + q_str
    except Exception:
        return u.lower().rstrip("/")


def load_deleted_stations() -> list:
    """
    Завантажує накопичений список видалених радіостанцій з файлу station_delete.json.
    Перевіряє як робочий шлях DELETED_JSON_PATH, так і альтернативний ALT_DELETED_JSON_PATH.
    Повертає список словників (station dicts).
    """
    target_path = None
    if DELETED_JSON_PATH.exists() and DELETED_JSON_PATH.stat().st_size > 0:
        target_path = DELETED_JSON_PATH
    elif ALT_DELETED_JSON_PATH.exists() and ALT_DELETED_JSON_PATH.stat().st_size > 0:
        target_path = ALT_DELETED_JSON_PATH

    if target_path:
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                loaded_data = json.load(f)
            if isinstance(loaded_data, list):
                logger.debug(f"Завантажено {len(loaded_data)} раніше видалених станцій з {target_path}")
                return loaded_data
            else:
                logger.warning(f"Вміст {target_path} не є коректним списком (типу {type(loaded_data)})")
        except Exception as e:
            logger.error(f"Помилка при читанні раніше видалених станцій з {target_path}: {e}")

    return []


def save_accumulated_deleted_stations(new_deleted: list) -> bool:
    """
    Накопичує видалені станції у station_delete.json, відсіюючи дублікати,
    та синхронізує збереження атомарно з викликом fsync.
    """
    if not new_deleted:
        return True

    existing = load_deleted_stations()
    existing_urls = {normalize_stream_url(str(s.get("url", ""))) for s in existing if s.get("url")}
    existing_names = {str(s.get("name", "")).strip().lower() for s in existing if s.get("name")}

    accumulated = list(existing)
    added_count = 0

    for s in new_deleted:
        u_key = normalize_stream_url(str(s.get("url", "")))
        n_key = str(s.get("name", "")).strip().lower()

        # Якщо станція вже присутня в списку видалених, уникаємо дублювання
        if (u_key and u_key in existing_urls) or (n_key and n_key in existing_names):
            continue

        clean_item = {k: v for k, v in s.items() if not k.startswith("_")}
        accumulated.append(clean_item)
        if u_key:
            existing_urls.add(u_key)
        if n_key:
            existing_names.add(n_key)
        added_count += 1

    # Перенумеровуємо ID від 1 до N
    for idx, s in enumerate(accumulated, 1):
        s["id"] = idx

    logger.info(f"Накопичення видалених станцій: додано {added_count} нових, всього у {DELETED_JSON_PATH.name}: {len(accumulated)}")

    try:
        tmp_path = DELETED_JSON_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(accumulated, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, DELETED_JSON_PATH)

        if ALT_DELETED_JSON_PATH.parent.exists():
            try:
                alt_tmp = ALT_DELETED_JSON_PATH.with_suffix(".json.tmp")
                with open(alt_tmp, "w", encoding="utf-8") as f:
                    json.dump(accumulated, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(alt_tmp, ALT_DELETED_JSON_PATH)
                logger.debug(f"Синхронізовано station_delete.json з {ALT_DELETED_JSON_PATH}")
            except Exception as ae:
                logger.warning(f"Не вдалося синхронізувати station_delete.json з {ALT_DELETED_JSON_PATH}: {ae}")

        return True
    except Exception as e:
        logger.exception(f"Критична помилка при збереженні station_delete.json: {e}")
        return False


def load_app_state() -> dict:
    """
    Завантажує збережений стан додатку (прослухані станції, нові станції, обрані країни та жанри/стилі).
    """
    for path in (APP_STATE_PATH, ALT_APP_STATE_PATH):
        if path.exists() and path.stat().st_size > 0:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as e:
                logger.warning(f"Не вдалося прочитати стан з {path}: {e}")
    return {}


def save_app_state(state_data: dict) -> bool:
    """
    Атомарно зберігає стан додатку (прослухані/нові станції, обрані стилі) у app_state.json.
    """
    success = False
    for path in (APP_STATE_PATH, ALT_APP_STATE_PATH):
        if not path.parent.exists():
            continue
        try:
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            success = True
        except Exception as e:
            logger.warning(f"Не вдалося зберегти стан додатку у {path}: {e}")
    return success


class FfmpegStderrInterceptor(QObject):
    """
    Перехоплювач повідомлень stderr на рівні дескриптора C/ОС (fd 2).
    Дозволяє фіксувати фатальні помилки FFmpeg при старті потоку:
    - HTTP error 404 Not Found (або Server returned 404 Not Found)
    - Failed to resolve hostname (або помилки DNS / Невідома назва чи сервіс)
    та безпечно передавати сигнал fatal_error_detected у головний потік Qt.
    """
    fatal_error_detected = pyqtSignal(str)
    stream_info_detected = pyqtSignal(str, str)   # (codec, bitrate)
    stream_title_detected = pyqtSignal(str)       # track title

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._orig_stderr_fd = None
        self._orig_stderr_file = None
        self._pipe_r = None
        self._pipe_w = None

        try:
            self._orig_stderr_fd = os.dup(2)
            self._pipe_r, self._pipe_w = os.pipe()
            os.dup2(self._pipe_w, 2)
            os.close(self._pipe_w)
            self._orig_stderr_file = open(self._orig_stderr_fd, "wb", buffering=0)

            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._reader_thread.start()
            logger.debug("Перехоплювач stderr (FfmpegStderrInterceptor) успішно активовано.")
        except Exception as e:
            logger.error(f"Не вдалося ініціалізувати перехоплювач stderr: {e}")

    def _read_loop(self):
        try:
            with open(self._pipe_r, "rb", buffering=0) as pipe_file:
                while self._running:
                    line = pipe_file.readline()
                    if not line:
                        break
                    # Дублюємо в оригінальний системний stderr
                    if self._orig_stderr_file:
                        try:
                            self._orig_stderr_file.write(line)
                        except Exception:
                            pass

                    try:
                        raw_line = line.decode("utf-8", errors="replace").strip()
                        line_str = raw_line.lower()
                    except Exception:
                        raw_line = ""
                        line_str = ""

                    # Ігноруємо повідомлення примусового переривання при зупинці плеєра
                    if "immediate exit" in line_str:
                        continue

                    # Витягуємо параметри аудіопотоку (кодек, бітрейт)
                    if "audio:" in line_str and ("stream #" in line_str or "input #" in line_str):
                        m_codec = re.search(r'Audio:\s*([a-zA-Z0-9_-]+)', raw_line, re.IGNORECASE)
                        m_bitrate = re.search(r'([0-9]+)\s*kb/s', raw_line, re.IGNORECASE)
                        codec_str = m_codec.group(1).upper() if m_codec else ""
                        bitrate_str = f"{m_bitrate.group(1)} kbps" if m_bitrate else ""
                        if codec_str or bitrate_str:
                            self.stream_info_detected.emit(codec_str, bitrate_str)

                    # Витягуємо назву поточної пісні з ICY/StreamTitle метаданих
                    if any(k in line_str for k in ("streamtitle", "stream title", "icy-title")):
                        m_title = re.search(r'(?:StreamTitle|Stream title|icy-title)\s*[:=]\s*[\'"]?([^\r\n;\"]+)', raw_line, re.IGNORECASE)
                        if m_title:
                            clean_t = m_title.group(1).strip()
                            if clean_t and clean_t.lower() not in ("n/a", "unknown", "none"):
                                self.stream_title_detected.emit(clean_t)

                    # Перевіряємо помилки 404, недоступність хоста, відмову у з'єднанні, помилки читання HTTP та відкриття файлу
                    if "404 not found" in line_str or "http error 404" in line_str or "server returned 404" in line_str:
                        self.fatal_error_detected.emit("HTTP error 404 Not Found")
                    elif "failed to resolve hostname" in line_str or "невідома назва чи сервіс" in line_str or "name or service not known" in line_str:
                        self.fatal_error_detected.emit("Failed to resolve hostname")
                    elif "у з'єднанні відмовлено" in line_str or "connection refused" in line_str or "в соединении отказано" in line_str:
                        self.fatal_error_detected.emit("У з'єднанні відмовлено")
                    elif "error reading http response" in line_str or "error reading response header" in line_str:
                        self.fatal_error_detected.emit("Error reading HTTP response")
                    elif "could not open file" in line_str or "could not open media" in line_str:
                        self.fatal_error_detected.emit("Could not open file")
                    elif "format lrc detected" in line_str or "misdetection possible" in line_str:
                        self.fatal_error_detected.emit("Format lrc / Misdetection")
                    elif "subtitle: text" in line_str or "stream #0:0: subtitle" in line_str:
                        self.fatal_error_detected.emit("Subtitle: text (немає аудіо)")
                    elif "invalid data found when processing input" in line_str:
                        self.fatal_error_detected.emit("Invalid data found")
                    elif "could not update timestamps" in line_str:
                        self.fatal_error_detected.emit("Could not update timestamps (битий потік)")
        except Exception as ex:
            logger.debug(f"Потік читання stderr зупинено: {ex}")

    def stop(self):
        if not self._running:
            return
        self._running = False
        try:
            if self._orig_stderr_fd is not None:
                os.dup2(self._orig_stderr_fd, 2)
                os.close(self._orig_stderr_fd)
                self._orig_stderr_fd = None
        except Exception:
            pass


_DEFAULT_PLACEHOLDER_PIXMAP = None
_LOCAL_LOGO_CACHE = {}

def get_default_placeholder_pixmap() -> QPixmap:
    global _DEFAULT_PLACEHOLDER_PIXMAP
    if _DEFAULT_PLACEHOLDER_PIXMAP is None or _DEFAULT_PLACEHOLDER_PIXMAP.isNull():
        p = QPixmap()
        placeholder = APP_DIR / "logos/placeholders/music-1.svg"
        if placeholder.exists():
            p.load(str(placeholder))
        if not p.isNull():
            _DEFAULT_PLACEHOLDER_PIXMAP = p.scaled(
                56, 56, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        else:
            _DEFAULT_PLACEHOLDER_PIXMAP = QPixmap()
    return _DEFAULT_PLACEHOLDER_PIXMAP


class StationWidget(QFrame):
    def __init__(self, station_data, on_play_toggle, on_delete_toggle, display_number=None, parent=None):
        super().__init__(parent)
        self.station_data = station_data
        self.display_number = display_number if display_number is not None else station_data.get("id", 0)
        self.on_play_toggle = on_play_toggle
        self.on_delete_toggle = on_delete_toggle
        self.is_playing = False
        self.has_played = bool(station_data.get("_has_played") or station_data.get("has_played"))
        self.is_imported_new = bool(station_data.get("_is_imported_new") or station_data.get("is_new"))
        self.is_hovered = False
        self.is_card_focused = False

        # Animated garland timer
        self.anim_tick = 0
        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(45)
        self.anim_timer.timeout.connect(self._on_anim_tick)

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Клікніть, щоб запустити або зупинити відтворення")
        self.setFixedHeight(92)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # 0. Number Badge (#1, #2, ...)
        st_id = station_data.get("id", 0)
        st_num = self.display_num
        self.num_label = QLabel(f"#{st_num}")
        self.num_label.setToolTip(f"№ {st_num} у поточному списку\n(Базовий ID: #{st_id})")
        self.num_label.setFixedWidth(52)
        self.num_label.setFixedHeight(64)
        self.num_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.num_label.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.num_label)

        # 1. Logo (оптимізовано з кешуванням)
        self.logo_label = QLabel()
        self.logo_label.setFixedSize(62, 62)
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logo_label.setStyleSheet("background-color: #1a1c1e; border: 1px solid #30363d; border-radius: 8px; padding: 2px;")
        
        logo_path = str(station_data.get("logo") or "").strip()
        pixmap = None
        
        if logo_path and not logo_path.startswith("http://") and not logo_path.startswith("https://") and len(logo_path) < 255:
            if logo_path in _LOCAL_LOGO_CACHE:
                pixmap = _LOCAL_LOGO_CACHE[logo_path]
            else:
                try:
                    full_logo_path = APP_DIR / logo_path
                    if full_logo_path.exists():
                        p = QPixmap(str(full_logo_path))
                        if not p.isNull():
                            pixmap = p.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                            _LOCAL_LOGO_CACHE[logo_path] = pixmap
                except Exception as _le:
                    logger.debug(f"Не вдалося завантажити локальний логотип {logo_path}: {_le}")
        
        if not pixmap or pixmap.isNull():
            pixmap = get_default_placeholder_pixmap()

        if pixmap and not pixmap.isNull():
            self.logo_label.setPixmap(pixmap)
        layout.addWidget(self.logo_label)

        # 2. Text Info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(1)
        
        raw_name = str(station_data.get("name") or "Без назви").strip()
        if len(raw_name) > 30:
            title_text = raw_name[:27] + "..."
        else:
            title_text = raw_name
        self.title_label = QLabel(title_text)
        self.title_label.setToolTip(f"{raw_name}\n(Права кнопка миші — налаштування станції)")
        self.title_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.title_label.setStyleSheet("color: #ffffff; font-size: 21px; font-weight: bold; background: transparent;")
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setMinimumWidth(30)

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.title_label, stretch=1)
        title_row.addStretch()
        info_layout.addLayout(title_row)

        self.now_playing_label = QLabel("")
        self.now_playing_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.now_playing_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.now_playing_label.setStyleSheet("color: #39ff14; font-size: 15px; font-weight: bold; background: transparent;")
        self.now_playing_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.now_playing_label.setVisible(False)
        info_layout.addWidget(self.now_playing_label)

        desc_text = str(station_data.get("description") or "").strip()
        if desc_text.lower() in ("m3u імпорт", "m3u import", "m3u", "власна станція"):
            desc_text = ""
        else:
            desc_text = re.sub(r'(?i)\b(m3u\s*імпорт|m3u\s*import)\b', '', desc_text).strip(" ,;-")
        desc_display = (desc_text[:37] + "...") if len(desc_text) > 40 else desc_text
        self.desc_label = QLabel(desc_display)
        self.desc_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        if desc_text:
            self.desc_label.setToolTip(desc_text)
        self.desc_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.desc_label.setStyleSheet("color: #8b949e; font-size: 16px; background: transparent;")
        self.desc_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.desc_label.setVisible(bool(desc_display))

        info_layout.addWidget(self.desc_label)
        layout.addLayout(info_layout, stretch=1)

        # 3. Play / Stop Button
        self.play_btn = QPushButton("▶ Грати")
        self.play_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_btn.setFixedWidth(98)
        self.play_btn.setFixedHeight(42)
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.clicked.connect(self._toggle_play)
        layout.addWidget(self.play_btn)

        # 4. Delete Checkbox
        self.delete_cb = QCheckBox("Видалити")
        self.delete_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.delete_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_cb.setFixedWidth(116)
        self.delete_cb.setStyleSheet("""
            QCheckBox {
                color: #e06c75;
                font-size: 17px;
                font-weight: bold;
                spacing: 6px;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 22px;
                height: 22px;
                border-radius: 5px;
                border: 2px solid #a8444c;
                background-color: #1e1416;
            }
            QCheckBox::indicator:checked {
                background-color: #e06c75;
                border-color: #ff7b72;
            }
        """)
        self.delete_cb.toggled.connect(self._on_check_changed)
        layout.addWidget(self.delete_cb)

        # Прекомпіляція пошукового рядка для миттєвої фільтрації тисяч станцій без навантаження на CPU
        url_text = str(station_data.get("url") or "").strip()
        _name_s = title_text.lower()
        _desc_s = desc_text.lower()
        _url_s = url_text.lower()
        _id_s = str(station_data.get("id", "")).strip().lower()
        self._search_blob = f"{_name_s} {_desc_s} {_url_s} #{_id_s} {_id_s}"

        # Відновлюємо стан, якщо станція вже була позначена на видалення або відіграна
        if station_data.get("_marked_delete", False):
            self.delete_cb.setChecked(True)
        if station_data.get("_has_played", False) or station_data.get("has_played", False):
            self.has_played = True
        if station_data.get("_is_imported_new", False) or station_data.get("is_new", False):
            self.is_imported_new = True

        self._update_elements_style()

    @property
    def display_num(self):
        return getattr(self, 'display_number', None) or self.station_data.get("id", 0)

    def enterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().leaveEvent(event)

    def _on_anim_tick(self):
        self.anim_tick += 1
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)

        if self.is_playing:
            # 1. ТА ЩО ГРАЄ: Глибокий чорний фон та неонова святкова гірлянда
            center = QPointF(self.width() / 2, self.height() / 2)
            grad = QConicalGradient(center, (self.anim_tick * 8) % 360)
            
            # Неонові кольори святкової гірлянди
            colors = [
                QColor(0, 245, 255),    # Cyan
                QColor(57, 255, 20),    # Neon Lime
                QColor(255, 220, 0),    # Gold
                QColor(255, 0, 110),    # Hot Pink
                QColor(160, 32, 240),   # Purple
                QColor(255, 80, 0),     # Neon Orange
                QColor(0, 245, 255),    # Wrap around
            ]
            for idx, c in enumerate(colors):
                grad.setColorAt(idx / (len(colors) - 1), c)

            # Ритмічна пульсація контуру під біт музики
            pulse = 3.8 + 1.2 * math.sin(self.anim_tick * 0.28)
            pen = QPen(QBrush(grad), pulse)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

            p.setPen(pen)
            p.setBrush(QColor(0, 0, 0))
            p.drawRoundedRect(rect, 10, 10)

        elif self.delete_cb.isChecked():
            # 2. ВИДІЛЕНА ЧЕКБОКСОМ: Трішки яскравіший насичений червоний
            pen_color = QColor(255, 68, 79) if not self.is_hovered else QColor(255, 100, 110)
            bg_color = QColor(68, 20, 25) if not self.is_hovered else QColor(85, 26, 32)
            pen_width = 2.4 if not self.is_card_focused else 3.2
            if self.is_card_focused:
                pen_color = QColor(255, 115, 125)
            p.setPen(QPen(pen_color, pen_width))
            p.setBrush(bg_color)
            p.drawRoundedRect(rect, 10, 10)

        elif self.has_played:
            # 3. ТА ЯКА ВЖЕ ВІДІГРАЛА (ПРОСЛУХАНА): ЧОРНИЙ ФОН
            pen_color = QColor(28, 32, 38) if not self.is_hovered else QColor(48, 54, 62)
            bg_color = QColor(0, 0, 0) if not self.is_hovered else QColor(12, 14, 16)
            pen_width = 1.5 if not self.is_card_focused else 2.6
            if self.is_card_focused:
                pen_color = QColor(88, 166, 255)
            p.setPen(QPen(pen_color, pen_width))
            p.setBrush(bg_color)
            p.drawRoundedRect(rect, 10, 10)

        elif self.is_imported_new:
            # 4. НОВА (НОВА ВКЛАДКА / ЗЕЛЕНИЙ ФОН):
            pen_color = QColor(46, 160, 67) if not self.is_hovered else QColor(63, 185, 80)
            bg_color = QColor(22, 45, 29) if not self.is_hovered else QColor(28, 56, 36)
            pen_width = 2.0 if not self.is_card_focused else 3.0
            if self.is_card_focused:
                pen_color = QColor(0, 245, 255)
            p.setPen(QPen(pen_color, pen_width))
            p.setBrush(bg_color)
            p.drawRoundedRect(rect, 10, 10)

        else:
            # 5. ЗВИЧАЙНА СІРА (НЕПРОСЛУХАНА): Спокійний сірий колір
            pen_color = QColor(60, 65, 72) if not self.is_hovered else QColor(88, 166, 255)
            bg_color = QColor(40, 43, 48) if not self.is_hovered else QColor(48, 52, 58)
            pen_width = 1.5 if not self.is_card_focused else 2.6
            if self.is_card_focused:
                pen_color = QColor(88, 166, 255)
            p.setPen(QPen(pen_color, pen_width))
            p.setBrush(bg_color)
            p.drawRoundedRect(rect, 10, 10)

        # Фокусний контур при виборі стрілками клавіатури
        if self.is_card_focused and not self.is_playing:
            focus_pen = QPen(QColor(88, 166, 255), 2.2)
            focus_pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(focus_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            inner_rect = QRectF(4, 4, self.width() - 8, self.height() - 8)
            p.drawRoundedRect(inner_rect, 8, 8)

        p.end()

    def set_card_focused(self, val: bool):
        if self.is_card_focused != val:
            self.is_card_focused = val
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            win = self.window()
            if hasattr(win, '_set_focused_widget'):
                win._set_focused_widget(self, scroll_into_view=False)
            return

        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            win = self.window()
            if hasattr(win, '_set_focused_widget'):
                win._set_focused_widget(self, scroll_into_view=False)
            try:
                win.setFocus()
            except Exception:
                pass

            # If clicked on/near delete checkbox, do not toggle playback
            if self.delete_cb.geometry().contains(pos):
                super().mousePressEvent(event)
                return
            # If clicked on play_btn, QPushButton handles it
            if self.play_btn.geometry().contains(pos):
                super().mousePressEvent(event)
                return
            # Clicked anywhere else on the card -> toggle play / stop!
            event.accept()
            self._toggle_play()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        event.accept()
        win = self.window()
        if hasattr(win, '_set_focused_widget'):
            win._set_focused_widget(self, scroll_into_view=False)
        if hasattr(win, '_open_edit_station_dialog'):
            win._open_edit_station_dialog(self.station_data, widget=self)
        else:
            super().contextMenuEvent(event)

    def _on_now_playing_context_menu(self, pos):
        win = self.window()
        if hasattr(win, '_set_focused_widget'):
            win._set_focused_widget(self, scroll_into_view=False)
        if hasattr(win, '_open_edit_station_dialog'):
            win._open_edit_station_dialog(self.station_data, widget=self)

    def update_station_info(self, station_data: dict):
        self.station_data = station_data
        raw_name = str(station_data.get("name") or "Без назви").strip()
        if len(raw_name) > 30:
            title_text = raw_name[:27] + "..."
        else:
            title_text = raw_name
        self.title_label.setText(title_text)
        self.title_label.setToolTip(f"{raw_name}\n(Права кнопка миші — налаштування станції)")

        desc_text = str(station_data.get("description") or "").strip()
        if desc_text.lower() in ("m3u імпорт", "m3u import", "m3u", "власна станція"):
            desc_text = ""
        else:
            desc_text = re.sub(r'(?i)\b(m3u\s*імпорт|m3u\s*import)\b', '', desc_text).strip(" ,;-")
        desc_display = (desc_text[:37] + "...") if len(desc_text) > 40 else desc_text
        self.desc_label.setText(desc_display)
        self.desc_label.setToolTip(desc_text if desc_text else "")
        self.desc_label.setVisible(bool(desc_display))

        url_text = str(station_data.get("url") or "").strip()

        logo_path = str(station_data.get("logo") or "").strip()
        pixmap = None
        if logo_path and not logo_path.startswith("http://") and not logo_path.startswith("https://") and len(logo_path) < 255:
            if logo_path in _LOCAL_LOGO_CACHE:
                pixmap = _LOCAL_LOGO_CACHE[logo_path]
            else:
                try:
                    full_logo_path = APP_DIR / logo_path
                    if full_logo_path.exists():
                        p = QPixmap(str(full_logo_path))
                        if not p.isNull():
                            pixmap = p.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                            _LOCAL_LOGO_CACHE[logo_path] = pixmap
                except Exception as _le:
                    logger.debug(f"Не вдалося завантажити локальний логотип {logo_path}: {_le}")
        if not pixmap or pixmap.isNull():
            pixmap = get_default_placeholder_pixmap()
        if pixmap and not pixmap.isNull():
            self.logo_label.setPixmap(pixmap)

        _name_s = title_text.lower()
        _desc_s = desc_text.lower()
        _url_s = url_text.lower()
        _id_s = str(station_data.get("id", "")).strip().lower()
        self._search_blob = f"{_name_s} {_desc_s} {_url_s} #{_id_s} {_id_s}"
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if (self.delete_cb.geometry().contains(pos) or 
                self.play_btn.geometry().contains(pos) or 
                (self.now_playing_label.isVisible() and self.now_playing_label.geometry().contains(pos))):
                super().mouseDoubleClickEvent(event)
                return
            event.accept()
            self._toggle_play()
            return
        super().mouseDoubleClickEvent(event)

    def _toggle_play(self):
        self.on_play_toggle(self)

    def set_number(self, num):
        self.display_number = num
        self._update_elements_style()

    def set_playing_state(self, is_playing):
        self.is_playing = is_playing
        try:
            if is_playing:
                self.has_played = True
                self.station_data["_has_played"] = True
                self.station_data["has_played"] = True
                self.is_imported_new = False
                self.station_data["is_new"] = False
                self.station_data.pop("_is_imported_new", None)
                if hasattr(self, 'anim_timer') and self.anim_timer:
                    self.anim_timer.start()
            else:
                if hasattr(self, 'anim_timer') and self.anim_timer:
                    self.anim_timer.stop()
                if hasattr(self, 'now_playing_label'):
                    self.now_playing_label.setText("")
                    self.now_playing_label.setVisible(False)
                if hasattr(self, 'quality_badge'):
                    self.quality_badge.setText("")
                    self.quality_badge.setVisible(False)
            self._update_elements_style()
            self.update()
            win = self.window()
            if hasattr(win, '_save_app_state'):
                win._save_app_state()
        except RuntimeError:
            pass

    def set_track_title(self, track: str):
        try:
            if track and self.is_playing:
                self.now_playing_label.setText(f"🎵 {track}")
                self.now_playing_label.setVisible(True)
            else:
                self.now_playing_label.setText("")
                self.now_playing_label.setVisible(False)
        except Exception:
            pass

    def set_stream_quality(self, codec: str, bitrate: str):
        pass

    def set_imported_new(self, val: bool):
        self.is_imported_new = val
        self.station_data["_is_imported_new"] = val
        self.station_data["is_new"] = val
        if val:
            self.has_played = False
            self.station_data["has_played"] = False
            self.station_data.pop("_has_played", None)
        self._update_elements_style()
        self.update()
        win = self.window()
        if hasattr(win, '_save_app_state'):
            win._save_app_state()

    def _on_check_changed(self, checked):
        self.station_data["_marked_delete"] = checked
        self._update_elements_style()
        if callable(self.on_delete_toggle):
            try:
                self.on_delete_toggle(self, checked)
            except TypeError:
                self.on_delete_toggle()

    def _update_elements_style(self):
        st_id = self.display_num
        db_id = self.station_data.get("id", 0)
        self.num_label.setToolTip(f"№ {st_id} у поточному списку\n(Базовий ID: #{db_id})")

        if self.is_playing:
            self.num_label.setText(f"#{st_id}\n🎵")
            self.num_label.setStyleSheet("""
                color: #ffffff;
                font-size: 22px;
                font-weight: 900;
                background-color: #1a5933;
                border: 2px solid #39ff14;
                border-radius: 8px;
            """)
            self.title_label.setStyleSheet("color: #ffffff; font-size: 23px; font-weight: 900; background: transparent;")
            self.desc_label.setStyleSheet("color: #e6edf3; font-size: 18px; font-weight: 600; background: transparent;")
            self.play_btn.setText("⏹ Стоп")
            self.play_btn.setStyleSheet("""
                QPushButton {
                    background-color: #e63946;
                    color: #ffffff;
                    font-size: 18px;
                    font-weight: bold;
                    border-radius: 8px;
                    border: 1px solid #ff7b72;
                }
                QPushButton:hover {
                    background-color: #cb2431;
                }
            """)
        elif self.delete_cb.isChecked():
            # Трішки яскравіший насичений червоний
            self.num_label.setText(f"#{st_id}")
            self.num_label.setStyleSheet("""
                color: #ff7b72;
                font-size: 26px;
                font-weight: 900;
                background-color: #351518;
                border: 1.5px solid #ff4d5a;
                border-radius: 8px;
            """)
            self.title_label.setStyleSheet("color: #ffccd0; font-size: 21px; font-weight: bold; background: transparent;")
            self.desc_label.setStyleSheet("color: #ffa198; font-size: 16px; background: transparent;")
            self.play_btn.setText("▶ Грати")
            self.play_btn.setStyleSheet("""
                QPushButton {
                    background-color: #4a1c21;
                    color: #ffccd0;
                    font-size: 18px;
                    font-weight: bold;
                    border-radius: 8px;
                    border: 1px solid #c93b45;
                }
                QPushButton:hover {
                    background-color: #0057b8;
                    color: #ffffff;
                }
            """)
        elif self.has_played:
            # Чорний фон для прослуханих треків
            self.num_label.setText(f"#{st_id}\n✓")
            self.num_label.setStyleSheet("""
                color: #6e7681;
                font-size: 21px;
                font-weight: 900;
                background-color: #000000;
                border: 1px solid #22262d;
                border-radius: 8px;
            """)
            self.title_label.setStyleSheet("color: #8b949e; font-size: 21px; font-weight: bold; background: transparent;")
            self.desc_label.setStyleSheet("color: #555d68; font-size: 16px; background: transparent;")
            self.play_btn.setText("▶ Грати")
            self.play_btn.setStyleSheet("""
                QPushButton {
                    background-color: #121417;
                    color: #7d8590;
                    font-size: 18px;
                    font-weight: bold;
                    border-radius: 8px;
                    border: 1px solid #252830;
                }
                QPushButton:hover {
                    background-color: #0057b8;
                    color: #ffffff;
                    border-color: #58a6ff;
                }
            """)
        elif self.is_imported_new:
            self.num_label.setText(f"#{st_id}\n✨")
            self.num_label.setStyleSheet("""
                color: #3fb950;
                font-size: 21px;
                font-weight: 900;
                background-color: #122216;
                border: 1.5px solid #2ea043;
                border-radius: 8px;
            """)
            self.title_label.setStyleSheet("color: #ffffff; font-size: 21px; font-weight: bold; background: transparent;")
            self.desc_label.setStyleSheet("color: #8b949e; font-size: 16px; background: transparent;")
            self.play_btn.setText("▶ Грати")
            self.play_btn.setStyleSheet("""
                QPushButton {
                    background-color: #238636;
                    color: #ffffff;
                    font-size: 18px;
                    font-weight: bold;
                    border-radius: 8px;
                    border: none;
                }
                QPushButton:hover {
                    background-color: #2ea043;
                }
            """)
        else:
            # Звичайна сіра непрослухана картка
            self.num_label.setText(f"#{st_id}")
            self.num_label.setStyleSheet("""
                color: #58a6ff;
                font-size: 26px;
                font-weight: 900;
                background-color: #1e2227;
                border: 1px solid #353b44;
                border-radius: 8px;
            """)
            self.title_label.setStyleSheet("color: #ffffff; font-size: 21px; font-weight: bold; background: transparent;")
            self.desc_label.setStyleSheet("color: #8b949e; font-size: 16px; background: transparent;")
            self.play_btn.setText("▶ Грати")
            self.play_btn.setStyleSheet("""
                QPushButton {
                    background-color: #0057b8;
                    color: #ffffff;
                    font-size: 18px;
                    font-weight: bold;
                    border-radius: 8px;
                    border: none;
                }
                QPushButton:hover {
                    background-color: #006ce6;
                }
            """)

        self.update()

    def minimumSizeHint(self):
        return QSize(100, 88)

    def sizeHint(self):
        return QSize(350, 88)


class AddStationCard(QFrame):
    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self.on_click = on_click
        self.is_hovered = False
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Натисніть, щоб додати нову радіостанцію вручну")
        self.setFixedHeight(92)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(14)

        icon_label = QLabel("➕")
        icon_label.setStyleSheet("font-size: 38px; color: #3fb950; background: transparent;")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setFixedWidth(50)
        layout.addWidget(icon_label)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        title_label = QLabel("Додати станцію (Insert)")
        title_label.setStyleSheet("color: #3fb950; font-size: 26px; font-weight: bold; background: transparent;")
        sub_label = QLabel("Клікніть або натисніть Insert для введення назви та URL")
        sub_label.setStyleSheet("color: #8b949e; font-size: 18px; background: transparent;")
        text_layout.addWidget(title_label)
        text_layout.addWidget(sub_label)
        layout.addLayout(text_layout, stretch=1)

    def enterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.on_click()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)

        if self.is_hovered:
            pen = QPen(QColor(63, 185, 80), 2, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(24, 52, 33))
        else:
            pen = QPen(QColor(46, 160, 67), 1.5, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(18, 36, 24))

        p.drawRoundedRect(rect, 10, 10)
        p.end()

    def minimumSizeHint(self):
        return QSize(100, 88)

    def sizeHint(self):
        return QSize(350, 88)


class StyledMessageBox(QMessageBox):
    def __init__(self, icon, title, text, buttons=QMessageBox.StandardButton.Ok, default_button=None, parent=None):
        super().__init__(parent)
        self.setIcon(icon)
        self.setWindowTitle(title)
        self.setText(text)
        self.setStandardButtons(buttons)
        if default_button:
            self.setDefaultButton(default_button)

        self.setStyleSheet("""
            QMessageBox {
                background-color: #1e1f22;
                border: 1px solid #3c3f41;
                border-radius: 10px;
            }
            QLabel {
                color: #ffffff;
                font-size: 19px;
                font-weight: 500;
                background: transparent;
                min-height: 54px;
                padding: 4px;
            }
            QPushButton {
                background-color: #2b2d30;
                color: #ffffff;
                font-size: 19px;
                font-weight: bold;
                border: 1px solid #3c3f41;
                border-radius: 8px;
                padding: 8px 24px;
                min-width: 105px;
                min-height: 34px;
            }
            QPushButton:hover {
                background-color: #35383c;
                border-color: #58a6ff;
            }
            QPushButton:focus {
                border-color: #58a6ff;
            }
        """)

        # Custom buttons styling
        btn_ok = self.button(QMessageBox.StandardButton.Ok)
        if btn_ok:
            btn_ok.setText("ОК")
            btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_ok.setStyleSheet("""
                QPushButton {
                    background-color: #238636;
                    color: #ffffff;
                    font-size: 19px;
                    font-weight: bold;
                    border: none;
                    border-radius: 8px;
                    padding: 8px 26px;
                    min-width: 105px;
                    min-height: 34px;
                }
                QPushButton:hover {
                    background-color: #2ea043;
                }
                QPushButton:pressed {
                    background-color: #1a6327;
                }
            """)

        btn_yes = self.button(QMessageBox.StandardButton.Yes)
        if btn_yes:
            btn_yes.setText("Так, видалити")
            btn_yes.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_yes.setStyleSheet("""
                QPushButton {
                    background-color: #d73a49;
                    color: #ffffff;
                    font-size: 19px;
                    font-weight: bold;
                    border: none;
                    border-radius: 8px;
                    padding: 8px 22px;
                    min-width: 125px;
                    min-height: 34px;
                }
                QPushButton:hover {
                    background-color: #cb2431;
                }
                QPushButton:pressed {
                    background-color: #b31d28;
                }
            """)

        btn_no = self.button(QMessageBox.StandardButton.No)
        if btn_no:
            btn_no.setText("Ні, скасувати")
            btn_no.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_no.setStyleSheet("""
                QPushButton {
                    background-color: #2b2d30;
                    color: #c9d1d9;
                    font-size: 19px;
                    font-weight: bold;
                    border: 1px solid #3c3f41;
                    border-radius: 8px;
                    padding: 8px 22px;
                    min-width: 125px;
                    min-height: 34px;
                }
                QPushButton:hover {
                    background-color: #35383c;
                    border-color: #58a6ff;
                }
            """)

        btn_cancel = self.button(QMessageBox.StandardButton.Cancel)
        if btn_cancel:
            btn_cancel.setText("Скасувати")
            btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)

    @classmethod
    def info(cls, parent, title, text):
        box = cls(QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.Ok, parent=parent)
        return box.exec()

    @classmethod
    def warning(cls, parent, title, text):
        box = cls(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, parent=parent)
        return box.exec()

    @classmethod
    def question_yes_no(cls, parent, title, text):
        box = cls(
            QMessageBox.Icon.Question,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            default_button=QMessageBox.StandardButton.No,
            parent=parent
        )
        return box.exec() == QMessageBox.StandardButton.Yes

    @classmethod
    def critical(cls, parent, title, text):
        box = cls(QMessageBox.Icon.Critical, title, text, QMessageBox.StandardButton.Ok, parent=parent)
        return box.exec()




class ImportProgressDialog(QDialog):
    def __init__(self, total_items: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Імпорт та перевірка на дублікати")
        self.setFixedSize(600, 280)
        self.setModal(True)
        self.total_items = max(1, total_items)
        self.is_cancelled = False

        self.setStyleSheet("""
            QDialog {
                background-color: #1e1f22;
                border: 1.5px solid #3c3f41;
                border-radius: 12px;
            }
            QLabel {
                color: #e6edf3;
            }
            QProgressBar {
                background-color: #121316;
                border: 1px solid #3c3f41;
                border-radius: 8px;
                text-align: center;
                color: #ffffff;
                font-size: 17px;
                font-weight: bold;
                height: 32px;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #238636, stop:1 #3fb950);
                border-radius: 7px;
            }
            QPushButton {
                background-color: #2b2d30;
                color: #c9d1d9;
                font-size: 18px;
                font-weight: bold;
                border: 1px solid #3c3f41;
                border-radius: 8px;
                padding: 8px 24px;
            }
            QPushButton:hover {
                background-color: #35383c;
                border-color: #58a6ff;
                color: #ffffff;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(14)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)
        icon_label = QLabel("🔍")
        icon_label.setStyleSheet("font-size: 34px;")
        header_layout.addWidget(icon_label)

        title_vbox = QVBoxLayout()
        title_vbox.setSpacing(2)
        title_label = QLabel("Аналіз плейлиста та пошук дублікатів")
        title_label.setStyleSheet("font-size: 22px; font-weight: bold; color: #58a6ff;")
        self.sub_title = QLabel("Перевірка кожного аудіопотоку та назви станції...")
        self.sub_title.setStyleSheet("font-size: 16px; color: #8b949e;")
        title_vbox.addWidget(title_label)
        title_vbox.addWidget(self.sub_title)
        header_layout.addLayout(title_vbox, stretch=1)
        layout.addLayout(header_layout)

        self.current_label = QLabel("Підготовка до аналізу...")
        self.current_label.setStyleSheet("color: #e6edf3; font-size: 17px; font-weight: 600;")
        layout.addWidget(self.current_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, self.total_items)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.stats_label = QLabel("Оброблено: 0 | Нових унікальних: 0 | Відсіяно дублікатів: 0")
        self.stats_label.setStyleSheet("color: #8b949e; font-size: 16px; font-weight: 500;")
        layout.addWidget(self.stats_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.cancel_btn = QPushButton("Скасувати")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

    def _on_cancel(self):
        self.is_cancelled = True
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("Скасування...")
        self.current_label.setText("Зупинка процесу імпорту...")
        QApplication.processEvents()

    def set_stage(self, title: str, subtitle: str = ""):
        if subtitle:
            self.sub_title.setText(subtitle)
        self.current_label.setText(title)
        QApplication.processEvents()

    def set_range(self, minimum: int, maximum: int):
        self.total_items = max(1, maximum)
        self.progress_bar.setRange(minimum, self.total_items)
        QApplication.processEvents()

    def set_progress(self, value: int, current_text: str = "", stats_text: str = ""):
        self.progress_bar.setValue(value)
        if current_text:
            trunc = current_text if len(current_text) <= 55 else current_text[:52] + "..."
            self.current_label.setText(trunc)
        if stats_text:
            self.stats_label.setText(stats_text)
        QApplication.processEvents()

    def update_progress(self, current_idx: int, station_name: str, new_count: int, dup_count: int, filtered_count: int = 0):
        self.progress_bar.setValue(current_idx)
        trunc_name = station_name if len(station_name) <= 45 else station_name[:42] + "..."
        self.current_label.setText(f"Перевірка: {trunc_name}")
        stats_msg = f"Оброблено: {current_idx}/{self.total_items} | Нових: {new_count} | Дублікатів: {dup_count}"
        if filtered_count > 0:
            stats_msg += f" | Відсіяно фільтром: {filtered_count}"
        self.stats_label.setText(stats_msg)
        QApplication.processEvents()


# Визначення всіх країн світу для фільтрації та імпорту (Назва, ISO код країни, Регулярний вираз)
COUNTRY_DEFINITIONS = [
    ("🌍 Усі країни", "", ""),
    ("🏳️ Не вказано", "__NONE__", "__NONE__"),
    ('🇺🇦 Україна', 'UA', '(?:\\.ua(?:/|:|\\?|$))|(?:\\bukrain|\\bукраїн|київ|львів|харків|одес|дніпр|запоріж|полтав|вінниц|черніг|черкас|житомир|суми|рівне|івано-франк|терноп|луцьк|ужгород|хмельницьк|кривий ріг|миколаїв|херсон|донбас|донбасс|бахмут|краматорськ|маріуполь|ukr\\.radio|z-polus)|(?:\\bukraine\\b)|(?:\\bукраїна\\b)'),
    ('🇪🇬 Єгипет', 'EG', '(?:\\.eg(?:/|:|\\?|$))|(?:\\begypt|\\bcairo\\b|єгипет)|(?:\\begypt\\b)|(?:\\bєгипет\\b)'),
    ('🇾🇪 Ємен', 'YE', '(?:\\.ye(?:/|:|\\?|$))|(?:\\byemen\\b|sanaa|ємен)|(?:\\byemen\\b)|(?:\\bємен\\b)'),
    ('🇮🇱 Ізраїль', 'IL', '(?:\\.il(?:/|:|\\?|$))|(?:\\bisrael|\\bjerusalem\\b|tel aviv|ізраїл)|(?:\\bisrael\\b)|(?:\\bізраїль\\b)'),
    ('🇮🇩 Індонезія', 'ID', '(?:\\.id(?:/|:|\\?|$))|(?:\\bindonesia|\\bjakarta\\b|індонезі)|(?:\\bindonesia\\b)|(?:\\bіндонезія\\b)'),
    ('🇮🇳 Індія', 'IN', '(?:\\bindia|\\bdelhi\\b|\\bmumbai\\b|bollywood|індія)|(?:\\bindia\\b)|(?:\\bіндія\\b)'),
    ('🇮🇶 Ірак', 'IQ', '(?:\\.iq(?:/|:|\\?|$))|(?:\\biraq\\b|\\bbaghdad\\b|ірак)|(?:\\biraq\\b)|(?:\\bірак\\b)'),
    ('🇮🇷 Іран', 'IR', '(?:\\.ir(?:/|:|\\?|$))|(?:\\biran\\b|\\btehran\\b|іран)|(?:\\bislamic\\ republic\\ of\\ iran\\b)|(?:\\bіран\\b)'),
    ('🇮🇪 Ірландія', 'IE', '(?:\\.ie(?:/|:|\\?|$))|(?:\\bireland|\\bdublin\\b|\\brte\\b|ірланді)|(?:\\bireland\\b)|(?:\\bірландія\\b)'),
    ('🇮🇸 Ісландія', 'IS', '(?:\\biceland|\\breykjavik\\b|ісланді)|(?:\\biceland\\b)|(?:\\bісландія\\b)'),
    ('🇪🇸 Іспанія', 'ES', '(?:\\.es(?:/|:|\\?|$))|(?:\\bspain|\\bespañ\\b|madrid|barcelona|valencia|sevilla|cadena|los 40|\\bcope\\b|onda cero|radio nacional|іспані)|(?:\\bspain\\b)|(?:\\bіспанія\\b)'),
    ('🇮🇹 Італія', 'IT', '(?:\\bitalia|\\bitaly|\\bitalian\\b|\\brome\\b|\\broma\\b|milano|napoli|torino|radio italia|\\brai\\b|\\bdeejay\\b|\\brds\\b|rtl 102\\.5|virgin radio italy)|(?:\\bitaly\\b)|(?:\\bіталія\\b)'),
    ('🇦🇺 Австралія', 'AU', '(?:\\.au(?:/|:|\\?|$))|(?:\\baustrali|sydney|melbourne|brisbane)|(?:\\baustralia\\b)|(?:\\bавстралія\\b)'),
    ('🇦🇹 Австрія', 'AT', '(?:\\baustria|österreich|\\bwien\\b|\\bvienna\\b|kronehit|\\b(?:oe3|fm4)\\b)|(?:\\baustria\\b)|(?:\\bавстрія\\b)'),
    ('🇦🇿 Азербайджан', 'AZ', '(?:\\.az(?:/|:|\\?|$))|(?:\\bazerbaijan|\\bbaku\\b|азербайджан)|(?:\\bazerbaijan\\b)|(?:\\bазербайджан\\b)'),
    ('🇦🇽 Аландські острови', 'AX', '(?:\\.ax(?:/|:|\\?|$))|(?:\\baland)|(?:\\baland\\ islands\\b)|(?:\\bаландські\\ острови\\b)'),
    ('🇦🇱 Албанія', 'AL', '(?:\\.al(?:/|:|\\?|$))|(?:\\balbania|\\bshqip)|(?:\\balbania\\b)|(?:\\bалбанія\\b)'),
    ('🇩🇿 Алжир', 'DZ', '(?:\\.dz(?:/|:|\\?|$))|(?:\\balgeria|\\balgerie)|(?:\\balgeria\\b)|(?:\\bалжир\\b)'),
    ('🇦🇸 Американське Самоа', 'AS', '(?:\\.as(?:/|:|\\?|$))|(?:american samoa)|(?:\\bamerican\\ samoa\\b)|(?:\\bамериканське\\ самоа\\b)'),
    ('🇻🇮 Американські Віргінські острови', 'VI', '(?:\\.vi(?:/|:|\\?|$))|(?:us virgin)|(?:\\bus\\ virgin\\ islands\\b)|(?:\\bамериканські\\ віргінські\\ острови\\b)'),
    ('🇦🇴 Ангола', 'AO', '(?:\\.ao(?:/|:|\\?|$))|(?:\\bangola)|(?:\\bangola\\b)|(?:\\bангола\\b)'),
    ('🇦🇮 Ангілья', 'AI', '(?:\\banguilla)|(?:\\banguilla\\b)|(?:\\bангілья\\b)'),
    ('🇦🇩 Андорра', 'AD', '(?:\\.ad(?:/|:|\\?|$))|(?:\\bandorra)|(?:\\bandorra\\b)|(?:\\bандорра\\b)'),
    ('🇦🇶 Антарктида', 'AQ', '(?:\\.aq(?:/|:|\\?|$))|(?:\\bantarctica)|(?:\\bantarctica\\b)|(?:\\bантарктида\\b)'),
    ('🇦🇬 Антигуа і Барбуда', 'AG', '(?:\\bantigua|\\bbarbuda)|(?:\\bantigua\\ and\\ barbuda\\b)|(?:\\bантигуа\\ і\\ барбуда\\b)'),
    ('🇦🇷 Аргентина', 'AR', '(?:\\.ar(?:/|:|\\?|$))|(?:\\bargentin|buenos aires)|(?:\\bargentina\\b)|(?:\\bаргентина\\b)'),
    ('🇦🇼 Аруба', 'AW', '(?:\\.aw(?:/|:|\\?|$))|(?:\\baruba)|(?:\\baruba\\b)|(?:\\bаруба\\b)'),
    ('🇦🇫 Афганістан', 'AF', '(?:\\.af(?:/|:|\\?|$))|(?:\\bafghan)|(?:\\bafghanistan\\b)|(?:\\bафганістан\\b)'),
    ('🇧🇸 Багами', 'BS', '(?:\\.bs(?:/|:|\\?|$))|(?:\\bbahamas\\b|nassau)|(?:\\bthe\\ bahamas\\b)|(?:\\bбагами\\b)'),
    ('🇧🇩 Бангладеш', 'BD', '(?:\\.bd(?:/|:|\\?|$))|(?:\\bbangladesh|\\bdhaka\\b)|(?:\\bbangladesh\\b)|(?:\\bбангладеш\\b)'),
    ('🇧🇧 Барбадос', 'BB', '(?:\\.bb(?:/|:|\\?|$))|(?:\\bbarbados)|(?:\\bbarbados\\b)|(?:\\bбарбадос\\b)'),
    ('🇧🇭 Бахрейн', 'BH', '(?:\\.bh(?:/|:|\\?|$))|(?:\\bbahrain|\\bmanama\\b)|(?:\\bbahrain\\b)|(?:\\bбахрейн\\b)'),
    ('🇧🇪 Бельгія', 'BE', '(?:\\bbelgi|brussels|bruxelles|\\bvrt\\b|\\brtbf\\b)|(?:\\bbelgium\\b)|(?:\\bбельгія\\b)'),
    ('🇧🇿 Беліз', 'BZ', '(?:\\bbelize)|(?:\\bbelize\\b)|(?:\\bбеліз\\b)'),
    ('🇧🇯 Бенін', 'BJ', '(?:\\.bj(?:/|:|\\?|$))|(?:\\bbenin)|(?:\\bbenin\\b)|(?:\\bбенін\\b)'),
    ('🇧🇲 Бермуди', 'BM', '(?:\\.bm(?:/|:|\\?|$))|(?:\\bbermuda)|(?:\\bbermuda\\b)|(?:\\bбермуди\\b)'),
    ('🇧🇬 Болгарія', 'BG', '(?:\\.bg(?:/|:|\\?|$))|(?:\\bbulgar|\\bsofia\\b|българ|болгар)|(?:\\bbulgaria\\b)|(?:\\bболгарія\\b)'),
    ('🇧🇴 Болівія', 'BO', '(?:\\.bo(?:/|:|\\?|$))|(?:\\bbolivia|la paz)|(?:\\bbolivia\\b)|(?:\\bболівія\\b)'),
    ('🇧🇶 Бонайре', 'BQ', '(?:\\.bq(?:/|:|\\?|$))|(?:\\bbonaire)|(?:\\bbonaire\\b)|(?:\\bбонайре\\b)'),
    ('🇧🇦 Боснія і Герцеговина', 'BA', '(?:\\.ba(?:/|:|\\?|$))|(?:\\bbosnia|\\bsarajevo\\b|herzegovina)|(?:\\bbosnia\\ and\\ herzegovina\\b)|(?:\\bбоснія\\ і\\ герцеговина\\b)'),
    ('🇧🇼 Ботсвана', 'BW', '(?:\\.bw(?:/|:|\\?|$))|(?:\\bbotswana)|(?:\\bbotswana\\b)|(?:\\bботсвана\\b)'),
    ('🇧🇷 Бразилія', 'BR', '(?:\\.br(?:/|:|\\?|$))|(?:\\bbrasil|\\bbrazil|sao paulo|rio de janeiro|jovem pan)|(?:\\bbrazil\\b)|(?:\\bбразилія\\b)'),
    ('🇮🇴 Британська територія в Індійському океані', 'IO', '(?:diego garcia)|(?:\\bbritish\\ indian\\ ocean\\ territory\\b)|(?:\\bбританська\\ територія\\ в\\ індійському\\ океані\\b)'),
    ('🇻🇬 Британські Віргінські острови', 'VG', '(?:\\.vg(?:/|:|\\?|$))|(?:tortola|british virgin)|(?:\\bbritish\\ virgin\\ islands\\b)|(?:\\bбританські\\ віргінські\\ острови\\b)'),
    ('🇧🇳 Бруней', 'BN', '(?:\\.bn(?:/|:|\\?|$))|(?:\\bbrunei)|(?:\\bbrunei\\ darussalam\\b)|(?:\\bбруней\\b)'),
    ('🇧🇫 Буркіна-Фасо', 'BF', '(?:\\.bf(?:/|:|\\?|$))|(?:burkina faso|ouagadougou)|(?:\\bburkina\\ faso\\b)|(?:\\bбуркіна\\-фасо\\b)'),
    ('🇧🇮 Бурунді', 'BI', '(?:\\.bi(?:/|:|\\?|$))|(?:\\bburundi)|(?:\\bburundi\\b)|(?:\\bбурунді\\b)'),
    ('🇧🇹 Бутан', 'BT', '(?:\\.bt(?:/|:|\\?|$))|(?:\\bbhutan)|(?:\\bbhutan\\b)|(?:\\bбутан\\b)'),
    ('🇧🇾 Білорусь', 'BY', '(?:\\bbelarus|\\bminsk\\b|білорусь)|(?:\\bbelarus\\b)|(?:\\bбілорусь\\b)'),
    ("🇻🇳 В'єтнам", 'VN', "(?:\\.vn(?:/|:|\\?|$))|(?:\\bvietnam\\b|hanoi|saigon|в'єтнам)|(?:\\bvietnam\\b)|(?:\\bв'єтнам\\b)"),
    ('🇻🇺 Вануату', 'VU', '(?:\\.vu(?:/|:|\\?|$))|(?:\\bvanuatu\\b)|(?:\\bvanuatu\\b)|(?:\\bвануату\\b)'),
    ('🇻🇦 Ватикан', 'VA', '(?:\\.va(?:/|:|\\?|$))|(?:\\bvatican\\b|holy see)|(?:\\bthe\\ holy\\ see\\b)|(?:\\bватикан\\b)'),
    ('🇬🇧 Велика Британія', 'GB', '(?:\\.gb(?:/|:|\\?|$))|(?:\\bbbc\\b|london|manchester|british|england|scotland|wales|glasgow|liverpool|birmingham|capital fm|heart|radio x|британі)|(?:\\bthe\\ united\\ kingdom\\ of\\ great\\ britain\\ and\\ northern\\ ireland\\b)|(?:\\bвелика\\ британія\\b)'),
    ('🇻🇪 Венесуела', 'VE', '(?:\\.ve(?:/|:|\\?|$))|(?:\\bvenezuela|\\bcaracas\\b)|(?:\\bbolivarian\\ republic\\ of\\ venezuela\\b)|(?:\\bвенесуела\\b)'),
    ('🇼🇫 Волліс і Футуна', 'WF', '(?:\\.wf(?:/|:|\\?|$))|(?:wallis and futuna)|(?:\\bwallis\\ and\\ futuna\\b)|(?:\\bволліс\\ і\\ футуна\\b)'),
    ('🇦🇲 Вірменія', 'AM', '(?:\\barmenia|yerevan|вірмен)|(?:\\barmenia\\b)|(?:\\bвірменія\\b)'),
    ('🇬🇦 Габон', 'GA', '(?:\\bgabon\\b|libreville)|(?:\\bgabon\\b)|(?:\\bгабон\\b)'),
    ('🇬🇲 Гамбія', 'GM', '(?:\\.gm(?:/|:|\\?|$))|(?:\\bgambia\\b)|(?:\\bthe\\ gambia\\b)|(?:\\bгамбія\\b)'),
    ('🇬🇭 Гана', 'GH', '(?:\\.gh(?:/|:|\\?|$))|(?:\\bghana|\\baccra\\b)|(?:\\bghana\\b)|(?:\\bгана\\b)'),
    ('🇬🇾 Гаяна', 'GY', '(?:\\.gy(?:/|:|\\?|$))|(?:\\bguyana\\b|georgetown)|(?:\\bguyana\\b)|(?:\\bгаяна\\b)'),
    ('🇭🇹 Гаїті', 'HT', '(?:\\.ht(?:/|:|\\?|$))|(?:\\bhaiti\\b|port-au-prince)|(?:\\bhaiti\\b)|(?:\\bгаїті\\b)'),
    ('🇬🇵 Гваделупа', 'GP', '(?:\\.gp(?:/|:|\\?|$))|(?:\\bguadeloupe\\b)|(?:\\bguadeloupe\\b)|(?:\\bгваделупа\\b)'),
    ('🇬🇹 Гватемала', 'GT', '(?:\\.gt(?:/|:|\\?|$))|(?:\\bguatemala\\b)|(?:\\bguatemala\\b)|(?:\\bгватемала\\b)'),
    ('🇬🇳 Гвінея', 'GN', '(?:\\.gn(?:/|:|\\?|$))|(?:\\bguinea\\b|conakry)|(?:\\bguinea\\b)|(?:\\bгвінея\\b)'),
    ('🇬🇼 Гвінея-Бісау', 'GW', '(?:\\.gw(?:/|:|\\?|$))|(?:guinea.bissau)|(?:\\bguinea\\ bissau\\b)|(?:\\bгвінея\\-бісау\\b)'),
    ('🇬🇬 Гернсі', 'GG', '(?:\\bguernsey\\b)|(?:\\bguernsey\\b)|(?:\\bгернсі\\b)'),
    ('🇭🇳 Гондурас', 'HN', '(?:\\.hn(?:/|:|\\?|$))|(?:\\bhonduras\\b|tegucigalpa)|(?:\\bhonduras\\b)|(?:\\bгондурас\\b)'),
    ('🇭🇰 Гонконг', 'HK', '(?:\\.hk(?:/|:|\\?|$))|(?:hong kong|\\brthk\\b)|(?:\\bhong\\ kong\\b)|(?:\\bгонконг\\b)'),
    ('🇬🇩 Гренада', 'GD', '(?:\\.gd(?:/|:|\\?|$))|(?:\\bgrenada\\b)|(?:\\bgrenada\\b)|(?:\\bгренада\\b)'),
    ('🇬🇱 Гренландія', 'GL', '(?:\\.gl(?:/|:|\\?|$))|(?:\\bgreenland|kalaallit)|(?:\\bgreenland\\b)|(?:\\bгренландія\\b)'),
    ('🇬🇷 Греція', 'GR', '(?:\\.gr(?:/|:|\\?|$))|(?:\\bgreece|\\bathens\\b|ellinikos|greek|греці)|(?:\\bgreece\\b)|(?:\\bгреція\\b)'),
    ('🇬🇪 Грузія', 'GE', '(?:\\.ge(?:/|:|\\?|$))|(?:\\bgeorgia|\\btbilisi\\b|грузія)|(?:\\bgeorgia\\b)|(?:\\bгрузія\\b)'),
    ('🇬🇺 Гуам', 'GU', '(?:\\.gu(?:/|:|\\?|$))|(?:\\bguam\\b)|(?:\\bguam\\b)|(?:\\bгуам\\b)'),
    ('🇬🇮 Гібралтар', 'GI', '(?:\\.gi(?:/|:|\\?|$))|(?:\\bgibraltar\\b)|(?:\\bgibraltar\\b)|(?:\\bгібралтар\\b)'),
    ('🇨🇩 ДР Конго', 'CD', '(?:dr congo|kinshasa)|(?:\\bthe\\ democratic\\ republic\\ of\\ the\\ congo\\b)|(?:\\bдр\\ конго\\b)'),
    ('🇩🇰 Данія', 'DK', '(?:\\.dk(?:/|:|\\?|$))|(?:\\bdenmark|\\bdanmark\\b|copenhagen|københavn)|(?:\\bdenmark\\b)|(?:\\bданія\\b)'),
    ('🇩🇯 Джибуті', 'DJ', '(?:\\bdjibouti\\b)|(?:\\bdjibouti\\b)|(?:\\bджибуті\\b)'),
    ('🇩🇲 Домініка', 'DM', '(?:\\.dm(?:/|:|\\?|$))|(?:\\bdominica\\b)|(?:\\bdominica\\b)|(?:\\bдомініка\\b)'),
    ('🇩🇴 Домініканська Республіка', 'DO', '(?:\\.do(?:/|:|\\?|$))|(?:dominican republic|santo domingo)|(?:\\bthe\\ dominican\\ republic\\b)|(?:\\bдомініканська\\ республіка\\b)'),
    ('🇪🇨 Еквадор', 'EC', '(?:\\.ec(?:/|:|\\?|$))|(?:\\becuador|\\bquito\\b|guayaquil)|(?:\\becuador\\b)|(?:\\bеквадор\\b)'),
    ('🇬🇶 Екваторіальна Гвінея', 'GQ', '(?:\\.gq(?:/|:|\\?|$))|(?:equatorial guinea)|(?:\\bequatorial\\ guinea\\b)|(?:\\bекваторіальна\\ гвінея\\b)'),
    ('🇪🇷 Еритрея', 'ER', '(?:\\beritrea|\\basmara\\b)|(?:\\beritrea\\b)|(?:\\bеритрея\\b)'),
    ('🇸🇿 Есватіні', 'SZ', '(?:\\.sz(?:/|:|\\?|$))|(?:\\beswatini|\\bswaziland\\b)|(?:\\beswatini\\b)|(?:\\bесватіні\\b)'),
    ('🇪🇪 Естонія', 'EE', '(?:\\.ee(?:/|:|\\?|$))|(?:\\bestonia|\\beesti\\b|tallinn)|(?:\\bestonia\\b)|(?:\\bестонія\\b)'),
    ('🇪🇹 Ефіопія', 'ET', '(?:\\.et(?:/|:|\\?|$))|(?:\\bethiopia|addis ababa)|(?:\\bethiopia\\b)|(?:\\bефіопія\\b)'),
    ('🇿🇲 Замбія', 'ZM', '(?:\\.zm(?:/|:|\\?|$))|(?:\\bzambia\\b|lusaka)|(?:\\bzambia\\b)|(?:\\bзамбія\\b)'),
    ('🇺🇲 Зовнішні малі острови США', 'UM', '(?:\\.um(?:/|:|\\?|$))|(?:minor outlying islands)|(?:\\bthe\\ united\\ states\\ minor\\ outlying\\ islands\\b)|(?:\\bзовнішні\\ малі\\ острови\\ сша\\b)'),
    ('🇿🇼 Зімбабве', 'ZW', '(?:\\.zw(?:/|:|\\?|$))|(?:\\bzimbabwe\\b|harare)|(?:\\bzimbabwe\\b)|(?:\\bзімбабве\\b)'),
    ('🇯🇴 Йорданія', 'JO', '(?:\\.jo(?:/|:|\\?|$))|(?:\\bjordan|\\bamman\\b|йордані)|(?:\\bjordan\\b)|(?:\\bйорданія\\b)'),
    ('🇰🇵 КНДР (Північна Корея)', 'KP', '(?:\\.kp(?:/|:|\\?|$))|(?:north korea|\\bdprk\\b|pyongyang)|(?:\\bthe\\ democratic\\ peoples\\ republic\\ of\\ korea\\b)|(?:\\bкндр\\ \\(північна\\ корея\\)\\b)'),
    ('🇨🇻 Кабо-Верде', 'CV', '(?:\\.cv(?:/|:|\\?|$))|(?:cabo verde|cape verde)|(?:\\bcabo\\ verde\\b)|(?:\\bкабо\\-верде\\b)'),
    ('🇰🇿 Казахстан', 'KZ', '(?:\\.kz(?:/|:|\\?|$))|(?:\\bkazakh|almaty|astana|казахстан)|(?:\\bkazakhstan\\b)|(?:\\bказахстан\\b)'),
    ('🇰🇾 Кайманові острови', 'KY', '(?:\\.ky(?:/|:|\\?|$))|(?:cayman islands)|(?:\\bthe\\ cayman\\ islands\\b)|(?:\\bкайманові\\ острови\\b)'),
    ('🇰🇭 Камбоджа', 'KH', '(?:\\.kh(?:/|:|\\?|$))|(?:\\bcambodia|phnom penh)|(?:\\bcambodia\\b)|(?:\\bкамбоджа\\b)'),
    ('🇨🇲 Камерун', 'CM', '(?:\\.cm(?:/|:|\\?|$))|(?:\\bcameroon|\\bcameroun\\b)|(?:\\bcameroon\\b)|(?:\\bкамерун\\b)'),
    ('🇨🇦 Канада', 'CA', '(?:\\.ca(?:/|:|\\?|$))|(?:\\bcanada|\\bcanadian\\b|toronto|montreal|vancouver|calgary|ottawa|\\bcbc\\b)|(?:\\bcanada\\b)|(?:\\bканада\\b)'),
    ('🇶🇦 Катар', 'QA', '(?:\\.qa(?:/|:|\\?|$))|(?:\\bqatar\\b|doha)|(?:\\bqatar\\b)|(?:\\bкатар\\b)'),
    ('🇰🇪 Кенія', 'KE', '(?:\\.ke(?:/|:|\\?|$))|(?:\\bkenya|\\bnairobi\\b|кені)|(?:\\bkenya\\b)|(?:\\bкенія\\b)'),
    ('🇰🇬 Киргизстан', 'KG', '(?:\\.kg(?:/|:|\\?|$))|(?:\\bkyrgyz|bishkek|киргиз)|(?:\\bkyrgyzstan\\b)|(?:\\bкиргизстан\\b)'),
    ('🇨🇳 Китай', 'CN', '(?:\\.cn(?:/|:|\\?|$))|(?:\\bchina|\\bchinese\\b|beijing|shanghai|китай)|(?:\\bchina\\b)|(?:\\bкитай\\b)'),
    ('🇨🇨 Кокосові острови', 'CC', '(?:cocos islands)|(?:\\bthe\\ cocos\\ keeling\\ islands\\b)|(?:\\bкокосові\\ острови\\b)'),
    ('🇨🇴 Колумбія', 'CO', '(?:\\bcolombia|\\bbogota\\b|medellin)|(?:\\bcolombia\\b)|(?:\\bколумбія\\b)'),
    ('🇰🇲 Комори', 'KM', '(?:\\.km(?:/|:|\\?|$))|(?:\\bcomoros\\b)|(?:\\bthe\\ comoros\\b)|(?:\\bкомори\\b)'),
    ('🇨🇬 Конго', 'CG', '(?:\\.cg(?:/|:|\\?|$))|(?:\\bcongo\\b|brazzaville)|(?:\\bthe\\ congo\\b)|(?:\\bконго\\b)'),
    ('🇽🇰 Косово', 'XK', '(?:\\.xk(?:/|:|\\?|$))|(?:\\bkosovo\\b|prishtin)|(?:\\bkosovo\\b)|(?:\\bкосово\\b)'),
    ('🇨🇷 Коста-Рика', 'CR', '(?:\\.cr(?:/|:|\\?|$))|(?:costa rica|san jose)|(?:\\bcosta\\ rica\\b)|(?:\\bкоста\\-рика\\b)'),
    ("🇨🇮 Кот-д'Івуар", 'CI', "(?:\\.ci(?:/|:|\\?|$))|(?:cote d.ivoire|ivory coast|abidjan)|(?:\\bcoted\\ ivoire\\b)|(?:\\bкот\\-д'івуар\\b)"),
    ('🇷🇺 Країна-агресор', 'RU', '(?:\\.ru(?:/|:|\\?|$))|(?:\\brussia\\b|moscow|росія)|(?:\\bthe\\ russian\\ federation\\b)|(?:\\bкраїна\\-агресор\\b)'),
    ('🇨🇺 Куба', 'CU', '(?:\\.cu(?:/|:|\\?|$))|(?:\\bcuba\\b|\\bhavana\\b)|(?:\\bcuba\\b)|(?:\\bкуба\\b)'),
    ('🇰🇼 Кувейт', 'KW', '(?:\\.kw(?:/|:|\\?|$))|(?:\\bkuwait\\b)|(?:\\bkuwait\\b)|(?:\\bкувейт\\b)'),
    ('🇨🇼 Кюрасао', 'CW', '(?:\\.cw(?:/|:|\\?|$))|(?:\\bcuracao\\b|\\bcuraçao\\b)|(?:\\bcuracao\\b)|(?:\\bкюрасао\\b)'),
    ('🇨🇾 Кіпр', 'CY', '(?:\\bcyprus|\\bnicosia\\b|кіпр)|(?:\\bcyprus\\b)|(?:\\bкіпр\\b)'),
    ('🇰🇮 Кірибаті', 'KI', '(?:\\.ki(?:/|:|\\?|$))|(?:\\bkiribati\\b)|(?:\\bkiribati\\b)|(?:\\bкірибаті\\b)'),
    ('🇱🇦 Лаос', 'LA', '(?:\\blaos\\b|vientiane)|(?:\\bthe\\ lao\\ peoples\\ democratic\\ republic\\b)|(?:\\bлаос\\b)'),
    ('🇱🇻 Латвія', 'LV', '(?:\\.lv(?:/|:|\\?|$))|(?:\\blatvia|\\blatvija\\b|\\briga\\b|латві)|(?:\\blatvia\\b)|(?:\\bлатвія\\b)'),
    ('🇱🇸 Лесото', 'LS', '(?:\\.ls(?:/|:|\\?|$))|(?:\\blesotho\\b)|(?:\\blesotho\\b)|(?:\\bлесото\\b)'),
    ('🇱🇹 Литва', 'LT', '(?:\\.lt(?:/|:|\\?|$))|(?:\\blithuania|\\blietuva\\b|vilnius|литв)|(?:\\blithuania\\b)|(?:\\bлитва\\b)'),
    ('🇱🇺 Люксембург', 'LU', '(?:\\.lu(?:/|:|\\?|$))|(?:\\bluxembourg\\b)|(?:\\bluxembourg\\b)|(?:\\bлюксембург\\b)'),
    ('🇱🇷 Ліберія', 'LR', '(?:\\.lr(?:/|:|\\?|$))|(?:\\bliberia\\b|monrovia)|(?:\\bliberia\\b)|(?:\\bліберія\\b)'),
    ('🇱🇧 Ліван', 'LB', '(?:\\.lb(?:/|:|\\?|$))|(?:\\blebanon|\\bbeirut\\b|ліван)|(?:\\blebanon\\b)|(?:\\bліван\\b)'),
    ('🇱🇾 Лівія', 'LY', '(?:\\.ly(?:/|:|\\?|$))|(?:\\blibya|\\btripoli\\b|ліві)|(?:\\blibya\\b)|(?:\\bлівія\\b)'),
    ('🇱🇮 Ліхтенштейн', 'LI', '(?:\\bliechtenstein\\b|vaduz)|(?:\\bliechtenstein\\b)|(?:\\bліхтенштейн\\b)'),
    ("🇲🇲 М'янма", 'MM', "(?:\\.mm(?:/|:|\\?|$))|(?:\\bmyanmar\\b|\\bburma\\b|yangon)|(?:\\bmyanmar\\b)|(?:\\bм'янма\\b)"),
    ('🇲🇺 Маврикій', 'MU', '(?:\\.mu(?:/|:|\\?|$))|(?:\\bmauritius\\b)|(?:\\bmauritius\\b)|(?:\\bмаврикій\\b)'),
    ('🇲🇷 Мавританія', 'MR', '(?:\\.mr(?:/|:|\\?|$))|(?:\\bmauritania\\b|nouakchott)|(?:\\bmauritania\\b)|(?:\\bмавританія\\b)'),
    ('🇲🇬 Мадагаскар', 'MG', '(?:\\.mg(?:/|:|\\?|$))|(?:\\bmadagascar\\b|antananarivo)|(?:\\bmadagascar\\b)|(?:\\bмадагаскар\\b)'),
    ('🇾🇹 Майотта', 'YT', '(?:\\.yt(?:/|:|\\?|$))|(?:\\bmayotte\\b)|(?:\\bmayotte\\b)|(?:\\bмайотта\\b)'),
    ('🇲🇴 Макао', 'MO', '(?:\\.mo(?:/|:|\\?|$))|(?:\\bmacao\\b|\\bmacau\\b)|(?:\\bmacao\\b)|(?:\\bмакао\\b)'),
    ('🇲🇼 Малаві', 'MW', '(?:\\.mw(?:/|:|\\?|$))|(?:\\bmalawi\\b|lilongwe)|(?:\\bmalawi\\b)|(?:\\bмалаві\\b)'),
    ('🇲🇾 Малайзія', 'MY', '(?:\\.my(?:/|:|\\?|$))|(?:\\bmalaysia\\b|kuala lumpur)|(?:\\bmalaysia\\b)|(?:\\bмалайзія\\b)'),
    ('🇲🇻 Мальдіви', 'MV', '(?:\\.mv(?:/|:|\\?|$))|(?:\\bmaldives\\b)|(?:\\bmaldives\\b)|(?:\\bмальдіви\\b)'),
    ('🇲🇹 Мальта', 'MT', '(?:\\.mt(?:/|:|\\?|$))|(?:\\bmalta\\b|valletta)|(?:\\bmalta\\b)|(?:\\bмальта\\b)'),
    ('🇲🇱 Малі', 'ML', '(?:\\bmali\\b|bamako)|(?:\\bmali\\b)|(?:\\bмалі\\b)'),
    ('🇲🇦 Марокко', 'MA', '(?:\\.ma(?:/|:|\\?|$))|(?:\\bmorocco|\\bmaroc\\b|casablanca|rabat|марокко)|(?:\\bmorocco\\b)|(?:\\bмарокко\\b)'),
    ('🇲🇶 Мартиніка', 'MQ', '(?:\\.mq(?:/|:|\\?|$))|(?:\\bmartinique\\b)|(?:\\bmartinique\\b)|(?:\\bмартиніка\\b)'),
    ('🇲🇭 Маршаллові Острови', 'MH', '(?:\\.mh(?:/|:|\\?|$))|(?:marshall islands)|(?:\\bthe\\ marshall\\ islands\\b)|(?:\\bмаршаллові\\ острови\\b)'),
    ('🇲🇽 Мексика', 'MX', '(?:\\.mx(?:/|:|\\?|$))|(?:\\bmexico|ciudad de mexico|guadalajara|мексик)|(?:\\bmexico\\b)|(?:\\bмексика\\b)'),
    ('🇲🇿 Мозамбік', 'MZ', '(?:\\.mz(?:/|:|\\?|$))|(?:\\bmozambique\\b|maputo)|(?:\\bmozambique\\b)|(?:\\bмозамбік\\b)'),
    ('🇲🇩 Молдова', 'MD', '(?:\\bmoldova\\b|chisinau|кишинів|молдов)|(?:\\bthe\\ republic\\ of\\ moldova\\b)|(?:\\bмолдова\\b)'),
    ('🇲🇨 Монако', 'MC', '(?:\\.mc(?:/|:|\\?|$))|(?:\\bmonaco\\b|monte carlo)|(?:\\bmonaco\\b)|(?:\\bмонако\\b)'),
    ('🇲🇳 Монголія', 'MN', '(?:\\.mn(?:/|:|\\?|$))|(?:\\bmongolia\\b|ulaanbaatar)|(?:\\bmongolia\\b)|(?:\\bмонголія\\b)'),
    ('🇲🇸 Монтсеррат', 'MS', '(?:\\bmontserrat\\b)|(?:\\bmontserrat\\b)|(?:\\bмонтсеррат\\b)'),
    ('🇫🇲 Мікронезія', 'FM', '(?:\\bmicronesia)|(?:\\bfederated\\ states\\ of\\ micronesia\\b)|(?:\\bмікронезія\\b)'),
    ('🇳🇦 Намібія', 'NA', '(?:\\.na(?:/|:|\\?|$))|(?:\\bnamibia\\b|windhoek)|(?:\\bnamibia\\b)|(?:\\bнамібія\\b)'),
    ('🇳🇷 Науру', 'NR', '(?:\\.nr(?:/|:|\\?|$))|(?:\\bnauru\\b)|(?:\\bnauru\\b)|(?:\\bнауру\\b)'),
    ('🇳🇵 Непал', 'NP', '(?:\\.np(?:/|:|\\?|$))|(?:\\bnepal\\b|kathmandu)|(?:\\bnepal\\b)|(?:\\bнепал\\b)'),
    ('🇳🇿 Нова Зеландія', 'NZ', '(?:\\.nz(?:/|:|\\?|$))|(?:new zealand|auckland|wellington)|(?:\\bnew\\ zealand\\b)|(?:\\bнова\\ зеландія\\b)'),
    ('🇳🇨 Нова Каледонія', 'NC', '(?:\\.nc(?:/|:|\\?|$))|(?:new caledonia|noumea)|(?:\\bnew\\ caledonia\\b)|(?:\\bнова\\ каледонія\\b)'),
    ('🇳🇴 Норвегія', 'NO', '(?:\\.no(?:/|:|\\?|$))|(?:\\bnorway|\\bnorge\\b|oslo|норвег)|(?:\\bnorway\\b)|(?:\\bнорвегія\\b)'),
    ('🇳🇪 Нігер', 'NE', '(?:\\.ne(?:/|:|\\?|$))|(?:\\bniger\\b|niamey)|(?:\\bthe\\ niger\\b)|(?:\\bнігер\\b)'),
    ('🇳🇬 Нігерія', 'NG', '(?:\\.ng(?:/|:|\\?|$))|(?:\\bnigeria\\b|lagos|abuja)|(?:\\bnigeria\\b)|(?:\\bнігерія\\b)'),
    ('🇳🇱 Нідерланди', 'NL', '(?:\\.nl(?:/|:|\\?|$))|(?:\\bnederland|\\bdutch\\b|amsterdam|rotterdam|utrecht|radio 538|qmusic|sky radio|\\bnpo\\b|нідерланд)|(?:\\bthe\\ netherlands\\b)|(?:\\bнідерланди\\b)'),
    ('🇳🇮 Нікарагуа', 'NI', '(?:\\.ni(?:/|:|\\?|$))|(?:\\bnicaragua\\b|managua)|(?:\\bnicaragua\\b)|(?:\\bнікарагуа\\b)'),
    ('🇩🇪 Німеччина', 'DE', '(?:\\.de(?:/|:|\\?|$))|(?:\\bdeutsch|berlin|hamburg|münchen|munich|köln|frankfurt|germany|antenne|radio hamburg|laut\\.fm|rautemusik|\\b(?:wdr|ndr|swr|br|mdr|rbb|hr)\\b)|(?:\\bgermany\\b)|(?:\\bнімеччина\\b)'),
    ('🇳🇺 Ніуе', 'NU', '(?:\\bniue\\b)|(?:\\bniue\\b)|(?:\\bніуе\\b)'),
    ('🇦🇪 ОАЕ', 'AE', '(?:\\.ae(?:/|:|\\?|$))|(?:\\bemirates\\b|\\buae\\b|dubai|abu dhabi|оае)|(?:\\bthe\\ united\\ arab\\ emirates\\b)|(?:\\bоае\\b)'),
    ('🇴🇲 Оман', 'OM', '(?:\\boman\\b|\\bmuscat\\b)|(?:\\boman\\b)|(?:\\bоман\\b)'),
    ('🇨🇰 Острови Кука', 'CK', '(?:\\.ck(?:/|:|\\?|$))|(?:cook islands)|(?:\\bthe\\ cook\\ islands\\b)|(?:\\bострови\\ кука\\b)'),
    ('🇸🇭 Острови Святої Єлени', 'SH', '(?:saint helena|ascension)|(?:\\bascension\\ and\\ tristan\\ da\\ cunha\\ saint\\ helena\\b)|(?:\\bострови\\ святої\\ єлени\\b)'),
    ('🇮🇲 Острів Мен', 'IM', '(?:isle of man|\\bmanx\\b)|(?:\\bisle\\ of\\ man\\b)|(?:\\bострів\\ мен\\b)'),
    ('🇨🇽 Острів Різдва', 'CX', '(?:\\.cx(?:/|:|\\?|$))|(?:christmas island)|(?:\\bchristmas\\ island\\b)|(?:\\bострів\\ різдва\\b)'),
    ('🇵🇰 Пакистан', 'PK', '(?:\\.pk(?:/|:|\\?|$))|(?:\\bpakistan\\b|karachi|islamabad)|(?:\\bpakistan\\b)|(?:\\bпакистан\\b)'),
    ('🇵🇼 Палау', 'PW', '(?:\\.pw(?:/|:|\\?|$))|(?:\\bpalau\\b)|(?:\\bpalau\\b)|(?:\\bпалау\\b)'),
    ('🇵🇸 Палестина', 'PS', '(?:\\.ps(?:/|:|\\?|$))|(?:\\bpalestine\\b|gaza|ramallah)|(?:\\bstate\\ of\\ palestine\\b)|(?:\\bпалестина\\b)'),
    ('🇵🇦 Панама', 'PA', '(?:\\.pa(?:/|:|\\?|$))|(?:\\bpanama\\b)|(?:\\bpanama\\b)|(?:\\bпанама\\b)'),
    ('🇵🇬 Папуа-Нова Гвінея', 'PG', '(?:\\.pg(?:/|:|\\?|$))|(?:papua new guinea|port moresby)|(?:\\bpapua\\ new\\ guinea\\b)|(?:\\bпапуа\\-нова\\ гвінея\\b)'),
    ('🇵🇾 Парагвай', 'PY', '(?:\\.py(?:/|:|\\?|$))|(?:\\bparaguay\\b|asuncion)|(?:\\bparaguay\\b)|(?:\\bпарагвай\\b)'),
    ('🇵🇪 Перу', 'PE', '(?:\\.pe(?:/|:|\\?|$))|(?:\\bperu\\b|lima)|(?:\\bperu\\b)|(?:\\bперу\\b)'),
    ('🇵🇱 Польща', 'PL', '(?:\\.pl(?:/|:|\\?|$))|(?:\\bpolsk|\\bpolska\\b|\\bpoland\\b|warszaw|krakow|kraków|wrocław|wroclaw|poznań|gdańsk|lodz|łódź|\\b(?:rmf|zet|eska)\\b|polskie radio)|(?:\\bpoland\\b)|(?:\\bпольща\\b)'),
    ('🇵🇹 Португалія', 'PT', '(?:\\.pt(?:/|:|\\?|$))|(?:\\bportugal|\\blisbon\\b|\\blisboa\\b|porto|португал)|(?:\\bportugal\\b)|(?:\\bпортугалія\\b)'),
    ('🇵🇷 Пуерто-Рико', 'PR', '(?:\\.pr(?:/|:|\\?|$))|(?:puerto rico|san juan)|(?:\\bpuerto\\ rico\\b)|(?:\\bпуерто\\-рико\\b)'),
    ('🇿🇦 Південна Африка', 'ZA', '(?:\\.za(?:/|:|\\?|$))|(?:south africa|johannesburg|cape town|південно-африкан)|(?:\\bsouth\\ africa\\b)|(?:\\bпівденна\\ африка\\b)'),
    ('🇰🇷 Південна Корея', 'KR', '(?:\\.kr(?:/|:|\\?|$))|(?:\\bkorea\\b|seoul|корея)|(?:\\bthe\\ republic\\ of\\ korea\\b)|(?:\\bпівденна\\ корея\\b)'),
    ('🇸🇸 Південний Судан', 'SS', '(?:\\.ss(?:/|:|\\?|$))|(?:south sudan)|(?:\\bsouth\\ sudan\\b)|(?:\\bпівденний\\ судан\\b)'),
    ('🇲🇰 Північна Македонія', 'MK', '(?:\\.mk(?:/|:|\\?|$))|(?:\\bmacedonia\\b|skopje|македоні)|(?:\\brepublic\\ of\\ north\\ macedonia\\b)|(?:\\bпівнічна\\ македонія\\b)'),
    ('🇷🇪 Реюньйон', 'RE', '(?:\\.re(?:/|:|\\?|$))|(?:\\breunion\\b|\\bréunion\\b)|(?:\\breunion\\b)|(?:\\bреюньйон\\b)'),
    ('🇷🇼 Руанда', 'RW', '(?:\\.rw(?:/|:|\\?|$))|(?:\\brwanda\\b|kigali)|(?:\\brwanda\\b)|(?:\\bруанда\\b)'),
    ('🇷🇴 Румунія', 'RO', '(?:\\.ro(?:/|:|\\?|$))|(?:\\bromani|bucuresti|bucharest|kiss fm romania|radio zu|virgin radio romania|europa fm romania|румун)|(?:\\bromania\\b)|(?:\\bрумунія\\b)'),
    ('🇺🇸 США', 'US', '(?:\\bamerican\\b|\\busa\\b|new york|california|texas|florida|chicago|los angeles|seattle|boston|k-love|\\bihr\\b|iheart|\\bnpr\\b|miami|atlanta|dallas|phoenix|san francisco|сша)|(?:\\bthe\\ united\\ states\\ of\\ america\\b)|(?:\\bсша\\b)'),
    ('🇸🇻 Сальвадор', 'SV', '(?:\\.sv(?:/|:|\\?|$))|(?:el salvador|san salvador)|(?:\\bel\\ salvador\\b)|(?:\\bсальвадор\\b)'),
    ('🇸🇲 Сан-Марино', 'SM', '(?:\\.sm(?:/|:|\\?|$))|(?:san marino)|(?:\\bsan\\ marino\\b)|(?:\\bсан\\-марино\\b)'),
    ('🇸🇹 Сан-Томе і Принсіпі', 'ST', '(?:sao tome|principe)|(?:\\bsao\\ tome\\ and\\ principe\\b)|(?:\\bсан\\-томе\\ і\\ принсіпі\\b)'),
    ('🇸🇦 Саудівська Аравія', 'SA', '(?:\\.sa(?:/|:|\\?|$))|(?:\\bsaudi\\b|riyadh|саудівськ)|(?:\\bsaudi\\ arabia\\b)|(?:\\bсаудівська\\ аравія\\b)'),
    ('🇸🇯 Свальбард і Ян-Маєн', 'SJ', '(?:\\.sj(?:/|:|\\?|$))|(?:svalbard|jan mayen)|(?:\\bsvalbard\\ and\\ jan\\ mayen\\b)|(?:\\bсвальбард\\ і\\ ян\\-маєн\\b)'),
    ('🇸🇨 Сейшели', 'SC', '(?:\\bseychelles\\b)|(?:\\bseychelles\\b)|(?:\\bсейшели\\b)'),
    ("🇵🇲 Сен-П'єр і Мікелон", 'PM', "(?:\\.pm(?:/|:|\\?|$))|(?:saint pierre|miquelon)|(?:\\bsaint\\ pierre\\ and\\ miquelon\\b)|(?:\\bсен\\-п'єр\\ і\\ мікелон\\b)"),
    ('🇸🇳 Сенегал', 'SN', '(?:\\.sn(?:/|:|\\?|$))|(?:\\bsenegal\\b|dakar)|(?:\\bsenegal\\b)|(?:\\bсенегал\\b)'),
    ('🇻🇨 Сент-Вінсент і Гренадини', 'VC', '(?:\\.vc(?:/|:|\\?|$))|(?:saint vincent|grenadines)|(?:\\bsaint\\ vincent\\ and\\ the\\ grenadines\\b)|(?:\\bсент\\-вінсент\\ і\\ гренадини\\b)'),
    ('🇰🇳 Сент-Кіттс і Невіс', 'KN', '(?:\\.kn(?:/|:|\\?|$))|(?:saint kitts|nevis)|(?:\\bsaint\\ kitts\\ and\\ nevis\\b)|(?:\\bсент\\-кіттс\\ і\\ невіс\\b)'),
    ('🇱🇨 Сент-Люсія', 'LC', '(?:\\.lc(?:/|:|\\?|$))|(?:saint lucia)|(?:\\bsaint\\ lucia\\b)|(?:\\bсент\\-люсія\\b)'),
    ('🇷🇸 Сербія', 'RS', '(?:\\.rs(?:/|:|\\?|$))|(?:\\bserbia|\\bsrbija\\b|beograd|belgrade|сербі)|(?:\\bserbia\\b)|(?:\\bсербія\\b)'),
    ('🇸🇾 Сирія', 'SY', '(?:\\.sy(?:/|:|\\?|$))|(?:\\bsyria\\b|damascus|сирі)|(?:\\bsyrian\\ arab\\ republic\\b)|(?:\\bсирія\\b)'),
    ('🇸🇰 Словаччина', 'SK', '(?:\\.sk(?:/|:|\\?|$))|(?:\\bslovak|slovensk|bratislava|kosice|expres|словачч)|(?:\\bslovakia\\b)|(?:\\bсловаччина\\b)'),
    ('🇸🇮 Словенія', 'SI', '(?:\\.si(?:/|:|\\?|$))|(?:\\bslovenia|\\bslovenija\\b|ljubljana|словені)|(?:\\bslovenia\\b)|(?:\\bсловенія\\b)'),
    ('🇸🇧 Соломонові Острови', 'SB', '(?:\\.sb(?:/|:|\\?|$))|(?:solomon islands)|(?:\\bsolomon\\ islands\\b)|(?:\\bсоломонові\\ острови\\b)'),
    ('🇸🇴 Сомалі', 'SO', '(?:\\bsomalia\\b|mogadishu)|(?:\\bsomalia\\b)|(?:\\bсомалі\\b)'),
    ('🇸🇩 Судан', 'SD', '(?:\\.sd(?:/|:|\\?|$))|(?:\\bsudan\\b|khartoum)|(?:\\bthe\\ sudan\\b)|(?:\\bсудан\\b)'),
    ('🇸🇷 Суринам', 'SR', '(?:\\.sr(?:/|:|\\?|$))|(?:\\bsuriname\\b|paramaribo)|(?:\\bsuriname\\b)|(?:\\bсуринам\\b)'),
    ('🇹🇱 Східний Тимор', 'TL', '(?:\\.tl(?:/|:|\\?|$))|(?:timor-leste)|(?:\\btimor\\ leste\\b)|(?:\\bсхідний\\ тимор\\b)'),
    ('🇸🇱 Сьєрра-Леоне', 'SL', '(?:\\.sl(?:/|:|\\?|$))|(?:sierra leone|freetown)|(?:\\bsierra\\ leone\\b)|(?:\\bсьєрра\\-леоне\\b)'),
    ('🇸🇬 Сінгапур', 'SG', '(?:\\.sg(?:/|:|\\?|$))|(?:\\bsingapore\\b|сінгапур)|(?:\\bsingapore\\b)|(?:\\bсінгапур\\b)'),
    ('🇹🇯 Таджикистан', 'TJ', '(?:\\.tj(?:/|:|\\?|$))|(?:\\btajik\\b|dushanbe|таджик)|(?:\\btajikistan\\b)|(?:\\bтаджикистан\\b)'),
    ('🇹🇼 Тайвань', 'TW', '(?:\\.tw(?:/|:|\\?|$))|(?:\\btaiwan\\b|taipei|тайван)|(?:\\btaiwan,\\ republic\\ of\\ china\\b)|(?:\\bтайвань\\b)'),
    ('🇹🇿 Танзанія', 'TZ', '(?:\\.tz(?:/|:|\\?|$))|(?:\\btanzania\\b|dar es salaam)|(?:\\bunited\\ republic\\ of\\ tanzania\\b)|(?:\\bтанзанія\\b)'),
    ('🇹🇭 Таїланд', 'TH', '(?:\\.th(?:/|:|\\?|$))|(?:\\bthailand\\b|\\bthai\\b|bangkok|таїланд)|(?:\\bthailand\\b)|(?:\\bтаїланд\\b)'),
    ('🇹🇨 Теркс і Кайкос', 'TC', '(?:\\.tc(?:/|:|\\?|$))|(?:turks and caicos)|(?:\\bthe\\ turks\\ and\\ caicos\\ islands\\b)|(?:\\bтеркс\\ і\\ кайкос\\b)'),
    ('🇹🇬 Того', 'TG', '(?:\\.tg(?:/|:|\\?|$))|(?:\\btogo\\b)|(?:\\btogo\\b)|(?:\\bтого\\b)'),
    ('🇹🇴 Тонга', 'TO', '(?:\\btonga\\b)|(?:\\btonga\\b)|(?:\\bтонга\\b)'),
    ('🇹🇹 Тринідад і Тобаго', 'TT', '(?:\\.tt(?:/|:|\\?|$))|(?:trinidad|tobago)|(?:\\btrinidad\\ and\\ tobago\\b)|(?:\\bтринідад\\ і\\ тобаго\\b)'),
    ('🇹🇻 Тувалу', 'TV', '(?:\\btuvalu\\b)|(?:\\btuvalu\\b)|(?:\\bтувалу\\b)'),
    ('🇹🇳 Туніс', 'TN', '(?:\\.tn(?:/|:|\\?|$))|(?:\\btunisia\\b|tunis|туніс)|(?:\\btunisia\\b)|(?:\\bтуніс\\b)'),
    ('🇹🇷 Туреччина', 'TR', '(?:\\.tr(?:/|:|\\?|$))|(?:\\bturk|\\btürk\\b|istanbul|ankara|\\bkral\\b|powerturk|slowturk|туреччин)|(?:\\btürkiye\\b)|(?:\\bтуреччина\\b)'),
    ('🇹🇲 Туркменістан', 'TM', '(?:\\.tm(?:/|:|\\?|$))|(?:\\bturkmen\\b|ashgabat|туркмен)|(?:\\bturkmenistan\\b)|(?:\\bтуркменістан\\b)'),
    ('🇺🇬 Уганда', 'UG', '(?:\\.ug(?:/|:|\\?|$))|(?:\\buganda\\b|kampala|уганд)|(?:\\buganda\\b)|(?:\\bуганда\\b)'),
    ('🇭🇺 Угорщина', 'HU', '(?:\\.hu(?:/|:|\\?|$))|(?:\\bhungary|\\bmagyar\\b|budapest|угорщин)|(?:\\bhungary\\b)|(?:\\bугорщина\\b)'),
    ('🇺🇿 Узбекистан', 'UZ', '(?:\\.uz(?:/|:|\\?|$))|(?:\\buzbek\\b|tashkent|узбек)|(?:\\buzbekistan\\b)|(?:\\bузбекистан\\b)'),
    ('🇺🇾 Уругвай', 'UY', '(?:\\.uy(?:/|:|\\?|$))|(?:\\buruguay\\b|montevideo)|(?:\\buruguay\\b)|(?:\\bуругвай\\b)'),
    ('🇫🇴 Фарерські острови', 'FO', '(?:\\.fo(?:/|:|\\?|$))|(?:faroe islands)|(?:\\bthe\\ faroe\\ islands\\b)|(?:\\bфарерські\\ острови\\b)'),
    ('🇫🇰 Фолклендські острови', 'FK', '(?:\\.fk(?:/|:|\\?|$))|(?:falkland)|(?:\\bthe\\ falkland\\ islands\\ malvinas\\b)|(?:\\bфолклендські\\ острови\\b)'),
    ('🇬🇫 Французька Гвіана', 'GF', '(?:\\.gf(?:/|:|\\?|$))|(?:french guiana|guyane)|(?:\\bfrench\\ guiana\\b)|(?:\\bфранцузька\\ гвіана\\b)'),
    ('🇵🇫 Французька Полінезія', 'PF', '(?:\\.pf(?:/|:|\\?|$))|(?:french polynesia|tahiti)|(?:\\bfrench\\ polynesia\\b)|(?:\\bфранцузька\\ полінезія\\b)'),
    ('🇹🇫 Французькі Південні і Антарктичні Території', 'TF', '(?:\\.tf(?:/|:|\\?|$))|(?:french southern)|(?:\\bthe\\ french\\ southern\\ territories\\b)|(?:\\bфранцузькі\\ південні\\ і\\ антарктичні\\ території\\b)'),
    ('🇫🇷 Франція', 'FR', '(?:\\.fr(?:/|:|\\?|$))|(?:\\bfranc|paris|lyon|marseille|france inter|nrj france|rtl france|nostalgie|chérie fm|europe 1)|(?:\\bfrance\\b)|(?:\\bфранція\\b)'),
    ('🇫🇯 Фіджі', 'FJ', '(?:\\.fj(?:/|:|\\?|$))|(?:\\bfiji\\b|\\bsuva\\b)|(?:\\bfiji\\b)|(?:\\bфіджі\\b)'),
    ('🇵🇭 Філіппіни', 'PH', '(?:\\.ph(?:/|:|\\?|$))|(?:\\bphilippines\\b|manila|філіппін)|(?:\\bthe\\ philippines\\b)|(?:\\bфіліппіни\\b)'),
    ('🇫🇮 Фінляндія', 'FI', '(?:\\.fi(?:/|:|\\?|$))|(?:\\bfinland|\\bsuomi\\b|helsinki)|(?:\\bfinland\\b)|(?:\\bфінляндія\\b)'),
    ('🇭🇷 Хорватія', 'HR', '(?:\\.hr(?:/|:|\\?|$))|(?:\\bcroatia|\\bhrvatsk|\\bzagreb\\b|хорват)|(?:\\bcroatia\\b)|(?:\\bхорватія\\b)'),
    ('🇨🇫 ЦАР', 'CF', '(?:central african republic)|(?:\\bthe\\ central\\ african\\ republic\\b)|(?:\\bцар\\b)'),
    ('🇹🇩 Чад', 'TD', '(?:\\btchad\\b|\\bchad\\b)|(?:\\bchad\\b)|(?:\\bчад\\b)'),
    ('🇨🇿 Чехія', 'CZ', '(?:\\.cz(?:/|:|\\?|$))|(?:\\bczech|česk|praha|prague|brno|evropa 2|radiozurnal|impulz|frekvence 1)|(?:\\bczechia\\b)|(?:\\bчехія\\b)'),
    ('🇨🇱 Чилі', 'CL', '(?:\\.cl(?:/|:|\\?|$))|(?:\\bchile\\b|santiago)|(?:\\bchile\\b)|(?:\\bчилі\\b)'),
    ('🇲🇪 Чорногорія', 'ME', '(?:\\bmontenegro|\\bcrna gora\\b|podgorica|чорногор)|(?:\\bmontenegro\\b)|(?:\\bчорногорія\\b)'),
    ('🇨🇭 Швейцарія', 'CH', '(?:\\bswitz|\\bschweiz\\b|\\bsuisse\\b|zurich|zürich|geneva|genève|\\b(?:srf|rts)\\b|швейцар)|(?:\\bswitzerland\\b)|(?:\\bшвейцарія\\b)'),
    ('🇸🇪 Швеція', 'SE', '(?:\\.se(?:/|:|\\?|$))|(?:\\bsweden|\\bsverige\\b|stockholm|швеці)|(?:\\bsweden\\b)|(?:\\bшвеція\\b)'),
    ('🇱🇰 Шрі-Ланка', 'LK', '(?:\\.lk(?:/|:|\\?|$))|(?:sri lanka|colombo)|(?:\\bsri\\ lanka\\b)|(?:\\bшрі\\-ланка\\b)'),
    ('🇯🇲 Ямайка', 'JM', '(?:\\.jm(?:/|:|\\?|$))|(?:\\bjamaica|\\bkingston\\b|ямайк)|(?:\\bjamaica\\b)|(?:\\bямайка\\b)'),
    ('🇯🇵 Японія', 'JP', '(?:\\.jp(?:/|:|\\?|$))|(?:\\bjapan|\\btokyo\\b|японі)|(?:\\bjapan\\b)|(?:\\bяпонія\\b)'),
]

# Жанрові категорії (Назва чіпа, Регулярний вираз)
GENRE_DEFINITIONS = [
    ("Усі жанри", ""),
    ("✨ Нова вкладка", "__NEW__"),
    ("🎸 Rock", r"rock|рок|metal|метал|punk|панк|grunge|гранж"),
    ("🎤 Pop", r"\bpop\b|поп|kiss\s*fm|virgin|europa\s*fm|pro\s*fm|music\s*fm|fun\s*fm|nrj|люкс"),
    ("🎧 Electro / Dance", r"dance|electro|techno|trance|house|edm|club|dj|party|rave|дискотек|mix"),
    ("🌟 Hits / Top 40", r"\bhits?\b|хіти?|\bhit\b|hot|шлягер|best|top\s*\d+|charts?|чарт"),
    ("🪗 Folk / Ethnic", r"folk|folclor|фольк|етно|ethno|народн|традиц|lautar|petrecere|traditional|zene"),
    ("🎷 Jazz / Blues", r"jazz|blues|джаз|блюз|swing|soul|funk"),
    ("🎻 Classical", r"classic|класик|orchestra|симфон|opera|опера|instrumental|інструмент"),
    ("📻 News / Talk", r"news|новини|радіо|talk|розмов|інформ|info|actualit|rfi|bbc|npr|gov"),
    ("🕺 80s / 90s Retro", r"80s|90s|70s|60s|50s|retro|retró|ретро|oldies|диско|gold|nostalg"),
    ("☕ Relax / Chill", r"chill|relax|lounge|ambient|спа|romantic|sunset|cozy|easy|спокійн"),
    ("🔥 Hip-Hop", r"hip\s*hop|rap|рэп|реп|r&b|urban"),
    ("❓ Інше / Різне", "__OTHER__")
]

_ALL_STANDARD_GENRES_PATTERN = re.compile("|".join(
    pat for _, pat in GENRE_DEFINITIONS if pat and pat not in ("__OTHER__", "__NEW__")
), re.IGNORECASE)


EXCLUDED_COUNTRY_TLD = {"FM", "AM", "TV", "IO", "CO", "ST", "CC", "WS", "ME", "TO", "LA", "IN", 
    "IS", "IT", "US", "AI", "SO", "NU", "AG", "DJ", "CD", "MD", "OM", "ER", 
    "SC", "MS", "SH", "GG", "JE", "IM", "BZ", "TK", "ML", "GA", "CF", "TD",
    "BE", "BY", "CH", "AT", "CY", "LI"
}

_VALID_COUNTRY_CODES = {code: pat for _, code, pat in COUNTRY_DEFINITIONS[2:]}

_COMPILED_COUNTRY_PATTERNS = [
    (code, re.compile(pat, re.IGNORECASE))
    for _, code, pat in COUNTRY_DEFINITIONS[2:]
    if pat and pat != "__NONE__"
]


def get_station_matched_country(station_data: dict) -> str:
    """
    Повертає знайдений ISO-код країни або порожній рядок, якщо країна не визначена.
    Результат кешується в station_data['_matched_country'] для миттєвої фільтрації тисяч станцій.
    """
    cached = station_data.get("_matched_country")
    if cached is not None:
        return cached

    # 1. Перевірка за явним ISO кодом країни
    st_code = str(station_data.get("countrycode") or station_data.get("country_code") or station_data.get("tvg-country") or "").strip().upper()
    if st_code == "UK":
        st_code = "GB"

    if st_code and st_code in _VALID_COUNTRY_CODES:
        station_data["_matched_country"] = st_code
        return st_code

    url_s = str(station_data.get("url") or "").lower()

    # 2. Пріоритет України
    if re.search(r'\.ua(?:/|:|\?|$)|ukr\.radio|z-polus', url_s):
        station_data["_matched_country"] = "UA"
        return "UA"

    # 3. Швидка перевірка за доменом верхнього рівня (ccTLD)
    tld_match = re.search(r'\.([a-z]{2})(?:/|:|\?|$)', url_s)
    if tld_match:
        cand = tld_match.group(1).upper()
        if cand not in EXCLUDED_COUNTRY_TLD and cand in _VALID_COUNTRY_CODES:
            station_data["_matched_country"] = cand
            return cand

    # 4. Перевірка за регекс-шаблонами в імені, описі, url та полі country
    blob = station_data.get("_search_blob")
    if blob is None:
        name_s = str(station_data.get("name") or "").lower()
        desc_s = str(station_data.get("description") or "").lower()
        id_s = str(station_data.get("id") or "").strip().lower()
        blob = f"{name_s} {desc_s} {url_s} #{id_s} {id_s}"
        station_data["_search_blob"] = blob

    country_field = str(station_data.get("country") or "").lower()
    check_str = f"{blob} {country_field} {st_code.lower()}"

    for code, cregex in _COMPILED_COUNTRY_PATTERNS:
        if cregex.search(check_str):
            station_data["_matched_country"] = code
            return code

    station_data["_matched_country"] = ""
    return ""


def match_station_country(station_data: dict, country_code: str, country_pattern: str) -> bool:
    """
    Перевіряє, чи станція відповідає заданій країні (за ISO-кодом або регекс-шаблоном).
    Якщо country_code і country_pattern порожні - повертає True (Усі країни).
    Якщо country_code == "__NONE__" - повертає True для станцій без визначеної країни (Не вказано).
    """
    if not country_code and not country_pattern:
        return True

    if country_code == "__NONE__":
        st_country = str(station_data.get("country") or "").strip().lower()
        if "не вказано" in st_country:
            return True
        return get_station_matched_country(station_data) == ""

    return get_station_matched_country(station_data) == country_code.upper()


class CountryButton(QPushButton):
    def __init__(self, label: str, code: str, pattern: str, count: int, checked: bool = False, parent=None):
        super().__init__(parent)
        self.code = code
        self.pattern = pattern
        self.label = label
        self.count = count

        self.setCheckable(True)
        self.setChecked(checked)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(54)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(8)

        self.lbl_name = QLabel(label)
        self.lbl_name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.lbl_count = QLabel(f"{count} ст.")
        self.lbl_count.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.lbl_count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(self.lbl_name, stretch=1)
        layout.addWidget(self.lbl_count, stretch=0)

        self._update_style()
        self.toggled.connect(lambda _: self._update_style())

    def _update_style(self):
        if self.isChecked():
            # Обрана країна світиться яскравим синім кольором
            self.setStyleSheet("""
                QPushButton {
                    background-color: #1f6feb;
                    border: 2px solid #58a6ff;
                    border-radius: 8px;
                }
                QPushButton:hover {
                    background-color: #388bfd;
                    border-color: #79c0ff;
                }
            """)
            self.lbl_name.setStyleSheet("font-size: 22px; font-weight: bold; color: #ffffff; background: transparent;")
            self.lbl_count.setStyleSheet("font-size: 18px; font-weight: bold; color: #d0e2ff; background: transparent;")
        else:
            # Не обрана країна - сіріший приглушений стиль
            self.setStyleSheet("""
                QPushButton {
                    background-color: #161b22;
                    border: 1px solid #30363d;
                    border-radius: 8px;
                }
                QPushButton:hover {
                    background-color: #21262d;
                    border-color: #58a6ff;
                }
            """)
            self.lbl_name.setStyleSheet("font-size: 22px; font-weight: bold; color: #8b949e; background: transparent;")
            self.lbl_count.setStyleSheet("font-size: 18px; font-weight: normal; color: #586069; background: transparent;")


class CountryMultiSelectDialog(QDialog):
    """
    Модальне вікно вибору країн у 5 стовпчиків без чекбоксів:
    шрифт удвічі більший (22px bold), вибрані країни світяться яскравим кольором,
    невибрані — темніші/сіріші.
    Країни з 0 станцій автоматично не відображаються.
    """
    def __init__(self, parent=None, country_items=None, selected_codes=None):
        super().__init__(parent)
        self.setWindowTitle("🌍 Вибір країн мовлення")
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            w = min(int(geom.width() * 0.88), 2400)
            h = min(int(geom.height() * 0.88), 1200)
            self.resize(max(w, 1500), max(h, 750))
        else:
            self.resize(1920, 1000)
        self.setMinimumSize(1280, 680)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog {
                background-color: #0d1117;
                color: #c9d1d9;
            }
        """)

        # country_items: [(label, code, pat, count), ...]
        self.country_items = country_items or []
        self.selected_codes = set(selected_codes or [])
        self.result_codes = set(self.selected_codes)
        self.buttons = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        # Верхня панель (Заголовок, Пошук, Кнопки Швидкого Вибору, Закрити)
        top_layout = QHBoxLayout()
        top_layout.setSpacing(12)

        title_lbl = QLabel("🌍 Оберіть країни:")
        title_lbl.setStyleSheet("font-size: 26px; font-weight: bold; color: #58a6ff;")
        top_layout.addWidget(title_lbl)

        self.search_box = QLineEdit()
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setPlaceholderText("🔍 Пошук країни за назвою або кодом...")
        self.search_box.setFixedHeight(44)
        self.search_box.setStyleSheet("""
            QLineEdit {
                background-color: #21262d;
                color: #ffffff;
                font-size: 20px;
                border: 1px solid #30363d;
                border-radius: 8px;
                padding: 4px 14px;
            }
            QLineEdit:focus {
                border-color: #58a6ff;
            }
        """)
        self.search_box.textChanged.connect(self._on_search_changed)
        top_layout.addWidget(self.search_box, stretch=1)

        btn_select_all = QPushButton("Обрати всі")
        btn_select_all.setFixedHeight(44)
        btn_select_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_select_all.setStyleSheet("""
            QPushButton {
                background-color: #21262d;
                color: #c9d1d9;
                font-size: 16px;
                font-weight: bold;
                border: 1px solid #30363d;
                border-radius: 8px;
                padding: 4px 14px;
            }
            QPushButton:hover {
                background-color: #30363d;
                color: #ffffff;
                border-color: #58a6ff;
            }
        """)
        btn_select_all.clicked.connect(self._select_all_visible)
        top_layout.addWidget(btn_select_all)

        btn_clear_all = QPushButton("Зняти всі")
        btn_clear_all.setFixedHeight(44)
        btn_clear_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear_all.setStyleSheet("""
            QPushButton {
                background-color: #21262d;
                color: #e06c75;
                font-size: 16px;
                font-weight: bold;
                border: 1px solid #30363d;
                border-radius: 8px;
                padding: 4px 14px;
            }
            QPushButton:hover {
                background-color: #30363d;
                border-color: #e06c75;
            }
        """)
        btn_clear_all.clicked.connect(self._clear_all)
        top_layout.addWidget(btn_clear_all)

        layout.addLayout(top_layout)

        # Область прокрутки для сітки У 5 СТОВПЧИКІВ
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("""
            QScrollArea {
                background-color: #161b22;
                border: 1px solid #30363d;
                border-radius: 10px;
            }
            QScrollBar:vertical {
                border: none;
                background-color: #161b22;
                width: 14px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #30363d;
                min-height: 30px;
                border-radius: 7px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #58a6ff;
            }
        """)

        self.grid_container = QWidget()
        self.grid_container.setStyleSheet("background-color: #161b22;")
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setContentsMargins(10, 10, 10, 10)
        self.grid_layout.setSpacing(6)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        for col in range(5):
            self.grid_layout.setColumnStretch(col, 1)

        self._build_items()
        self.scroll_area.setWidget(self.grid_container)
        layout.addWidget(self.scroll_area, stretch=1)

        # Нижня панель: Інфо рядок + Скасувати + Застосувати
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(12)

        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #c9d1d9;")
        bottom_layout.addWidget(self.summary_label)

        bottom_layout.addStretch()

        btn_cancel = QPushButton("Скасувати")
        btn_cancel.setFixedHeight(44)
        btn_cancel.setFixedWidth(130)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #21262d;
                color: #c9d1d9;
                font-size: 16px;
                font-weight: bold;
                border: 1px solid #30363d;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #30363d;
                color: #ffffff;
            }
        """)
        btn_cancel.clicked.connect(self.reject)
        bottom_layout.addWidget(btn_cancel)

        self.btn_apply = QPushButton("✓ Застосувати")
        self.btn_apply.setFixedHeight(44)
        self.btn_apply.setMinimumWidth(180)
        self.btn_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply.setStyleSheet("""
            QPushButton {
                background-color: #238636;
                color: #ffffff;
                font-size: 18px;
                font-weight: bold;
                border: none;
                border-radius: 8px;
                padding: 4px 18px;
            }
            QPushButton:hover {
                background-color: #2ea043;
            }
        """)
        self.btn_apply.clicked.connect(self._apply)
        bottom_layout.addWidget(self.btn_apply)

        layout.addLayout(bottom_layout)

        self._update_summary()

    def _build_items(self):
        self.buttons = []
        for label, code, pat, count in self.country_items:
            is_chk = code in self.selected_codes
            btn = CountryButton(label, code, pat, count, checked=is_chk, parent=self.grid_container)
            btn.toggled.connect(self._on_item_toggled)
            self.buttons.append(btn)

        self._relayout_items()

    def _relayout_items(self):
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)

        visible_buttons = [b for b in self.buttons if not b.isHidden()]
        for idx, btn in enumerate(visible_buttons):
            row = idx // 5
            col = idx % 5
            self.grid_layout.addWidget(btn, row, col)

    def _on_search_changed(self, text):
        query = text.lower().strip()
        for btn in self.buttons:
            match = (not query) or (query in btn.label.lower()) or (query in btn.code.lower())
            btn.setVisible(match)
        self._relayout_items()

    def _on_item_toggled(self, checked):
        self._update_summary()

    def _select_all_visible(self):
        for btn in self.buttons:
            if not btn.isHidden():
                btn.setChecked(True)
        self._update_summary()

    def _clear_all(self):
        for btn in self.buttons:
            btn.setChecked(False)
        self._update_summary()

    def _update_summary(self):
        checked_buttons = [b for b in self.buttons if b.isChecked()]
        total_st = sum(b.count for b in checked_buttons)
        if len(checked_buttons) == 0:
            self.summary_label.setText("Усі країни мовлення (фільтр вимкнено)")
            self.btn_apply.setText("✓ Застосувати (всі)")
        else:
            self.summary_label.setText(f"Обрано країн: {len(checked_buttons)} | Станцій: {total_st}")
            self.btn_apply.setText(f"✓ Застосувати ({len(checked_buttons)})")

    def _apply(self):
        checked_buttons = [b for b in self.buttons if b.isChecked()]
        if len(checked_buttons) == 0 or len(checked_buttons) == len(self.buttons):
            self.result_codes = set()
        else:
            self.result_codes = {b.code for b in checked_buttons}
        self.accept()


def match_station_genre(station_data: dict, genre_pattern: str) -> bool:
    """
    Перевіряє, чи станція відповідає заданому жанровому шаблону.
    Якщо genre_pattern порожній - повертає True.
    Якщо genre_pattern == '__NEW__' - повертає True для нових станцій (is_imported_new / is_new).
    Якщо genre_pattern == '__OTHER__' - повертає True для станцій, які не підпадають під жоден стандартний жанр.
    """
    if not genre_pattern:
        return True
    if genre_pattern == "__NEW__":
        return bool(station_data.get("_is_imported_new") or station_data.get("is_new"))
    blob = station_data.get("_search_blob")
    if blob is None:
        name_s = str(station_data.get("name") or "").lower()
        desc_s = str(station_data.get("description") or "").lower()
        url_s = str(station_data.get("url") or "").lower()
        id_s = str(station_data.get("id") or "").strip().lower()
        blob = f"{name_s} {desc_s} {url_s} #{id_s} {id_s}"
        station_data["_search_blob"] = blob
    genre_field = str(station_data.get("genre") or "").lower()
    tags_field = str(station_data.get("tags") or "").lower()
    group_field = str(station_data.get("group") or "").lower()
    check_str = f"{blob} {genre_field} {tags_field} {group_field}"

    if genre_pattern == "__OTHER__":
        return not bool(_ALL_STANDARD_GENRES_PATTERN.search(check_str))

    return bool(re.search(genre_pattern, check_str, re.IGNORECASE))


class ImportOptionsDialog(QDialog):
    """
    Діалогове вікно попереднього вибору фільтрів (Країна та Жанр) перед імпортом файлу M3U.
    """
    def __init__(self, filename: str, preselected_country_idx: int = 0, preselected_genre_pattern: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Налаштування імпорту M3U")
        self.setFixedSize(580, 410)
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1f22;
                border: 1px solid #3c3f41;
                border-radius: 12px;
            }
            QLabel {
                color: #e6edf3;
            }
            QComboBox {
                background-color: #2b2d30;
                color: #58a6ff;
                font-size: 15px;
                font-weight: bold;
                border: 1px solid #4e5157;
                border-radius: 8px;
                padding: 6px 12px;
            }
            QComboBox:hover {
                background-color: #35383d;
                border-color: #58a6ff;
                color: #ffffff;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox QAbstractItemView {
                background-color: #1e1f22;
                color: #ffffff;
                selection-background-color: #1f6feb;
                selection-color: #ffffff;
                border: 1px solid #3c3f41;
                font-size: 14px;
                padding: 4px;
            }
            QPushButton {
                background-color: #2b2d30;
                color: #c9d1d9;
                font-size: 15px;
                font-weight: bold;
                border: 1px solid #4e5157;
                border-radius: 8px;
                padding: 10px 20px;
            }
            QPushButton:hover {
                background-color: #35383d;
                border-color: #8b949e;
                color: #ffffff;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # Заголовок
        title_label = QLabel("📥 Налаштування імпорту плейлиста")
        title_label.setStyleSheet("color: #58a6ff; font-size: 20px; font-weight: bold;")
        layout.addWidget(title_label)

        # Інформація про файл
        trunc_fn = filename if len(filename) <= 45 else filename[:42] + "..."
        file_info = QLabel(f"📄 Файл: <b>{trunc_fn}</b>")
        file_info.setStyleSheet("color: #8b949e; font-size: 14px;")
        layout.addWidget(file_info)

        # Розділювач
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #30363d; background-color: #30363d; height: 1px;")
        layout.addWidget(line)

        # Вибір країни
        country_lbl = QLabel("🌍 Країна для імпорту:")
        country_lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #c9d1d9;")
        layout.addWidget(country_lbl)

        self.country_combo = QComboBox()
        for label, code, pat in COUNTRY_DEFINITIONS:
            self.country_combo.addItem(label, (code, pat))
        if 0 <= preselected_country_idx < self.country_combo.count():
            self.country_combo.setCurrentIndex(preselected_country_idx)
        layout.addWidget(self.country_combo)

        # Вибір жанру
        genre_lbl = QLabel("🎵 Жанр для імпорту:")
        genre_lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #c9d1d9;")
        layout.addWidget(genre_lbl)

        self.genre_combo = QComboBox()
        selected_g_idx = 0
        for idx, (label, pat) in enumerate(GENRE_DEFINITIONS):
            self.genre_combo.addItem(label, pat)
            if preselected_genre_pattern and pat == preselected_genre_pattern:
                selected_g_idx = idx
        self.genre_combo.setCurrentIndex(selected_g_idx)
        layout.addWidget(self.genre_combo)

        # Кнопки дії
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        self.cancel_btn = QPushButton("Скасувати")
        self.cancel_btn.setFixedSize(120, 38)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #30363d;
                color: #c9d1d9;
                font-size: 15px;
                font-weight: bold;
                border: 1px solid #4e5157;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #3c444d;
                color: #ffffff;
            }
        """)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.start_btn = QPushButton("Почати імпорт")
        self.start_btn.setFixedSize(140, 38)
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.setStyleSheet("""
            QPushButton {
                background-color: #238636;
                color: #ffffff;
                font-size: 15px;
                font-weight: bold;
                border: 1px solid #2ea043;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #2ea043;
            }
        """)
        self.start_btn.clicked.connect(self.accept)
        self.start_btn.setDefault(True)
        btn_layout.addWidget(self.start_btn)

        layout.addLayout(btn_layout)

    def get_selected_filters(self):
        country_code, country_pat = self.country_combo.currentData()
        country_label = self.country_combo.currentText()
        genre_pat = self.genre_combo.currentData()
        genre_label = self.genre_combo.currentText()
        country_idx = self.country_combo.currentIndex()
        return {
            "country_code": country_code,
            "country_pattern": country_pat,
            "country_label": country_label,
            "country_index": country_idx,
            "genre_pattern": genre_pat,
            "genre_label": genre_label
        }


class EditStationDialog(QDialog):
    """
    Діалогове вікно редагування параметрів та тегів радіостанції:
    Назва, адреса потоку (URL), країна (випадаючий список з прапорцями),
    жанр, опис, теги та логотип.
    """
    def __init__(self, station_data: dict, parent=None):
        super().__init__(parent)
        self.station_data = station_data
        st_id = station_data.get("id", 0)
        st_name = str(station_data.get("name") or "Без назви")
        self.setWindowTitle(f"Налаштування станції #{st_id} — {st_name}")
        self.setFixedSize(680, 620)
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1f22;
                border: 1px solid #3c3f41;
                border-radius: 12px;
            }
            QLabel {
                color: #e6edf3;
                font-size: 16px;
                font-weight: bold;
            }
            QLineEdit {
                background-color: #2b2d30;
                color: #ffffff;
                border: 1.5px solid #3c3f41;
                border-radius: 8px;
                padding: 8px 12px;
                font-size: 16px;
                font-weight: normal;
            }
            QLineEdit:focus {
                border: 1.5px solid #0057b8;
            }
            QComboBox {
                background-color: #2b2d30;
                color: #58a6ff;
                font-size: 16px;
                font-weight: bold;
                border: 1.5px solid #3c3f41;
                border-radius: 8px;
                padding: 6px 12px;
            }
            QComboBox:hover {
                background-color: #35383d;
                border-color: #58a6ff;
                color: #ffffff;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox QAbstractItemView {
                background-color: #1e1f22;
                color: #ffffff;
                selection-background-color: #1f6feb;
                selection-color: #ffffff;
                border: 1px solid #3c3f41;
                font-size: 15px;
                padding: 4px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 20, 26, 20)
        layout.setSpacing(12)

        # Заголовок
        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)
        header = QLabel(f"⚙️ Налаштування радіостанції #{st_id}")
        header.setStyleSheet("color: #ffd700; font-size: 24px; font-weight: bold;")
        sub_header = QLabel("Змініть назву, адресу потоку, країну та жанрові теги станції")
        sub_header.setStyleSheet("color: #8b949e; font-size: 15px; font-weight: normal;")
        header_layout.addWidget(header)
        header_layout.addWidget(sub_header)
        layout.addLayout(header_layout)

        # 1. Назва станції
        layout.addWidget(QLabel("Назва радіостанції *: "))
        self.name_edit = QLineEdit(st_name)
        self.name_edit.setPlaceholderText("Введіть назву станції...")
        layout.addWidget(self.name_edit)

        # 2. URL потоку
        layout.addWidget(QLabel("Адреса потоку (URL) *: "))
        self.url_edit = QLineEdit(str(station_data.get("url") or ""))
        self.url_edit.setPlaceholderText("https://... або http://...")
        layout.addWidget(self.url_edit)

        # Двоколонковий блок для Країни та Жанру
        row_layout = QHBoxLayout()
        row_layout.setSpacing(14)

        # 3. Країна (випадаючий список з прапорцями)
        country_vbox = QVBoxLayout()
        country_vbox.setSpacing(4)
        country_vbox.addWidget(QLabel("🌍 Країна:"))
        self.country_combo = QComboBox()
        self.country_combo.addItem("🏳️ Не вказано / Інша країна", ("", ""))

        target_country_code = str(station_data.get("countrycode") or station_data.get("country_code") or "").strip().upper()
        selected_c_idx = 0

        for idx, (label, code, pat) in enumerate(COUNTRY_DEFINITIONS[1:], start=1):
            self.country_combo.addItem(label, (code, pat))
            if target_country_code and code == target_country_code:
                selected_c_idx = idx
            elif selected_c_idx == 0 and match_station_country(station_data, code, pat):
                selected_c_idx = idx

        self.country_combo.setCurrentIndex(selected_c_idx)
        country_vbox.addWidget(self.country_combo)
        row_layout.addLayout(country_vbox, stretch=1)

        # 4. Жанр (список з можливістю редагування)
        genre_vbox = QVBoxLayout()
        genre_vbox.setSpacing(4)
        genre_vbox.addWidget(QLabel("🎵 Жанр:"))
        self.genre_combo = QComboBox()
        self.genre_combo.setEditable(True)
        current_genre = str(station_data.get("genre") or "").strip()
        selected_g_idx = -1
        for idx, (label, pat) in enumerate(GENRE_DEFINITIONS[1:]):
            clean_label = label.split(" ", 1)[-1] if " " in label else label
            self.genre_combo.addItem(clean_label)
            if current_genre and (clean_label.lower() in current_genre.lower() or (pat and re.search(pat, current_genre, re.IGNORECASE))):
                if selected_g_idx == -1:
                    selected_g_idx = idx

        if current_genre:
            if selected_g_idx != -1:
                self.genre_combo.setCurrentIndex(selected_g_idx)
            else:
                self.genre_combo.setEditText(current_genre)
        else:
            self.genre_combo.setCurrentIndex(0)

        genre_vbox.addWidget(self.genre_combo)
        row_layout.addLayout(genre_vbox, stretch=1)
        layout.addLayout(row_layout)

        # 5. Опис станції
        layout.addWidget(QLabel("Опис станції:"))
        self.desc_edit = QLineEdit(str(station_data.get("description") or ""))
        self.desc_edit.setPlaceholderText("Короткий опис, слоган або частота (FM)...")
        layout.addWidget(self.desc_edit)

        # 6. Теги станції
        layout.addWidget(QLabel("Теги (через кому):"))
        self.tags_edit = QLineEdit(str(station_data.get("tags") or ""))
        self.tags_edit.setPlaceholderText("rock, news, ukrainian, hit...")
        layout.addWidget(self.tags_edit)

        # 7. Логотип
        layout.addWidget(QLabel("Шлях до логотипу або URL:"))
        self.logo_edit = QLineEdit(str(station_data.get("logo") or ""))
        self.logo_edit.setPlaceholderText("logos/... або https://...")
        layout.addWidget(self.logo_edit)

        layout.addStretch()

        # Кнопки
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        self.btn_cancel = QPushButton("Скасувати")
        self.btn_cancel.setFixedHeight(46)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #2b2d30;
                color: #c9d1d9;
                font-size: 16px;
                font-weight: bold;
                border: 1px solid #3c3f41;
                border-radius: 8px;
                padding: 0 22px;
            }
            QPushButton:hover {
                background-color: #35383c;
                border-color: #58a6ff;
            }
        """)
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_save = QPushButton("💾 Зберегти зміни")
        self.btn_save.setFixedHeight(46)
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.setStyleSheet("""
            QPushButton {
                background-color: #238636;
                color: #ffffff;
                font-size: 16px;
                font-weight: bold;
                border: none;
                border-radius: 8px;
                padding: 0 26px;
            }
            QPushButton:hover {
                background-color: #2ea043;
            }
            QPushButton:pressed {
                background-color: #1a6327;
            }
        """)
        self.btn_save.clicked.connect(self._on_save)
        btn_layout.addWidget(self.btn_save)

        layout.addLayout(btn_layout)

    def _on_save(self):
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        if not name:
            StyledMessageBox.warning(self, "Помилка валідації", "Будь ласка, введіть назву радіостанції.")
            self.name_edit.setFocus()
            return
        if not url:
            StyledMessageBox.warning(self, "Помилка валідації", "Будь ласка, введіть адресу потоку (URL).")
            self.url_edit.setFocus()
            return
        self.accept()

    def get_data(self):
        country_code, _ = self.country_combo.currentData()
        country_name = self.country_combo.currentText()
        if country_code:
            clean_country = country_name.split(" ", 1)[-1] if " " in country_name else country_name
        else:
            clean_country = ""

        genre = self.genre_combo.currentText().strip()
        return {
            "name": self.name_edit.text().strip(),
            "url": self.url_edit.text().strip(),
            "country": clean_country,
            "countrycode": country_code,
            "genre": genre,
            "description": self.desc_edit.text().strip(),
            "tags": self.tags_edit.text().strip(),
            "logo": self.logo_edit.text().strip()
        }

# Сумісність для створення нової станції через EditStationDialog
AddStationDialog = EditStationDialog


class FilterBarContainer(QWidget):
    """
    Панель керування та фільтрів:
    - Зліва: Пошук станції + кнопка оновлення
    - По центру: Всі кнопки стилів музики (без повзунка/скролу), строго відцентровані
      відносно центру вікна (X = total_width / 2) не враховуючи інші компоненти в рядку
    - Справа: Випадаючий список країн + індикатор знайдених станцій
    """
    def __init__(self, left_w, center_w, right_w, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setFixedHeight(42)
        self.left_w = left_w
        self.center_w = center_w
        self.right_w = right_w
        self.left_w.setParent(self)
        self.center_w.setParent(self)
        self.right_w.setParent(self)

    def update_positions(self):
        total_w = self.width()
        if total_w <= 0:
            return
        h = self.height()

        cw = self.center_w.sizeHint().width()
        ch = 40
        lw_pref = min(320, self.left_w.sizeHint().width())
        rw_pref = self.right_w.sizeHint().width()

        # Ідеальний центр вікна
        ideal_cx = (total_w - cw) // 2

        # Забезпечуємо, щоб центр не заходив на лівий або правий блоки
        min_cx = lw_pref + 8
        max_cx = total_w - rw_pref - cw - 8

        if max_cx >= min_cx:
            cx = max(min_cx, min(ideal_cx, max_cx))
            lw = min(lw_pref, cx - 8)
            rw = min(rw_pref, total_w - (cx + cw) - 8)
        else:
            # Якщо вікно надто вузьке
            lw = max(110, (total_w - cw) // 2 - 8)
            cx = lw + 8
            rw = max(110, total_w - (cx + cw) - 8)

        rx = total_w - rw
        lh = 40
        ly = (h - lh) // 2
        rh = 40
        ry = (h - rh) // 2
        cy = (h - ch) // 2

        self.left_w.setGeometry(0, ly, lw, lh)
        self.center_w.setGeometry(cx, cy, cw, ch)
        self.right_w.setGeometry(rx, ry, rw, rh)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_positions()


class RadioManagerWindow(QMainWindow):
    CHUNK_SIZE = 60

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Менеджер радіостанцій (RadioJS)")
        self.resize(1720, 960)
        self.setMinimumSize(1200, 700)

        # Audio Player
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.85)

        self.player.errorOccurred.connect(self._on_player_error)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.tracksChanged.connect(self._on_tracks_changed)
        self.player.metaDataChanged.connect(self._on_metadata_changed)

        # Перехоплювач помилок stderr (404, неможливість резолву хоста) та метаданих потоку
        self.stderr_interceptor = FfmpegStderrInterceptor(self)
        self.stderr_interceptor.fatal_error_detected.connect(self._on_fatal_stream_error)
        self.stderr_interceptor.stream_info_detected.connect(self._on_stream_info_detected)
        self.stderr_interceptor.stream_title_detected.connect(self._on_stream_title_detected)

        # Таймер таймауту підключення/відкриття потоку (6 секунд)
        self.load_timeout_timer = QTimer(self)
        self.load_timeout_timer.setSingleShot(True)
        self.load_timeout_timer.setInterval(6000)
        self.load_timeout_timer.timeout.connect(self._on_load_timeout)

        # Таймер автоматичного переходу на наступну невідіграну станцію
        self.auto_advance_timer = QTimer(self)
        self.auto_advance_timer.setSingleShot(True)
        self.auto_advance_timer.setInterval(200)
        self.auto_advance_timer.timeout.connect(self._do_auto_advance)

        # Таймер автоматичного перемикання станцій через X секунд
        self.auto_switch_timer = QTimer(self)
        self.auto_switch_timer.setSingleShot(True)
        self.auto_switch_timer.timeout.connect(self._on_auto_switch_timeout)

        self.playback_start_time = None
        self.current_playing_widget = None
        self.pending_play_widget = None
        self.focused_widget = None
        self.focused_index = -1
        self.stations = []
        self.filtered_station_items = []
        self.station_widgets = []
        self.rendered_cards_count = 0
        self.CHUNK_SIZE = 60
        self.cards_per_batch = 60
        self.selected_genre_patterns = set()
        self.selected_genre_pattern = ""
        self.selected_country_code = ""
        self.selected_country_pattern = ""
        self.selected_track_title = ""

        self.init_ui()
        self._load_stations()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(12)

        # Top Bar: Stats Label (замість заголовку) & Now Playing Status & M3U Button & Volume
        top_layout = QHBoxLayout()
        self.stats_label = QLabel("Всього: 0 | Позначено до видалення: 0")
        self.stats_label.setFixedHeight(40)
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.stats_label.setStyleSheet("font-size: 20px; font-weight: 800; color: #ffd700;")
        top_layout.addWidget(self.stats_label)

        top_layout.addStretch(1)

        # Now Playing info box (по центру шапки: 1 рядок: назва станції + бітрейт; 2 рядок: назва пісні + кнопка копіювання)
        now_playing_box = QVBoxLayout()
        now_playing_box.setSpacing(4)
        now_playing_box.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 1-й рядок: Назва станції та якість / бітрейт
        station_info_layout = QHBoxLayout()
        station_info_layout.setSpacing(10)
        station_info_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        station_info_layout.addStretch(1)

        self.status_label = QLabel("⏹ Нічого не відтворюється")
        self.status_label.setStyleSheet("color: #ffffff; font-size: 24px; font-weight: 800;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.status_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.status_label.setToolTip("Виділіть мишкою для копіювання | Права кнопка миші — меню")
        self.status_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.status_label.customContextMenuRequested.connect(self._on_status_label_context_menu)
        station_info_layout.addWidget(self.status_label)

        station_info_layout.addStretch(1)
        now_playing_box.addLayout(station_info_layout)

        # 2-й рядок: Назва поточної пісні та кнопка копіювання
        sub_info_layout = QHBoxLayout()
        sub_info_layout.setSpacing(8)
        sub_info_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_info_layout.addStretch(1)

        self.track_title_label = QLabel("")
        self.track_title_label.setStyleSheet("color: #39ff14; font-size: 24px; font-weight: 800;")
        self.track_title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.track_title_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.track_title_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.track_title_label.setToolTip("Виділіть мишкою для копіювання | Права кнопка миші — копіювати назву пісні")
        self.track_title_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.track_title_label.customContextMenuRequested.connect(self._on_track_title_context_menu)
        sub_info_layout.addWidget(self.track_title_label)

        self.btn_copy_track = QPushButton("📋 Копіювати")
        self.btn_copy_track.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_copy_track.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_track.setFixedHeight(30)
        self.btn_copy_track.setStyleSheet("""
            QPushButton {
                background-color: #21262d;
                color: #58a6ff;
                font-size: 14px;
                font-weight: bold;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 2px 10px;
            }
            QPushButton:hover {
                background-color: #30363d;
                border-color: #58a6ff;
                color: #ffffff;
            }
        """)
        self.btn_copy_track.clicked.connect(self._copy_track_title)
        self.btn_copy_track.setEnabled(False)
        self.btn_copy_track.setVisible(False)
        sub_info_layout.addWidget(self.btn_copy_track)

        sub_info_layout.addStretch(1)
        now_playing_box.addLayout(sub_info_layout)

        top_layout.addLayout(now_playing_box, stretch=1)
        top_layout.addStretch(1)

        # Кнопка додавання M3U (перенесена в правий верхній кут)
        self.btn_import_m3u = QPushButton("➕ Додати з M3U")
        self.btn_import_m3u.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_import_m3u.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_import_m3u.setFixedHeight(40)
        self.btn_import_m3u.setStyleSheet("""
            QPushButton {
                background-color: #238636;
                color: #ffffff;
                font-size: 15px;
                font-weight: bold;
                border: 1px solid #2ea043;
                border-radius: 8px;
                padding: 4px 14px;
            }
            QPushButton:hover {
                background-color: #2ea043;
            }
        """)
        self.btn_import_m3u.clicked.connect(self._import_m3u_file)
        top_layout.addWidget(self.btn_import_m3u)

        top_layout.addSpacing(14)

        # Автоперехід на наступну невідіграну станцію
        auto_switch_layout = QHBoxLayout()
        auto_switch_layout.setSpacing(6)

        self.auto_switch_cb = QCheckBox("⏱ Автоперехід")
        self.auto_switch_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.auto_switch_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_switch_cb.setToolTip("Автоматично перемикати на наступну невідіграну станцію через X секунд")
        self.auto_switch_cb.setStyleSheet("""
            QCheckBox {
                color: #8b949e;
                font-size: 15px;
                font-weight: bold;
                spacing: 6px;
            }
            QCheckBox:hover {
                color: #ffffff;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid #30363d;
                background-color: #21262d;
            }
            QCheckBox::indicator:checked {
                background-color: #1f6feb;
                border-color: #58a6ff;
            }
        """)
        self.auto_switch_cb.toggled.connect(self._on_auto_switch_toggled)
        auto_switch_layout.addWidget(self.auto_switch_cb)

        self.auto_switch_combo = QComboBox()
        self.auto_switch_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.auto_switch_combo.setToolTip("Час відтворення станції перед автоматичним переходом")
        self.auto_switch_combo.addItems(["3 сек", "5 сек", "10 сек", "15 сек", "30 сек", "60 сек"])
        self.auto_switch_combo.setCurrentText("3 сек")
        self.auto_switch_combo.setStyleSheet("""
            QComboBox {
                background-color: #21262d;
                color: #8b949e;
                font-size: 14px;
                font-weight: bold;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 2px 8px;
            }
            QComboBox:hover {
                color: #ffffff;
                border-color: #58a6ff;
            }
            QComboBox::drop-down {
                border: none;
                width: 14px;
            }
            QComboBox QAbstractItemView {
                background-color: #1e1f22;
                color: #ffffff;
                selection-background-color: #1f6feb;
                selection-color: #ffffff;
                border: 1px solid #3c3f41;
                font-size: 14px;
            }
        """)
        self.auto_switch_combo.currentIndexChanged.connect(self._on_auto_switch_interval_changed)
        auto_switch_layout.addWidget(self.auto_switch_combo)

        top_layout.addLayout(auto_switch_layout)

        main_layout.addLayout(top_layout)

        # 1. Лівий блок: Поле пошуку
        self.search_left_widget = QWidget()
        self.search_left_widget.setStyleSheet("background: transparent;")
        search_left_layout = QHBoxLayout(self.search_left_widget)
        search_left_layout.setContentsMargins(0, 0, 0, 0)
        search_left_layout.setSpacing(6)

        # Поле пошуку (вбудована кнопка очищення setClearButtonEnabled(True))
        self.search_input = QLineEdit()
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText("🔍 Пошук станції (#1, назва, URL, жанр)... [Tab]")
        self.search_input.setFixedHeight(40)
        self.search_input.setMinimumWidth(140)
        self.search_input.setMaximumWidth(420)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: #21262d;
                color: #ffffff;
                font-size: 17px;
                border: 1px solid #30363d;
                border-radius: 8px;
                padding: 4px 12px;
            }
            QLineEdit:focus {
                border: 2px solid #58a6ff;
                background-color: #161b22;
            }
        """)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(lambda: self._filter_stations(self.search_input.text()))
        self.search_input.textChanged.connect(self._on_search_text_changed)
        search_left_layout.addWidget(self.search_input)

        # 2. Центральний блок: Всі кнопки стилів музики (без повзунка/скролу)
        self.genre_bar = QWidget()
        self.genre_bar.setStyleSheet("background: transparent;")
        self.genre_chips_layout = QHBoxLayout(self.genre_bar)
        self.genre_chips_layout.setContentsMargins(0, 0, 0, 0)
        self.genre_chips_layout.setSpacing(4)
        self.genre_chips_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        short_names = {
            "Усі жанри": "Усі",
            "✨ Нова вкладка": "✨ Нова вкладка",
            "🎧 Electro / Dance": "🎧 Dance",
            "🌟 Hits / Top 40": "🌟 Hits",
            "🪗 Folk / Ethnic": "🪗 Folk",
            "🎷 Jazz / Blues": "🎷 Jazz",
            "🎻 Classical": "🎻 Classic",
            "📻 News / Talk": "📻 News",
            "🕺 80s / 90s Retro": "🕺 Retro",
            "☕ Relax / Chill": "☕ Relax",
            "❓ Інше / Різне": "❓ Інше"
        }

        self.genre_btn_group = []
        for idx, (title, tag) in enumerate(GENRE_DEFINITIONS):
            short_title = short_names.get(title, title)
            btn = QPushButton(short_title)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            if tag == "":
                btn.setChecked(True)
            btn.setProperty("genre_filter", tag)
            btn.setToolTip(f"{title} (клікніть для вибору/комбінації)")
            if tag == "__NEW__":
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #122216;
                        color: #3fb950;
                        font-size: 15px;
                        font-weight: 600;
                        border: 1px solid #238636;
                        border-radius: 10px;
                        padding: 3px 8px;
                    }
                    QPushButton:hover {
                        background-color: #238636;
                        border-color: #3fb950;
                        color: #ffffff;
                    }
                    QPushButton:checked {
                        background-color: #238636;
                        color: #ffffff;
                        border: 2px solid #3fb950;
                        font-weight: bold;
                    }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: #21262d;
                        color: #c9d1d9;
                        font-size: 15px;
                        font-weight: 600;
                        border: 1px solid #30363d;
                        border-radius: 10px;
                        padding: 3px 8px;
                    }
                    QPushButton:hover {
                        background-color: #30363d;
                        border-color: #8b949e;
                        color: #ffffff;
                    }
                    QPushButton:checked {
                        background-color: #1f6feb;
                        color: #ffffff;
                        border-color: #58a6ff;
                        font-weight: bold;
                    }
                """)
            btn.clicked.connect(lambda checked, b=btn: self._on_genre_chip_clicked(b))
            self.genre_chips_layout.addWidget(btn)
            self.genre_btn_group.append(btn)

        # 3. Правий блок: Кнопка вибору країн (модальне вікно) та Індикатор кількості знайдених станцій
        self.search_right_widget = QWidget()
        self.search_right_widget.setStyleSheet("background: transparent;")
        search_right_layout = QHBoxLayout(self.search_right_widget)
        search_right_layout.setContentsMargins(0, 0, 0, 0)
        search_right_layout.setSpacing(8)

        self.country_btn = QPushButton("🌍 Усі країни  ▼")
        self.country_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.country_btn.setToolTip("Обрати країни для фільтрації (можна обрати одну, дві або більше)")
        self.country_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.country_btn.setFixedHeight(40)
        self.country_btn.setMinimumWidth(130)
        self.country_btn.setMaximumWidth(280)
        self.country_btn.setStyleSheet("""
            QPushButton {
                background-color: #21262d;
                color: #58a6ff;
                font-size: 16px;
                font-weight: bold;
                border: 1px solid #30363d;
                border-radius: 8px;
                padding: 4px 12px;
                text-align: center;
            }
            QPushButton:hover {
                background-color: #30363d;
                border-color: #58a6ff;
                color: #ffffff;
            }
        """)
        self.country_btn.clicked.connect(self._open_country_multi_select_dialog)
        search_right_layout.addWidget(self.country_btn)

        # Індикатор кількості знайдених станцій
        self.country_count_badge = QLabel("0 ст.")
        self.country_count_badge.setFixedHeight(40)
        self.country_count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.country_count_badge.setToolTip("Кількість станцій за активними фільтрами")
        self.country_count_badge.setStyleSheet("""
            QLabel {
                background-color: #21262d;
                color: #58a6ff;
                font-size: 16px;
                font-weight: 800;
                border: 1px solid #30363d;
                border-radius: 8px;
                padding: 2px 10px;
            }
        """)
        search_right_layout.addWidget(self.country_count_badge)

        self.filter_bar_container = FilterBarContainer(
            self.search_left_widget,
            self.genre_bar,
            self.search_right_widget
        )
        main_layout.addWidget(self.filter_bar_container)

        # Cards Scroll Area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("""
            QScrollArea {
                background-color: #121316;
                border: 1px solid #282c34;
                border-radius: 10px;
            }
            QScrollBar:vertical {
                border: none;
                background-color: #161b22;
                width: 14px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #30363d;
                min-height: 30px;
                border-radius: 7px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #58a6ff;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                height: 0px;
            }
        """)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)

        self.grid_container = QWidget()
        self.grid_container.setStyleSheet("background-color: #18191c;")
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setContentsMargins(10, 10, 10, 10)
        self.grid_layout.setSpacing(10)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.grid_layout.setColumnStretch(0, 1)
        self.grid_layout.setColumnStretch(1, 1)
        self.grid_layout.setColumnStretch(2, 1)
        self.grid_layout.setColumnStretch(3, 1)

        self.scroll_area.setWidget(self.grid_container)
        main_layout.addWidget(self.scroll_area, stretch=1)

        # Bottom Bar: Info & OK button
        bottom_layout = QHBoxLayout()
        bottom_info = QLabel("💡 Навігація: ◀ ▲ ▼ ▶, Home/End, PgUp/PgDn | Tab: пошук/картки | Грати: Space/Enter | Позначити: Del/D | Додати: Insert")
        bottom_info.setStyleSheet("color: #8b949e; font-size: 21px;")
        bottom_layout.addWidget(bottom_info)

        bottom_layout.addStretch()

        self.btn_ok = QPushButton("ОК — Видалити позначені з JSON")
        self.btn_ok.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ok.setFixedHeight(56)
        self.btn_ok.setStyleSheet("""
            QPushButton {
                background-color: #2da44e;
                color: #ffffff;
                font-size: 26px;
                font-weight: bold;
                border-radius: 8px;
                padding: 0 28px;
                border: none;
            }
            QPushButton:hover {
                background-color: #238636;
            }
            QPushButton:pressed {
                background-color: #1a6327;
            }
        """)
        self.btn_ok.clicked.connect(self._apply_deletions)
        bottom_layout.addWidget(self.btn_ok)

        main_layout.addLayout(bottom_layout)

        # Keyboard event filtering for seamless navigation and Space/Enter playback
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.installEventFilter(self)
        self.scroll_area.installEventFilter(self)
        self.scroll_area.viewport().installEventFilter(self)
        self.grid_container.installEventFilter(self)
        self.search_input.installEventFilter(self)

        # Глобальний шорткат клавіші Insert для відкриття вікна додавання станції
        self.shortcut_insert = QShortcut(QKeySequence(Qt.Key.Key_Insert), self)
        self.shortcut_insert.setContext(Qt.ShortcutContext.WindowShortcut)
        self.shortcut_insert.activated.connect(self._open_add_station_dialog)

        # Глобальний шорткат клавіші F5 для швидкого оновлення списку станцій
        self.shortcut_f5 = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        self.shortcut_f5.setContext(Qt.ShortcutContext.WindowShortcut)
        self.shortcut_f5.activated.connect(self._load_stations)

    def _open_country_multi_select_dialog(self):
        items = getattr(self, "all_active_country_items", [])
        if not items:
            self._update_country_data()
            items = getattr(self, "all_active_country_items", [])

        current_codes = getattr(self, "selected_country_codes", set())
        dlg = CountryMultiSelectDialog(self, country_items=items, selected_codes=current_codes)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.selected_country_codes = set(dlg.result_codes)
            # Скидаємо жанрові кнопки на «Усі», щоб користувач одразу побачив станції обраних країн
            if hasattr(self, 'genre_btn_group') and self.genre_btn_group:
                for btn in self.genre_btn_group:
                    tag = btn.property("genre_filter") or ""
                    btn.setChecked(tag == "")
            self.selected_genre_patterns = set()
            self.selected_genre_pattern = ""

            self._update_country_button_label()
            search_query = self.search_input.text() if hasattr(self, 'search_input') else ""
            self._filter_stations(search_query, reset_scroll=True)
            self._save_app_state()

    def _update_country_button_label(self):
        if not hasattr(self, 'country_btn'):
            return
        codes = getattr(self, "selected_country_codes", set())
        items = getattr(self, "all_active_country_items", [])

        if not codes or (items and len(codes) >= len(items)):
            self.country_btn.setText("🌍 Усі країни  ▼")
            self.country_btn.setToolTip("Фільтр вимкнено: показано станції усіх країн")
            return

        item_map = {c: lbl for lbl, c, _, _ in items}
        selected_labels = [item_map[c] for c in codes if c in item_map]

        if len(selected_labels) == 1:
            lbl = selected_labels[0]
            self.country_btn.setText(f"{lbl}  ▼")
            self.country_btn.setToolTip(f"Фільтр країни: {lbl}")
        elif len(selected_labels) == 2:
            flags = []
            for l in selected_labels:
                parts = l.split(" ", 1)
                flags.append(parts[0] if len(parts) > 1 else l)
            self.country_btn.setText(f"{' '.join(flags)} (2 країни)  ▼")
            self.country_btn.setToolTip(f"Фільтр країн: {', '.join(selected_labels)}")
        else:
            flags = " ".join(l.split(" ", 1)[0] for l in selected_labels[:3])
            self.country_btn.setText(f"{flags} ({len(selected_labels)} країн)  ▼")
            self.country_btn.setToolTip(f"Фільтр країн: {', '.join(selected_labels)}")

    def _update_country_data(self, preselected_code=None):
        if not self.stations:
            return

        counts = {code: 0 for _, code, _ in COUNTRY_DEFINITIONS if code and code != "__NONE__"}
        none_count = 0
        for s in self.stations:
            matched = get_station_matched_country(s)
            if matched and matched in counts:
                counts[matched] += 1
            else:
                none_count += 1

        active_countries = []
        if none_count > 0:
            active_countries.append(("🏴 Не вказано", "__NONE__", "__NONE__", none_count))

        for label, code, pat in COUNTRY_DEFINITIONS[2:]:
            cnt = counts.get(code, 0)
            if cnt > 0:
                active_countries.append((label, code, pat, cnt))

        def sort_key(item):
            lbl, code, pat, cnt = item
            if code == "__NONE__":
                return (0, "")
            if code == "UA":
                return (1, "")
            clean = lbl.split(" ", 1)[-1] if " " in lbl else lbl
            return (2, clean)

        active_countries.sort(key=sort_key)
        self.all_active_country_items = active_countries

        if preselected_code is not None:
            if preselected_code:
                self.selected_country_codes = {preselected_code}
            else:
                self.selected_country_codes = set()

        self._update_country_button_label()

    def _update_country_combo(self, preselected_code=None):
        self._update_country_data(preselected_code=preselected_code)

    def _load_stations(self):
        logger.info(f"Завантаження станцій з файлу: {JSON_PATH}")
        loaded_data = None

        if not JSON_PATH.exists() or JSON_PATH.stat().st_size == 0:
            logger.warning(f"Головний файл {JSON_PATH} відсутній або порожній (0 байт)!")
            bak_path = JSON_PATH.with_suffix(".json.bak")
            if bak_path.exists() and bak_path.stat().st_size > 0:
                logger.info(f"Знайдено резервну копію {bak_path}, спроба відновлення...")
                try:
                    with open(bak_path, "r", encoding="utf-8") as f:
                        loaded_data = json.load(f)
                    logger.info(f"Успішно відновлено {len(loaded_data)} станцій з резервної копії {bak_path}")
                except Exception as be:
                    logger.error(f"Не вдалося прочитати резервну копію {bak_path}: {be}")

            if not loaded_data and ALT_JSON_PATH.exists() and ALT_JSON_PATH.stat().st_size > 0:
                logger.info(f"Спроба завантаження з альтернативного шляху {ALT_JSON_PATH}...")
                try:
                    with open(ALT_JSON_PATH, "r", encoding="utf-8") as f:
                        loaded_data = json.load(f)
                    logger.info(f"Успішно завантажено {len(loaded_data)} станцій з {ALT_JSON_PATH}")
                except Exception as ae:
                    logger.error(f"Не вдалося прочитати з {ALT_JSON_PATH}: {ae}")

            if not loaded_data:
                logger.error("Не вдалося знайти або відновити stations.json з жодного джерела!")
                StyledMessageBox.critical(self, "Помилка", f"Файл {JSON_PATH} не знайдено або він порожній!")
                self.stations = []
                self.populate_list()
                return
        else:
            try:
                with open(JSON_PATH, "r", encoding="utf-8") as f:
                    loaded_data = json.load(f)
            except Exception as je:
                logger.exception(f"Помилка парсингу JSON з {JSON_PATH}: {je}")
                # Спроба з резервної копії
                bak_path = JSON_PATH.with_suffix(".json.bak")
                if bak_path.exists() and bak_path.stat().st_size > 0:
                    try:
                        logger.info(f"Спроба прочитати резервну копію {bak_path} після помилки парсингу...")
                        with open(bak_path, "r", encoding="utf-8") as f:
                            loaded_data = json.load(f)
                        logger.info(f"Відновлено з резервної копії: {len(loaded_data)} станцій")
                    except Exception as be:
                        logger.error(f"Резервна копія також пошкоджена: {be}")

        if isinstance(loaded_data, list):
            self.stations = loaded_data
            logger.info(f"Успішно завантажено станцій: {len(self.stations)}")
            self._apply_saved_app_state()
            self._update_country_combo()
        else:
            logger.warning(f"Дані в JSON мають тип {type(loaded_data)}, очікувався list. Ініціалізація порожнім списком.")
            self.stations = []

        self.populate_list()

    def _apply_saved_app_state(self):
        """
        Відновлює збережений стан додатку:
        - прослухані станції (чорний фон)
        - нові станції (зелений фон)
        - збережені фільтри країн
        - збережені фільтри стилів / жанрів (зокрема «✨ Нова вкладка»)
        """
        state = load_app_state()
        if not state:
            for s in self.stations:
                if s.get("has_played"):
                    s["_has_played"] = True
                if s.get("is_new"):
                    s["_is_imported_new"] = True
            return

        played_urls = set(state.get("played_urls", []))
        new_urls = set(state.get("new_urls", []))

        for s in self.stations:
            url_norm = normalize_stream_url(str(s.get("url") or ""))
            if url_norm in played_urls or s.get("has_played") or s.get("_has_played"):
                s["_has_played"] = True
                s["has_played"] = True
                s["is_new"] = False
                s.pop("_is_imported_new", None)
            elif url_norm in new_urls or s.get("is_new") or s.get("_is_imported_new"):
                s["_is_imported_new"] = True
                s["is_new"] = True
                s["has_played"] = False
                s.pop("_has_played", None)

        saved_countries = state.get("selected_country_codes")
        if saved_countries is not None and isinstance(saved_countries, (list, set)):
            self.selected_country_codes = set(saved_countries)

        saved_genres = state.get("selected_genre_patterns")
        if saved_genres is not None and isinstance(saved_genres, (list, set)):
            self.selected_genre_patterns = set(saved_genres)
            if hasattr(self, 'genre_btn_group') and self.genre_btn_group:
                for btn in self.genre_btn_group:
                    tag = btn.property("genre_filter") or ""
                    if not self.selected_genre_patterns:
                        btn.setChecked(tag == "")
                    else:
                        btn.setChecked(tag in self.selected_genre_patterns)

        last_url = state.get("last_played_url")
        if last_url:
            self.last_played_url = last_url

    def _save_app_state(self):
        """
        Зберігає поточний стан додатку (прослухані/нові картки, фільтри країн, активні стилі).
        """
        try:
            played_urls = []
            new_urls = []
            for s in self.stations:
                url_norm = normalize_stream_url(str(s.get("url") or ""))
                if not url_norm:
                    continue
                if s.get("_has_played") or s.get("has_played"):
                    played_urls.append(url_norm)
                elif s.get("_is_imported_new") or s.get("is_new"):
                    new_urls.append(url_norm)

            last_url = ""
            if self.current_playing_widget and self.current_playing_widget.station_data:
                last_url = str(self.current_playing_widget.station_data.get("url") or "")
            elif hasattr(self, 'last_played_url'):
                last_url = self.last_played_url

            state = {
                "played_urls": played_urls,
                "new_urls": new_urls,
                "selected_country_codes": list(getattr(self, "selected_country_codes", set())),
                "selected_genre_patterns": list(getattr(self, "selected_genre_patterns", set())),
                "last_played_url": last_url
            }
            save_app_state(state)
        except Exception as e:
            logger.warning(f"Помилка при збереженні стану додатку: {e}")

    def _get_station_display_num(self, widget) -> str:
        if widget is None:
            return ""
        if hasattr(widget, 'display_num'):
            return str(widget.display_num)
        if hasattr(widget, 'display_number') and widget.display_number is not None:
            return str(widget.display_number)
        if hasattr(widget, 'station_data') and widget.station_data:
            return str(widget.station_data.get("id", ""))
        return ""

    def get_auto_switch_seconds(self) -> int:
        try:
            if hasattr(self, 'auto_switch_combo'):
                txt = self.auto_switch_combo.currentText()
                digits = re.findall(r'\d+', txt)
                if digits:
                    return int(digits[0])
        except Exception:
            pass
        return 5

    def _on_auto_switch_interval_changed(self, index):
        secs = self.get_auto_switch_seconds()
        logger.info(f"Змінено інтервал автопереходу на {secs} сек")
        if hasattr(self, 'auto_switch_cb') and self.auto_switch_cb.isChecked():
            if self.current_playing_widget and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                num = self._get_station_display_num(self.current_playing_widget)
                name = self.current_playing_widget.station_data.get("name", "")
                if hasattr(self, 'auto_switch_timer'):
                    self.auto_switch_timer.start(secs * 1000)
                if self.status_label.text().startswith("▶ Грає"):
                    self.status_label.setText(f"▶ Грає: #{num} {name} (автоперехід через {secs}с)")

    def _on_metadata_changed(self):
        try:
            meta = self.player.metaData()
            if not meta:
                return

            title = meta.value(QMediaMetaData.Key.Title)
            artist = (
                meta.value(QMediaMetaData.Key.ContributingArtist)
                or meta.value(QMediaMetaData.Key.AlbumArtist)
                or meta.value(QMediaMetaData.Key.Author)
            )
            track_str = ""
            if title and artist:
                track_str = f"{artist} — {title}".strip()
            elif title:
                track_str = str(title).strip()
            elif artist:
                track_str = str(artist).strip()

            if track_str and track_str.lower() not in ("none", "unknown", "n/a"):
                self._set_current_track_title(track_str)

            codec = meta.value(QMediaMetaData.Key.AudioCodec)
            bitrate = meta.value(QMediaMetaData.Key.AudioBitRate)
            codec_str = str(codec).strip() if codec else ""
            bitrate_str = f"{int(bitrate) // 1000} kbps" if bitrate and int(bitrate) > 0 else ""
            if codec_str or bitrate_str:
                self._set_current_stream_quality(codec_str, bitrate_str)
        except Exception as e:
            logger.debug(f"Помилка при зчитуванні QMediaMetaData: {e}")

    def _on_stream_info_detected(self, codec: str, bitrate: str):
        self._set_current_stream_quality(codec, bitrate)

    def _on_stream_title_detected(self, title: str):
        self._set_current_track_title(title)

    def _set_current_track_title(self, track: str):
        clean_track = track.strip() if track else ""
        if clean_track and clean_track.lower() in ("n/a", "none", "unknown", "streamtitle", "undefined", "null"):
            clean_track = ""
        self.current_track_title = clean_track

        if clean_track:
            self.track_title_label.setText(f"🎵 {clean_track}")
            self.track_title_label.setStyleSheet("color: #39ff14; font-size: 27px; font-weight: 800;")
            self.track_title_label.setVisible(True)
            if hasattr(self, 'btn_copy_track'):
                self.btn_copy_track.setEnabled(True)
                self.btn_copy_track.setVisible(True)
                self.btn_copy_track.setToolTip("Скопіювати назву пісні в буфер обміну")
        else:
            if self.current_playing_widget:
                self.track_title_label.setText("Назва відсутня")
                self.track_title_label.setStyleSheet("color: #8b949e; font-size: 22px; font-weight: normal;")
                self.track_title_label.setVisible(True)
                if hasattr(self, 'btn_copy_track'):
                    self.btn_copy_track.setEnabled(False)
                    self.btn_copy_track.setVisible(True)
                    self.btn_copy_track.setToolTip("Назва треку відсутня")
            else:
                self.track_title_label.setText("")
                self.track_title_label.setVisible(False)
                if hasattr(self, 'btn_copy_track'):
                    self.btn_copy_track.setEnabled(False)
                    self.btn_copy_track.setVisible(False)

        if self.current_playing_widget:
            try:
                self.current_playing_widget.set_track_title(clean_track)
            except Exception:
                pass

    def _set_current_stream_quality(self, codec: str, bitrate: str):
        if codec:
            self.current_stream_codec = codec
        if bitrate:
            self.current_stream_bitrate = bitrate
        if hasattr(self, 'quality_badge_top'):
            self.quality_badge_top.setVisible(False)

    def _reset_playback_metadata(self):
        self.current_track_title = ""
        self.current_stream_codec = ""
        self.current_stream_bitrate = ""
        if hasattr(self, 'quality_badge_top'):
            self.quality_badge_top.setText("")
            self.quality_badge_top.setVisible(False)
        self.track_title_label.setText("")
        self.track_title_label.setVisible(False)
        if hasattr(self, 'btn_copy_track'):
            self.btn_copy_track.setEnabled(False)
            self.btn_copy_track.setVisible(False)

    def _on_country_filter_changed(self, index):
        data = self.country_combo.currentData()
        if isinstance(data, (tuple, list)):
            code, pattern = data[0], data[1]
        else:
            code, pattern = "", data or ""

        # Уникаємо надлишкового оновлення, якщо значення не змінилося і жанри вже скинуті
        if (getattr(self, "selected_country_code", None) == code and 
            getattr(self, "selected_country_pattern", None) == pattern and
            not getattr(self, "selected_genre_patterns", None)):
            return

        # Скидаємо вибір жанрів на «Усі», щоб для нової країни одразу показувалися всі станції без натискання «Усі»
        if hasattr(self, 'genre_btn_group') and self.genre_btn_group:
            for btn in self.genre_btn_group:
                tag = btn.property("genre_filter") or ""
                btn.setChecked(tag == "")
        self.selected_genre_patterns = set()
        self.selected_genre_pattern = ""

        self.selected_country_code = code
        self.selected_country_pattern = pattern
        logger.info(f"Змінено фільтр країни: {self.country_combo.currentText()} (код: '{code}', шаблон: '{pattern}')")
        search_query = self.search_input.text() if hasattr(self, 'search_input') else ""
        self._filter_stations(search_query, reset_scroll=True)

    def _on_genre_chip_clicked(self, clicked_btn):
        clicked_tag = clicked_btn.property("genre_filter") or ""

        if clicked_tag == "":
            # Натиснуто кнопку «Усі» - вона завжди вмикається, а окремі жанри вимикаються
            clicked_btn.setChecked(True)
            for btn in self.genre_btn_group:
                if btn != clicked_btn:
                    btn.setChecked(False)
            self.selected_genre_patterns = set()
            self.selected_genre_pattern = ""
        else:
            # Натиснуто окремий жанр - підтримуємо мульти-вибір (можна обрати 1, 2 або довільну кількість)
            all_btn = self.genre_btn_group[0] if self.genre_btn_group else None

            # Збираємо всі обрані жанри
            active_patterns = set()
            for btn in self.genre_btn_group:
                tag = btn.property("genre_filter") or ""
                if tag != "" and btn.isChecked():
                    active_patterns.add(tag)

            if not active_patterns or len(active_patterns) == (len(self.genre_btn_group) - 1):
                # Якщо всі окремі жанри знято АБО обрано всі наявні жанрові кнопки одночасно:
                # Вмикаємо «Усі», а окремі кнопки знімаємо (це еквівалент вибору всіх станцій)
                if all_btn:
                    all_btn.setChecked(True)
                for btn in self.genre_btn_group:
                    if btn != all_btn:
                        btn.setChecked(False)
                self.selected_genre_patterns = set()
                self.selected_genre_pattern = ""
            else:
                # Вимикаємо «Усі», зберігаємо обрані жанри
                if all_btn:
                    all_btn.setChecked(False)
                self.selected_genre_patterns = active_patterns
                self.selected_genre_pattern = "|".join(f"(?:{p})" for p in sorted(active_patterns))

        # Миттєво автоматично оновлюємо фільтрацію
        search_query = self.search_input.text() if hasattr(self, 'search_input') else ""
        self._filter_stations(search_query, reset_scroll=True)
        self._save_app_state()

    def _on_scroll_value_changed(self, value):
        vsb = self.scroll_area.verticalScrollBar()
        if vsb.maximum() > 0 and value >= vsb.maximum() - 350:
            if self.rendered_cards_count < len(self.filtered_station_items):
                self.load_more_cards()

    def populate_list(self):
        search_query = self.search_input.text() if hasattr(self, 'search_input') else ""
        self._filter_stations(search_query, reset_scroll=True)

    def _filter_stations(self, text=None, reset_scroll=True):
        if text is None:
            text = self.search_input.text() if hasattr(self, 'search_input') else ""
        query = text.lower().strip()

        selected_codes = getattr(self, "selected_country_codes", set())
        all_items = getattr(self, "all_active_country_items", [])
        has_multi_country_filter = bool(selected_codes and (not all_items or len(selected_codes) < len(all_items)))

        country_code = getattr(self, "selected_country_code", "")
        country_pat = getattr(self, "selected_country_pattern", "")
        genre_pats = getattr(self, "selected_genre_patterns", set())
        if not genre_pats and getattr(self, "selected_genre_pattern", ""):
            genre_pats = {self.selected_genre_pattern}

        filtered = []
        for s in self.stations:
            blob = s.get("_search_blob")
            if blob is None:
                name_s = str(s.get("name") or "").lower()
                desc_s = str(s.get("description") or "").lower()
                url_s = str(s.get("url") or "").lower()
                id_s = str(s.get("id") or "").strip().lower()
                blob = f"{name_s} {desc_s} {url_s} #{id_s} {id_s}"
                s["_search_blob"] = blob

            if query and query not in blob:
                continue

            if has_multi_country_filter:
                st_code = get_station_matched_country(s)
                target = st_code if st_code else "__NONE__"
                if target not in selected_codes:
                    continue
            elif (country_code or country_pat) and not match_station_country(s, country_code, country_pat):
                continue

            if genre_pats and not any(match_station_genre(s, gp) for gp in genre_pats):
                continue

            filtered.append(s)

        self.filtered_station_items = filtered
        logger.debug(f"Фільтрація: знайдено {len(self.filtered_station_items)} з {len(self.stations)} станцій.")

        if hasattr(self, 'country_count_badge'):
            count = len(self.filtered_station_items)
            self.country_count_badge.setText(f"{count} ст.")
            self.country_count_badge.setToolTip(f"Знайдено станцій: {count} (загалом у базі: {len(self.stations)})")

        currently_playing_url = None
        if self.current_playing_widget:
            try:
                currently_playing_url = str(self.current_playing_widget.station_data.get("url", "")).strip()
            except Exception:
                currently_playing_url = None

        was_search_focused = hasattr(self, 'search_input') and (
            self.search_input.hasFocus() or self.focusWidget() is self.search_input
        )
        self.grid_container.setUpdatesEnabled(False)
        try:
            # Безпечно від'єднуємо add_station_card перед очищенням сітки
            if hasattr(self, 'add_station_card') and self.add_station_card is not None:
                try:
                    self.grid_layout.removeWidget(self.add_station_card)
                    self.add_station_card.setParent(None)
                except RuntimeError:
                    self.add_station_card = None

            for i in reversed(range(self.grid_layout.count())):
                item = self.grid_layout.takeAt(i)
                if item and item.widget():
                    w = item.widget()
                    if hasattr(self, 'add_station_card') and w is self.add_station_card:
                        continue
                    w.deleteLater()

            self.station_widgets = []
            self.rendered_cards_count = 0
            self.current_playing_widget = None
            self.focused_card_widget = None

            self.load_more_cards(self.CHUNK_SIZE, currently_playing_url=currently_playing_url)

            self._update_stats()

            has_active = bool(self.current_playing_widget or getattr(self, "current_attempted_widget", None))
            if reset_scroll and not has_active:
                self.scroll_area.verticalScrollBar().setValue(0)

            self._position_on_active_card()
            if was_search_focused and hasattr(self, 'search_input'):
                self.search_input.setFocus()
        finally:
            self.grid_container.setUpdatesEnabled(True)
            self.grid_container.update()

    def load_more_cards(self, count=None, currently_playing_url=None):
        if count is None:
            count = self.CHUNK_SIZE

        start = self.rendered_cards_count
        end = min(len(self.filtered_station_items), start + count)
        if start >= end and self.rendered_cards_count > 0:
            return

        if currently_playing_url is None and self.current_playing_widget:
            try:
                currently_playing_url = str(self.current_playing_widget.station_data.get("url", "")).strip()
            except Exception:
                pass

        if hasattr(self, 'add_station_card') and self.add_station_card is not None:
            try:
                self.grid_layout.removeWidget(self.add_station_card)
            except RuntimeError:
                self.add_station_card = None

        self.grid_container.setUpdatesEnabled(False)
        try:
            for idx in range(start, end):
                station = self.filtered_station_items[idx]
                try:
                    display_num = idx + 1
                    widget = StationWidget(
                        station_data=station,
                        on_play_toggle=self._handle_play_toggle,
                        on_delete_toggle=self._on_station_delete_toggled,
                        display_number=display_num,
                        parent=self.grid_container
                    )
                    if currently_playing_url and str(station.get("url", "")).strip() == currently_playing_url:
                        self.current_playing_widget = widget
                        try:
                            widget.set_playing_state(True)
                            if self.current_track_title:
                                widget.set_track_title(self.current_track_title)
                            if self.current_stream_codec or self.current_stream_bitrate:
                                widget.set_stream_quality(self.current_stream_codec, self.current_stream_bitrate)
                        except Exception:
                            pass

                    widget.installEventFilter(self)
                    widget.show()
                    self.station_widgets.append(widget)
                    self.grid_layout.addWidget(widget, idx // 4, idx % 4)
                except Exception as we:
                    logger.error(f"Помилка створення картки для станції #{station.get('id', idx+1)}: {we}", exc_info=True)

            self.rendered_cards_count = end

            is_add_card_valid = False
            if hasattr(self, 'add_station_card') and self.add_station_card is not None:
                try:
                    self.add_station_card.isVisible()
                    is_add_card_valid = True
                except RuntimeError:
                    is_add_card_valid = False

            if not is_add_card_valid:
                self.add_station_card = AddStationCard(self._open_add_station_dialog, parent=self.grid_container)
                self.add_station_card.installEventFilter(self)

            self.add_station_card.setParent(self.grid_container)
            self.add_station_card.show()
            end_idx = self.rendered_cards_count
            self.grid_layout.addWidget(self.add_station_card, end_idx // 4, end_idx % 4)
        finally:
            self.grid_container.setUpdatesEnabled(True)
            self.grid_container.update()

    def _get_visible_widgets(self):
        result = []
        for w in self.station_widgets:
            try:
                if w and not w.isHidden():
                    result.append(w)
            except RuntimeError:
                pass
        return result

    def _set_focused_widget(self, target_widget, scroll_into_view=True):
        if self.focused_card_widget and self.focused_card_widget != target_widget:
            try:
                self.focused_card_widget.set_card_focused(False)
            except (RuntimeError, Exception):
                pass
        self.focused_card_widget = target_widget
        if self.focused_card_widget:
            try:
                self.focused_card_widget.set_card_focused(True)
                if scroll_into_view:
                    self.scroll_area.ensureWidgetVisible(self.focused_card_widget, 40, 40)
            except (RuntimeError, Exception):
                pass

    def _navigate_cards(self, delta):
        visible = self._get_visible_widgets()
        if not visible:
            return

        if not self.focused_card_widget or self.focused_card_widget not in visible:
            if self.current_playing_widget and self.current_playing_widget in visible:
                target_idx = visible.index(self.current_playing_widget)
            else:
                target_idx = 0
        else:
            try:
                cur_idx = visible.index(self.focused_card_widget)
                target_idx = cur_idx + delta
                if delta > 0 and target_idx >= len(visible) - 4:
                    if self.rendered_cards_count < len(self.filtered_station_items):
                        self.load_more_cards()
                        visible = self._get_visible_widgets()

                if target_idx < 0:
                    target_idx = 0
                elif target_idx >= len(visible):
                    target_idx = len(visible) - 1
            except ValueError:
                target_idx = 0

        self._set_focused_widget(visible[target_idx])

    def _scroll_to_top(self):
        """Перехід у самий верх вікна (Home) до упору."""
        visible = self._get_visible_widgets()
        if visible:
            self._set_focused_widget(visible[0])
        self.scroll_area.verticalScrollBar().setValue(0)

    def _scroll_to_bottom(self):
        """Перехід у самий низ вікна (End) до упору."""
        while self.rendered_cards_count < len(self.filtered_station_items):
            prev_cnt = self.rendered_cards_count
            self.load_more_cards(200)
            if self.rendered_cards_count == prev_cnt:
                break
        visible = self._get_visible_widgets()
        if visible:
            self._set_focused_widget(visible[-1])
        vsb = self.scroll_area.verticalScrollBar()
        vsb.setValue(vsb.maximum())

    def _page_scroll(self, direction: int):
        """Прокрутка на один екран за раз (Page Up / Page Down). direction: 1 (вниз) або -1 (вгору)."""
        if direction > 0 and self.rendered_cards_count < len(self.filtered_station_items):
            self.load_more_cards()

        visible = self._get_visible_widgets()
        vsb = self.scroll_area.verticalScrollBar()
        viewport_h = self.scroll_area.viewport().height()
        row_height = 98
        rows_per_screen = max(1, viewport_h // row_height)
        cards_per_screen = rows_per_screen * 4
        step = rows_per_screen * row_height

        cur_scroll = vsb.value()

        if not visible:
            if direction > 0:
                vsb.setValue(min(vsb.maximum(), cur_scroll + step))
            else:
                vsb.setValue(max(0, cur_scroll - step))
            return

        if not self.focused_card_widget or self.focused_card_widget not in visible:
            if self.current_playing_widget and self.current_playing_widget in visible:
                cur_idx = visible.index(self.current_playing_widget)
            else:
                cur_idx = 0
        else:
            try:
                cur_idx = visible.index(self.focused_card_widget)
            except ValueError:
                cur_idx = 0

        target_idx = cur_idx + (cards_per_screen * direction)
        if target_idx < 0:
            target_idx = 0
        elif target_idx >= len(visible):
            target_idx = len(visible) - 1

        self._set_focused_widget(visible[target_idx])

        if direction > 0:
            if target_idx == len(visible) - 1:
                vsb.setValue(vsb.maximum())
            else:
                vsb.setValue(min(vsb.maximum(), cur_scroll + step))
        else:
            if target_idx == 0:
                vsb.setValue(0)
            else:
                vsb.setValue(max(0, cur_scroll - step))

    def _schedule_play_next_unplayed(self, from_widget=None):
        """Планує автоматичний перехід до наступної невідіграної станції з захистом від дублювання."""
        self._next_advance_from_widget = from_widget
        if hasattr(self, 'auto_advance_timer'):
            self.auto_advance_timer.start(200)

    def _do_auto_advance(self):
        """Виконує відкладений перехід до наступної невідіграної станції."""
        target = getattr(self, '_next_advance_from_widget', None)
        self._next_advance_from_widget = None
        self._play_next_unplayed_station(from_widget=target)

    def _play_next_unplayed_station(self, from_widget=None):
        """
        Знаходить наступну станцію, яка ще не програвалась і не позначена на видалення,
        переводить на неї фокус і вмикає її на програвання.
        Підтримує ліниве довантаження карток, якщо невідіграна станція знаходиться нижче.
        """
        station_cards = [w for w in self.station_widgets if isinstance(w, StationWidget)]

        start_idx = 0
        if from_widget and from_widget in station_cards:
            start_idx = station_cards.index(from_widget) + 1

        next_card = None

        # 1. Пошук вперед від поточної станції
        while True:
            station_cards = [w for w in self.station_widgets if isinstance(w, StationWidget)]
            for i in range(start_idx, len(station_cards)):
                card = station_cards[i]
                if not card.delete_cb.isChecked() and not getattr(card, "has_played", False) and not getattr(card, "is_playing", False):
                    next_card = card
                    break
            if next_card or self.rendered_cards_count >= len(self.filtered_station_items):
                break
            prev_rendered = self.rendered_cards_count
            self.load_more_cards()
            if self.rendered_cards_count == prev_rendered:
                break
            start_idx = len(station_cards)

        # 2. Якщо попереду все відіграно, шукаємо спочатку списку (циклічно)
        if not next_card:
            station_cards = [w for w in self.station_widgets if isinstance(w, StationWidget)]
            limit = station_cards.index(from_widget) if (from_widget and from_widget in station_cards) else len(station_cards)
            for i in range(0, limit):
                card = station_cards[i]
                if not card.delete_cb.isChecked() and not getattr(card, "has_played", False) and not getattr(card, "is_playing", False):
                    next_card = card
                    break

        if next_card:
            st_id = next_card.station_data.get("id", "")
            name = next_card.station_data.get("name", "Без назви")
            logger.info(f"▶ Автоматичний перехід та запуск наступної невідіграної станції: #{st_id} «{name}»")
            self._set_focused_widget(next_card)
            self.scroll_area.ensureWidgetVisible(next_card, 40, 40)
            self._handle_play_toggle(next_card)
        else:
            logger.info("Більше немає невідіграних станцій для відтворення.")
            if "позначено до видалення" not in self.status_label.text():
                self.status_label.setText("⏹ Усі доступні станції вже відіграні або позначені на видалення")

    def _on_station_delete_toggled(self, widget, checked):
        """Обробник зміни стану чекбокса 'Видалити'."""
        self._update_stats()
        if checked and widget == self.current_playing_widget:
            st_id = widget.station_data.get("id", "")
            name = widget.station_data.get("name", "Без назви")
            logger.info(f"Станцію #{st_id} «{name}», що грала, позначено на видалення користувачем.")
            if hasattr(self, 'load_timeout_timer'):
                self.load_timeout_timer.stop()
            if hasattr(self, 'auto_switch_timer'):
                self.auto_switch_timer.stop()
            try:
                self.player.stop()
                widget.set_playing_state(False)
            except Exception:
                pass
            self.current_playing_widget = None
            self._reset_playback_metadata()
            self._schedule_play_next_unplayed(from_widget=widget)

    def _handle_play_toggle(self, widget):
        if hasattr(self, 'auto_advance_timer'):
            self.auto_advance_timer.stop()
        if hasattr(self, 'auto_switch_timer'):
            self.auto_switch_timer.stop()
        try:
            self._set_focused_widget(widget, scroll_into_view=False)
            if self.current_playing_widget == widget:
                # Зупиняємо поточну станцію
                st_id = widget.station_data.get("id", "")
                name = widget.station_data.get("name", "Без назви")
                logger.info(f"Зупинка відтворення: #{st_id} «{name}»")
                if hasattr(self, 'load_timeout_timer'):
                    self.load_timeout_timer.stop()
                self.player.stop()
                try:
                    widget.set_playing_state(False)
                except Exception:
                    pass
                self.current_playing_widget = None
                self.current_attempted_widget = None
                self._reset_playback_metadata()
                self.status_label.setText("⏹ Відтворення зупинено")
            else:
                # Зупиняємо попередню станцію та плеєр перед підключенням нової
                if hasattr(self, 'load_timeout_timer'):
                    self.load_timeout_timer.stop()
                try:
                    self.player.stop()
                except Exception:
                    pass
                if self.current_playing_widget:
                    try:
                        self.current_playing_widget.set_playing_state(False)
                    except Exception as prev_err:
                        logger.debug(f"Попередній віджет уже видалений або недоступний: {prev_err}")
                    self.current_playing_widget = None

                self._reset_playback_metadata()

                # Запускаємо нову станцію
                stream_url = str(widget.station_data.get("url", "")).strip()
                name = str(widget.station_data.get("name", "Без назви"))
                st_id = self._get_station_display_num(widget)
                logger.info(f"Запуск відтворення: #{st_id} «{name}», URL: {stream_url}")
                self.status_label.setText(f"⏳ Підключення: #{st_id} {name}...")

                qurl = QUrl(stream_url)
                if not qurl.isValid():
                    logger.warning(f"Недійсний QUrl для станції #{st_id}: {stream_url}")

                self.current_playing_widget = widget
                self.current_attempted_widget = widget
                self._attempt_timestamp = time.time()
                self._set_current_track_title("")

                # Запускаємо 6-секундний таймаут на відкриття та підключення потоку
                if hasattr(self, 'load_timeout_timer'):
                    self.load_timeout_timer.start(6000)

                self.player.setSource(qurl)
                self.player.play()
                try:
                    widget.set_playing_state(True)
                except Exception:
                    pass
        except Exception as e:
            logger.exception(f"Помилка в _handle_play_toggle: {e}")

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            if QApplication.activeModalWidget() is not None:
                return super().eventFilter(watched, event)

            key = event.key()

            if watched == self.search_input:
                # Перемикання на список карток клавішею Tab або Shift+Tab
                if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                    self.search_input.clearFocus()
                    self.setFocus()
                    visible = self._get_visible_widgets()
                    if visible:
                        target = self.focused_card_widget if (self.focused_card_widget in visible) else visible[0]
                        self._set_focused_widget(target)
                        self.scroll_area.ensureWidgetVisible(target, 40, 40)
                    return True

                elif key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    visible = self._get_visible_widgets()
                    if visible:
                        self.search_input.clearFocus()
                        target = self.focused_card_widget if (self.focused_card_widget in visible) else visible[0]
                        self._set_focused_widget(target)
                        self.setFocus()
                        if key == Qt.Key.Key_Down and len(visible) > 4:
                            self._navigate_cards(4)
                        elif key == Qt.Key.Key_Up and len(visible) > 4:
                            self._navigate_cards(-4)
                        return True
                elif key == Qt.Key.Key_PageDown:
                    self.search_input.clearFocus()
                    self.setFocus()
                    self._page_scroll(1)
                    return True
                elif key == Qt.Key.Key_PageUp:
                    self.search_input.clearFocus()
                    self.setFocus()
                    self._page_scroll(-1)
                    return True
                elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    visible = self._get_visible_widgets()
                    if visible:
                        self.search_input.clearFocus()
                        target = self.focused_card_widget if (self.focused_card_widget in visible) else visible[0]
                        self._set_focused_widget(target)
                        self._handle_play_toggle(target)
                        self.setFocus()
                        return True
                elif key == Qt.Key.Key_Escape:
                    self.search_input.clear()
                    self.search_input.clearFocus()
                    self.setFocus()
                    return True
                else:
                    return super().eventFilter(watched, event)

            # Перемикання зі списку карток на поле пошуку клавішею Tab або Shift+Tab
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.search_input.setFocus()
                self.search_input.selectAll()
                return True

            # Навігація між картками: стрілки, Home, End, PageUp, PageDown
            if key == Qt.Key.Key_Left:
                self._navigate_cards(-1)
                return True
            elif key == Qt.Key.Key_Right:
                self._navigate_cards(1)
                return True
            elif key == Qt.Key.Key_Up:
                self._navigate_cards(-4)
                return True
            elif key == Qt.Key.Key_Down:
                self._navigate_cards(4)
                return True
            elif key == Qt.Key.Key_Home:
                self._scroll_to_top()
                return True
            elif key == Qt.Key.Key_End:
                self._scroll_to_bottom()
                return True
            elif key == Qt.Key.Key_PageDown:
                self._page_scroll(1)
                return True
            elif key == Qt.Key.Key_PageUp:
                self._page_scroll(-1)
                return True
            elif key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                target = self.focused_card_widget or self.current_playing_widget
                if not target:
                    visible = self._get_visible_widgets()
                    if visible:
                        target = visible[0]
                if target:
                    self._set_focused_widget(target)
                    self._handle_play_toggle(target)
                return True
            elif key in (Qt.Key.Key_Delete, Qt.Key.Key_D):
                if self.focused_card_widget and hasattr(self.focused_card_widget, 'delete_cb'):
                    cb = self.focused_card_widget.delete_cb
                    cb.setChecked(not cb.isChecked())
                    return True
            elif key == Qt.Key.Key_Insert:
                self._open_add_station_dialog()
                return True
            elif key == Qt.Key.Key_F5:
                self._load_stations()
                return True

        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'filter_bar_container'):
            self.filter_bar_container.update_positions()

    def _position_on_active_card(self):
        """
        Позиціонує клавіатуру (фокус) та список (скрол) на активну картку:
        1. Яка зараз відтворюється (грає).
        2. Або яка зараз перевіряється / буферизується / завантажується.
        Якщо активної картки немає - фокусує першу видиму картку.
        """
        active_target = self.current_playing_widget or getattr(self, "current_attempted_widget", None)

        visible = self._get_visible_widgets()
        if not visible:
            return

        target = None
        if active_target and active_target in visible:
            target = active_target
        elif active_target and hasattr(active_target, "station_data"):
            target_url = str(active_target.station_data.get("url", "")).strip()
            target_id = active_target.station_data.get("id")
            for w in visible:
                if (target_url and str(w.station_data.get("url", "")).strip() == target_url) or (target_id is not None and w.station_data.get("id") == target_id):
                    target = w
                    break

        if not target:
            target = self.focused_card_widget if (self.focused_card_widget in visible) else visible[0]

        if target:
            self._set_focused_widget(target)

            # Прокручуємо список так, щоб активна картка була по центру або гарантовано видимою
            try:
                target_pos = target.mapTo(self.grid_container, QPoint(0, 0))
                viewport_h = self.scroll_area.viewport().height()
                target_h = target.height() or 80
                if viewport_h > target_h:
                    desired_val = max(0, target_pos.y() - (viewport_h - target_h) // 2)
                    self.scroll_area.verticalScrollBar().setValue(desired_val)
                else:
                    self.scroll_area.ensureWidgetVisible(target, 40, 40)
            except Exception:
                self.scroll_area.ensureWidgetVisible(target, 40, 40)

            # Якщо користувач зараз перебуває в полі пошуку (вводить текст або перейшов табом) — не скидати фокус!
            is_search_focused = hasattr(self, 'search_input') and (
                self.search_input.hasFocus() or self.focusWidget() is self.search_input
            )
            if is_search_focused:
                self.search_input.setFocus()
                return

            if hasattr(self, 'search_input'):
                self.search_input.clearFocus()
            self.setFocus()

    def showEvent(self, event):
        super().showEvent(event)
        # При показі вікна фокус обов'язково встановлюється на навігацію карток, а не в поле вводу
        self.search_input.clearFocus()
        self.setFocus()
        QTimer.singleShot(60, self._position_on_active_card)

    def mousePressEvent(self, event):
        # Якщо клік не по самому полю пошуку - повертаємо фокус на картки
        if hasattr(self, 'search_input') and self.search_input.underMouse():
            super().mousePressEvent(event)
            return
        if hasattr(self, 'search_input'):
            self.search_input.clearFocus()
        self.setFocus()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.search_input.setFocus()
            self.search_input.selectAll()
            return
        elif key == Qt.Key.Key_Left:
            self._navigate_cards(-1)
            return
        elif key == Qt.Key.Key_Right:
            self._navigate_cards(1)
            return
        elif key == Qt.Key.Key_Up:
            self._navigate_cards(-4)
            return
        elif key == Qt.Key.Key_Down:
            self._navigate_cards(4)
            return
        elif key == Qt.Key.Key_Home:
            self._scroll_to_top()
            return
        elif key == Qt.Key.Key_End:
            self._scroll_to_bottom()
            return
        elif key == Qt.Key.Key_PageDown:
            self._page_scroll(1)
            return
        elif key == Qt.Key.Key_PageUp:
            self._page_scroll(-1)
            return
        elif key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            target = self.focused_card_widget or self.current_playing_widget
            if not target:
                visible = self._get_visible_widgets()
                if visible:
                    target = visible[0]
            if target:
                self._set_focused_widget(target)
                self._handle_play_toggle(target)
            return
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_D):
            if self.focused_card_widget and hasattr(self.focused_card_widget, 'delete_cb'):
                cb = self.focused_card_widget.delete_cb
                cb.setChecked(not cb.isChecked())
                return
        elif key == Qt.Key.Key_Insert:
            self._open_add_station_dialog()
            return
        elif key == Qt.Key.Key_F5:
            self._load_stations()
            return
        super().keyPressEvent(event)

    def _on_load_timeout(self):
        """Спрацьовує, якщо потік станції підключається/відкривається довше 6 секунд."""
        if hasattr(self, 'auto_switch_timer'):
            self.auto_switch_timer.stop()
        target = self.current_playing_widget or getattr(self, "current_attempted_widget", None)
        if not target:
            return

        st_id = target.station_data.get("id", "")
        name = target.station_data.get("name", "Без назви")

        logger.warning(
            f"⏱ Таймаут підключення/відкриття потоку станції #{st_id} «{name}» (> 6 сек). "
            f"Станцію автоматично позначено до видалення."
        )

        try:
            self.player.stop()
        except Exception:
            pass

        try:
            target.set_playing_state(False)
        except Exception:
            pass

        if hasattr(target, "delete_cb") and not target.delete_cb.isChecked():
            target.delete_cb.setChecked(True)
        else:
            target.station_data["_marked_delete"] = True

        self.status_label.setText(f"❌ #{st_id} {name}: [Підключення > 6 сек] — позначено до видалення")
        self._last_failed_widget = target
        self._last_failed_time = time.time()
        self.current_playing_widget = None
        self.current_attempted_widget = None
        self._reset_playback_metadata()
        self._update_stats()

        # Автоматично переходимо на наступну невідіграну картку і вмикаємо її
        self._schedule_play_next_unplayed(from_widget=target)

    def _on_fatal_stream_error(self, error_desc: str):
        if hasattr(self, 'load_timeout_timer'):
            self.load_timeout_timer.stop()
        if hasattr(self, 'auto_switch_timer'):
            self.auto_switch_timer.stop()

        target = self.current_playing_widget or getattr(self, "current_attempted_widget", None)

        if not target and hasattr(self, "_last_failed_widget") and self._last_failed_widget:
            if time.time() - getattr(self, "_last_failed_time", 0) <= 2.5:
                if error_desc != "Could not open file":
                    st_id = self._last_failed_widget.station_data.get("id", "")
                    name = self._last_failed_widget.station_data.get("name", "Без назви")
                    self.status_label.setText(f"❌ #{st_id} {name}: [{error_desc}] — позначено до видалення")
            return

        if not target:
            return

        if time.time() - getattr(self, "_attempt_timestamp", 0) > 25:
            return

        st_id = target.station_data.get("id", "")
        name = target.station_data.get("name", "Без назви")

        logger.warning(
            f"🚨 Фатальна помилка старту потоку станції #{st_id} «{name}»: "
            f"[{error_desc}]. Станцію автоматично позначено до видалення."
        )

        try:
            self.player.stop()
        except Exception:
            pass

        try:
            target.set_playing_state(False)
        except Exception:
            pass

        if hasattr(target, "delete_cb") and not target.delete_cb.isChecked():
            target.delete_cb.setChecked(True)
        else:
            target.station_data["_marked_delete"] = True

        self.status_label.setText(f"❌ #{st_id} {name}: [{error_desc}] — позначено до видалення")
        self._last_failed_widget = target
        self._last_failed_time = time.time()
        self.current_playing_widget = None
        self.current_attempted_widget = None
        self._reset_playback_metadata()
        self._update_stats()

        self._schedule_play_next_unplayed(from_widget=target)

    def _on_player_error(self, error, error_string):
        logger.warning(f"Помилка медіаплеєра [код {error}]: {error_string}")
        if hasattr(self, 'load_timeout_timer'):
            self.load_timeout_timer.stop()

        lower_err = str(error_string).lower()
        if "immediate exit" in lower_err:
            return
        elif "404" in lower_err:
            self._on_fatal_stream_error("HTTP error 404 Not Found")
            return
        elif "resolve" in lower_err or "hostname" in lower_err or "невідома назва" in lower_err:
            self._on_fatal_stream_error("Failed to resolve hostname")
            return
        elif "could not open file" in lower_err or "could not open media" in lower_err:
            self._on_fatal_stream_error("Could not open file")
            return
        elif "error reading http response" in lower_err:
            self._on_fatal_stream_error("Error reading HTTP response")
            return
        elif "у з'єднанні відмовлено" in lower_err or "connection refused" in lower_err:
            self._on_fatal_stream_error("У з'єднанні відмовлено")
            return
        elif "lrc" in lower_err or "misdetection" in lower_err:
            self._on_fatal_stream_error("Format lrc / Misdetection")
            return
        elif "subtitle" in lower_err:
            self._on_fatal_stream_error("Subtitle: text (немає аудіо)")
            return
        elif "invalid data" in lower_err:
            self._on_fatal_stream_error("Invalid data found")
            return
        elif "could not update timestamps" in lower_err:
            self._on_fatal_stream_error("Could not update timestamps (битий потік)")
            return

        if "позначено до видалення" in self.status_label.text():
            return

        target = self.current_playing_widget or getattr(self, "current_attempted_widget", None)
        if target:
            try:
                name = target.station_data.get("name", "")
                self.status_label.setText(f"⚠️ Помилка ({name}): {error_string}")
                target.set_playing_state(False)
            except Exception:
                pass
            if hasattr(self, 'auto_switch_timer'):
                self.auto_switch_timer.stop()
            self.current_playing_widget = None
            self._reset_playback_metadata()

    def _on_tracks_changed(self):
        """Спрацьовує при зміні/виявленні доріжок медіаплеєром."""
        target = self.current_playing_widget or getattr(self, "current_attempted_widget", None)
        if not target:
            return
        try:
            audio_count = len(self.player.audioTracks())
            sub_count = len(self.player.subtitleTracks())
            video_count = len(self.player.videoTracks())

            if sub_count > 0 and audio_count == 0 and video_count == 0:
                num = target.station_data.get("id", "")
                name = target.station_data.get("name", "")
                logger.warning(
                    f"Виявлено потік лише з субтитрами/текстом для #{num} «{name}» "
                    f"(subtitles: {sub_count}, audio: 0). Позначаємо на видалення."
                )
                self._on_fatal_stream_error("Subtitle: text (немає аудіо)")
        except Exception as e:
            logger.debug(f"Помилка в _on_tracks_changed: {e}")

    def _on_position_changed(self, pos):
        """Коли надходять аудіопакети і позиція починає рухатися (> 0), відтворення точно почалося."""
        if pos > 0:
            if hasattr(self, 'load_timeout_timer') and self.load_timeout_timer.isActive():
                self.load_timeout_timer.stop()
            target = self.current_playing_widget
            if target and "позначено до видалення" not in self.status_label.text():
                name = target.station_data.get("name", "")
                num = self._get_station_display_num(target)
                if hasattr(self, 'auto_switch_cb') and self.auto_switch_cb.isChecked():
                    secs = self.get_auto_switch_seconds()
                    if hasattr(self, 'auto_switch_timer') and not self.auto_switch_timer.isActive():
                        self.auto_switch_timer.start(secs * 1000)
                        logger.info(f"Запущено {secs}-секундний таймер автопереходу для станції #{num} {name}")
                    if not self.status_label.text().startswith("▶ Грає"):
                        self.status_label.setText(f"▶ Грає: #{num} {name} (автоперехід через {secs}с)")
                else:
                    if not self.status_label.text().startswith("▶ Грає"):
                        self.status_label.setText(f"▶ Грає: #{num} {name}")

    def _on_media_status(self, status):
        logger.debug(f"Статус мультимедіа змінився: {status}")
        target = self.current_playing_widget or getattr(self, "current_attempted_widget", None)
        if not target:
            return
        try:
            name = target.station_data.get("name", "")
            num = self._get_station_display_num(target)
            if status in (QMediaPlayer.MediaStatus.BufferingMedia, QMediaPlayer.MediaStatus.LoadingMedia):
                if "позначено до видалення" not in self.status_label.text():
                    self.status_label.setText(f"⏳ Буферизація: #{num} {name}...")
                if hasattr(self, 'load_timeout_timer') and not self.load_timeout_timer.isActive():
                    elapsed = time.time() - getattr(self, "_attempt_timestamp", time.time())
                    if elapsed < 6.0:
                        remaining = max(100, int((6.0 - elapsed) * 1000))
                        self.load_timeout_timer.start(remaining)
                    else:
                        self._on_load_timeout()
                        return
            elif status == QMediaPlayer.MediaStatus.LoadedMedia:
                try:
                    if len(self.player.subtitleTracks()) > 0 and len(self.player.audioTracks()) == 0 and len(self.player.videoTracks()) == 0:
                        logger.warning(f"У LoadedMedia для #{num} {name} виявлено лише субтитри без аудіо!")
                        self._on_fatal_stream_error("Subtitle: text (немає аудіо)")
                        return
                except Exception:
                    pass

                if "позначено до видалення" not in self.status_label.text():
                    self.status_label.setText(f"⏳ Підключення: #{num} {name}...")
            elif status == QMediaPlayer.MediaStatus.BufferedMedia:
                try:
                    if len(self.player.subtitleTracks()) > 0 and len(self.player.audioTracks()) == 0 and len(self.player.videoTracks()) == 0:
                        logger.warning(f"У BufferedMedia для #{num} {name} виявлено лише субтитри без аудіо!")
                        self._on_fatal_stream_error("Subtitle: text (немає аудіо)")
                        return
                except Exception:
                    pass

                try:
                    dur = self.player.duration()
                    if 0 < dur < 5000:
                        logger.warning(f"У BufferedMedia для #{num} {name} виявлено короткий аудіофайл ({dur} мс)!")
                        self._on_fatal_stream_error(f"Короткий файл ({dur/1000.0:.2f} сек, не радіо)")
                        return
                except Exception:
                    pass

                if hasattr(self, 'load_timeout_timer'):
                    self.load_timeout_timer.stop()
                self.current_attempted_widget = None

                if hasattr(self, 'auto_switch_cb') and self.auto_switch_cb.isChecked():
                    secs = self.get_auto_switch_seconds()
                    if hasattr(self, 'auto_switch_timer'):
                        self.auto_switch_timer.start(secs * 1000)
                        logger.info(f"Запущено {secs}-секундний таймер автопереходу для станції #{num} {name}")
                    self.status_label.setText(f"▶ Грає: #{num} {name} (автоперехід через {secs}с)")
                else:
                    self.status_label.setText(f"▶ Грає: #{num} {name}")
            elif status == QMediaPlayer.MediaStatus.EndOfMedia:
                logger.warning(f"Потік станції #{num} {name} раптово завершився (EndOfMedia)!")
                if hasattr(self, 'load_timeout_timer'):
                    self.load_timeout_timer.stop()
                if hasattr(self, 'auto_switch_timer'):
                    self.auto_switch_timer.stop()
                self._on_fatal_stream_error("EndOfMedia (короткий звук / завершення потоку)")
            elif status == QMediaPlayer.MediaStatus.StalledMedia:
                logger.warning(f"Потік завис (StalledMedia) для #{num} {name}")
                if "позначено до видалення" not in self.status_label.text():
                    self.status_label.setText(f"⏳ Затримка буферизації: #{num} {name}...")
            elif status == QMediaPlayer.MediaStatus.InvalidMedia:
                logger.warning(f"Недійсний потік InvalidMedia для #{num} {name}")
                if hasattr(self, 'load_timeout_timer'):
                    self.load_timeout_timer.stop()
                if hasattr(self, 'auto_switch_timer'):
                    self.auto_switch_timer.stop()
                self._on_fatal_stream_error("InvalidMedia: недійсний потік")
        except Exception as e:
            logger.debug(f"Помилка при обробці media_status: {e}")
            self.current_playing_widget = None

    def _on_playback_state_changed(self, state):
        logger.debug(f"Стан відтворення змінився: {state}")
        if state == QMediaPlayer.PlaybackState.StoppedState:
            if hasattr(self, 'load_timeout_timer'):
                self.load_timeout_timer.stop()
            if hasattr(self, 'auto_switch_timer'):
                self.auto_switch_timer.stop()
            if not self.current_playing_widget:
                self._reset_playback_metadata()
                self.status_label.setText("⏹ Відтворення зупинено")

    def _on_auto_switch_toggled(self, checked):
        secs = self.get_auto_switch_seconds()
        logger.info(f"Режим автопереходу станцій через {secs} сек: {'УВІМКНЕНО' if checked else 'ВИМКНЕНО'}")
        if not checked:
            if hasattr(self, 'auto_switch_timer'):
                self.auto_switch_timer.stop()
            if self.current_playing_widget:
                num = self._get_station_display_num(self.current_playing_widget)
                name = self.current_playing_widget.station_data.get("name", "")
                if self.status_label.text().startswith("▶ Грає"):
                    self.status_label.setText(f"▶ Грає: #{num} {name}")
        else:
            if self.current_playing_widget and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                num = self._get_station_display_num(self.current_playing_widget)
                name = self.current_playing_widget.station_data.get("name", "")
                if hasattr(self, 'auto_switch_timer'):
                    self.auto_switch_timer.start(secs * 1000)
                    logger.info(f"Запущено {secs}-секундний таймер автопереходу для поточної станції #{num} {name}")
                if self.status_label.text().startswith("▶ Грає"):
                    self.status_label.setText(f"▶ Грає: #{num} {name} (автоперехід через {secs}с)")

    def _on_auto_switch_timeout(self):
        if not hasattr(self, 'auto_switch_cb') or not self.auto_switch_cb.isChecked():
            return
        if not self.current_playing_widget:
            return

        current_card = self.current_playing_widget
        st_id = current_card.station_data.get("id", "")
        name = current_card.station_data.get("name", "Без назви")
        secs = self.get_auto_switch_seconds()
        logger.info(f"⏱ Станція #{st_id} «{name}» успішно відіграла {secs} секунд. Автоперехід на наступну невідіграну станцію...")

        self._play_next_unplayed_station(from_widget=current_card)

    def _update_stats(self):
        total = len(self.stations)
        marked = sum(1 for s in self.stations if s.get("_marked_delete", False))
        self.stats_label.setText(f"Всього: {total} | Позначено до видалення: {marked}")

    def _on_search_text_changed(self, text):
        if not text.strip():
            self._search_timer.stop()
            self._filter_stations("", reset_scroll=True)
        else:
            self._search_timer.start(120)

    def _clear_search(self):
        self.search_input.clear()
        self.search_input.setFocus()

    def _save_clean_stations(self):
        logger.info(f"Збереження списку станцій ({len(self.stations)} шт.) у файл {JSON_PATH}...")
        try:
            clean_list = []
            for s in self.stations:
                clean_item = {k: v for k, v in s.items() if not k.startswith("_")}
                if s.get("_has_played") or s.get("has_played"):
                    clean_item["has_played"] = True
                if s.get("_is_imported_new") or s.get("is_new"):
                    clean_item["is_new"] = True
                clean_list.append(clean_item)

            # 1. Створюємо резервну копію перед перезаписом
            bak_path = JSON_PATH.with_suffix(".json.bak")
            if JSON_PATH.exists() and JSON_PATH.stat().st_size > 0:
                try:
                    shutil.copy2(JSON_PATH, bak_path)
                    logger.debug(f"Створено бекап: {bak_path}")
                except Exception as be:
                    logger.warning(f"Не вдалося створити бекап {bak_path}: {be}")

            # 2. Атомарний запис через тимчасовий файл
            tmp_path = JSON_PATH.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(clean_list, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, JSON_PATH)
            logger.info(f"Успішно атомарно збережено {len(clean_list)} станцій у {JSON_PATH}")
            if hasattr(self, '_update_country_data'):
                self._update_country_data()

            # 3. Синхронізація з альтернативним шляхом
            if ALT_JSON_PATH.exists() or ALT_JSON_PATH.parent.exists():
                try:
                    alt_tmp = ALT_JSON_PATH.with_suffix(".json.tmp")
                    with open(alt_tmp, "w", encoding="utf-8") as f:
                        json.dump(clean_list, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(alt_tmp, ALT_JSON_PATH)
                    logger.debug(f"Синхронізовано з {ALT_JSON_PATH}")
                except Exception as ae:
                    logger.warning(f"Помилка синхронізації з {ALT_JSON_PATH}: {ae}")

            # 4. Зберігаємо стан додатку
            self._save_app_state()

        except Exception as e:
            logger.exception(f"Критична помилка при збереженні станцій: {e}")
            StyledMessageBox.critical(self, "Помилка збереження", f"Не вдалося зберегти станції у JSON:\n{e}")

    def _copy_track_title(self):
        if hasattr(self, 'btn_copy_track') and not self.btn_copy_track.isEnabled():
            return
        text = ""
        if hasattr(self, 'track_title_label') and self.track_title_label.hasSelectedText():
            text = self.track_title_label.selectedText().strip()
        if not text and hasattr(self, 'track_title_label'):
            raw = self.track_title_label.text().strip()
            if raw.startswith("🎵"):
                text = raw[1:].strip()
            elif raw != "Назва відсутня":
                text = raw
        if not text and hasattr(self, 'current_track_title') and self.current_track_title:
            text = self.current_track_title.strip()

        if text and text != "Назва відсутня":
            QApplication.clipboard().setText(text)
            logger.info(f"Скопійовано назву треку: {text}")
            if hasattr(self, 'btn_copy_track'):
                self.btn_copy_track.setText("✓ Скопійовано")
                self.btn_copy_track.setStyleSheet("""
                    QPushButton {
                        background-color: #238636;
                        color: #ffffff;
                        font-size: 14px;
                        font-weight: bold;
                        border: 1px solid #2ea043;
                        border-radius: 6px;
                        padding: 0 10px;
                    }
                """)
                self.btn_copy_track.setToolTip("Скопійовано в буфер обміну!")
                QTimer.singleShot(1800, self._reset_copy_btn)

    def _reset_copy_btn(self):
        try:
            if hasattr(self, 'btn_copy_track'):
                self.btn_copy_track.setText("📋 Копіювати")
                self.btn_copy_track.setStyleSheet("""
                    QPushButton {
                        background-color: #21262d;
                        color: #58a6ff;
                        font-size: 14px;
                        font-weight: bold;
                        border: 1px solid #30363d;
                        border-radius: 6px;
                        padding: 0 10px;
                    }
                    QPushButton:hover {
                        background-color: #30363d;
                        border-color: #58a6ff;
                        color: #ffffff;
                    }
                    QPushButton:pressed {
                        background-color: #1f6feb;
                        color: #ffffff;
                    }
                    QPushButton:disabled {
                        background-color: #161b22;
                        color: #484f58;
                        border: 1px solid #21262d;
                    }
                """)
                is_valid = bool(self.current_track_title and self.current_track_title.strip() not in ("Назва відсутня", "n/a", "none", "unknown"))
                self.btn_copy_track.setEnabled(is_valid)
                self.btn_copy_track.setToolTip("Скопіювати назву пісні в буфер обміну" if is_valid else "Назва треку відсутня")
        except RuntimeError:
            pass

    def _on_track_title_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1e1f22;
                color: #e6edf3;
                border: 1px solid #3c3f41;
                border-radius: 8px;
                padding: 6px;
                font-size: 15px;
            }
            QMenu::item {
                padding: 6px 24px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #1f6feb;
                color: #ffffff;
            }
        """)

        selected_text = self.track_title_label.selectedText().strip() if self.track_title_label.hasSelectedText() else ""
        if selected_text:
            disp_sel = (selected_text[:25] + "...") if len(selected_text) > 28 else selected_text
            act_copy = menu.addAction(f"📋 Копіювати виділене («{disp_sel}»)")
        else:
            act_copy = menu.addAction("📋 Копіювати назву пісні")
        act_copy.triggered.connect(self._copy_track_title)

        menu.addSeparator()
        act_settings = menu.addAction("⚙️ Налаштування станції та тегів...")
        act_settings.triggered.connect(lambda: self._on_now_playing_right_clicked(pos))

        menu.exec(self.track_title_label.mapToGlobal(pos))

    def _on_status_label_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1e1f22;
                color: #e6edf3;
                border: 1px solid #3c3f41;
                border-radius: 8px;
                padding: 6px;
                font-size: 15px;
            }
            QMenu::item {
                padding: 6px 24px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #1f6feb;
                color: #ffffff;
            }
        """)

        selected_text = self.status_label.selectedText().strip() if self.status_label.hasSelectedText() else ""
        if selected_text:
            disp_sel = (selected_text[:25] + "...") if len(selected_text) > 28 else selected_text
            act_copy = menu.addAction(f"📋 Копіювати виділене («{disp_sel}»)")
            act_copy.triggered.connect(lambda: QApplication.clipboard().setText(selected_text))
        else:
            raw = self.status_label.text().strip()
            clean_st_name = raw
            for prefix in ("▶ Грає:", "▶", "⏹", "⏳", "❌", "⚠️"):
                if clean_st_name.startswith(prefix):
                    clean_st_name = clean_st_name[len(prefix):].strip()
            act_copy = menu.addAction("📋 Копіювати назву станції")
            act_copy.triggered.connect(lambda: QApplication.clipboard().setText(clean_st_name))

        menu.addSeparator()
        act_settings = menu.addAction("⚙️ Налаштування станції та тегів...")
        act_settings.triggered.connect(lambda: self._on_now_playing_right_clicked(pos))

        menu.exec(self.status_label.mapToGlobal(pos))

    def _on_now_playing_right_clicked(self, pos=None):
        if self.current_playing_widget and self.current_playing_widget.station_data:
            self._open_edit_station_dialog(self.current_playing_widget.station_data, widget=self.current_playing_widget)

    def _open_edit_station_dialog(self, station_data: dict, widget=None):
        if not station_data or getattr(self, '_is_editing_station', False):
            return

        self._is_editing_station = True
        try:
            if widget is None:
                st_id = station_data.get("id")
                for w in self.station_widgets:
                    if w.station_data.get("id") == st_id:
                        widget = w
                        break

            dialog = EditStationDialog(station_data, parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                new_data = dialog.get_data()
                old_url = str(station_data.get("url") or "").strip()
                
                # Оновлюємо переданий словник
                station_data.update(new_data)
                station_data.pop("_matched_country", None)
                if "title" in station_data:
                    station_data["title"] = new_data["name"]

                # Оновлюємо станцію у списку self.stations (на випадок різних посилань)
                st_id = station_data.get("id")
                for s in self.stations:
                    if s.get("id") == st_id:
                        s.update(new_data)
                        s.pop("_matched_country", None)
                        if "title" in s:
                            s["title"] = new_data["name"]
                        _name_s = str(s.get("name", "")).lower()
                        _desc_s = str(s.get("description", "")).lower()
                        _url_s = str(s.get("url", "")).lower()
                        _id_s = str(s.get("id", "")).strip().lower()
                        s["_search_blob"] = f"{_name_s} {_desc_s} {_url_s} #{_id_s} {_id_s}"
                        break

                if widget is not None:
                    widget.update_station_info(station_data)

                # Якщо станція відтворюється зараз, оновлюємо заголовок шапки
                if self.current_playing_widget and self.current_playing_widget.station_data.get("id") == st_id:
                    num = self._get_station_display_num(self.current_playing_widget)
                    name = station_data.get("name", "")
                    if hasattr(self, 'auto_switch_cb') and self.auto_switch_cb.isChecked():
                        secs = getattr(self, 'auto_switch_interval_sec', 5)
                        self.status_label.setText(f"▶ Грає: #{num} {name} (автоперехід через {secs}с)")
                    else:
                        self.status_label.setText(f"▶ Грає: #{num} {name}")
                    
                    new_url = str(new_data.get("url") or "").strip()
                    if new_url and new_url != old_url:
                        logger.info(f"URL відтворюваної станції #{num} змінено на {new_url}. Перезапуск потоку...")
                        self.player.setSource(QUrl(new_url))
                        self.player.play()

                # Зберігаємо зміни у stations.json
                self._save_clean_stations()

                # Оновлюємо лічильник станцій
                if hasattr(self, 'country_count_badge'):
                    count = len(self.filtered_station_items)
                    self.country_count_badge.setText(f"{count} ст.")
                    self.country_count_badge.setToolTip(f"Знайдено станцій: {count} (загалом у базі: {len(self.stations)})")

                StyledMessageBox.info(
                    self,
                    "Успішно збережено",
                    f"Параметри станції «{station_data.get('name')}» успішно оновлено та збережено!"
                )
        finally:
            self._is_editing_station = False

    def _apply_deletions(self):
        to_delete = [s for s in self.stations if s.get("_marked_delete", False)]
        if not to_delete:
            StyledMessageBox.info(self, "Інформація", "Жодної станції не позначено для видалення.")
            return

        del_count = len(to_delete)
        if not StyledMessageBox.question_yes_no(
            self,
            "Підтвердження видалення",
            f"Ви дійсно хочете видалити {del_count} позначених станцій з stations.json?"
        ):
            logger.info("Користувач скасував видалення станцій.")
            return

        # Накопичуємо видалені станції у station_delete.json
        save_accumulated_deleted_stations(to_delete)

        # Безпечне видалення за ID
        delete_ids = {s.get("id") for s in to_delete if s.get("id") is not None}
        logger.info(f"Видаляємо станції з ID: {sorted(list(delete_ids))}")

        # Зупиняємо відтворення якщо видалена станція грає
        if self.current_playing_widget:
            try:
                if self.current_playing_widget.station_data.get("id") in delete_ids:
                    logger.info("Видалена станція наразі грала. Зупинка плеєра.")
                    self.player.stop()
                    self.current_playing_widget = None
                    self._reset_playback_metadata()
                    self.status_label.setText("⏹ Відтворення зупинено")
            except Exception:
                self.current_playing_widget = None

        # Фільтруємо станції
        self.stations = [s for s in self.stations if s.get("id") not in delete_ids]

        # Перенумеровуємо всі ID послідовно
        for i, s in enumerate(self.stations, 1):
            s["id"] = i

        self._save_clean_stations()
        self._save_app_state()
        self.populate_list()

        StyledMessageBox.info(
            self,
            "Успішно",
            f"Видалено {del_count} станцій (додано до station_delete.json)!\nЗалишилось станцій: {len(self.stations)}."
        )

    def _open_add_station_dialog(self):
        logger.info("Відкриття модального вікна додавання станції вручну")
        dlg = EditStationDialog({}, parent=self, is_new=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            logger.info("Користувач скасував додавання станції")
            return

        data = dlg.get_data()
        name = str(data.get("name", "")).strip()
        url = str(data.get("url", "")).strip()
        desc = str(data.get("description", "")).strip()
        country = str(data.get("country", "")).strip()
        countrycode = str(data.get("countrycode", "")).strip()
        genre = str(data.get("genre", "")).strip()
        tags = str(data.get("tags", "")).strip()
        logo = str(data.get("logo", "")).strip()
        logger.info(f"Отримано дані нової станції: назва=«{name}», URL=«{url}», країна=«{country}», жанр=«{genre}»")

        # Валідація
        if not name:
            logger.warning("Валідація не пройдена: порожня назва")
            StyledMessageBox.warning(self, "Помилка валідації", "Будь ласка, введіть назву радіостанції!")
            return

        if not url:
            logger.warning("Валідація не пройдена: порожній URL")
            StyledMessageBox.warning(self, "Помилка валідації", "Будь ласка, введіть URL станції (посилання на аудіопотік)!")
            return

        if not re.match(r'^(https?|mms|rtmp)://', url, re.IGNORECASE):
            logger.warning(f"Валідація не пройдена: некоректний протокол в URL «{url}»")
            StyledMessageBox.warning(
                self,
                "Помилка валідації",
                "Некоректний URL аудіопотоку!\nАдреса має починатися з http://, https://, mms:// або rtmp://"
            )
            return

        # Перевірка на дублікат за потоком та назвою в поточному списку
        norm_key = normalize_stream_url(url)
        norm_name = name.strip().lower()
        for s in self.stations:
            s_key = normalize_stream_url(str(s.get("url", "")))
            s_name = str(s.get("name", "")).strip().lower()
            if (norm_key and s_key == norm_key) or (norm_name and s_name == norm_name):
                logger.warning(f"Виявлено дублікат станції: #{s.get('id')} {s.get('name')}")
                StyledMessageBox.warning(
                    self,
                    "Дублікат станції",
                    f"Станція з такою назвою або адресою потоку вже існує у списку:\n#{s.get('id')} {s.get('name')}"
                )
                return

        # Перевірка у списку раніше видалених станцій (station_delete.json)
        deleted_stations = load_deleted_stations()
        for ds in deleted_stations:
            ds_key = normalize_stream_url(str(ds.get("url", "")))
            ds_name = str(ds.get("name", "")).strip().lower()
            if (norm_key and ds_key == norm_key) or (norm_name and ds_name == norm_name):
                logger.warning(f"Спроба додати раніше видалену станцію: {ds.get('name')}")
                StyledMessageBox.warning(
                    self,
                    "Раніше видалена станція",
                    f"Станція з такою назвою або адресою потоку була раніше видалена та міститься у station_delete.json:\n"
                    f"«{ds.get('name')}» ({ds.get('url')})\n\n"
                    f"Додавання скасовано, оскільки станція відмічена як видалена."
                )
                return

        new_station = {
            "id": len(self.stations) + 1,
            "name": name,
            "title": name,
            "description": desc or "Власна станція",
            "url": url,
            "country": country,
            "countrycode": countrycode,
            "genre": genre,
            "tags": tags,
            "logo": logo or "",
            "_is_imported_new": True,
            "is_new": True,
            "has_played": False,
        }

        self.stations.append(new_station)

        # Перенумеровуємо всі ID
        for i, s in enumerate(self.stations, 1):
            s["id"] = i

        self._save_clean_stations()

        # Активуємо стиль «✨ Нова вкладка»
        self.selected_genre_patterns = {"__NEW__"}
        self.selected_genre_pattern = "__NEW__"
        if hasattr(self, 'genre_btn_group') and self.genre_btn_group:
            for btn in self.genre_btn_group:
                tag = btn.property("genre_filter") or ""
                btn.setChecked(tag == "__NEW__")

        self._save_app_state()

        # Очищуємо пошук, щоб нову станцію було видно
        if self.search_input.text():
            self.search_input.blockSignals(True)
            self.search_input.clear()
            self.search_input.blockSignals(False)

        self.populate_list()
        self._filter_stations("")

        # Прокрутка вниз до нової станції
        QTimer.singleShot(150, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))

        logger.info(f"Станцію «{name}» успішно додано та збережено")
        StyledMessageBox.info(
            self,
            "Успішно додано",
            f"Станцію «{name}» успішно додано до stations.json!"
        )

    def _import_m3u_file(self):
        logger.info("=== Запуск майстра імпорту M3U / M3U8 ===")
        try:
            dialog = QFileDialog(self, "Виберіть M3U / M3U8 файл для імпорту", str(APP_DIR))
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setNameFilters(["Плейлисти M3U (*.m3u *.m3u8)", "Текстові файли (*.txt)", "Усі файли (*)"])
            dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
            dialog.setStyleSheet("""
                QFileDialog {
                    background-color: #1e1f22;
                    color: #ffffff;
                }
                QLabel {
                    color: #ffffff;
                    font-size: 18px;
                }
                QLineEdit {
                    background-color: #2b2d30;
                    color: #ffffff;
                    border: 1px solid #3c3f41;
                    border-radius: 6px;
                    padding: 6px 10px;
                    font-size: 18px;
                }
                QTreeView, QListView {
                    background-color: #18191c;
                    color: #ffffff;
                    border: 1px solid #3c3f41;
                    border-radius: 6px;
                    font-size: 18px;
                    selection-background-color: #0057b8;
                    selection-color: #ffffff;
                }
                QHeaderView::section {
                    background-color: #22252a;
                    color: #8b949e;
                    font-size: 17px;
                    padding: 5px;
                    border: 1px solid #30363d;
                }
                QPushButton {
                    background-color: #2b2d30;
                    color: #ffffff;
                    font-size: 18px;
                    font-weight: bold;
                    border: 1px solid #3c3f41;
                    border-radius: 6px;
                    padding: 6px 18px;
                }
                QPushButton:hover {
                    background-color: #35383c;
                    border-color: #58a6ff;
                }
                QComboBox {
                    background-color: #2b2d30;
                    color: #ffffff;
                    border: 1px solid #3c3f41;
                    border-radius: 6px;
                    padding: 5px 10px;
                    font-size: 17px;
                }
            """)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                logger.info("Вибір файлу M3U скасовано користувачем.")
                return

            selected = dialog.selectedFiles()
            if not selected:
                logger.warning("Файл не вибрано.")
                return
            file_path = selected[0]

            # Вікно налаштування фільтрів перед імпортом
            curr_country_idx = self.country_combo.currentIndex() if hasattr(self, 'country_combo') else 0
            curr_genre_pat = getattr(self, "selected_genre_pattern", "")
            opts_dialog = ImportOptionsDialog(
                Path(file_path).name,
                preselected_country_idx=curr_country_idx,
                preselected_genre_pattern=curr_genre_pat,
                parent=self
            )
            if not opts_dialog.exec():
                logger.info("Імпорт скасовано користувачем у вікні параметрів фільтрації.")
                return

            import_filters = opts_dialog.get_selected_filters()
            self._process_m3u_import(file_path, import_filters=import_filters)
        except Exception as e:
            logger.exception(f"Критична помилка під час виконання імпорту M3U: {e}")
            StyledMessageBox.critical(
                self,
                "Помилка імпорту",
                f"Сталася помилка під час імпорту файлу M3U:\n{e}\n\nПовний стек помилки записано у radio_manager.log"
            )

    def _process_m3u_import(self, file_path, import_filters=None):
        try:
            if import_filters is None:
                import_filters = {
                    "country_code": "",
                    "country_pattern": "",
                    "country_label": "🏳️ Усі країни",
                    "country_index": 0,
                    "genre_pattern": "",
                    "genre_label": "Усі жанри"
                }

            logger.info(f"Обробка файлу M3U: {file_path}, фільтр: {import_filters.get('country_label')} / {import_filters.get('genre_label')}")
            path = Path(file_path)
            if not path.exists():
                logger.error(f"Файл не існує: {file_path}")
                StyledMessageBox.warning(self, "Помилка", f"Файл не знайдено:\n{file_path}")
                return

            file_size = path.stat().st_size
            logger.info(f"Розмір файлу M3U: {file_size} байт")
            if file_size == 0:
                logger.warning("Файл має нульовий розмір (0 байт)")
                StyledMessageBox.warning(self, "Помилка", "Вибраний файл порожній (0 байт).")
                return

            # Відкриваємо вікно прогресу ОДРАЗУ, щоб користувач бачив усі етапи
            progress_dialog = ImportProgressDialog(100, parent=self)
            progress_dialog.show()
            QApplication.processEvents()

            # ЕТАП 1: Зчитування та визначення кодування
            progress_dialog.set_stage("1/4. Зчитування та декодування файлу", f"Файл: {path.name} ({max(1, file_size // 1024)} КБ)")
            progress_dialog.set_progress(10, f"Визначення кодування: {path.name}...")
            QApplication.processEvents()

            content = None
            detected_encoding = None
            encodings_to_test = ("utf-8", "utf-8-sig", "cp1251", "windows-1251", "latin-1")
            for enc in encodings_to_test:
                if progress_dialog.is_cancelled:
                    break
                try:
                    with open(path, "r", encoding=enc) as f:
                        content = f.read()
                    detected_encoding = enc
                    logger.info(f"Файл успішно декодовано за допомогою кодування: {enc} ({len(content)} символів)")
                    break
                except UnicodeDecodeError:
                    logger.debug(f"Кодування {enc} не підійшло (UnicodeDecodeError)")
                except Exception as fe:
                    logger.debug(f"Помилка при читанні у кодуванні {enc}: {fe}")

            if progress_dialog.is_cancelled:
                progress_dialog.close()
                StyledMessageBox.warning(
                    self,
                    "Імпорт скасовано",
                    "Імпорт файлу було перервано користувачем.\nЖодних змін до списку радіостанцій не внесено."
                )
                return

            if content is None:
                progress_dialog.close()
                logger.error(f"Не вдалося декодувати файл {file_path} жодним з кодувань: {encodings_to_test}")
                StyledMessageBox.warning(self, "Помилка", "Не вдалося прочитати вміст файлу у підтримуваному кодуванні.")
                return

            progress_dialog.set_progress(100, f"Декодовано ({detected_encoding}, {len(content)} симв.)")
            QApplication.processEvents()

            lines = content.splitlines()
            logger.info(f"Файл розбито на {len(lines)} рядків.")

            # ЕТАП 2: Парсинг рядків M3U
            progress_dialog.set_stage("2/4. Парсинг структури плейлиста", f"Всього рядків для аналізу: {len(lines)}")
            progress_dialog.set_range(0, max(1, len(lines)))

            existing_stream_keys = {
                normalize_stream_url(str(s.get("url", "")))
                for s in self.stations if s.get("url")
            }
            existing_names = {
                str(s.get("name", "")).strip().lower()
                for s in self.stations if s.get("name")
            }
            logger.debug(f"Кількість наявних унікальних потоків перед імпортом: {len(existing_stream_keys)}")

            # Завантажуємо раніше видалені станції (station_delete.json) для фільтрації
            deleted_stations = load_deleted_stations()
            deleted_stream_keys = {
                normalize_stream_url(str(s.get("url", "")))
                for s in deleted_stations if s.get("url")
            }
            deleted_names = {
                str(s.get("name", "")).strip().lower()
                for s in deleted_stations if s.get("name")
            }
            logger.info(f"Завантажено {len(deleted_stations)} раніше видалених станцій з station_delete.json для перевірки дублікатів")

            parsed_items = []
            current_title = None
            current_logo = ""
            current_group = ""
            current_country = ""
            extinf_count = 0
            stream_count = 0

            for line_no, line in enumerate(lines, 1):
                if progress_dialog.is_cancelled:
                    logger.info("Імпорт перервано користувачем під час парсингу M3U.")
                    break

                line_str = line.strip()
                if not line_str:
                    continue

                if line_str.startswith("#EXTINF:"):
                    extinf_count += 1
                    try:
                        # Extract tvg-logo
                        logo_match = re.search(r'tvg-logo=["\']([^"\']+)["\']', line_str, re.IGNORECASE)
                        if not logo_match:
                            logo_match = re.search(r'tvg-logo=([^\s,]+)', line_str, re.IGNORECASE)
                        if logo_match:
                            current_logo = logo_match.group(1).strip()

                        # Extract tvg-country
                        country_match = re.search(r'tvg-country=["\']([^"\']+)["\']', line_str, re.IGNORECASE)
                        if not country_match:
                            country_match = re.search(r'tvg-country=([^\s,]+)', line_str, re.IGNORECASE)
                        if country_match:
                            current_country = country_match.group(1).strip()

                        # Extract group-title
                        group_match = re.search(r'group-title=["\']([^"\']+)["\']', line_str, re.IGNORECASE)
                        if not group_match:
                            group_match = re.search(r'group-title=([^\s,]+)', line_str, re.IGNORECASE)
                        if group_match:
                            current_group = group_match.group(1).strip()

                        # Extract title after the comma
                        if "," in line_str:
                            current_title = line_str.split(",", 1)[1].strip()
                        else:
                            current_title = None
                    except Exception as pe:
                        logger.warning(f"Рядок {line_no}: помилка розбору тегу #EXTINF: {pe}")
                    continue

                if line_str.startswith("#"):
                    # Інші службові теги M3U (#EXTM3U, #EXTVLCOPT тощо)
                    continue

                # Перевірка чи є рядок адресою потоку
                if re.match(r'^(https?|mms|rtmp)://', line_str, re.IGNORECASE):
                    stream_count += 1
                    url = line_str
                    title = current_title
                    if not title:
                        try:
                            parsed = urllib.parse.urlparse(url)
                            path_part = parsed.path.strip("/").split("/")[-1]
                            title = path_part if path_part else parsed.netloc
                        except Exception:
                            title = f"Станція #{stream_count}"

                    desc = current_group if current_group and current_group.lower() not in ("m3u імпорт", "m3u import", "m3u") else ""

                    parsed_items.append({
                        "name": str(title),
                        "description": str(desc),
                        "url": str(url),
                        "logo": str(current_logo),
                        "country": str(current_country),
                        "countrycode": str(current_country),
                        "genre": str(current_group)
                    })

                    current_title = None
                    current_logo = ""
                    current_group = ""
                    current_country = ""

                # Регулярно оновлюємо вікно прогресу під час парсингу
                if (line_no % 35 == 0) or (line_no == len(lines)):
                    progress_dialog.set_progress(
                        line_no,
                        f"Рядок {line_no}/{len(lines)}: знайдено {len(parsed_items)} станцій...",
                        f"Розпізнано аудіопотоків: {len(parsed_items)} | Тегів: {extinf_count}"
                    )

            if progress_dialog.is_cancelled:
                progress_dialog.close()
                StyledMessageBox.warning(
                    self,
                    "Імпорт скасовано",
                    "Імпорт файлу було перервано користувачем.\nЖодних змін до списку радіостанцій не внесено."
                )
                return

            logger.info(f"Знайдено #EXTINF тегів: {extinf_count}, розпізнано аудіопотоків: {len(parsed_items)}")

            if not parsed_items:
                progress_dialog.close()
                logger.warning(f"У файлі {file_path} не знайдено жодного потоку!")
                StyledMessageBox.info(
                    self,
                    "Імпорт M3U",
                    "У вибраному файлі не знайдено аудіопотоків (посилань на радіостанції)."
                )
                return

            # ЕТАП 3: Фільтрація за країною/жанром та перевірка дублікатів
            progress_dialog.set_stage("3/4. Фільтрація та перевірка дублікатів", "Застосування фільтрів та звірка з базою")
            progress_dialog.set_range(0, max(1, len(parsed_items)))

            new_stations = []
            duplicate_count = 0
            filtered_by_filter_count = 0
            seen_imported_keys = set()
            seen_imported_names = set()

            f_country_code = import_filters.get("country_code", "")
            f_country_pat = import_filters.get("country_pattern", "")
            f_genre_pat = import_filters.get("genre_pattern", "")

            for idx, item in enumerate(parsed_items):
                if progress_dialog.is_cancelled:
                    logger.info("Імпорт перервано користувачем.")
                    break

                # 1. Перевірка відповідності фільтру країни
                if not match_station_country(item, f_country_code, f_country_pat):
                    filtered_by_filter_count += 1
                    if (idx % 10 == 0) or (idx == len(parsed_items) - 1):
                        progress_dialog.update_progress(
                            idx + 1, item["name"], len(new_stations), duplicate_count, filtered_by_filter_count
                        )
                    continue

                # 2. Перевірка відповідності фільтру жанру
                if not match_station_genre(item, f_genre_pat):
                    filtered_by_filter_count += 1
                    if (idx % 10 == 0) or (idx == len(parsed_items) - 1):
                        progress_dialog.update_progress(
                            idx + 1, item["name"], len(new_stations), duplicate_count, filtered_by_filter_count
                        )
                    continue

                # 3. Перевірка дублікатів
                stream_k = normalize_stream_url(item["url"])
                name_k = item["name"].strip().lower()

                is_in_stations = (stream_k and stream_k in existing_stream_keys) or \
                                 (name_k and name_k in existing_names)
                is_in_deleted = (stream_k and stream_k in deleted_stream_keys) or \
                                (name_k and name_k in deleted_names)
                is_in_imported = (stream_k and stream_k in seen_imported_keys) or \
                                 (name_k and name_k in seen_imported_names)

                if is_in_stations or is_in_deleted or is_in_imported:
                    duplicate_count += 1
                else:
                    if stream_k:
                        seen_imported_keys.add(stream_k)
                    if name_k:
                        seen_imported_names.add(name_k)
                    item["_is_imported_new"] = True
                    new_stations.append(item)

                if (idx % 5 == 0) or (idx == len(parsed_items) - 1):
                    progress_dialog.update_progress(
                        idx + 1, item["name"], len(new_stations), duplicate_count, filtered_by_filter_count
                    )

            if progress_dialog.is_cancelled:
                progress_dialog.close()
                StyledMessageBox.warning(
                    self,
                    "Імпорт скасовано",
                    "Імпорт файлу було перервано користувачем.\nЖодних змін до списку радіостанцій не внесено."
                )
                return

            logger.info(
                f"Результат обробки: нових = {len(new_stations)}, "
                f"відсіяно фільтром = {filtered_by_filter_count}, дублікатів = {duplicate_count}"
            )

            if not new_stations:
                progress_dialog.close()
                logger.info("Жодної нової станції не додано (відсіяно фільтром або вже наявні).")
                filter_info = f"Країна: {import_filters['country_label']}"
                if import_filters.get('genre_label') and import_filters['genre_label'] != "Усі жанри":
                    filter_info += f", Жанр: {import_filters['genre_label']}"
                StyledMessageBox.info(
                    self,
                    "Імпорт M3U — Результат",
                    f"Аналіз файлу завершено!\n\n"
                    f"• Фільтр імпорту: {filter_info}\n"
                    f"• Всього перевірено станцій: {len(parsed_items)}\n"
                    f"• Відсіяно фільтром (інші країни/жанри): {filtered_by_filter_count}\n"
                    f"• Відсіяно дублікатів (наявні або раніше видалені): {duplicate_count}\n"
                    f"• Нових станцій для додавання: 0\n\n"
                    f"Всі станції, що відповідали обраному фільтру, вже є у вашому списку або були раніше видалені."
                )
                return

            # ЕТАП 4: Додавання нових станцій, збереження та оновлення сітки
            progress_dialog.set_stage("4/4. Оновлення списку станцій", f"Додавання {len(new_stations)} нових станцій")
            progress_dialog.set_progress(len(parsed_items), "Збереження stations.json та побудова карток...")
            QApplication.processEvents()

            for item in new_stations:
                item["_is_imported_new"] = True
                item["is_new"] = True
                item["has_played"] = False
                item.pop("_has_played", None)

            logger.info(f"Додавання {len(new_stations)} нових станцій до списку...")
            self.stations.extend(new_stations)

            # Перенумеровуємо ID
            for i, s in enumerate(self.stations, 1):
                s["id"] = i

            # Зберігаємо
            self._save_clean_stations()

            # Автоматично перемикаємо на стиль «✨ Нова вкладка»
            self.selected_genre_patterns = {"__NEW__"}
            self.selected_genre_pattern = "__NEW__"
            if hasattr(self, 'genre_btn_group') and self.genre_btn_group:
                for btn in self.genre_btn_group:
                    tag = btn.property("genre_filter") or ""
                    btn.setChecked(tag == "__NEW__")

            self._save_app_state()

            # Очищуємо поле пошуку перед рендером, щоб не сховати додані станції
            if self.search_input.text():
                logger.debug("Скидання тексту пошуку перед оновленням сітки")
                self.search_input.blockSignals(True)
                self.search_input.clear()
                self.search_input.blockSignals(False)

            # Синхронізуємо фільтр країни у вікні, якщо було обрано конкретну країну для імпорту
            imp_code = import_filters.get("country_code", "")
            if imp_code:
                self.selected_country_codes = {imp_code}
            if hasattr(self, '_update_country_data'):
                self._update_country_data()
            logger.info("Оновлення списку станцій у вікні...")
            self.populate_list()

            progress_dialog.close()

            # Прокручуємо до нових станцій
            QTimer.singleShot(150, lambda: self.scroll_area.verticalScrollBar().setValue(
                self.scroll_area.verticalScrollBar().maximum()
            ))

            filter_info = f"Країна: {import_filters['country_label']}"
            if import_filters.get('genre_label') and import_filters['genre_label'] != "Усі жанри":
                filter_info += f", Жанр: {import_filters['genre_label']}"

            msg = f"Аналіз та імпорт успішно завершено!\n\n" \
                  f"• Фільтр імпорту: {filter_info}\n" \
                  f"• Всього станцій у файлі: {len(parsed_items)}\n" \
                  f"• Відсіяно фільтром: {filtered_by_filter_count}\n" \
                  f"• Відсіяно дублікатів: {duplicate_count}\n" \
                  f"• Додано нових станцій: {len(new_stations)}\n\n" \
                  f"✨ Всі нові станції підсвічені зеленим кольором та відкриті у стилі «✨ Нова вкладка».\n" \
                  f"Ця вкладка залишається постійно відкритою, а стан карток надійно збережено."

            logger.info("=== Імпорт файлу M3U успішно завершено ===")
            StyledMessageBox.info(self, "Імпорт завершено", msg)

        except Exception as e:
            logger.exception(f"Критична помилка під час виконання імпорту M3U: {e}")
            StyledMessageBox.critical(
                self,
                "Помилка імпорту",
                f"Сталася помилка під час імпорту файлу M3U:\n{e}\n\nПовний стек помилки записано у radio_manager.log"
            )

    def closeEvent(self, event):
        logger.info("Закриття додатку Radio Manager")
        try:
            self._save_app_state()
            self._save_clean_stations()
        except Exception as e:
            logger.warning(f"Помилка при збереженні стану перед закриттям: {e}")
        try:
            if hasattr(self, 'load_timeout_timer'):
                self.load_timeout_timer.stop()
        except Exception:
            pass
        try:
            self.player.stop()
        except Exception as e:
            logger.warning(f"Помилка при зупинці плеєра: {e}")
        try:
            if hasattr(self, 'stderr_interceptor') and self.stderr_interceptor:
                self.stderr_interceptor.stop()
        except Exception as e:
            logger.debug(f"Помилка при зупинці stderr interceptor: {e}")
        super().closeEvent(event)


def main():
    logger.info("=" * 60)
    logger.info("Запуск додатку Radio Manager (RadioJS)")
    logger.info(f"Python версія: {sys.version}")
    logger.info(f"Шлях до файлу: {__file__}")
    logger.info(f"Робочий каталог: {APP_DIR}")
    logger.info(f"Файл stations.json: {JSON_PATH} (існує: {JSON_PATH.exists()})")
    logger.info(f"Файл журналу: {LOG_PATH}")
    logger.info("=" * 60)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Масштабування базового системного шрифту додатку на +20%
    app_font = app.font()
    pt = app_font.pointSize()
    if pt > 0:
        app_font.setPointSize(int(round(pt * 1.2)))
    elif app_font.pixelSize() > 0:
        app_font.setPixelSize(int(round(app_font.pixelSize() * 1.2)))
    app.setFont(app_font)

    # Global Dark Palette
    dark_palette = QPalette()
    dark_palette.setColor(QPalette.ColorRole.Window, QColor(30, 31, 34))
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor(240, 246, 252))
    dark_palette.setColor(QPalette.ColorRole.Base, QColor(43, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(30, 31, 34))
    dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(43, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(240, 246, 252))
    dark_palette.setColor(QPalette.ColorRole.Text, QColor(240, 246, 252))
    dark_palette.setColor(QPalette.ColorRole.Button, QColor(43, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor(240, 246, 252))
    dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 87, 184))
    dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(dark_palette)

    window = RadioManagerWindow()
    window.show()
    exit_code = app.exec()
    logger.info(f"Роботу додатку завершено з кодом: {exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
