import os
import uuid
import threading
import time
import requests
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

jobs = {}

def human_size(b):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"

def download_worker(job_id, url, filename):
    jobs[job_id]["status"] = "downloading"
    try:
        resp = requests.get(url, stream=True, timeout=60, allow_redirects=True)
        resp.raise_for_status()
        if not filename:
            cd = resp.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                filename = cd.split("filename=")[-1].strip().strip('"')
            else:
                filename = url.split("?")[0].rstrip("/").split("/")[-1] or "download"
        filename = "".join(c for c in filename if c not in r'\/:*?"<>|')
        dest = UPLOAD_DIR / filename
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        counter = 1
        while dest.exists():
            dest = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        jobs[job_id]["progress"] = round(downloaded / total * 100, 1)
                    jobs[job_id]["downloaded"] = downloaded
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["filename"] = dest.name
        jobs[job_id]["size"] = dest.stat().st_size
        jobs[job_id]["size_human"] = human_size(dest.stat().st_size)
        jobs[job_id]["progress"] = 100
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/remote-download", methods=["POST"])
def remote_download():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    filename = (data.get("filename") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"id": job_id, "url": url, "filename": filename or None,
                    "status": "pending", "progress": 0, "downloaded": 0,
                    "size": 0, "size_human": "—", "error": None, "created_at": time.time()}
    threading.Thread(target=download_worker, args=(job_id, url, filename), daemon=True).start()
    return jsonify({"job_id": job_id}), 202

@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    return jsonify(list(jobs.values()))

@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)

@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    job = jobs.pop(job_id, None)
    if not job:
        return jsonify({"error": "Not found"}), 404
    if job.get("filename"):
        path = UPLOAD_DIR / job["filename"]
        if path.exists():
            path.unlink()
    return jsonify({"ok": True})

@app.route("/api/files", methods=["GET"])
def list_files():
    files = []
    for f in sorted(UPLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            stat = f.stat()
            files.append({"name": f.name, "size": stat.st_size,
                          "size_human": human_size(stat.st_size), "modified": stat.st_mtime})
    return jsonify(files)

@app.route("/api/files/<path:filename>", methods=["GET"])
def download_file(filename):
    path = UPLOAD_DIR / filename
    if not path.exists() or not path.is_file():
        abort(404)
    return send_from_directory(UPLOAD_DIR.resolve(), filename, as_attachment=True)

@app.route("/api/files/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    path = UPLOAD_DIR / filename
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    path.unlink()
    for jid, job in list(jobs.items()):
        if job.get("filename") == filename:
            jobs.pop(jid, None)
    return jsonify({"ok": True})

@app.route("/api/upload", methods=["POST"])
def upload_local():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    files = request.files.getlist("file")
    saved = []
    for f in files:
        if not f.filename:
            continue
        filename = "".join(c for c in f.filename if c not in r'\/:*?"<>|')
        dest = UPLOAD_DIR / filename
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        counter = 1
        while dest.exists():
            dest = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
        f.save(dest)
        size = dest.stat().st_size
        saved.append({"name": dest.name, "size": size, "size_human": human_size(size)})
    return jsonify({"uploaded": saved})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
