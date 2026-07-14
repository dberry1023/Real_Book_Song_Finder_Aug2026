"""Real Book Song Finder - local web server.

Serves a browser UI for looking up any song across the Real Book collection
and displays the actual sheet-music page rendered from the PDFs.

Local use:        python server.py
Multi-device use:  python server.py --lan   (reachable from other devices
                    on your Tailscale network / home LAN, protected by an
                    access token printed on first run)
"""
import argparse
import json
import logging
import secrets
import threading
import webbrowser
from functools import lru_cache, wraps
from pathlib import Path

import pymupdf
from flask import (Flask, abort, jsonify, redirect, request,
                    send_from_directory, session, Response)

BOOKS_DIR = Path(r"C:\Users\stats\OneDrive\Desktop\Jazz Night School\Real_Books")
APP_DIR = Path(__file__).parent
PORT = 8765
RENDER_DPI = 170

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("song_finder")

app = Flask(__name__, static_folder=str(APP_DIR / "static"))

DB = json.loads((APP_DIR / "data" / "songs.json").read_text(encoding="utf-8"))

# Merge composer/key metadata (built by extract_metadata.py) if present.
_META_PATH = APP_DIR / "data" / "metadata.json"
if _META_PATH.exists():
    _meta = json.loads(_META_PATH.read_text(encoding="utf-8"))
    for _s in DB["songs"]:
        _m = _meta.get(_s["id"])
        if _m and not _m.get("page_mismatch"):
            _s["composer"] = _m.get("composer", "unknown")
            _s["key"] = _m.get("key", "unknown")

_docs: dict[str, pymupdf.Document] = {}
_render_lock = threading.Lock()  # PyMuPDF rendering is not thread-safe


# --------------------------------------------------------------------------
# Access token (only enforced when serving beyond localhost, i.e. --lan)
# --------------------------------------------------------------------------
def _load_or_create(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(16)
    path.write_text(value, encoding="utf-8")
    return value


SECRET_PATH = APP_DIR / "data" / ".session_secret"
TOKEN_PATH = APP_DIR / "data" / ".access_token"
app.secret_key = _load_or_create(SECRET_PATH)
ACCESS_TOKEN = _load_or_create(TOKEN_PATH)
REQUIRE_AUTH = False  # flipped on by --lan


def auth_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not REQUIRE_AUTH or session.get("authed"):
            return view(*args, **kwargs)
        key = request.args.get("key")
        if key and secrets.compare_digest(key, ACCESS_TOKEN):
            session["authed"] = True
            session.permanent = True
            # drop the token from the visible URL once the cookie is set
            if request.path == "/":
                return redirect("/")
            return view(*args, **kwargs)
        abort(403, "missing or invalid access key")
    return wrapped


def get_doc(book_id: str) -> pymupdf.Document:
    if book_id not in _docs:
        pdf_name = DB["books"][book_id]["pdf"]
        _docs[book_id] = pymupdf.open(str(BOOKS_DIR / pdf_name))
    return _docs[book_id]


@lru_cache(maxsize=300)
def render_png(book_id: str, pdf_page: int) -> bytes:
    with _render_lock:
        doc = get_doc(book_id)
        pix = doc[pdf_page - 1].get_pixmap(dpi=RENDER_DPI)
        return pix.tobytes("png")


@app.route("/")
@auth_required
def home() -> Response:
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/songs")
@auth_required
def songs() -> Response:
    return jsonify(DB)


@app.route("/api/page/<book_id>/<int:pdf_page>")
@auth_required
def page_image(book_id: str, pdf_page: int) -> Response:
    if book_id not in DB["books"]:
        abort(404, "unknown book")
    if not (1 <= pdf_page <= DB["books"][book_id]["pages"]):
        abort(404, "page out of range")
    png = render_png(book_id, pdf_page)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--lan", action="store_true",
                   help="Bind to all interfaces (reachable from other "
                        "devices on your Tailscale/home network) and "
                        "require the access token.")
    p.add_argument("--no-browser", action="store_true",
                   help="Don't auto-open a browser tab (used for autostart).")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    REQUIRE_AUTH = args.lan
    host = "0.0.0.0" if args.lan else "127.0.0.1"

    logger.info("Real Book Song Finder: %d songs, %d books",
                len(DB["songs"]), len(DB["books"]))
    if args.lan:
        logger.info("LAN mode: reachable from your other devices.")
        logger.info("Access URL (use this on your phone/laptop too):")
        logger.info("    http://<this-PCs-Tailscale-name>:%d/?key=%s",
                    PORT, ACCESS_TOKEN)
        logger.info("Access token (keep private): %s", ACCESS_TOKEN)
        local_url = f"http://127.0.0.1:{PORT}/?key={ACCESS_TOKEN}"
    else:
        logger.info("Local-only mode. Serving at http://127.0.0.1:%d", PORT)
        local_url = f"http://127.0.0.1:{PORT}"

    if not args.no_browser:
        threading.Timer(1.2, open_browser, args=[local_url]).start()
    app.run(host=host, port=PORT, threaded=True)
