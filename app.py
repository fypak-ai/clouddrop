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

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}

try:
    import libtorrent as lt
    HAS_LT = True
except ImportError:
    HAS_LT = False

# Public trackers injected into every magnet to maximise peers
PUBLIC_TRACKERS = [
    # HTTP/HTTPS ONLY — Railway blocks UDP outbound
    # These work via TCP and are the most reliable on PaaS
    "https://tracker.tamersunion.org:443/announce",
    "https://tracker1.520.jp:443/announce",
    "https://tracker.gbitt.info:443/announce",
    "https://tracker.loligirl.cn:443/announce",
    "https://tracker.yemekyedim.com:443/announce",
    "https://tracker.lilithraws.org:443/announce",
    "https://tracker.pictoker.com:443/announce",
    "https://tracker.foreverpirates.co:443/announce",
    "https://tr.burnabitsoon.com:443/announce",
    "https://t1.hloli.org:443/announce",
    "http://tracker.moeking.me:6969/announce",
    "http://open.acgnxtracker.com:80/announce",
    "http://tracker.bt4g.com:2095/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "http://tracker.torrent.eu.org:451/announce",
    "http://tracker1.bt.moack.co.kr:80/announce",
    "http://tracker.dler.org:6969/announce",
    "http://open.tracker.cl:1337/announce",
    "http://tracker.theoks.net:6969/announce",
]


def _make_lt_session():
    """Create a libtorrent session optimised for speed."""
    ses = lt.session()
    ses.listen_on(6881, 6891)

    settings = {
        # DHT disabled — Railway blocks UDP, DHT is UDP-only
        "enable_dht": False,
        "enable_lsd": True,
        "enable_upnp": False,
        "enable_natpmp": False,
        # Connection limits
        "connections_limit": 800,
        "connection_speed": 200,
        "unchoke_slots_limit": 32,
        # Download aggressiveness
        "num_want": 400,
        "request_queue_time": 3,
        "max_out_request_queue": 3000,
        "piece_timeout": 30,
        "whole_pieces_threshold": 20,
        # Announce to every tracker simultaneously
        "announce_to_all_trackers": True,
        "announce_to_all_tiers": True,
        # Timeouts
        "peer_connect_timeout": 8,
        "request_timeout": 15,
        # Active limits
        "active_downloads": 20,
        "active_seeds": 10,
        "active_limit": 30,
        # Speed — unlimited
        "download_rate_limit": 0,
        "upload_rate_limit": 0,
        # Prefer TCP connections (Railway blocks UDP)
        "enable_outgoing_utp": False,
        "enable_incoming_utp": False,
    }
    try:
        ses.apply_settings(settings)
    except Exception:
        pass

    try:
        # DHT disabled (Railway blocks UDP) — usando HTTP trackers apenas
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
    """List top-level files and folders.
    - Files at root level → individual entries (type: file)
    - Subdirectories (torrent multi-file) → single folder entry (type: folder)
      with children list and main_file (largest file) for quick play/download.
    """
    entries = []
    for item in sorted(UPLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if item.is_file():
            st = item.stat()
            ext = item.suffix.lower()
            entries.append({
                "name": item.name,
                "type": "file",
                "size": st.st_size,
                "size_human": human_size(st.st_size),
                "modified": st.st_mtime,
                "is_video": ext in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".flv", ".wmv", ".3gp", ".mpeg", ".mpg", ".vob", ".rm", ".rmvb", ".divx"},
                "is_audio": ext in {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma", ".opus"},
            })
        elif item.is_dir():
            all_files = sorted(
                [p for p in item.rglob("*") if p.is_file()],
                key=lambda p: p.stat().st_size, reverse=True,
            )
            if not all_files:
                continue
            total_size = sum(p.stat().st_size for p in all_files)
            main_file = all_files[0]
            main_ext = main_file.suffix.lower()
            children = [
                {
                    "name": str(p.relative_to(UPLOAD_DIR)),
                    "size": p.stat().st_size,
                    "size_human": human_size(p.stat().st_size),
                    "is_video": p.suffix.lower() in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".flv", ".wmv", ".3gp", ".mpeg", ".mpg", ".vob", ".rm", ".rmvb", ".divx"},
                    "is_audio": p.suffix.lower() in {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma", ".opus"},
                }
                for p in all_files
            ]
            entries.append({
                "name": item.name,
                "type": "folder",
                "size": total_size,
                "size_human": human_size(total_size),
                "modified": item.stat().st_mtime,
                "file_count": len(all_files),
                "main_file": str(main_file.relative_to(UPLOAD_DIR)),
                "is_video": main_ext in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".flv", ".wmv", ".3gp", ".mpeg", ".mpg", ".vob", ".rm", ".rmvb", ".divx"},
                "is_audio": main_ext in {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma", ".opus"},
                "children": children,
            })
    return jsonify(entries)


# Formats that need server-side transcoding to play in browser
TRANSCODE_EXTS = {".avi", ".flv", ".wmv", ".ts", ".3gp", ".mpeg", ".mpg", ".vob", ".rm", ".rmvb", ".divx"}

def _mime(suffix):
    return {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".avi": "video/x-msvideo",
        ".mov": "video/quicktime", ".m4v": "video/mp4",
        ".ts": "video/mp2t", ".flv": "video/x-flv",
        ".wmv": "video/x-ms-wmv", ".3gp": "video/3gpp",
        ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
        ".vob": "video/dvd", ".rm": "application/vnd.rn-realmedia",
        ".rmvb": "application/vnd.rn-realmedia-vbr", ".divx": "video/divx",
        ".mp3": "audio/mpeg", ".flac": "audio/flac",
        ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".wma": "audio/x-ms-wma", ".opus": "audio/opus",
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



# ── Transcode (AVI/FLV/WMV/etc → webm via FFmpeg) ──────────────────────────
@app.route("/api/transcode/<path:filename>", methods=["GET"])
def transcode_file(filename):
    """Stream-transcode unsupported formats to webm via FFmpeg."""
    import subprocess, shutil
    path = (UPLOAD_DIR / filename).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve())):
        abort(403)
    if not path.exists() or not path.is_file():
        abort(404)
    if not shutil.which("ffmpeg"):
        # FFmpeg not available – fall back to direct serve (may not play)
        return serve_file(filename)
    cmd = [
        "ffmpeg", "-i", str(path),
        "-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0",
        "-c:a", "libopus", "-b:a", "128k",
        "-f", "webm", "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def generate():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
    return Response(generate(), mimetype="video/webm",
                    headers={"Content-Disposition": f'inline; filename="{path.stem}.webm"'})


# ── Dropbox integration ──────────────────────────────────────────────────────
# Simple token storage (in-memory; survives redeploys if you set DROPBOX_TOKEN env)
_dropbox_token = os.getenv("DROPBOX_TOKEN", "")

@app.route("/api/dropbox/status", methods=["GET"])
def dropbox_status():
    return jsonify({"connected": bool(_dropbox_token)})

@app.route("/api/dropbox/set-token", methods=["POST"])
def dropbox_set_token():
    global _dropbox_token
    data = request.get_json(force=True)
    token = (data or {}).get("token", "").strip()
    if not token:
        return jsonify({"error": "token required"}), 400
    _dropbox_token = token
    return jsonify({"ok": True})

@app.route("/api/dropbox/upload", methods=["POST"])
def dropbox_upload():
    """Upload one or more files/folders to Dropbox.
    Body JSON: { files: ["rel/path1", "Folder/Name", ...] }
    Returns per-file results.
    """
    global _dropbox_token
    if not _dropbox_token:
        return jsonify({"error": "Dropbox not connected"}), 401
    data = request.get_json(force=True) or {}
    names = data.get("files", [])
    if not names:
        return jsonify({"error": "no files specified"}), 400

    results = []
    HEADERS = {
        "Authorization": f"Bearer {_dropbox_token}",
        "Content-Type": "application/octet-stream",
        "Dropbox-API-Arg": "",
    }

    def upload_one(rel_path):
        path = (UPLOAD_DIR / rel_path).resolve()
        if not str(path).startswith(str(UPLOAD_DIR.resolve())):
            return {"file": rel_path, "ok": False, "error": "forbidden"}
        if not path.exists():
            return {"file": rel_path, "ok": False, "error": "not found"}

        files_to_upload = []
        if path.is_file():
            files_to_upload = [(path, "/" + path.name)]
        elif path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file():
                    files_to_upload.append((p, "/" + str(p.relative_to(UPLOAD_DIR))))

        errs = []
        for fpath, dbx_path in files_to_upload:
            import json as _json
            h = dict(HEADERS)
            h["Dropbox-API-Arg"] = _json.dumps({
                "path": dbx_path,
                "mode": "overwrite",
                "autorename": False,
                "mute": False,
            })
            with open(fpath, "rb") as fh:
                r = requests.post(
                    "https://content.dropboxapi.com/2/files/upload",
                    headers=h,
                    data=fh,
                    timeout=300,
                )
            if r.status_code != 200:
                errs.append(f"{fpath.name}: {r.text[:120]}")

        if errs:
            return {"file": rel_path, "ok": False, "error": "; ".join(errs)}
        return {"file": rel_path, "ok": True}

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(upload_one, n): n for n in names}
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    return jsonify(results)



# ════════════════════════════════════════════════════════════════════════════════
# TORRENT SEARCH — agregador inline (9 fontes)
# Uses threads + asyncio.run() per source to avoid Gunicorn event loop conflicts
# ════════════════════════════════════════════════════════════════════════════════
import asyncio as _asyncio
import urllib.parse as _urllib_parse
import aiohttp as _aiohttp
from abc import ABC as _ABC, abstractmethod as _abstractmethod
from typing import List as _List, Dict as _Dict
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor, as_completed as _as_completed

_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class _TorrentSource(_ABC):
    id: str = ""
    name: str = ""
    categories: _List[str] = []

    @_abstractmethod
    async def search(self, session, query: str, category: str, limit: int) -> _List[_Dict]:
        pass

    def _result(self, title, magnet, size="", seeders=0, leechers=0, category="", date="", extra=None):
        def _int(v):
            try:
                return int(str(v).strip().replace(",", ""))
            except Exception:
                return 0
        r = {
            "title": title, "magnet": magnet, "size": str(size).strip(),
            "seeders": _int(seeders), "leechers": _int(leechers),
            "source": self.name, "source_id": self.id,
            "category": category, "date": date,
        }
        if extra:
            r.update(extra)
        return r

    def _build_magnet(self, info_hash, name="", trackers=None):
        m = f"magnet:?xt=urn:btih:{info_hash}&dn={_urllib_parse.quote(name)}"
        for tr in (trackers or []):
            m += f"&tr={_urllib_parse.quote(tr)}"
        return m

    def search_sync(self, query: str, category: str, limit: int) -> _List[_Dict]:
        """Run async search in a fresh event loop (safe inside a thread).
        NOTE: TCPConnector must be created INSIDE the coroutine — creating it
        outside raises RuntimeError('no running event loop') in aiohttp 3.10+."""
        loop = _asyncio.new_event_loop()
        try:
            async def _run():
                connector = _aiohttp.TCPConnector(ssl=False)
                timeout = _aiohttp.ClientTimeout(total=12)
                async with _aiohttp.ClientSession(
                    connector=connector, headers=_SEARCH_HEADERS,
                    timeout=timeout
                ) as session:
                    return await self.search(session, query, category, limit)
            return loop.run_until_complete(_run())
        except Exception:
            return []
        finally:
            try:
                loop.close()
            except Exception:
                pass


# ── sources/yts.py ──
import urllib.parse

TRACKERS = [
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:80",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://glotorrents.pw:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
]


class _YTSSource(_TorrentSource):
    id = "yts"
    name = "YTS"
    categories = ["movies", "all"]
    BASE_URL = "https://yts.mx/api/v2"

    async def search(self, session, query, category, limit):
        if category not in ("all", "movies"):
            return []
        try:
            params = {"query_term": query, "limit": min(limit, 50), "sort_by": "seeds"}
            async with session.get(f"{self.BASE_URL}/list_movies.json", params=params, timeout=12) as r:
                data = await r.json(content_type=None)
            movies = data.get("data", {}).get("movies") or []
            results = []
            for m in movies:
                for t in m.get("torrents", []):
                    magnet = self._magnet(t.get("hash", ""), m.get("title_long", "Unknown"))
                    results.append(self._result(
                        title=f"{m.get('title_long', 'Unknown')} [{t.get('quality','?')}] [{t.get('type','?')}]",
                        magnet=magnet,
                        size=t.get("size", ""),
                        seeders=t.get("seeds", 0),
                        leechers=t.get("peers", 0),
                        category="movies",
                        date=t.get("date_uploaded", ""),
                        extra={"cover": m.get("medium_cover_image", ""), "rating": m.get("rating", "")}
                    ))
            return results
        except Exception as e:
            print(f"[YTS] error: {e}")
            return []

    def _magnet(self, hash_, title):
        tr = "&tr=".join(TRACKERS)
        return f"magnet:?xt=urn:btih:{hash_}&dn={urllib.parse.quote(title)}&tr={tr}"


# ── sources/nyaa.py ──
import re


class _NyaaSource(_TorrentSource):
    id = "nyaa"
    name = "Nyaa"
    categories = ["anime", "manga", "software", "audio", "video"]
    BASE_URL = "https://nyaa.si"
    CATEGORY_MAP = {
        "anime": "1_0", "manga": "3_0", "audio": "2_0",
        "software": "6_0", "video": "4_0", "all": "0_0",
    }

    async def search(self, session, query, category, limit):
        cat = self.CATEGORY_MAP.get(category, "0_0")
        try:
            params = {"f": 0, "c": cat, "q": query, "p": 1}
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(f"{self.BASE_URL}/?page=rss", params=params, headers=headers, timeout=10) as r:
                text = await r.text()
            return self._parse_rss(text, limit)
        except Exception as e:
            print(f"[Nyaa] error: {e}")
            return []

    def _parse_rss(self, text, limit):
        items = re.findall(r"<item>(.*?)</item>", text, re.DOTALL)
        results = []
        for item in items[:limit]:
            title = re.search(r"<title><!\[CDATA\[(.*?)]]></title>", item)
            magnet = re.search(r"<nyaa:magnetUri><!\[CDATA\[(magnet:\?.*?)]]>", item)
            seeders = re.search(r"<nyaa:seeders>(\d+)</nyaa:seeders>", item)
            leechers = re.search(r"<nyaa:leechers>(\d+)</nyaa:leechers>", item)
            size = re.search(r"<nyaa:size>(.*?)</nyaa:size>", item)
            pub_date = re.search(r"<pubDate>(.*?)</pubDate>", item)
            if title and magnet:
                results.append(self._result(
                    title=title.group(1),
                    magnet=magnet.group(1),
                    size=size.group(1) if size else "",
                    seeders=int(seeders.group(1)) if seeders else 0,
                    leechers=int(leechers.group(1)) if leechers else 0,
                    category="anime",
                    date=pub_date.group(1) if pub_date else "",
                ))
        return results


# ── sources/tpb.py ──
import urllib.parse

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://tracker.coppersurfer.tk:6969/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
]


class _TPBSource(_TorrentSource):
    id = "tpb"
    name = "The Pirate Bay"
    categories = ["all", "movies", "tv", "music", "games", "software", "anime"]
    # apibay.org is the official TPB JSON API
    APIS = [
        "https://apibay.org",
        "https://apibay.nocensor.space",
    ]
    CAT_MAP = {
        "movies": 200, "tv": 205, "music": 100,
        "games": 400, "software": 300, "anime": 205, "all": 0
    }

    async def search(self, session, query, category, limit):
        cat = self.CAT_MAP.get(category, 0)
        for api in self.APIS:
            try:
                params = {"q": query, "cat": cat}
                async with session.get(
                    f"{api}/q.php", params=params, timeout=15
                ) as r:
                    if r.status != 200:
                        print(f"[TPB] {api} status {r.status}")
                        continue
                    data = await r.json(content_type=None)
                if not data or (len(data) == 1 and data[0].get("name") == "No results returned"):
                    return []
                results = []
                for t in data[:limit]:
                    info_hash = t.get("info_hash", "")
                    if not info_hash or info_hash == "0" * 40:
                        continue
                    magnet = self._magnet(info_hash, t.get("name", ""))
                    results.append(self._result(
                        title=t.get("name", ""),
                        magnet=magnet,
                        size=t.get("size", ""),
                        seeders=t.get("seeders", 0),
                        leechers=t.get("leechers", 0),
                        category=category,
                        date=str(t.get("added", "")),
                        extra={"imdb": t.get("imdb", "")}
                    ))
                return results
            except Exception as e:
                print(f"[TPB] {api} error: {e}")
        return []

    def _magnet(self, hash_, name):
        dn = urllib.parse.quote(name)
        tr = "&tr=".join(urllib.parse.quote(t) for t in TRACKERS)
        return f"magnet:?xt=urn:btih:{hash_}&dn={dn}&tr={tr}"


# ── sources/x1337.py ──
import urllib.parse
from bs4 import BeautifulSoup


class _X1337Source(_TorrentSource):
    id = "1337x"
    name = "1337x"
    categories = ["movies", "tv", "games", "music", "apps", "anime"]
    MIRRORS = ["https://1337x.to", "https://1337x.st", "https://x1337x.ws"]
    CAT_MAP = {
        "movies": "Movies", "tv": "TV", "games": "Games",
        "music": "Music", "apps": "Apps", "anime": "Anime",
    }

    async def search(self, session, query, category, limit):
        cat_path = (
            f"category-search/{urllib.parse.quote(query)}/{self.CAT_MAP[category]}/1/"
            if category in self.CAT_MAP
            else f"search/{urllib.parse.quote(query)}/1/"
        )
        for mirror in self.MIRRORS:
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with session.get(f"{mirror}/{cat_path}", headers=headers, timeout=10) as r:
                    if r.status != 200:
                        continue
                    html = await r.text()
                results = await self._parse_and_fetch(session, mirror, html, limit)
                if results:
                    return results
            except Exception as e:
                print(f"[1337x] {mirror} error: {e}")
        return []

    async def _parse_and_fetch(self, session, mirror, html, limit):
        import asyncio
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.table-list tbody tr")[:limit]
        tasks = [self._fetch_magnet(session, mirror, row) for row in rows]
        magnets = await asyncio.gather(*tasks, return_exceptions=True)
        results = []
        for row, magnet in zip(rows, magnets):
            if isinstance(magnet, Exception) or not magnet:
                continue
            cols = row.find_all("td")
            if len(cols) < 4:
                continue
            name = cols[0].find("a", href=lambda h: h and "/torrent/" in h)
            seeders = cols[1].text.strip()
            leechers = cols[2].text.strip()
            size = cols[4].text.strip() if len(cols) > 4 else ""
            results.append(self._result(
                title=name.text.strip() if name else "Unknown",
                magnet=magnet, size=size,
                seeders=seeders, leechers=leechers, category="general",
            ))
        return results

    async def _fetch_magnet(self, session, mirror, row):
        link = row.find("a", href=lambda h: h and "/torrent/" in h)
        if not link:
            return None
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(f"{mirror}{link['href']}", headers=headers, timeout=8) as r:
                html = await r.text()
            soup = BeautifulSoup(html, "html.parser")
            magnet = soup.find("a", href=lambda h: h and h.startswith("magnet:"))
            return magnet["href"] if magnet else None
        except Exception:
            return None


# ── sources/eztv.py ──


class _EZTVSource(_TorrentSource):
    id = "eztv"
    name = "EZTV"
    categories = ["all", "tv", "shows"]
    BASE_URL = "https://eztv.re/api"

    async def search(self, session, query, category, limit):
        try:
            params = {"query": query, "limit": min(limit, 100), "page": 1}
            async with session.get(f"{self.BASE_URL}/get-torrents", params=params, timeout=12) as r:
                data = await r.json()
            torrents = data.get("torrents") or []
            results = []
            for t in torrents[:limit]:
                results.append(self._result(
                    title=t.get("title", ""),
                    magnet=t.get("magnet_url", ""),
                    size=str(t.get("size_bytes", "")),
                    seeders=t.get("seeds", 0),
                    leechers=t.get("peers", 0),
                    category="tv",
                    date=str(t.get("date_released_unix", "")),
                    extra={"imdb": t.get("imdb_id", "")}
                ))
            return results
        except Exception as e:
            print(f"[EZTV] error: {e}")
            return []


# ── sources/torrentgalaxy.py ──
import urllib.parse
from bs4 import BeautifulSoup


class _TorrentGalaxySource(_TorrentSource):
    id = "tgx"
    name = "Torrent Galaxy"
    categories = ["movies", "tv", "music", "games", "software", "anime", "books"]
    MIRRORS = ["https://torrentgalaxy.to", "https://tgx.rs"]
    CAT_MAP = {
        "movies": "c3=1", "tv": "c5=1", "music": "c22=1",
        "games": "c10=1", "software": "c18=1", "anime": "c28=1", "books": "c13=1",
    }

    async def search(self, session, query, category, limit):
        cat_param = self.CAT_MAP.get(category, "")
        q = urllib.parse.quote(query)
        for mirror in self.MIRRORS:
            try:
                url = f"{mirror}/torrents.php?search={q}&lang=0&nox=2&{cat_param}"
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with session.get(url, headers=headers, timeout=12) as r:
                    if r.status != 200:
                        continue
                    html = await r.text()
                results = self._parse(html, limit)
                if results:
                    return results
            except Exception as e:
                print(f"[TGX] {mirror} error: {e}")
        return []

    def _parse(self, html, limit):
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("div.tgxtablerow")[:limit]
        results = []
        for row in rows:
            try:
                name_tag = row.select_one("div.tgxtablecell:nth-child(4) a")
                magnet_tag = row.find("a", href=lambda h: h and h.startswith("magnet:"))
                if not name_tag or not magnet_tag:
                    continue
                seeders_tag = row.select_one("span.tgxtableseeds")
                leechers_tag = row.select_one("span.tgxtableleechers")
                size_tag = row.select_one("span.badge-secondary")
                results.append(self._result(
                    title=name_tag.get_text(strip=True),
                    magnet=magnet_tag["href"],
                    size=size_tag.get_text(strip=True) if size_tag else "",
                    seeders=seeders_tag.get_text(strip=True) if seeders_tag else 0,
                    leechers=leechers_tag.get_text(strip=True) if leechers_tag else 0,
                    category="general",
                ))
            except Exception:
                continue
        return results


# ── sources/kickass.py ──
import urllib.parse

TRACKERS = [
    "udp://tracker.coppersurfer.tk:6969/announce",
    "udp://9.rarbg.to:2920/announce",
    "udp://tracker.opentrackr.org:1337",
    "udp://tracker.internetwarriors.net:1337/announce",
]


class _KickassSource(_TorrentSource):
    id = "kat"
    name = "Kickass Torrents"
    categories = ["movies", "tv", "music", "games", "software", "anime", "books"]
    # Uses katcr.co JSON API
    BASE_URL = "https://katcr.co/api/v2/search"

    async def search(self, session, query, category, limit):
        try:
            params = {
                "phraseSearch": query,
                "page": 1,
            }
            if category not in ("all", ""):
                params["category"] = category
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(self.BASE_URL, params=params, headers=headers, timeout=12) as r:
                if r.status != 200:
                    return []
                data = await r.json(content_type=None)
            items = data.get("results", []) or []
            results = []
            for t in items[:limit]:
                hash_ = t.get("hash", "")
                name = t.get("title", "Unknown")
                magnet = self._build_magnet(hash_, name) if hash_ else t.get("magnet", "")
                if not magnet:
                    continue
                results.append(self._result(
                    title=name,
                    magnet=magnet,
                    size=t.get("size", ""),
                    seeders=t.get("seeders", 0),
                    leechers=t.get("leechers", 0),
                    category=category,
                    date=t.get("added", ""),
                ))
            return results
        except Exception as e:
            print(f"[KAT] error: {e}")
            return []

    def _build_magnet(self, hash_, name):
        tr = "&tr=".join(TRACKERS)
        return f"magnet:?xt=urn:btih:{hash_}&dn={urllib.parse.quote(name)}&tr={tr}"


# ── sources/limetorrents.py ──
import urllib.parse
from bs4 import BeautifulSoup


class _LimeTorrentsSource(_TorrentSource):
    id = "lime"
    name = "Lime Torrents"
    categories = ["movies", "tv", "music", "games", "software", "anime"]
    MIRRORS = ["https://www.limetorrents.lol", "https://www.limetorrents.info"]
    CAT_MAP = {
        "movies": "movies", "tv": "tv", "music": "music",
        "games": "games", "software": "applications", "anime": "anime",
    }

    async def search(self, session, query, category, limit):
        cat = self.CAT_MAP.get(category, "all")
        q = urllib.parse.quote(query)
        for mirror in self.MIRRORS:
            try:
                url = f"{mirror}/search/{cat}/{q}/seeds/1/"
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with session.get(url, headers=headers, timeout=12) as r:
                    if r.status != 200:
                        continue
                    html = await r.text()
                results = self._parse(html, limit)
                if results:
                    return results
            except Exception as e:
                print(f"[LIME] {mirror} error: {e}")
        return []

    def _parse(self, html, limit):
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.table2 tbody tr")[:limit]
        results = []
        for row in rows:
            try:
                cols = row.find_all("td")
                if len(cols) < 5:
                    continue
                name_tag = cols[0].find("a", href=lambda h: h and "/torrent/" in h)
                magnet_tag = row.find("a", href=lambda h: h and h.startswith("magnet:"))
                if not name_tag:
                    continue
                # Lime Torrents detail page needed for magnet — skip if no direct magnet
                if not magnet_tag:
                    continue
                results.append(self._result(
                    title=name_tag.get_text(strip=True),
                    magnet=magnet_tag["href"],
                    size=cols[2].get_text(strip=True),
                    seeders=cols[3].get_text(strip=True),
                    leechers=cols[4].get_text(strip=True),
                    category="general",
                ))
            except Exception:
                continue
        return results


# ── sources/rarbg.py ──
import urllib.parse

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:80",
    "udp://tracker.coppersurfer.tk:6969/announce",
]


class _RARBGSource(_TorrentSource):
    """
    RARBG closed in 2023. This source queries the community-maintained
    RARBG database mirror at rargb.to (db-search).
    Falls back to the apibay.org API (TPB) scoped to a known RARBG-style query.
    """
    id = "rarbg"
    name = "RARBG (mirror)"
    categories = ["movies", "tv", "games", "music", "software", "anime"]
    # Public RARBG DB search mirrors
    MIRRORS = [
        "https://rargb.to",
        "https://rarbg.unblockit.onl",
    ]

    async def search(self, session, query, category, limit):
        q = urllib.parse.quote(query)
        for mirror in self.MIRRORS:
            try:
                url = f"{mirror}/search/?search={q}"
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with session.get(url, headers=headers, timeout=12, allow_redirects=True) as r:
                    if r.status != 200:
                        continue
                    html = await r.text()
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                rows = soup.select("tr.table2t, tr.lista2")[:limit]
                results = []
                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) < 4:
                        continue
                    name_el = cols[1].find("a") if len(cols) > 1 else None
                    magnet_el = row.find("a", href=lambda h: h and h.startswith("magnet:"))
                    if not name_el or not magnet_el:
                        continue
                    results.append(self._result(
                        title=name_el.text.strip(),
                        magnet=magnet_el["href"],
                        size=cols[3].text.strip() if len(cols) > 3 else "",
                        seeders=cols[4].text.strip() if len(cols) > 4 else 0,
                        leechers=cols[5].text.strip() if len(cols) > 5 else 0,
                        category=category,
                    ))
                if results:
                    return results
            except Exception as e:
                print(f"[RARBG] {mirror} error: {e}")
        return []


# ── Aggregator & Flask routes ──────────────────────────────────────────────────
_SEARCH_SOURCES = [
    _YTSSource(), _NyaaSource(), _EZTVSource(), _X1337Source(), _TPBSource(),
    _RARBGSource(), _TorrentGalaxySource(), _KickassSource(), _LimeTorrentsSource(),
]

def _run_search(enabled_sources, query, category, limit):
    results = []
    with _ThreadPoolExecutor(max_workers=len(enabled_sources)) as ex:
        futures = {ex.submit(s.search_sync, query, category, limit): s for s in enabled_sources}
        try:
            for fut in _as_completed(futures, timeout=20):
                try:
                    r = fut.result()
                    if isinstance(r, list):
                        results.extend(r)
                except Exception:
                    pass
        except Exception:
            # TimeoutError or other — collect results already done
            for fut in futures:
                if fut.done():
                    try:
                        r = fut.result()
                        if isinstance(r, list):
                            results.extend(r)
                    except Exception:
                        pass
    results.sort(key=lambda x: x.get("seeders", 0), reverse=True)
    return results


@app.route("/api/search")
def api_search():
    query    = request.args.get("q", "").strip()
    category = request.args.get("category", "all")
    sources_param = request.args.get("sources", "all")
    limit    = min(int(request.args.get("limit", 30)), 100)

    if not query:
        return jsonify({"error": "query required"}), 400

    enabled = _SEARCH_SOURCES if sources_param == "all" else [
        s for s in _SEARCH_SOURCES if s.id in sources_param.split(",")
    ]
    try:
        results = _run_search(enabled, query, category, limit)
    except Exception as e:
        return jsonify({"error": "search failed: " + str(e), "results": []}), 500
    return jsonify({
        "query": query,
        "total": len(results),
        "sources": [s.id for s in enabled],
        "results": results,
    })


@app.route("/api/search/sources")
def api_search_sources():
    return jsonify([
        {"id": s.id, "name": s.name, "categories": s.categories}
        for s in _SEARCH_SOURCES
    ])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
