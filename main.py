from flask import Flask, request, jsonify, send_file
import yt_dlp
import threading
import os
import uuid
import time
import logging
from logging.handlers import RotatingFileHandler

def create_app():
    app = Flask(__name__)
    DOWNLOAD_DIR = "downloads"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    TTL = 1800
    active_downloads = {}

    LOG_FILE = "server.log"
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
    logger = logging.getLogger(__name__)

    def cleanup_old_files():
        while True:
            now = time.time()
            expired = [
                fid for fid, data in list(active_downloads.items())
                if now - data["timestamp"] > TTL
            ]
            for fid in expired:
                try:
                    os.remove(active_downloads[fid]["path"])
                    logger.info(f"Удалён просроченный файл: {active_downloads[fid]['path']}")
                except Exception as e:
                    logger.warning(f"Ошибка при удалении {fid}: {e}")
                active_downloads.pop(fid, None)
            time.sleep(60)

    threading.Thread(target=cleanup_old_files, daemon=True).start()

    def normalize_youtube_url(url: str) -> str:
        try:
            if "youtu.be/" in url:
                video_id = url.split("youtu.be/")[-1].split("?")[0]
            elif "youtube.com/watch" in url and "v=" in url:
                video_id = url.split("v=")[-1].split("&")[0]
            else:
                return url
            return f"https://www.youtube.com/watch?v={video_id}"
        except Exception:
            return url

    @app.route("/api/formats", methods=["POST"])
    def get_formats():
        data = request.get_json() or {}
        url = normalize_youtube_url(data.get("url", ""))

        if not url:
            return jsonify({"error": "No URL provided"}), 400

        try:
            with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                info = ydl.extract_info(url, download=False)

            formats = []
            seen = set()

            for f in info.get("formats", []):
                if f.get("vcodec") != "none" and f.get("height") and f.get("format_note"):
                    if f['format_note'] == "(default)":
                        continue
                    label = f"{f['height']}p ({f['format_note']})"
                    if label not in seen:
                        formats.append(label)
                        seen.add(label)

            logger.info(f"Форматы для {url}: {formats}")
            return jsonify(formats or ["Не удалось определить форматы"])

        except Exception as e:
            logger.error(f"Ошибка при получении форматов: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/download", methods=["POST"])
    def download_video():
        data = request.get_json() or {}
        url = normalize_youtube_url(data.get("url", ""))
        format_choice = (data.get("format_id") or data.get("quality", "")).strip()

        if not url or not format_choice:
            return jsonify({"error": "Missing parameters"}), 400

        try:
            format_choice = format_choice.split()[0] if " " in format_choice else format_choice

            if "p" in format_choice:
                with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                    info = ydl.extract_info(url, download=False)

                found_format = next(
                    (f["format_id"] for f in info["formats"]
                     if f.get("height") and f"{f['height']}p" == format_choice and f.get("vcodec") != "none"),
                    None
                )

                if not found_format:
                    found_format = f"bestvideo[height<={format_choice.replace('p','')}]+bestaudio/best"
                    logger.warning(f"Точный формат не найден, выбран автоформат: {found_format}")

                format_choice = found_format

            file_id = str(uuid.uuid4())
            output_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

            ydl_opts = {
                "quiet": True,
                "format": format_choice,
                "merge_output_format": "mp4",
                "outtmpl": output_path,
            }

            logger.info(f"Начало загрузки: {url} (качество {format_choice})")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(url, download=True)
                final_ext = info_dict.get("ext", "mp4")

            final_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.{final_ext}")
            active_downloads[file_id] = {"path": final_path, "timestamp": time.time()}

            download_url = f"http://{request.host}/api/file/{file_id}"
            logger.info(f"Видео скачано: {final_path}")
            return jsonify({"download_url": download_url, "file_id": file_id})

        except Exception as e:
            logger.error(f"Ошибка при загрузке видео: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/file/<file_id>")
    def serve_file(file_id):
        entry = active_downloads.get(file_id)
        if not entry or not os.path.exists(entry["path"]):
            logger.warning(f"Файл не найден: {file_id}")
            return jsonify({"error": "File not found"}), 404

        logger.info(f"Файл отдан клиенту: {file_id}")
        return send_file(entry["path"], as_attachment=True)

    @app.route("/api/status", methods=["POST"])
    def api_status():
        data = request.get_json() or {}
        url = data.get("url")
        if not url:
            return jsonify({"error": "Missing URL parameter"}), 400

        try:
            file_id = url.strip().split("/")[-1]
        except Exception:
            return jsonify({"error": "Invalid URL format"}), 400

        entry = active_downloads.get(file_id)
        if not entry:
            return jsonify({"error": "File not found or already deleted"}), 404

        try:
            os.remove(entry["path"])
            del active_downloads[file_id]
            logger.info(f"Файл {file_id} удалён по POST-запросу")
            return jsonify({"message": "Файл удалён"}), 200
        except Exception as e:
            logger.warning(f"Ошибка при удалении {file_id}: {e}")
            return jsonify({"error": f"Ошибка при удалении: {e}"}), 500

    return app


# --- Для локального теста ---
if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5000)
