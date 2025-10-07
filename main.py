from flask import Flask, request, jsonify, send_file
import yt_dlp
import threading
import os
import uuid
import time
import logging

# --- Настройки приложения ---
app = Flask(__name__)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

TTL = 1800  # 30 минут
active_downloads = {}

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,  # лог в терминал
    format="%(asctime)s [%(levelname)s] %(message)s",
)

file_handler = logging.FileHandler("server.log")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(file_handler)


# --- Очистка старых файлов ---
def cleanup_old_files():
    while True:
        now = time.time()
        expired = []
        for file_id, data in list(active_downloads.items()):
            if now - data["timestamp"] > TTL:
                try:
                    os.remove(data["path"])
                    logging.info(f"Удалён просроченный файл: {data['path']}")
                except Exception as e:
                    logging.warning(f"Ошибка при удалении файла: {e}")
                expired.append(file_id)
        for f in expired:
            del active_downloads[f]
        time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


# --- Получение списка доступных разрешений ---
@app.route("/api/formats", methods=["POST"])
def get_formats():
    data = request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        if "youtube.com" in url or "youtu.be" in url:
            if "v=" in url:
                url = url.split("v=")[-1].split("&")[0]
            elif "youtu.be/" in url:
                url = url.split("youtu.be/")[-1].split("?")[0]
            url = f"https://www.youtube.com/watch?v={url}"
    except Exception as e:
        logging.warning(f"Не удалось корректно обработать ссылку: {url} ({e})")

    try:
        ydl_opts = {"quiet": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        seen = set()

        for f in info.get("formats", []):
            # берём только реальные видеоформаты с высотой и кодеком
            height = f.get("height")
            fmt_note = f.get("format_note")
            if f.get("vcodec") != "none" and height and fmt_note:
                quality = f"{height}p ({fmt_note})"
                if quality not in seen:
                    formats.append(quality)
                    seen.add(quality)

        logging.info(f"Запрошен список форматов для {url}: {formats}")
        return jsonify(formats)

    except Exception as e:
        logging.error(f"Ошибка при получении форматов: {e}")
        return jsonify({"error": str(e)}), 500



# --- Загрузка видео ---
@app.route("/api/download", methods=["POST"])
def download_video():
    data = request.get_json()
    url = data.get("url")
    format_choice = data.get("format_id") or data.get("quality")

    if not url or not format_choice:
        return jsonify({"error": "Missing parameters"}), 400

    # Преобразуем короткую ссылку
    try:
        if "youtu.be/" in url:
            video_id = url.split("youtu.be/")[-1].split("?")[0]
            url = f"https://www.youtube.com/watch?v={video_id}"
        elif "youtube.com/watch" in url and "v=" in url:
            video_id = url.split("v=")[-1].split("&")[0]
            url = f"https://www.youtube.com/watch?v={video_id}"
    except Exception as e:
        logging.warning(f"Не удалось обработать ссылку: {url} ({e})")

    try:
        # Парсим формат, если указан в виде "720p (hd720)"
        format_choice = format_choice.split()[0] if " " in format_choice else format_choice

        if "p" in format_choice:
            with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            found_format = None
            for f in info["formats"]:
                if (
                    f.get("height")
                    and f"{f['height']}p" == format_choice
                    and f.get("vcodec") != "none"
                ):
                    found_format = f["format_id"]
                    break

            if not found_format:
                found_format = f"bestvideo[height<={format_choice.replace('p','')}]+bestaudio/best"
                logging.warning(f"Точный формат не найден, выбран автоформат: {found_format}")

            format_choice = found_format

        file_id = str(uuid.uuid4())
        output_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

        ydl_opts = {
            "quiet": False,
            "format": format_choice,
            "merge_output_format": "mp4",
            "outtmpl": output_path,
        }

        logging.info(f"Начало загрузки {url} с качеством {format_choice}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            final_ext = info_dict.get("ext", "mp4")
            final_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.{final_ext}")

        active_downloads[file_id] = {"path": final_path, "timestamp": time.time()}
        download_url = f"http://{request.host}/api/file/{file_id}"

        logging.info(f"Файл скачан: {final_path}")
        return jsonify({"download_url": download_url, "file_id": file_id})

    except Exception as e:
        logging.error(f"Ошибка при загрузке видео: {e}")
        return jsonify({"error": str(e)}), 500



# --- Отдача файла клиенту ---
@app.route("/api/file/<file_id>")
def serve_file(file_id):
    entry = active_downloads.get(file_id)
    if not entry or not os.path.exists(entry["path"]):
        logging.warning(f"Файл не найден: {file_id}")
        return jsonify({"error": "File not found"}), 404
    logging.info(f"Файл отдан клиенту: {file_id}")
    return send_file(entry["path"], as_attachment=True)


@app.route("/api/status", methods=["POST"])
def api_status():
    url = request.args.get("url")
    status = "POST"
    if not url:
        return jsonify({"error": "Missing URL parameter"}), 400

    # --- Извлекаем file_id из ссылки ---
    try:
        file_id = url.strip().split("/")[-1]
    except Exception:
        return jsonify({"error": "Invalid URL format"}), 400

    entry = active_downloads.get(file_id)
    if not entry:
        return jsonify({"error": "File not found or already deleted"}), 404

    path = entry["path"]

    # --- Удаление файла ---
    try:
        os.remove(path)
        del active_downloads[file_id]
        logging.info(f"Файл {file_id} удалён (запрос типа {status})")
        return jsonify({"message": f"Файл удалён (запрос типа {status})"}), 200
    except Exception as e:
        logging.warning(f"Ошибка при удалении {file_id}: {e}")
        return jsonify({"error": f"Ошибка при удалении файла: {e}"}), 500


if __name__ == "__main__":
    app.run(host="172.20.10.2", port=8080)
