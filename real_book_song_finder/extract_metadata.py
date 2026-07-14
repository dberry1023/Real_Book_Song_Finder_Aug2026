"""Extract composer and key for every song using Claude vision.

Renders two high-res crops of each song's first page (title/composer/first
staff, and the final bars) and asks Claude Sonnet to count the key-signature
accidentals, read the last chord, and name the key. If the page shows a
different tune than expected (bad index entry), the song's other sources and
neighboring pages are tried; unresolvable songs are flagged page_mismatch.
Results are written incrementally to data/metadata.json so the script can
be stopped and re-run at any time — already-done songs are skipped.

Usage:
    python extract_metadata.py            # process songs not yet in metadata.json
    python extract_metadata.py --upgrade  # redo songs still carrying old-method data
    python extract_metadata.py --redo     # redo everything
    python extract_metadata.py --only "tune up"   # redo matching songs
"""
import argparse
import base64
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import difflib
import unicodedata

import anthropic
import pymupdf
from dotenv import load_dotenv

APP_DIR = Path(__file__).parent
BOOKS_DIR = Path(r"C:\Users\stats\OneDrive\Desktop\Jazz Night School\Real_Books")
DATA_DIR = APP_DIR / "data"
META_PATH = DATA_DIR / "metadata.json"

MODEL = "claude-sonnet-5"
RENDER_DPI = 240          # high enough to count key-signature accidentals
WORKERS = 4

VALID_KEYS = {
    "C", "Db", "D", "Eb", "E", "F", "F#", "Gb", "G", "Ab", "A", "Bb", "B",
    "Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "G#m", "Am", "Bbm", "Bm",
    "unknown",
}

PROMPT = """You are looking at two high-resolution crops of one jazz chart from a Real Book:
the FIRST image is the top of the page (title, composer credit, and the first
one or two staves), the SECOND image is the bottom of the page (the final bars).

Work step by step:
1. Read the title printed at the top.
2. Read the composer credit (usually top-right under or beside the title).
3. Look at the very start of the first staff, right after the clef: carefully
   COUNT the sharps or flats in the key signature. Be precise - this decides
   the key. (0 accidentals = C/Am, 1 flat = F/Dm, 2 flats = Bb/Gm,
   3 flats = Eb/Cm, 4 flats = Ab/Fm, 5 flats = Db/Bbm, 1 sharp = G/Em,
   2 sharps = D/Bm, 3 sharps = A/F#m, 4 sharps = E/C#m, 5 sharps = B/G#m.)
4. Read the FIRST chord symbol of the form and the LAST chord symbol at the end
   of the tune (second image). Decide major vs relative minor from these: if
   the tune starts/ends on the minor tonic, report the minor key.

Reply with ONLY a JSON object, no other text:
{
  "title": "<title as printed>",
  "composer": "<name(s) as printed, ' / ' between multiple; 'unknown' if illegible>",
  "sig": "<number and type of accidentals, e.g. '2 sharps', '1 flat', 'none'>",
  "last_chord": "<final chord symbol, e.g. 'Bb6', or 'unknown'>",
  "key": "<one of: C Db D Eb E F F# Gb G Ab A Bb B Cm C#m Dm Ebm Em Fm F#m Gm G#m Am Bbm Bm unknown>"
}"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extract")

_docs: dict[str, pymupdf.Document] = {}
_render_lock = threading.Lock()
_save_lock = threading.Lock()


def render_crops(books: dict, book_id: str, pdf_page: int) -> tuple[bytes, bytes]:
    """Return (top 45% of page, bottom 22% of page) as high-res JPEGs."""
    with _render_lock:
        if book_id not in _docs:
            _docs[book_id] = pymupdf.open(str(BOOKS_DIR / books[book_id]["pdf"]))
        page = _docs[book_id][pdf_page - 1]
        r = page.rect
        top = pymupdf.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * 0.45)
        bottom = pymupdf.Rect(r.x0, r.y0 + r.height * 0.78, r.x1, r.y1)
        return (page.get_pixmap(dpi=RENDER_DPI, clip=top).tobytes("jpeg"),
                page.get_pixmap(dpi=RENDER_DPI, clip=bottom).tobytes("jpeg"))


def parse_reply(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    composer = str(obj.get("composer", "unknown")).strip() or "unknown"
    key = str(obj.get("key", "unknown")).strip()
    if key not in VALID_KEYS:
        key = "unknown"
    return {"composer": composer, "key": key,
            "seen_title": str(obj.get("title", "")).strip(),
            "sig": str(obj.get("sig", "")).strip(),
            "last_chord": str(obj.get("last_chord", "")).strip()}


def _norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for art in ("the ", "a ", "an "):
        if t.startswith(art):
            t = t[len(art):]
    return t


def titles_match(expected: str, seen: str) -> bool:
    a, b = _norm_title(expected), _norm_title(seen)
    if not b:
        return True  # nothing legible to compare against; accept
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _img_block(data: bytes) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(data).decode()}}


def ask_page(client: anthropic.Anthropic, books: dict,
             book_id: str, pdf_page: int) -> dict | None:
    top, bottom = render_crops(books, book_id, pdf_page)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,  # Sonnet thinks before answering; 300 truncated replies to nothing
        messages=[{
            "role": "user",
            "content": [_img_block(top), _img_block(bottom),
                        {"type": "text", "text": PROMPT}],
        }],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return parse_reply(text)


def process_song(client: anthropic.Anthropic, books: dict, song: dict) -> dict | None:
    """Try the song's sources in order; if the page shows a different tune
    (bad index entry), also try neighboring pages of the first source."""
    src0 = song["sources"][0]
    candidates = [(s["book"], s["pdf_page"]) for s in song["sources"]]
    for delta in (1, -1, 2, -2):
        pg = src0["pdf_page"] + delta
        if 1 <= pg <= books[src0["book"]]["pages"]:
            candidates.append((src0["book"], pg))

    first = None
    for book_id, pdf_page in candidates:
        result = ask_page(client, books, book_id, pdf_page)
        if result is None:
            continue
        if first is None:
            first = result
        if titles_match(song["title"], result["seen_title"]):
            return result
    if first is not None:
        first["page_mismatch"] = True  # keep the original page's read, flagged
    return first


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max songs this run (0 = all)")
    ap.add_argument("--only", default="", help="reprocess only songs whose id contains this")
    ap.add_argument("--redo", action="store_true", help="reprocess everything, ignoring metadata.json")
    ap.add_argument("--upgrade", action="store_true",
                    help="reprocess only songs still carrying old-method data (no 'sig' field)")
    ap.add_argument("--mismatch", action="store_true",
                    help="reprocess only songs currently flagged page_mismatch")
    args = ap.parse_args()

    load_dotenv(APP_DIR.parent / ".env")
    client = anthropic.Anthropic()

    db = json.loads((DATA_DIR / "songs.json").read_text(encoding="utf-8"))
    meta: dict[str, dict] = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))

    if args.only:
        todo = [s for s in db["songs"] if args.only.lower() in s["id"]]
    elif args.upgrade:
        todo = [s for s in db["songs"] if "sig" not in meta.get(s["id"], {})]
    elif args.mismatch:
        todo = [s for s in db["songs"] if meta.get(s["id"], {}).get("page_mismatch")]
    elif args.redo:
        todo = db["songs"]
    else:
        todo = [s for s in db["songs"] if s["id"] not in meta]
    if args.limit:
        todo = todo[: args.limit]
    log.info("%d songs total, %d already done, %d to process",
             len(db["songs"]), len(meta), len(todo))

    def save() -> None:
        with _save_lock:
            META_PATH.write_text(
                json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")

    done_count = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(process_song, client, db["books"], s): s for s in todo}
        for fut in as_completed(futures):
            song = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                log.warning("FAILED %s: %s", song["title"], e)
                continue
            if result is None:
                log.warning("unparseable reply for %s", song["title"])
                continue
            meta[song["id"]] = result
            done_count += 1
            if done_count % 25 == 0:
                save()
                log.info("progress: %d/%d  (%s -> %s, %s)", done_count, len(todo),
                         song["title"], result["composer"], result["key"])
    save()
    log.info("finished: %d newly processed, %d total in metadata.json",
             done_count, len(meta))


if __name__ == "__main__":
    main()
