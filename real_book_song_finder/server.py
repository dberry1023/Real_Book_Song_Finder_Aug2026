"""Real Book Song Finder - local web server.

Serves a browser UI for looking up any song across the Real Book collection
and displays the actual sheet-music page rendered from the PDFs.

Local use:        python server.py
Multi-device use:  python server.py --lan   (reachable from other devices
                    on your Tailscale network / home LAN, protected by an
                    access token printed on first run)
"""
import argparse
import datetime
import json
import logging
import os
import secrets
import threading
import webbrowser
from functools import lru_cache, wraps
from pathlib import Path

import pymupdf
from flask import (Flask, abort, jsonify, redirect, request,
                    send_from_directory, session, Response)

BOOKS_DIR = Path(os.environ.get(
    "BOOKS_DIR", r"C:\Users\stats\OneDrive\Desktop\Jazz Night School\Real_Books"))
APP_DIR = Path(__file__).parent
PORT = 8765
RENDER_DPI = 170

# Multi-user cloud mode (MULTI_USER=1): individual email+password accounts
# instead of the single shared password, and a private book library per user.
# Off by default, so local mode and the single-password deployment are untouched.
MULTI_USER = os.environ.get("MULTI_USER") == "1"
USERS_DB = Path(os.environ.get("USERS_DB", str(APP_DIR / "data" / "users.db")))
USER_DATA_ROOT = Path(os.environ.get("USER_DATA_ROOT", str(APP_DIR / "data" / "users")))
SIGNUP_CODE = os.environ.get("SIGNUP_CODE")  # optional invite code for beta

if MULTI_USER:
    import library
    import multiuser

    multiuser.init_db(USERS_DB)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("song_finder")

app = Flask(__name__, static_folder=str(APP_DIR / "static"))
if MULTI_USER:
    app.config["MAX_CONTENT_LENGTH"] = library.MAX_UPLOAD_BYTES

def _load_db(songs_path: Path, meta_path: Path) -> dict:
    """Load a song index and merge composer/key metadata if present."""
    if not songs_path.exists():
        return {"books": {}, "songs": []}
    db = json.loads(songs_path.read_text(encoding="utf-8"))
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for s in db["songs"]:
            m = meta.get(s["id"])
            if m and not m.get("page_mismatch"):
                s["composer"] = m.get("composer", "unknown")
                s["key"] = m.get("key", "unknown")
    return db


DB = _load_db(APP_DIR / "data" / "songs.json", APP_DIR / "data" / "metadata.json")

# Per-user indexes, loaded lazily in multi-user mode (keyed by user id).
_user_dbs: dict[int, dict] = {}


def user_dir(uid: int) -> Path:
    return USER_DATA_ROOT / str(uid)


def get_db() -> dict:
    """The song index for the current request (shared, or the user's own)."""
    if not MULTI_USER:
        return DB
    uid = session["user_id"]
    if uid not in _user_dbs:
        d = user_dir(uid)
        _user_dbs[uid] = _load_db(d / "songs.json", d / "metadata.json")
    return _user_dbs[uid]


def get_books_dir() -> Path:
    if not MULTI_USER:
        return BOOKS_DIR
    return user_dir(session["user_id"]) / "books"

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
# In the cloud these come from env vars (Modal secret); locally from files.
app.secret_key = os.environ.get("SESSION_SECRET") or _load_or_create(SECRET_PATH)
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN") or _load_or_create(TOKEN_PATH)
# Flipped on by --lan locally, or REQUIRE_AUTH=1 env var in the cloud
# (the __main__ block never runs under a WSGI host).
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH") == "1"
app.permanent_session_lifetime = datetime.timedelta(days=90)

_AUTH_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Real Book Song Finder</title>
<style>
  body {{ background:#14161c; color:#e8e6e3; font-family:system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center;
         min-height:100vh; margin:0; }}
  form {{ background:#1d2027; padding:2.2rem 2.6rem; border-radius:12px;
         text-align:center; box-shadow:0 8px 30px rgba(0,0,0,.45); }}
  h1 {{ font-size:1.25rem; margin:0 0 1.2rem; }}
  h1 span {{ color:#e8b849; }}
  input {{ font-size:1.05rem; padding:.55rem .8rem; border-radius:8px;
          border:1px solid #3a3f4b; background:#14161c; color:#e8e6e3;
          width:16rem; display:block; margin:0 auto .6rem; }}
  button {{ font-size:1.05rem; padding:.55rem 1.3rem; margin-top:.3rem;
           border-radius:8px; border:0; background:#e8b849; color:#14161c;
           font-weight:600; cursor:pointer; }}
  .err {{ color:#e46f6f; margin-top:.9rem; font-size:.95rem; }}
  .alt {{ margin-top:1.1rem; font-size:.9rem; color:#9a97a8; }}
  .alt a {{ color:#e8b849; }}
</style></head><body>
<form method="post" action="{action}">
  <h1>&#119070; Real Book <span>Song Finder</span></h1>
  {fields}
  <button type="submit">{button}</button>
  {error}
  {footer}
</form></body></html>"""

_PW_FIELD = ('<input type="password" name="password" placeholder="Password" '
             'autocomplete="{ac}">')
_EMAIL_FIELD = ('<input type="email" name="email" placeholder="Email" '
                'autofocus autocomplete="email">')


def _auth_page(action: str, fields: str, button: str,
               error: str = "", footer: str = "") -> Response:
    err = f'<div class="err">{error}</div>' if error else ""
    return Response(
        _AUTH_PAGE.format(action=action, fields=fields, button=button,
                          error=err, footer=footer),
        mimetype="text/html")


def _login_page(error: str = "") -> Response:
    if MULTI_USER:
        fields = _EMAIL_FIELD + _PW_FIELD.format(ac="current-password")
        footer = ('<div class="alt">New here? '
                  '<a href="/signup">Create an account</a></div>')
        return _auth_page("/login", fields, "Sign in", error, footer)
    fields = _PW_FIELD.format(ac="current-password").replace(
        'type="password"', 'type="password" autofocus', 1)
    return _auth_page("/login", fields, "Enter", error)


def _signup_page(error: str = "") -> Response:
    fields = _EMAIL_FIELD + _PW_FIELD.format(ac="new-password")
    if SIGNUP_CODE:
        fields += ('<input type="text" name="code" placeholder="Invite code" '
                   'autocomplete="off">')
    footer = ('<div class="alt">Already have an account? '
              '<a href="/">Sign in</a></div>')
    return _auth_page("/signup", fields, "Create account", error, footer)


def auth_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if MULTI_USER:
            if session.get("user_id"):
                return view(*args, **kwargs)
            if request.path == "/":
                return _login_page()
            abort(403, "sign in required")
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
        # Browsers hitting the app root get a login form; API calls get 403.
        if request.path == "/":
            return _login_page()
        abort(403, "missing or invalid access key")
    return wrapped


@app.route("/login", methods=["POST"])
def login() -> Response:
    if MULTI_USER:
        uid, email = multiuser.verify_user(
            USERS_DB, request.form.get("email", ""), request.form.get("password", ""))
        if uid is None:
            return _login_page("Wrong email or password.")
        session.clear()
        session["user_id"] = uid
        session["email"] = email
        session.permanent = True
        return redirect("/")
    password = request.form.get("password", "")
    if password and secrets.compare_digest(password, ACCESS_TOKEN):
        session["authed"] = True
        session.permanent = True
        return redirect("/")
    return _login_page("Wrong password — try again.")


@app.route("/signup", methods=["GET", "POST"])
def signup() -> Response:
    if not MULTI_USER:
        abort(404)
    if request.method == "GET":
        return _signup_page()
    if SIGNUP_CODE and not secrets.compare_digest(
            request.form.get("code", ""), SIGNUP_CODE):
        return _signup_page("Invalid invite code.")
    uid, err = multiuser.create_user(
        USERS_DB, request.form.get("email", ""), request.form.get("password", ""))
    if err:
        return _signup_page(err)
    (user_dir(uid) / "books").mkdir(parents=True, exist_ok=True)
    session.clear()
    session["user_id"] = uid
    session["email"] = request.form.get("email", "").strip().lower()
    session.permanent = True
    return redirect("/")


@app.route("/logout", methods=["POST"])
def logout() -> Response:
    session.clear()
    return redirect("/")


# --------------------------------------------------------------------------
# "Load My Books" library management (multi-user mode only)
# --------------------------------------------------------------------------
@app.route("/api/books")
@auth_required
def list_books() -> Response:
    if not MULTI_USER:
        abort(404)
    d = user_dir(session["user_id"])
    manifest = library.load_manifest(d)
    counts: dict[str, int] = {}
    for s in get_db()["songs"]:
        for book in {src["book"] for src in s["sources"]}:
            counts[book] = counts.get(book, 0) + 1
    return jsonify({"books": [
        {"sha256": e["sha256"], "label": e["label"], "status": e["status"],
         "songs": counts.get(e.get("book_id"), 0)}
        for e in manifest
    ]})


@app.route("/api/books/upload", methods=["POST"])
@auth_required
def upload_book() -> Response:
    if not MULTI_USER:
        abort(404)
    f = request.files.get("file")
    if f is None or not f.filename:
        abort(400, "no file uploaded")
    uid = session["user_id"]
    d = user_dir(uid)
    tmp = d / "books" / ".upload.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    f.save(tmp)
    try:
        with open(tmp, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                abort(400, "That file is not a PDF.")
        try:
            doc = pymupdf.open(str(tmp))
            pages = doc.page_count
            doc.close()
        except Exception:
            abort(400, "That PDF could not be opened.")
        if pages < 1:
            abort(400, "That PDF has no pages.")
        entry, err = library.add_upload(d, tmp, f.filename, DB)
        if err:
            abort(409, err)
    finally:
        tmp.unlink(missing_ok=True)
    _user_dbs.pop(uid, None)
    if entry["status"] == "indexed":
        msg = f"Recognized “{entry['label']}” — indexed instantly."
    else:
        msg = ("Saved. Automatic indexing for new books is coming soon — "
               "this book will appear in search once it's indexed.")
    return jsonify({**entry, "message": msg})


@app.route("/api/books/<digest>", methods=["DELETE"])
@auth_required
def remove_book(digest: str) -> Response:
    if not MULTI_USER:
        abort(404)
    uid = session["user_id"]
    d = user_dir(uid)
    entry = next((e for e in library.load_manifest(d)
                  if e["sha256"] == digest), None)
    if entry is None:
        abort(404, "book not found")
    # Close any open PyMuPDF handle so Windows lets us delete the file,
    # and drop rendered pages so a future same-named upload can't serve
    # stale images.
    doc = _docs.pop(str(d / "books" / entry["pdf"]), None)
    if doc is not None:
        doc.close()
    render_png.cache_clear()
    library.delete_book(d, digest, DB)
    _user_dbs.pop(uid, None)
    return jsonify({"deleted": True})


@app.after_request
def _noindex(resp: Response) -> Response:
    if REQUIRE_AUTH:
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def get_doc(pdf_path: Path) -> pymupdf.Document:
    key = str(pdf_path)
    if key not in _docs:
        _docs[key] = pymupdf.open(key)
    return _docs[key]


@lru_cache(maxsize=300)
def render_png(pdf_path: Path, pdf_page: int) -> bytes:
    # Keyed by full path, so users' identically-named books never collide.
    with _render_lock:
        doc = get_doc(pdf_path)
        pix = doc[pdf_page - 1].get_pixmap(dpi=RENDER_DPI)
        return pix.tobytes("png")


@app.route("/")
@auth_required
def home() -> Response:
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/songs")
@auth_required
def songs() -> Response:
    db = get_db()
    if MULTI_USER:
        return jsonify({**db, "user": {"email": session.get("email")}})
    return jsonify(db)


@app.route("/api/page/<book_id>/<int:pdf_page>")
@auth_required
def page_image(book_id: str, pdf_page: int) -> Response:
    db = get_db()
    if book_id not in db["books"]:
        abort(404, "unknown book")
    if not (1 <= pdf_page <= db["books"][book_id]["pages"]):
        abort(404, "page out of range")
    pdf_path = get_books_dir() / db["books"][book_id]["pdf"]
    if not pdf_path.exists():
        abort(404, "book file missing")
    png = render_png(pdf_path, pdf_page)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    # Per-user pages must never be served from a shared cache.
    if MULTI_USER:
        resp.headers["Cache-Control"] = "private, max-age=86400"
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
    REQUIRE_AUTH = args.lan or REQUIRE_AUTH
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
