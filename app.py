import os
import uuid
import threading
import time
import requests
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, abort, Response
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

jobs = {}

try:
    import libtorrent as lt
    HAS_LT = True
except ImportError:
    HAS_LT = False

# Public trackers injected into every magnet to maximise peers
PUBLIC_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.bt4g.com:2095/announce",
    "udp://opentracker.io:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "https://tracker.tamersunion.org:443/announce",
]


def _make_lt_session():
    """Create a libtorrent session optimised for speed."""
    ses = lt.session()
    ses.listen_on(6881, 6891)

    settings = {
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": True,
        "enable_natpmp": True,
        "connections_limit": 300,
        "connection_speed": 50,
        "num_want": 200,
        "unchoke_slots_limit": 8,
        "request_queue_time": 3,
        "max_out_request_queue": 1500,
        "piece_timeout": 20,
        "announce_to_all_trackers": True,
        "announce_to_all_tiers": True,
    }
    try:
        ses.apply_settings(settings)
    except Exception:
        pass

    try:
        ses.add_dht_router("router.bittorrent.com", 6881)
        ses.add_dht_router("router.utorrent.com", 6881)
        ses.add_dht_router("dht.transmissionbt.com", 6881)
        ses.start_dht()
        ses.start_lsd()
        ses.start_upnp()
        ses.start_natpmp()
    except Exception:
        pass

    return ses


def human_size(b):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def is_magnet(url):
    return url.strip().lower().startswith("magnet:")


def _unique_path(path: Path) -> Path:
    stem, suffix, counter = path.stem, path.suffix, 1
    while path.exists():
        path = path.parent / f"{stem}_{counter}{suffix}"
        counter += 1
    return path


def _job_done(job_id, dest: Path):
    """Mark job as completed. Handles single-file AND multi-file (folder) torrents."""
    if not dest.exists():
        jobs[job_id].update({"status": "completed", "progress": 100})
        return

    if dest.is_file():
        # Single-file torrent or HTTP download
        size = dest.stat().st_size
        jobs[job_id].update({
            "status": "completed",
            "filename": dest.name,
            "size": size,
            "size_human": human_size(size),
            "progress": 100,
        })
    else:
        # Multi-file torrent saved as a folder
        all_files = [p for p in dest.rglob("*") if p.is_file()]
        if not all_files:
            jobs[job_id].update({"status": "completed", "progress": 100})
            return

        all_files.sort(key=lambda p: p.stat().st_size, reverse=True)
        main_file = all_files[0]
        total_size = sum(p.stat().st_size for p in all_files)

        # Relative paths so frontend can request /api/files/<rel>
        file_list = [
            {
                "name": str(p.relative_to(UPLOAD_DIR)),
                "size": p.stat().st_size,
                "size_human": human_size(p.stat().st_size),
            }
            for p in all_files
        ]

        jobs[job_id].update({
            "status": "completed",
            # filename = largest file (triggers frontend detection)
            "filename": str(main_file.relative_to(UPLOAD_DIR)),
            "folder": dest.name,
            "files": file_list,
            "size": total_size,
            "size_human": human_size(total_size),
            "progress": 100,
        })


def download_http(job_id, url, filename):
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
        dest = _unique_path(UPLOAD_DIR / filename)
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
        _job_done(job_id, dest)
    except Exception as e:
        jobs[job_id].update({"status": "failed", "error": str(e)})


def _inject_trackers(magnet_url: str) -> str:
    """Append public trackers to the magnet URI."""
    for t in PUBLIC_TRACKERS:
        encoded = requests.utils.quote(t, safe="")
        if encoded not in magnet_url:
            magnet_url += f"&tr={encoded}"
    return magnet_url


def download_torrent(job_id, magnet_url):
    if not HAS_LT:
        jobs[job_id].update({"status": "failed",
                              "error": "libtorrent nao instalado. Execute: pip install libtorrent"})
        return

    jobs[job_id]["status"] = "downloading"
    try:
        magnet_url = _inject_trackers(magnet_url)
        ses = _make_lt_session()

        params = lt.add_torrent_params()
        params.url = magnet_url
        params.save_path = str(UPLOAD_DIR)
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse
        handle = ses.add_torrent(params)

        jobs[job_id]["info"] = "Aguardando metadados (DHT/trackers)..."

        for _ in range(180):
            if handle.has_metadata():
                break
            time.sleep(0.5)

        if not handle.has_metadata():
            jobs[job_id].update({"status": "failed",
                                  "error": "Timeout aguardando metadados (sem peers/trackers)"})
            return

        try:
            handle.force_reannounce()
            handle.force_dht_announce()
        except Exception:
            pass

        torrent_info = handle.get_torrent_info()
        name = torrent_info.name()
        jobs[job_id]["filename"] = name
        jobs[job_id]["info"] = f"Baixando: {name}"

        while True:
            s = handle.status()
            progress = round(s.progress * 100, 1)
            dl_rate = s.download_rate
            jobs[job_id]["progress"] = progress
            jobs[job_id]["downloaded"] = int(s.total_done)
            jobs[job_id]["speed"] = (
                f"{dl_rate/1024/1024:.1f} MB/s" if dl_rate >= 1_000_000
                else f"{dl_rate/1024:.1f} KB/s"
            )
            jobs[job_id]["peers"] = s.num_peers
            jobs[job_id]["seeds"] = s.num_seeds

            if s.is_seeding or progress >= 100:
                break
            if jobs[job_id].get("_cancel"):
                handle.pause()
                jobs[job_id].update({"status": "failed", "error": "Cancelado"})
                return
            time.sleep(1)

        dest = UPLOAD_DIR / name
        _job_done(job_id, dest)

    except Exception as e:
        jobs[job_id].update({"status": "failed", "error": str(e)})


# Routes

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/remote-download", methods=["POST"])
def remote_download():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    filename = (data.get("filename") or "").strip()
    if not url:
        return jsonify({"error": "URL e obrigatoria"}), 400
    job_id = str(uuid.uuid4())
    dtype = "torrent" if is_magnet(url) else "http"
    jobs[job_id] = {
        "id": job_id, "url": url, "filename": filename or None,
        "type": dtype, "status": "pending", "progress": 0,
        "downloaded": 0, "size": 0, "size_human": "--",
        "error": None, "speed": None, "peers": None, "seeds": None,
        "created_at": time.time(),
    }
    if dtype == "torrent":
        threading.Thread(target=download_torrent, args=(job_id, url), daemon=True).start()
    else:
        threading.Thread(target=download_http, args=(job_id, url, filename), daemon=True).start()
    return jsonify({"job_id": job_id, "type": dtype}), 202


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    return jsonify(list(jobs.values()))


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    job = jobs.get(job_id)
    return jsonify(job) if job else (jsonify({"error": "Not found"}), 404)


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    job = jobs.pop(job_id, None)
    if not job:
        return jsonify({"error": "Not found"}), 404
    target = job.get("folder") or job.get("filename")
    if target:
        p = UPLOAD_DIR / target.split("/")[0]
        if p.is_file():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            import shutil
            shutil.rmtree(p, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/files", methods=["GET"])
def list_files():
    """List all files recursively (includes files inside torrent sub-folders)."""
    files = []
    for f in sorted(UPLOAD_DIR.rglob("*"),
                    key=lambda x: x.stat().st_mtime if x.is_file() else 0,
                    reverse=True):
        if f.is_file():
            st = f.stat()
            ext = f.suffix.lower()
            rel = str(f.relative_to(UPLOAD_DIR))
            files.append({
                "name": rel,
                "size": st.st_size,
                "size_human": human_size(st.st_size),
                "modified": st.st_mtime,
                "is_video": ext in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v"},
                "is_audio": ext in {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac"},
            })
    return jsonify(files)


def _mime(suffix):
    return {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".avi": "video/x-msvideo",
        ".mov": "video/quicktime", ".m4v": "video/mp4",
        ".mp3": "audio/mpeg", ".flac": "audio/flac",
        ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".m4a": "audio/mp4", ".aac": "audio/aac",
    }.get(suffix.lower(), "application/octet-stream")


@app.route("/api/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    path = (UPLOAD_DIR / filename).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve())):
        abort(403)
    if not path.exists() or not path.is_file():
        abort(404)
    range_header = request.headers.get("Range")
    if range_header:
        size = path.stat().st_size
        m = range_header.replace("bytes=", "").split("-")
        byte1 = int(m[0])
        byte2 = int(m[1]) if m[1] else size - 1
        length = byte2 - byte1 + 1
        with open(path, "rb") as f:
            f.seek(byte1)
            data = f.read(length)
        resp = Response(data, 206, mimetype=_mime(path.suffix), direct_passthrough=True)
        resp.headers.add("Content-Range", f"bytes {byte1}-{byte2}/{size}")
        resp.headers.add("Accept-Ranges", "bytes")
        resp.headers.add("Content-Length", length)
        return resp
    as_attachment = request.args.get("dl") == "1"
    return send_from_directory(UPLOAD_DIR.resolve(), filename, as_attachment=as_attachment)


@app.route("/api/files/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    path = (UPLOAD_DIR / filename).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve())):
        abort(403)
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    if path.is_file():
        path.unlink()
    elif path.is_dir():
        import shutil
        shutil.rmtree(path)
    for jid, job in list(jobs.items()):
        jf = job.get("folder") or job.get("filename") or ""
        if jf == filename or jf.startswith(filename + "/") or jf.startswith(filename + os.sep):
            jobs.pop(jid, None)
    return jsonify({"ok": True})


@app.route("/api/upload", methods=["POST"])
def upload_local():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    saved = []
    for f in request.files.getlist("file"):
        if not f.filename:
            continue
        filename = "".join(c for c in f.filename if c not in r'\/:*?"<>|')
        dest = _unique_path(UPLOAD_DIR / filename)
        f.save(dest)
        size = dest.stat().st_size
        saved.append({"name": dest.name, "size": size, "size_human": human_size(size)})
    return jsonify({"uploaded": saved})


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "libtorrent": HAS_LT,
        "libtorrent_version": str(lt.version) if HAS_LT else None,
        "files_count": sum(1 for f in UPLOAD_DIR.rglob("*") if f.is_file()),
        "public_trackers": len(PUBLIC_TRACKERS),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
