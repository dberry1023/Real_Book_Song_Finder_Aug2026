# Technical Overview

*How the Real Book Song Finder went from a localhost-only Flask app to a
private web app reachable from anywhere (July 2026), then gained per-user
libraries, chords for every instrument and a chord editor (November 2026).
Sections 1 to 6 describe the July work as it was done. Sections 7 onward
describe what came after.*

## Current state (9 November 2026)

| Piece | What it is |
|---|---|
| `server.py` | Flask app. Search, page rendering, LOAD and DELETE, chords, editor, auth, billing |
| `static/index.html` | Single-file frontend, including the Hide list button |
| `library.py`, `index_book.py` | Book libraries and automatic indexing of uploaded books |
| `chords.py` | Saved charts, transposition, chart checks and the editor's text format |
| `generate_chords.py` | Builds `data/chords.json`, from memory or from the printed pages |
| `multiuser.py` | Accounts and plans for multi-user mode |
| `modal_app.py` | Private single-password cloud app (`real-book-finder`) |
| `modal_saas.py` | Multi-user cloud app (`song-finder-saas`), live since 5 November 2026 |
| `tests/` | 115 pytest tests |

Main routes: `/api/songs`, `/api/page/<book>/<page>`, `/api/books`,
`/api/books/upload`, `DELETE /api/books/<sha256>`, `/api/chords/songs`,
`/api/chords/lookup`, `/api/chords/source`, `POST /api/chords/edit`,
`/api/chords/pending` and `POST /api/chords/pending/<id>`. Every change to a
library or a chart needs the `X-Song-Finder: 1` header.

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

## 7. LOAD, DELETE and Chords (November 2026)

### LOAD and DELETE

| Mode | Library folder | Master PDFs |
|---|---|---|
| Local (`python server.py`, `--lan`) | `data/local_library/` | Stay in `BOOKS_DIR`, always included |
| Multi-user (`MULTI_USER=1`) | `USER_DATA_ROOT/<id>/` | Users upload their own copies |
| Private cloud (`modal_app.py`) | Off (`LOCAL_LIBRARY=0`) | Modal volume |

- `library_dir()` and `library_key()` hide the difference between the modes, so
  one set of routes serves both. Local mode uses key `0`, which no account has.
- `library.merged_index(..., all_master=True)` adds uploads on top of the master
  index. It is computed in memory for local mode, so a rebuilt master index can't
  be masked by an old copy.
- `book_pdf_path()` reads `u_*` books from the library and everything else from
  `BOOKS_DIR`. `library.delete_book()` only touches files inside the library
  folder and only acts on manifest entries, so no route can delete a master PDF.
- **One at a time:** the `_loading` set refuses a second upload until the first
  has finished indexing, and refuses deletes during that time. If the server
  stops partway through, `/api/books` reports the book as failed so it can be
  deleted rather than staying stuck on "indexing".
- **Confirmation:** the page asks "Are you sure…?" with **Yes, delete it** and
  **No, keep it** before it sends the `DELETE`.
- **Cross-site protection:** local mode has no login, so another website could
  otherwise post to `127.0.0.1`. `app_request_only` requires an `X-Song-Finder`
  header on upload and delete. Browsers won't send a custom header cross-site
  without a CORS grant, and this server gives none.

### Chords

```text
generate_chords.py --> Claude (once per song) --> data/chords.json (concert pitch)
                                                        |
browser form --> GET /api/chords/lookup --> chords.transpose_chart() --> chart
```

- `server.py` has no AI call on this path, so lookups cost nothing and need no
  API key.
- `/api/chords/lookup` returns 404 unless the song id is in the master index,
  and 400 unless the instrument is one of `chords.INSTRUMENTS`.
- `chords.transpose_chord()` changes only the root and slash bass, so qualities
  such as `m7b5`, `7#9` and `6/9` pass through unchanged. Sharps or flats follow
  the written key.
- `is_servable()`: rejected charts never show, approved charts always show, and
  anything else needs `CHORDS_REVIEWED_ONLY` off and enough confidence: high for
  charts from memory, high or medium for charts read from the page (see below).
- `generate_chords.py --all` is safe to stop and rerun. Songs the model declines
  go in `data/chord_declined.json` so they aren't paid for twice.
- Review order is unverified composer or key first (139 songs), then the songs
  printed in the most books.

## 8. Chord accuracy test (November 2026)

Twenty tunes were run three ways: 12 well known and 8 less common. Results are in
`data/chord_test/`, which git ignores. Accuracy below is a judgment from knowledge
of these tunes, not a bar-by-bar comparison with the printed pages.

| Method | Charts returned | Result | Measured cost per song |
|---|---|---|---|
| Haiku 5.5, from memory | 8 of 20 | Not usable. Giant Steps and All The Things You Are were wrong. | $0.0005 |
| Sonnet 5.5, from memory | 13 of 20 | 8 rated high confidence: 5 looked right, 3 had errors. Declined 7 of the 8 less common tunes. | $0.0049 |
| Sonnet 5.5, reading the printed page | 15 of 20 | 7 looked right, 1 misread (Blue Bossa), 7 need checking. Worked on the less common tunes. | $0.0142 |
| Opus 5.5, high effort, from memory (21 tunes) | 21 of 21 | All 7 high-confidence charts looked right. About 5 of 7 medium looked right or close. Low were mostly guesses. | $0.027 |
| Fable 5.1, high effort, from memory (21 tunes) | 21 of 21 | 9 of 11 high-confidence charts looked right. | $0.094 |

The Sonnet and Haiku rows used low effort and told the model to decline songs it
wasn't sure of. The Opus and Fable rows used high effort and asked for the version
most commonly played, which is closer to how a person asks Claude directly.

### Full run

Opus 5.5 at high effort, through the Batches API at half price, cost **$18.42**.
All 1,775 requests succeeded.

| Result | Songs |
|---|---|
| High confidence | 25 |
| Medium confidence | 205 |
| Low confidence | 1,386 |
| Declined | 164 |

Findings:

- Far fewer songs rated high than in the test, because the test tunes were
  unusually famous.
- **The ratings were then tested.** 58 charts were compared bar by bar with the
  printed page: Opus read each page without seeing its own chart, then Sonnet
  compared the two. Average bars with the same harmony: **high 89%, medium 54%,
  low 25%**. The low group is not just cautious: many of its charts have the wrong
  form or changes unrelated to the printed tune.
- A difference isn't always a Claude error, because the books have mistakes and
  tunes have alternate changes. Page readings can also be wrong.
- iReal Pro and iGigBook appear to use charts entered by people, so the owner chose
  to read the printed pages and add an editor. The owner, an attorney, reviewed and
  approved showing these charts to customers on 5 November 2026.

### Reading the printed pages

`generate_chords.py --pages-submit` sends Claude Opus 5.5 at high effort each song's
page as a 200 DPI greyscale image, through half-price batches split at 30 MB. A
first attempt at 200 MB lost its connection during the upload, and Anthropic's
batch list confirmed it never arrived. Each finished upload is saved straight away,
so a dropped one costs nothing and a rerun sends only the songs left.

- Typeset books are preferred over handwritten ones (`book_order()`).
- `page_span()` also sends later pages, up to three, when the next song starts later.
- `page_entry()` saves a chart only if the title printed on the page matches the
  song, so a wrong page number can't attach another tune's chords.
- `clean_bar()` removes ending marks and words. A chart with no chords isn't saved.
- The 80 songs with flagged page numbers are skipped, as are high-confidence charts,
  approved charts and charts already read.
- The first results copy `chords.json` to `data/chords_from_memory_backup.json`.

In a 20-song trial, all 20 were saved for $0.53 ($0.0265 each), and 3 of the 20 were
checked by eye against their pages and matched bar for bar, including 2 handwritten.
One song (Flags) had no chords printed, which added the rule above. Opus rated 17 of
the 20 medium, including those matching pages, so `is_servable()` shows charts read
from the page at high or medium, and charts from memory at high only. This applies
in the multi-user app too, with `CHORDS_REVIEWED_ONLY=1` available to limit it to
approved charts.

The full run read 1,658 charts for **$43.70** in total, rated 418 high, 1,228 medium
and 12 low. With the 25 high-confidence charts from memory, **1,671 of 1,780 songs
show**. The other 109 are 78 songs with flagged page numbers, 19 where nothing could
be saved (no chords printed, unreadable, or another title on the page), and 12
low-confidence readings. Five readings checked by eye all matched. Seven charts are
more than 30% blank bars and should be looked at: Fox Hunt, Golden Notebooks, Horizon,
Hotel Vamp, In Case You Haven't Heard, Mysterious Traveller and Señor Mouse.

### Chord editor

- `chart_for()` puts a person's correction ahead of the saved chart. Corrections live
  in `chord_edits.json`, separate from `chords.json`, with the previous chart kept.
- `parse_chart_text()` accepts `[Section] chord chord | chord` and refuses anything
  that isn't a chord symbol, with a plain message.
- Local mode publishes at once. In multi-user mode, only `CHORD_ADMIN_EMAILS` publish,
  and everyone else's edit waits in `pending` for approval.
- Editing is off in the private cloud app, whose disk does not persist.
- `data/chords.json` is gitignored because `Real_Book_Song_Finder_Aug2026` is
  public, and is force-added only to the private `real-book-song-finder` repo.

## 9. Hide list (9 November 2026)

- **◀ Hide list**, at the left of the toolbar, or the `\` key adds the
  `list-hidden` class to `<body>`, which hides `#sidebar`.
- `applyZoom()` caps **Fit** at 1,100 px with the list showing and at `ZOOM_MAX`
  (2,600 px) with it hidden, so the music uses the whole window. In testing,
  500 Miles High went from 1,100 to 1,890 px wide on a 1,920 px screen.
- `/` shows the list again and focuses search. The `\` key is ignored while
  typing in a box. The choice is kept in `localStorage`, wrapped in `try`, so
  the page still works where storage is blocked.

## 10. The multi-user deployment (5 November 2026)

| Modal resource | Holds |
|---|---|
| App `song-finder-saas` | `https://statsias--song-finder-saas-web.modal.run` |
| Secret `songfinder-saas` | `SESSION_SECRET`, `SIGNUP_CODE`, `ANTHROPIC_API_KEY`, later Stripe keys |
| Secret `songfinder-admins` | `CHORD_ADMIN_EMAILS`, kept apart so the main secret needn't be retyped |
| Volume `songfinder-users` | `users.db`, each account's books and indexes, `chord_edits.json` |

- The image bundles `data/chords.json` when it exists, so redeploy after
  regenerating charts. The chord data stays out of the public GitHub repo.
- `CHORDS_REVIEWED_ONLY` is not set, so customers see the same charts as the
  owner. The owner approved this on 5 November 2026.
- Checks after deploy: login and signup pages load, and every private route
  returns 403 without a session. Inside the running container (read-only, through
  `modal container exec`): 1,757 charts loaded, 1,671 showing, the admin email
  set, and the page carrying LOAD, DELETE, Chords and the editor. Nobody signed in
  on the live site, to keep test accounts out of the real user list.
- `max_containers=1` keeps a single writer for SQLite, and `INDEX_SYNC=1` indexes
  uploads inside the request because threads don't survive scale-down.
