"""Extract composer and key for every song using Claude vision.

Renders the first (most legible) source page of each song from the PDFs,
sends it to Claude Haiku, and asks for the composer credit and the key.
Results are written incrementally to data/metadata.json so the script can
be stopped and re-run at any time — already-done songs are skipped.

Usage:
    python extract_metadata.py            # process everything not yet done
    python extract_metadata.py --limit 5  # small test run
"""
import argparse
import base64
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
import pymupdf
from dotenv import load_dotenv

APP_DIR = Path(__file__).parent
BOOKS_DIR = Path(r"C:\Users\stats\OneDrive\Desktop\Jazz Night School\Real_Books")
DATA_DIR = APP_DIR / "data"
META_PATH = DATA_DIR / "metadata.json"

MODEL = "claude-haiku-4-5"
RENDER_DPI = 130          # enough to read the credit line and key signature
WORKERS = 4

VALID_KEYS = {
    "C", "Db", "D", "Eb", "E", "F", "F#", "Gb", "G", "Ab", "A", "Bb", "B",
    "Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "G#m", "Am", "Bbm", "Bm",
    "unknown",
}

PROMPT = """This is a page of jazz sheet music from a Real Book. Look at it and report:

1. "composer": the composer credit printed on the chart (usually top-right, under
   or beside the title). Give the name(s) as printed, e.g. "Miles Davis" or
   "John Coltrane". If two names are given (composer/lyricist) include both
   joined with " / ". If no composer is legible, use "unknown".
2. "key": the key of the tune, judged from the key signature at the start of the
   staves plus the first/last chord symbols. Use one of exactly these values:
   C, Db, D, Eb, E, F, F#, Gb, G, Ab, A, Bb, B for major keys, or
   Cm, C#m, Dm, Ebm, Em, Fm, F#m, Gm, G#m, Am, Bbm, Bm for minor keys.
   If you cannot tell, use "unknown". Prefer the minor spelling when the tune is
   clearly in the relative minor (e.g. one flat with a D minor tonality is "Dm").
3. "title": the title printed at the top of the page, so we can sanity-check
   that the page shows the expected tune.

Reply with ONLY a JSON object, no other text:
{"composer": "...", "key": "...", "title": "..."}"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extract")

_docs: dict[str, pymupdf.Document] = {}
_render_lock = threading.Lock()
_save_lock = threading.Lock()


def render_page(books: dict, book_id: str, pdf_page: int) -> bytes:
    with _render_lock:
        if book_id not in _docs:
            _docs[book_id] = pymupdf.open(str(BOOKS_DIR / books[book_id]["pdf"]))
        pix = _docs[book_id][pdf_page - 1].get_pixmap(dpi=RENDER_DPI)
        return pix.tobytes("jpeg")


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
            "seen_title": str(obj.get("title", "")).strip()}


def process_song(client: anthropic.Anthropic, books: dict, song: dict) -> dict | None:
    src = song["sources"][0]  # sources are pre-sorted most-legible first
    img = render_page(books, src["book"], src["pdf_page"])
    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(img).decode()}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return parse_reply(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max songs this run (0 = all)")
    args = ap.parse_args()

    load_dotenv(APP_DIR.parent / ".env")
    client = anthropic.Anthropic()

    db = json.loads((DATA_DIR / "songs.json").read_text(encoding="utf-8"))
    meta: dict[str, dict] = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))

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
