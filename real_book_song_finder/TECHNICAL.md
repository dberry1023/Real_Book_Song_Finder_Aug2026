# Technical Overview: From Local App to Password-Protected Cloud Deployment

*How the Real Book Song Finder went from a localhost-only Flask app to a
private web app reachable from anywhere — July 2026.*

## 1. Starting point

The app began as a purely local tool:

- **`server.py`** — a ~165-line Flask server on `127.0.0.1:8765` with three routes:
  - `GET /` → serves `static/index.html` (a single-file vanilla-JS frontend)
  - `GET /api/songs` → the full song database as JSON
  - `GET /api/page/<book_id>/<pdf_page>` → one sheet-music page rendered to PNG
- **Rendering** — PyMuPDF opens the source PDFs lazily (one cached
  `Document` per book), renders pages at 170 DPI inside a global
  `threading.Lock` (PyMuPDF rendering is not thread-safe), and memoizes
  results with `functools.lru_cache(maxsize=300)`.
- **Data** — `data/songs.json` (1,780 songs, each with one or more
  `{book, printed, pdf_page}` sources) merged at startup with
  `data/metadata.json` (composer/key per song).
- **The PDFs** — 7 Real Book PDFs (~218 MB total) sitting in a OneDrive
  folder, referenced by a hardcoded Windows path.
- **Auth** — an optional `--lan` mode for Tailscale use: a token in
  `data/.access_token`, accepted as `?key=` and remembered in a signed
  Flask session cookie. In plain local mode auth is off entirely.

Three things blocked running this anywhere else: the hardcoded PDF path,
secrets stored as files written at startup (impossible on a read-only
container filesystem), and auth that defaulted to *off* and could only be
enabled by a CLI flag that never runs under a WSGI host.

## 2. What changed in `server.py`

The design constraint was: **local behavior must remain byte-for-byte
identical** — every change is an environment-variable override whose
default is the old behavior.

| Concern | Before | After |
|---|---|---|
| PDF location | hardcoded Windows path | `BOOKS_DIR` env var, defaulting to the old path |
| Session secret | always written/read from `data/.session_secret` | `SESSION_SECRET` env var wins; file fallback locally |
| Access token | always written/read from `data/.access_token` | `ACCESS_TOKEN` env var wins; file fallback locally |
| Auth switch | `REQUIRE_AUTH = False`, flipped only in `__main__` by `--lan` | module-level `REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH") == "1"`; `--lan` still ORs it on locally |

The env-var auth switch matters because under a WSGI host the
`if __name__ == "__main__":` block never executes — the module is
imported, so anything set there would silently never apply.

Two user-facing additions, both active only when `REQUIRE_AUTH` is on:

- **Login page** — previously an unauthenticated browser got a bare
  `403`. Now `auth_required` returns a small inline HTML login form for
  `GET /` (API routes still get 403 JSON, and the original `?key=`
  bookmark flow still works). A new `POST /login` route compares the
  submitted password with `secrets.compare_digest` (constant-time, no
  timing side channel), sets `session["authed"] = True` with
  `session.permanent = True`, and redirects. Sessions are signed cookies
  (`itsdangerous` via Flask's `secret_key`) with a 90-day lifetime, so a
  device stays logged in.
- **`X-Robots-Tag: noindex, nofollow`** on every response via an
  `after_request` hook, so search engines never index the URL.

## 3. The Modal deployment (`modal_app.py`)

[Modal](https://modal.com) runs Python functions in containers on demand
and can expose a Flask app as a web endpoint. The whole deployment is one
file:

```python
app = modal.App("real-book-finder")
vol = modal.Volume.from_name("real-books", create_if_missing=True)

image = (modal.Image.debian_slim(python_version="3.12")
    .pip_install("flask>=3.0.0", "pymupdf>=1.24.0")
    .env({"BOOKS_DIR": "/books", "REQUIRE_AUTH": "1"})
    .add_local_python_source("server")
    .add_local_dir("static", remote_path="/root/static")
    .add_local_file("data/songs.json", remote_path="/root/data/songs.json")
    .add_local_file("data/metadata.json", remote_path="/root/data/metadata.json"))

@app.function(image=image, volumes={"/books": vol},
              secrets=[modal.Secret.from_name("realbook-auth")],
              memory=1024, timeout=120)
@modal.concurrent(max_inputs=20)
@modal.wsgi_app()
def web():
    from server import app as flask_app
    return flask_app
```

The pieces, and why each lives where it does:

- **Container image** — Debian slim + Flask + PyMuPDF, plus the app code,
  the frontend, and the two small JSON indexes (< 1 MB combined). These
  redeploy in seconds whenever the code changes.
- **`modal.Volume` "real-books"** — the 7 PDFs (~218 MB) uploaded once
  with `modal volume put`, mounted read-only in practice at `/books`.
  Keeping them out of the image means code redeploys never re-upload
  them. `BOOKS_DIR=/books` points the unchanged rendering code at the
  mount.
- **`modal.Secret` "realbook-auth"** — holds `ACCESS_TOKEN` (the login
  password) and `SESSION_SECRET` (cookie-signing key). Injected as env
  vars at runtime; never present in code, git, or the image. Because the
  session secret is stable across container restarts, login cookies
  survive scale-to-zero cycles.
- **`@modal.wsgi_app()`** — wraps the Flask app; Modal terminates TLS and
  provides the stable URL `https://statsias--real-book-finder-web.modal.run`.
  The frontend needed **zero changes** because every fetch was already a
  relative same-origin path.
- **`@modal.concurrent(max_inputs=20)`** — one container handles up to 20
  simultaneous requests with threads, mirroring the original
  `app.run(threaded=True)` design. The global render lock serializes
  PyMuPDF anyway, and the `lru_cache` + browser-side
  `Cache-Control: public, max-age=86400` keep repeat page loads off the
  renderer entirely.
- **Scale-to-zero** — no `min_containers`, so the container shuts down
  after idle and costs nothing until the next request (~3–5 s cold
  start: pull cached image, import Flask/PyMuPDF, lazy-open a PDF on
  first render).

## 4. Security / copyright posture

The books are copyrighted, so the deployment is deliberately **private
personal access, not publication**:

- Every route requires authentication (`REQUIRE_AUTH=1` baked into the
  image env — it cannot be off in the cloud).
- Password checked in constant time; sessions are signed, not guessable.
- `X-Robots-Tag: noindex, nofollow` keeps the URL out of search engines.
- The PDFs exist only in a private Modal volume; the git repos contain
  code and title/page indexes only.
- Secrets live in a Modal Secret; `data/.access_token` and
  `data/.session_secret` remain gitignored for local use.

## 5. Operations

| Task | Command (from the project folder) |
|---|---|
| Redeploy after code change | `python -m modal deploy modal_app.py` |
| Change the password | `python -m modal secret create realbook-auth ACCESS_TOKEN=<new> SESSION_SECRET=<random> --force`, then redeploy |
| Replace/add a PDF | `python -m modal volume put real-books <local.pdf> /<name.pdf>` |
| Inspect the volume | `python -m modal volume ls real-books` |
| Logs / dashboard | https://modal.com/apps/statsias/main/deployed/real-book-finder |

Windows quirks: the Modal CLI isn't on `PATH` here, so use
`python -m modal ...`; and set `PYTHONUTF8=1` first or Modal's
box-drawing output crashes PowerShell's cp1252 console.

Verification performed after deploy: unauthenticated requests → 403 and
login page with noindex header; wrong password rejected; correct password
→ 1,780 songs and PNG page renders from three different books; `?key=`
flow works; local `Start_Song_Finder.bat` re-tested unchanged (no login,
full access).

## 6. Cost

Modal Starter plan: $30/month of included compute credits, no card on
file (workloads stop rather than bill if exceeded). This app bills only
while a container is awake (~$0.05–0.15/hour of active use at
1 CPU / 1 GB); storage for 218 MB is negligible. Practical cost for
personal use: **$0/month**.
