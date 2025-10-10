from flask import Flask, request, jsonify, send_file
import yt_dlp
import time
import threading
import os
import uuid
from pathlib import Path
from waitress import serve
import logging
from logging.handlers import RotatingFileHandler

# --- Конфигурация ---
app = Flask(__name__)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

active_downloads = []  # [{session_id, url, filename, created_at, formats_map, best_audio}]

# --- Логирование в файл и консоль ---
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
file_handler = RotatingFileHandler("server.log", maxBytes=1_000_000, backupCount=3)
file_handler.setFormatter(log_formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger(__name__)


# --- Вспомогательные функции ---
def cleanup_expired_sessions():
    """Удаляет старые записи (>30 мин)."""
    while True:
        now = time.time()
        for record in active_downloads[:]:
            if now - record["created_at"] > 1800:  # 30 минут
                if record.get("filename"):
                    path = os.path.join(DOWNLOAD_DIR, record["filename"])
                    if os.path.exists(path):
                        os.remove(path)
                        logger.info(f"[AUTO] Удалён файл {record['filename']} (сессия {record['session_id']})")
                try:
                    active_downloads.remove(record)
                except ValueError:
                    pass
                logger.info(f"[AUTO] Очистка сессии {record['session_id']}")
        time.sleep(300)


def find_download(session_id):
    for entry in active_downloads:
        if entry["session_id"] == session_id:
            return entry
    return None


def build_formats_map(info):
    formats_map = {}
    best_audio = None
    best_audio_abr = 0

    # ищем лучший аудио поток
    for f in info.get("formats", []):
        if f.get("acodec") and f.get("vcodec") in (None, "none"):
            abr = f.get("abr") or f.get("tbr") or 0
            if abr > best_audio_abr:
                best_audio_abr = abr
                best_audio = f.get("format_id")

    for f in info.get("formats", []):
        if f.get("vcodec") == "none":
            continue

        width = f.get("width") or 0
        height = f.get("height") or 0
        if not width or not height:
            continue

        display_height = min(width, height)

        # Ограничение: не выше 1080p
        if display_height > 1080:
            continue

        fps = int(f.get("fps") or 0)
        fps_suffix = f"{fps}" if fps >= 50 else ""
        label = f"{display_height}p{fps_suffix}".strip()

        entry = formats_map.setdefault(label, {"combined": None, "video": None, "height": display_height, "v_tbr": 0})
        if f.get("vcodec") != "none" and f.get("acodec") != "none":
            entry["combined"] = f.get("format_id")
        elif f.get("vcodec") != "none":
            tbr = f.get("tbr") or 0
            if tbr >= entry.get("v_tbr", 0):
                entry["video"] = f.get("format_id")
                entry["v_tbr"] = tbr

    labels = sorted(formats_map.keys(), key=lambda x: int(''.join(filter(str.isdigit, x)) or 0))
    return labels, formats_map, best_audio


def download_and_find_file(url, format_selector, file_id):
    """
    Запускает yt_dlp с format_selector и возвращает фактический путь к скачанному файлу.
    Ищем файл по шаблону file_id.* в DOWNLOAD_DIR.
    """
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    ydl_opts = {
        "format": format_selector,
        "quiet": True,
        "outtmpl": str(output_template),
        "merge_output_format": "mp4",
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
        "postprocessor_args": [
            "-c:v", "copy",
            "-c:a", "aac",
            "-movflags", "+faststart"
        ],
        "concurrent_fragment_downloads": 3,
        "retries": 10,
        "fragment_retries": 10,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        # найти созданный файл
        candidates = list(Path(DOWNLOAD_DIR).glob(f"{file_id}.*"))
        if not candidates:
            logger.error("После загрузки файл не найден по шаблону %s.*", file_id)
            return None
        # если есть несколько — взять первый
        return str(candidates[0])
    except Exception as e:
        logger.error("yt_dlp failed: %s", e)
        return None


# --- API ---
@app.route("/get_formats", methods=["POST"])
def get_formats():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL не передан"}), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        labels, fmt_map, best_audio = build_formats_map(info)

        if not labels:
            return jsonify({"error": "Не удалось определить форматы"}), 500

        session_id = str(uuid.uuid4())
        active_downloads.append({
            "session_id": session_id,
            "url": url,
            "filename": None,
            "created_at": time.time(),
            "formats_map": fmt_map,
            "labels": labels,
            "best_audio": best_audio
        })

        logger.info("Создана сессия %s для %s — форматов: %s", session_id, url, labels)
        return jsonify({"session_id": session_id, "available_formats": labels})

    except Exception as e:
        logger.exception("Ошибка при получении форматов")
        return jsonify({"error": str(e)}), 500


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json() or {}
    session_id = data.get("session_id")
    selected_label = data.get("quality")
    if not session_id or not selected_label:
        return jsonify({"error": "Параметры отсутствуют"}), 400

    record = find_download(session_id)
    if not record:
        return jsonify({"error": "Сессия не найдена"}), 404

    fmt_map = record.get("formats_map") or {}
    entry = fmt_map.get(selected_label)
    if not entry:
        # safety: try nearest available resolution
        labels = record.get("labels", [])
        if not labels:
            return jsonify({"error": "Форматы для сессии не найдены"}), 500
        # pick nearest by absolute difference
        try:
            target_h = int(selected_label.replace("p", ""))
            nearest = min(labels, key=lambda x: abs(int(x.replace("p", "")) - target_h))
            logger.warning("Запрошенное качество %s отсутствует — выбрано ближайшее %s", selected_label, nearest)
            entry = fmt_map.get(nearest)
        except Exception:
            return jsonify({"error": "Неправильный формат качества"}), 400

    format_selector = None
    if entry.get("combined"):
        format_selector = entry["combined"]
        logger.info("Используется комбинированный формат %s для %s", format_selector, selected_label)
    else:
        best_audio = record.get("best_audio")
        if entry.get("video") and best_audio:
            format_selector = f"{entry['video']}+{best_audio}"
            logger.info("Собираем video_id+best_audio: %s", format_selector)
        elif entry.get("video"):
            # fallback: try height-based selector
            h = entry.get("height")
            format_selector = f"bestvideo[height<={h}]+bestaudio/best"
            logger.info("Fallback: используем селектор %s", format_selector)
        else:
            logger.error("Не удалось подобрать формат для %s", selected_label)
            return jsonify({"error": "Невозможно подобрать формат для скачивания"}), 500

    # выполняем скачивание
    file_id = str(uuid.uuid4())
    filepath = download_and_find_file(record["url"], format_selector, file_id)
    if not filepath:
        return jsonify({"error": "Ошибка при скачивании"}), 500

    record["filename"] = os.path.basename(filepath)
    logger.info("Видео скачано: %s (сессия %s)", record["filename"], session_id)
    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


@app.route("/cleanup", methods=["POST"])
def cleanup():
    data = request.get_json() or {}
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id обязателен"}), 400

    record = find_download(session_id)
    if not record:
        return jsonify({"error": "Сессия не найдена"}), 404

    if record.get("filename"):
        path = os.path.join(DOWNLOAD_DIR, record["filename"])
        if os.path.exists(path):
            os.remove(path)
            logger.info("Файл %s удалён (сессия %s)", record["filename"], session_id)
        else:
            logger.warning("Файл %s не найден при очистке (сессия %s)", record["filename"], session_id)

    try:
        active_downloads.remove(record)
    except ValueError:
        pass
    return jsonify({"status": "deleted", "session_id": session_id})


# --- Запуск ---
if __name__ == "__main__":
    threading.Thread(target=cleanup_expired_sessions, daemon=True).start()
    logger.info("Сервер запущен и ожидает запросы")
    serve(app, host="192.168.0.101", port=3306)
