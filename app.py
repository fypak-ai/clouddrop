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
