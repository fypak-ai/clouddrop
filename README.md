# CloudDrop

Cloud download manager — Offcloud-style. Flask backend + SPA frontend.

## Features

- **Remote Download** — paste a URL, server downloads it in the background
- **Real-time queue** — live progress bar and status (pending → downloading → completed)
- **File Manager** — list, download, delete stored files
- **Local Upload** — drag & drop any file
- **Stats dashboard** — total downloads, active, files stored, total size

## Run locally

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

## Deploy on Railway

```bash
railway login
railway init
railway up
```

Or connect this GitHub repo in the Railway dashboard for auto-deploy on push.

## Stack

- Python / Flask 3
- Vanilla JS SPA (no build step)
- Gunicorn for production
