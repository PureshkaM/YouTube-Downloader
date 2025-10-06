from flask import Flask, request, jsonify, send_from_directory
import yt_dlp
import tempfile
import os
import threading
import time

app = Flask(__name__)

TEMP_DIR = tempfile.gettempdir()
FILES_TTL = 600  # хранить файлы 10 минут


# --- Очистка временных файлов ---
def cleanup_temp_files():
    while True:
        try:
            now = time.time()
            for f in os.listdir(TEMP_DIR):
                if f.endswith(".mp4"):
                    path = os.path.join(TEMP_DIR, f)
                    if os.path.getmtime(path) < now - FILES_TTL:
                        os.remove(path)
        except Exception:
            pass
        time.sleep(60)


threading.Thread(target=cleanup_temp_files, daemon=True).start()


# --- API для скачивания ---
@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True)
    url = data.get("url")

    if isinstance(url, dict):
        url = url.get("text") or url.get("url") or next(iter(url.values()), None)

    if not isinstance(url, str) or "youtu" not in url:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    # Нормализуем короткие ссылки
    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[-1].split("?")[0]
        url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        filename = f"youtube_{int(time.time())}.mp4"
        output_path = os.path.join(TEMP_DIR, filename)

        ydl_opts = {
            "format": "mp4/best",
            "outtmpl": output_path,
            "quiet": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        download_url = f"http://{request.host}/temp/{filename}"
        return jsonify({"download_url": download_url})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Раздача временных файлов ---
@app.route("/temp/<path:filename>")
def serve_temp(filename):
    return send_from_directory(TEMP_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(host="192.168.0.101", port=8080)
