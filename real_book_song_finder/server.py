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

import chords as chord_store
import index_book
import library

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

# LOAD and DELETE. Multi-user mode keeps a library per account. Local mode keeps
# uploads in their own folder, separate from the master PDFs in BOOKS_DIR, so
# DELETE can never reach those. The private single-password cloud app turns this
# off (LOCAL_LIBRARY=0) because its container disk does not persist.
LOCAL_LIBRARY_DIR = Path(os.environ.get(
    "LOCAL_LIBRARY_DIR", str(APP_DIR / "data" / "local_library")))
LIBRARY_ENABLED = MULTI_USER or os.environ.get("LOCAL_LIBRARY", "1") == "1"
LOCAL_LIBRARY_KEY = 0  # users.db ids start at 1

# Saved chord charts. CHORDS_REVIEWED_ONLY=1 shows only charts you have approved.
CHORDS_PATH = Path(os.environ.get(
    "CHORDS_PATH", str(APP_DIR / "data" / "chords.json")))
CHORDS_REVIEWED_ONLY = os.environ.get("CHORDS_REVIEWED_ONLY") == "1"
CHORD_DAILY_LIMIT = int(os.environ.get("CHORD_DAILY_LIMIT", "200"))
# People's corrections. On your own PC they apply at once. In multi-user mode
# they wait for approval, and only CHORD_ADMIN_EMAILS can approve them.
CHORD_EDITS_PATH = Path(os.environ.get(
    "CHORD_EDITS_PATH", str(APP_DIR / "data" / "chord_edits.json")))
CHORD_EDITS_ENABLED = LIBRARY_ENABLED
CHORD_ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get("CHORD_ADMIN_EMAILS", "").split(",")
    if e.strip()}
MAX_CHART_TEXT = 6000

if MULTI_USER:
    import multiuser

    multiuser.init_db(USERS_DB)
try:  # local dev convenience; deployments set real env vars
    from dotenv import load_dotenv
    load_dotenv(APP_DIR.parent / ".env")
except ImportError:
    pass

# Automatic indexing of unknown books needs a Claude API key.
INDEXING_ENABLED = LIBRARY_ENABLED and bool(os.environ.get("ANTHROPIC_API_KEY"))
# INDEX_SYNC=1: index within the upload request instead of a background
# thread. Used on serverless hosts (Modal) where threads don't survive
# container scale-down. Uploads of unknown books then take 1-3 minutes.
INDEX_SYNC = os.environ.get("INDEX_SYNC") == "1"

# Stripe billing (item 4). Without keys the app runs in "beta mode":
# every account is unlimited and no payment UI is shown.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_MONTHLY = os.environ.get("STRIPE_PRICE_MONTHLY", "")
STRIPE_PRICE_ANNUAL = os.environ.get("STRIPE_PRICE_ANNUAL", "")
PAYMENTS_ENABLED = MULTI_USER and bool(STRIPE_SECRET_KEY)
FREE_BOOK_LIMIT = int(os.environ.get("FREE_BOOK_LIMIT", "1"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("song_finder")

app = Flask(__name__, static_folder=str(APP_DIR / "static"))
if LIBRARY_ENABLED:
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
CHORDS = chord_store.load_store(CHORDS_PATH)
CHORD_EDITS = chord_store.load_edits(CHORD_EDITS_PATH)

# Library indexes, loaded lazily (keyed by user id, or LOCAL_LIBRARY_KEY).
_user_dbs: dict[int, dict] = {}


def user_dir(uid: int) -> Path:
    return USER_DATA_ROOT / str(uid)


def library_key() -> int:
    """Which library this request belongs to."""
    return session["user_id"] if MULTI_USER else LOCAL_LIBRARY_KEY


def library_dir(key: int | None = None) -> Path:
    """The folder holding a library's uploaded books and indexes."""
    if MULTI_USER:
        return user_dir(library_key() if key is None else key)
    return LOCAL_LIBRARY_DIR


def get_db() -> dict:
    """The song index for the current request (shared, or the user's own)."""
    if not LIBRARY_ENABLED:
        return DB
    key = library_key()
    if key not in _user_dbs:
        d = library_dir()
        if MULTI_USER:
            _user_dbs[key] = _load_db(d / "songs.json", d / "metadata.json")
        elif library.load_manifest(d):
            _user_dbs[key] = library.merged_index(d, DB, all_master=True)
        else:
            _user_dbs[key] = DB
    return _user_dbs[key]


def book_pdf_path(book_id: str, db: dict) -> Path:
    """Where a book's PDF lives: uploads in the library, the rest in BOOKS_DIR."""
    pdf = db["books"][book_id]["pdf"]
    if MULTI_USER or book_id.startswith("u_"):
        return library_dir() / "books" / pdf
    return BOOKS_DIR / pdf


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
# LOAD and DELETE: book library management
# --------------------------------------------------------------------------
_lib_lock = threading.Lock()  # serializes manifest/index writes per process
_loading: set[int] = set()    # libraries with a book loading right now


def app_request_only(view):
    """Refuse changes that did not come from this app's own page.

    Local mode has no login, so without this any website open in the same
    browser could post to 127.0.0.1. A browser will not send this custom header
    from another site without a permission check this server never grants.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.headers.get("X-Song-Finder") != "1":
            abort(403, "request did not come from the app")
        return view(*args, **kwargs)
    return wrapped


def _update_manifest_entry(d: Path, digest: str, **fields) -> None:
    with _lib_lock:
        manifest = library.load_manifest(d)
        for e in manifest:
            if e["sha256"] == digest:
                e.update(fields)
        library.save_manifest(d, manifest)


def _index_worker(key: int, d: Path, digest: str, pdf_name: str,
                  label: str) -> None:
    """Background indexing of an unknown book (runs in a daemon thread).

    Note for the Modal deployment: threads don't survive scale-to-zero, so
    there this should become a spawned Modal function (deploy-time change).
    """
    try:
        result = index_book.build_index(
            d / "books" / pdf_name, library.custom_book_key(digest), label)
        idx_path = library.custom_index_path(d, digest)
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        idx_path.write_text(json.dumps(result), encoding="utf-8")
        _update_manifest_entry(d, digest, status="indexed",
                               book_id=library.custom_book_key(digest))
        logger.info("indexed custom book %s for library %d: %d songs",
                    label, key, len(result["songs"]))
    except index_book.IndexingError as e:
        _update_manifest_entry(d, digest, status="failed", error=str(e))
        logger.warning("indexing failed for library %d book %s: %s", key, label, e)
    except Exception:
        logger.exception("unexpected indexing failure (library %d, %s)", key, label)
        _update_manifest_entry(d, digest, status="failed",
                               error="Unexpected error during indexing.")
    finally:
        with _lib_lock:
            library.rebuild_index(d, DB, all_master=not MULTI_USER)
            _loading.discard(key)
        _user_dbs.pop(key, None)


@app.route("/api/books")
@auth_required
def list_books() -> Response:
    if not LIBRARY_ENABLED:
        abort(404)
    d = library_dir()
    key = library_key()
    with _lib_lock:
        manifest = library.load_manifest(d)
        busy = key in _loading
    counts: dict[str, int] = {}
    for s in get_db()["songs"]:
        for book in {src["book"] for src in s["sources"]}:
            counts[book] = counts.get(book, 0) + 1
    books = []
    for e in manifest:
        status, error = e["status"], e.get("error", "")
        if status == "indexing" and not busy:
            # The server stopped partway through, so nothing is working on it.
            status = "failed"
            error = "Indexing was interrupted. Delete this book and load it again."
        books.append({"sha256": e["sha256"], "label": e["label"],
                      "status": status, "error": error,
                      "songs": counts.get(e.get("book_id"), 0)})
    return jsonify({"books": books})


def _save_upload(f, d: Path) -> dict:
    """Check an uploaded PDF and add it to the library."""
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
        with _lib_lock:
            entry, err = library.add_upload(d, tmp, f.filename, DB,
                                            all_master=not MULTI_USER)
        if err:
            abort(409, err)
        return entry
    finally:
        tmp.unlink(missing_ok=True)


@app.route("/api/books/upload", methods=["POST"])
@auth_required
@app_request_only
def upload_book() -> Response:
    """Load one book. A second load is refused until the first has finished."""
    if not LIBRARY_ENABLED:
        abort(404)
    f = request.files.get("file")
    if f is None or not f.filename:
        abort(400, "no file uploaded")
    key = library_key()
    d = library_dir()
    if (PAYMENTS_ENABLED
            and multiuser.get_plan(USERS_DB, key) != "pro"
            and len(library.load_manifest(d)) >= FREE_BOOK_LIMIT):
        # werkzeug has no exception class for 402, so build it directly
        return app.response_class(
            json.dumps({"error": "upgrade_required",
                        "message": f"The free plan includes {FREE_BOOK_LIMIT} "
                                   "book — upgrade to Pro for unlimited books."}),
            status=402, mimetype="application/json")
    with _lib_lock:
        if key in _loading:
            abort(409, "Another book is still loading. Please wait for it "
                       "to finish, then load the next one.")
        _loading.add(key)
    handed_to_worker = False
    try:
        entry = _save_upload(f, d)
        _user_dbs.pop(key, None)
        if entry["status"] == "indexed":
            msg = f"Recognized “{entry['label']}” — indexed instantly."
        elif INDEXING_ENABLED and INDEX_SYNC:
            _update_manifest_entry(d, entry["sha256"], status="indexing")
            handed_to_worker = True
            _index_worker(key, d, entry["sha256"], entry["pdf"], entry["label"])
            done = next((e for e in library.load_manifest(d)
                         if e["sha256"] == entry["sha256"]), entry)
            entry.update(done)
            if entry["status"] == "indexed":
                msg = f"“{entry['label']}” indexed successfully."
            else:
                msg = "Indexing failed: " + entry.get("error", "unknown reason")
        elif INDEXING_ENABLED:
            _update_manifest_entry(d, entry["sha256"], status="indexing")
            entry["status"] = "indexing"
            handed_to_worker = True
            threading.Thread(
                target=_index_worker,
                args=(key, d, entry["sha256"], entry["pdf"], entry["label"]),
                daemon=True,
            ).start()
            msg = (f"“{entry['label']}” saved. Building its song list now, "
                   "which usually takes a minute or two.")
        else:
            msg = (f"“{entry['label']}” saved, but it can't be searched yet "
                   "because automatic indexing needs a Claude API key.")
    finally:
        if not handed_to_worker:
            with _lib_lock:
                _loading.discard(key)
    return jsonify({**entry, "message": msg})


@app.route("/api/books/<digest>", methods=["DELETE"])
@auth_required
@app_request_only
def remove_book(digest: str) -> Response:
    """Delete an uploaded book. The page asks the user to confirm first."""
    if not LIBRARY_ENABLED:
        abort(404)
    key = library_key()
    d = library_dir()
    entry = next((e for e in library.load_manifest(d)
                  if e["sha256"] == digest), None)
    if entry is None:
        abort(404, "book not found")
    with _lib_lock:
        if key in _loading:
            abort(409, "A book is still loading. Please try again when it "
                       "has finished.")
    # Close any open PyMuPDF handle so Windows lets us delete the file,
    # and drop rendered pages so a future same-named upload can't serve
    # stale images.
    with _render_lock:
        doc = _docs.pop(str(d / "books" / entry["pdf"]), None)
        if doc is not None:
            doc.close()
    render_png.cache_clear()
    with _lib_lock:
        library.delete_book(d, digest, DB, all_master=not MULTI_USER)
    _user_dbs.pop(key, None)
    return jsonify({"deleted": True, "label": entry["label"]})


# --------------------------------------------------------------------------
# Chords: saved charts only. No AI model is called from this server.
# --------------------------------------------------------------------------
_chord_lookups: dict[str, dict[int, int]] = {}
_chord_lock = threading.Lock()


def _over_chord_limit() -> bool:
    """Count one lookup and report whether this account is over today's limit.

    Lookups cost nothing, so this only stops the saved charts being scraped. The
    count is kept in memory and restarts if the server restarts.
    """
    if not MULTI_USER:
        return False
    today = datetime.date.today().isoformat()
    uid = session["user_id"]
    with _chord_lock:
        counts = _chord_lookups.setdefault(today, {})
        for day in [d for d in _chord_lookups if d != today]:
            del _chord_lookups[day]
        counts[uid] = counts.get(uid, 0) + 1
        return counts[uid] > CHORD_DAILY_LIMIT


def index_song(song_id: str) -> dict | None:
    """A song from the Real Book index, or None if it isn't listed."""
    return next((s for s in DB["songs"] if s["id"] == song_id), None)


def chart_for(song_id: str) -> dict | None:
    """The chart to use: a person's correction if there is one, else the saved one."""
    edit = CHORD_EDITS["edits"].get(song_id)
    base = CHORDS.get(song_id)
    if not edit:
        return base
    song = index_song(song_id) or {}
    return {"key": song.get("key", "unknown"), **(base or {}),
            "sections": edit["sections"], "review": "approved", "edited": True}


def can_publish() -> bool:
    """Whether this person's corrections apply straight away."""
    return not MULTI_USER or session.get("email", "") in CHORD_ADMIN_EMAILS


def _publish_edit(song_id: str, sections: list[dict], by: str) -> None:
    """Apply a correction. Call with _chord_lock held."""
    old = chart_for(song_id)
    CHORD_EDITS["edits"][song_id] = {
        "sections": sections, "by": by,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "previous": old["sections"] if old else None,
    }


@app.route("/api/chords/songs")
@auth_required
def chord_songs() -> Response:
    """The song choices: every song in the Real Book index, and nothing else."""
    return jsonify({
        "instruments": [{"id": k, "label": v[0]}
                        for k, v in chord_store.INSTRUMENTS.items()],
        "songs": [
            {"id": s["id"], "title": s["title"],
             "composer": s.get("composer", "unknown"),
             "ready": chord_store.is_servable(chart_for(s["id"]),
                                              CHORDS_REVIEWED_ONLY)}
            for s in sorted(DB["songs"], key=lambda s: s["id"])
        ],
        "editing": {"enabled": CHORD_EDITS_ENABLED, "can_publish": can_publish(),
                    "pending": (len(CHORD_EDITS["pending"])
                                if can_publish() else 0)},
    })


@app.route("/api/chords/source")
@auth_required
def chord_source() -> Response:
    """A song's chart as editable text, always in concert pitch."""
    if not CHORD_EDITS_ENABLED:
        abort(404)
    song = index_song(request.args.get("song", ""))
    if song is None:
        abort(404, "That song is not in the Real Book index.")
    chart = chart_for(song["id"])
    return jsonify({"title": song["title"],
                    "text": chord_store.chart_to_text(chart["sections"]) if chart else ""})


@app.route("/api/chords/edit", methods=["POST"])
@auth_required
@app_request_only
def chord_edit() -> Response:
    """Save a correction, or hold it for approval in multi-user mode."""
    if not CHORD_EDITS_ENABLED:
        abort(404)
    body = request.get_json(silent=True) or {}
    song = index_song(str(body.get("song", "")))
    if song is None:
        abort(404, "That song is not in the Real Book index.")
    text = str(body.get("text", ""))
    if len(text) > MAX_CHART_TEXT:
        abort(400, "That chart is too long.")
    sections, problem = chord_store.parse_chart_text(text)
    if problem:
        return app.response_class(json.dumps({"message": problem}), status=400,
                                  mimetype="application/json")
    if _over_chord_limit():
        return app.response_class(
            json.dumps({"message": "You've reached today's limit. Please try "
                                   "again tomorrow."}),
            status=429, mimetype="application/json")
    by = session.get("email", "owner")
    published = can_publish()
    with _chord_lock:
        if published:
            _publish_edit(song["id"], sections, by)
        else:
            CHORD_EDITS["pending"][secrets.token_hex(6)] = {
                "song": song["id"], "title": song["title"],
                "sections": sections, "by": by,
                "at": datetime.datetime.now(
                    datetime.timezone.utc).isoformat(timespec="seconds"),
            }
        chord_store.save_edits(CHORD_EDITS_PATH, CHORD_EDITS)
    return jsonify({"published": published})


@app.route("/api/chords/pending")
@auth_required
def chord_pending() -> Response:
    """Suggested corrections waiting for approval."""
    if not CHORD_EDITS_ENABLED or not can_publish():
        abort(403, "only the owner can review suggestions")
    return jsonify({"pending": [
        {"id": key, "title": item["title"], "by": item["by"],
         "text": chord_store.chart_to_text(item["sections"]),
         "current": (chord_store.chart_to_text(chart_for(item["song"])["sections"])
                     if chart_for(item["song"]) else "")}
        for key, item in CHORD_EDITS["pending"].items()
    ]})


@app.route("/api/chords/pending/<key>", methods=["POST"])
@auth_required
@app_request_only
def chord_pending_decide(key: str) -> Response:
    """Approve or reject one suggested correction."""
    if not CHORD_EDITS_ENABLED or not can_publish():
        abort(403, "only the owner can review suggestions")
    action = (request.get_json(silent=True) or {}).get("action")
    if action not in ("approve", "reject"):
        abort(400, "unknown action")
    with _chord_lock:
        item = CHORD_EDITS["pending"].pop(key, None)
        if item is None:
            abort(404, "suggestion not found")
        if action == "approve":
            _publish_edit(item["song"], item["sections"], item["by"])
        chord_store.save_edits(CHORD_EDITS_PATH, CHORD_EDITS)
    return jsonify({"done": True})


@app.route("/api/chords/lookup")
@auth_required
def chord_lookup() -> Response:
    """Saved chords for one Real Book song, written for one instrument."""
    song_id = request.args.get("song", "")
    instrument = request.args.get("instrument", "concert")
    song = index_song(song_id)
    if song is None:
        abort(404, "That song is not in the Real Book index.")
    if instrument not in chord_store.INSTRUMENTS:
        abort(400, "unknown instrument")
    if _over_chord_limit():
        return app.response_class(
            json.dumps({"message": "You've reached today's chord lookup limit. "
                                   "Please try again tomorrow."}),
            status=429, mimetype="application/json")
    entry = chart_for(song_id)
    if not chord_store.is_servable(entry, CHORDS_REVIEWED_ONLY):
        return jsonify({"id": song_id, "title": song["title"],
                        "composer": song.get("composer", "unknown"),
                        "available": False})
    return jsonify({
        **chord_store.transpose_chart(entry, instrument),
        "id": song_id, "title": song["title"],
        "composer": song.get("composer", "unknown"),
        "available": True,
        "reviewed": entry.get("review") == "approved",
        "edited": bool(entry.get("edited")),
        "source_book": entry.get("source_book"),
        "source_page": entry.get("source_page"),
    })


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
        plan = multiuser.get_plan(USERS_DB, session["user_id"])
        return jsonify({**db, "library": True,
                        "user": {"email": session.get("email"),
                                 "plan": plan,
                                 "billing": PAYMENTS_ENABLED}})
    return jsonify({**db, "library": LIBRARY_ENABLED})


@app.route("/api/page/<book_id>/<int:pdf_page>")
@auth_required
def page_image(book_id: str, pdf_page: int) -> Response:
    db = get_db()
    if book_id not in db["books"]:
        abort(404, "unknown book")
    if not (1 <= pdf_page <= db["books"][book_id]["pages"]):
        abort(404, "page out of range")
    pdf_path = book_pdf_path(book_id, db)
    if not pdf_path.exists():
        abort(404, "book file missing")
    png = render_png(pdf_path, pdf_page)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    # Per-user pages must never be served from a shared cache.
    if MULTI_USER:
        resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# --------------------------------------------------------------------------
# Stripe billing (multi-user mode with STRIPE_SECRET_KEY configured)
# --------------------------------------------------------------------------
@app.route("/api/billing/checkout", methods=["POST"])
@auth_required
def billing_checkout() -> Response:
    if not PAYMENTS_ENABLED:
        abort(404)
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    plan = (request.get_json(silent=True) or {}).get("plan", "annual")
    price = STRIPE_PRICE_MONTHLY if plan == "monthly" else STRIPE_PRICE_ANNUAL
    if not price:
        abort(500, "price not configured")
    checkout = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price, "quantity": 1}],
        client_reference_id=str(session["user_id"]),
        customer_email=session.get("email"),
        success_url=request.host_url.rstrip("/") + "/?upgraded=1",
        cancel_url=request.host_url,
        allow_promotion_codes=True,
    )
    return jsonify({"url": checkout.url})


@app.route("/api/billing/portal", methods=["POST"])
@auth_required
def billing_portal() -> Response:
    if not PAYMENTS_ENABLED:
        abort(404)
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    customer = multiuser.get_stripe_customer(USERS_DB, session["user_id"])
    if not customer:
        abort(400, "no billing account yet")
    portal = stripe.billing_portal.Session.create(
        customer=customer, return_url=request.host_url)
    return jsonify({"url": portal.url})


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook() -> Response:
    if not PAYMENTS_ENABLED:
        abort(404)
    import stripe
    try:
        event = stripe.Webhook.construct_event(
            request.data, request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET)
    except Exception:
        abort(400, "invalid signature")
    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "checkout.session.completed":
        uid = int(obj.get("client_reference_id") or 0)
        if uid:
            multiuser.set_stripe_ids(USERS_DB, uid, obj.get("customer"),
                                     obj.get("subscription"))
            multiuser.set_plan(USERS_DB, uid, "pro")
            logger.info("user %d upgraded to pro", uid)
    elif etype in ("customer.subscription.deleted",
                   "customer.subscription.updated"):
        uid = multiuser.find_by_stripe_customer(USERS_DB, obj.get("customer"))
        if uid:
            active = (etype == "customer.subscription.updated"
                      and obj.get("status") in ("active", "trialing"))
            multiuser.set_plan(USERS_DB, uid, "pro" if active else "free")
            logger.info("user %d plan -> %s (stripe %s)", uid,
                        "pro" if active else "free", etype)
    return jsonify({"received": True})


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
