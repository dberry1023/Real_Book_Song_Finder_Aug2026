"""Automatic index builder for user-uploaded fake books (roadmap item 3).

Given an arbitrary songbook PDF, produce the same index structure the master
database uses: {"book": {label, pdf, pages}, "songs": [{id, title, sources}]}.

Strategy, cheapest first:
  1. PDF bookmarks (many digital books have per-song bookmarks) — free, exact.
  2. Claude vision: find the table-of-contents pages, read the (title, page)
     entries, then calibrate the printed->PDF page offset by sampling pages
     and verify it by checking real titles before trusting it.

All Claude calls go through the Vision class so tests can stub it.
"""
import base64
import json
import logging
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path

import pymupdf

log = logging.getLogger("index_book")

INDEX_MODEL = os.environ.get("INDEX_MODEL", "claude-opus-4-8")
SCAN_FRONT = 15   # pages searched for a TOC at the start of the book
SCAN_BACK = 10    # ... and at the end
THUMB_DPI = 100
TOC_DPI = 200
SAMPLE_DPI = 130


class IndexingError(Exception):
    """Raised with a user-facing reason when a book can't be indexed."""


def norm_key(title: str) -> str:
    """Same normalization as build_database.py, so ids merge cleanly."""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for art in ("the ", "a ", "an "):
        if t.startswith(art):
            t = t[len(art):]
    return t


def smart_title(text: str) -> str:
    small = {"a", "an", "and", "as", "at", "by", "de", "em", "for", "from",
             "in", "is", "of", "on", "or", "the", "to", "with", "you"}
    words = text.title().split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        out.append(lw if (0 < i < len(words) - 1 and lw in small) else w)
    return " ".join(out)


def _titles_match(a: str, b: str) -> bool:
    import difflib
    a, b = norm_key(a), norm_key(b)
    if not a or not b:
        return False
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _json_reply(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise IndexingError("The AI reply could not be parsed.")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise IndexingError("The AI reply could not be parsed.") from e


class Vision:
    """Thin wrapper over the Anthropic client (stubbed in tests)."""

    def __init__(self) -> None:
        import anthropic

        self._client = anthropic.Anthropic()

    def ask(self, images: list[bytes], prompt: str, max_tokens: int = 8000) -> str:
        content = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(img).decode()}}
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        resp = self._client.messages.create(
            model=INDEX_MODEL,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": content}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")


def _render(page: pymupdf.Page, dpi: int) -> bytes:
    return page.get_pixmap(dpi=dpi).tobytes("jpeg")


# --------------------------------------------------------------------------
# Step 1: bookmarks (free path)
# --------------------------------------------------------------------------
def entries_from_bookmarks(doc: pymupdf.Document) -> list[dict] | None:
    toc = doc.get_toc()
    skip = re.compile(
        r"^(cover|preface|table of contents|contents|index|foreword|"
        r"intro(duction)?|title page|copyright)$", re.I)
    entries = [{"title": t, "printed": p, "pdf_page": p}
               for _, t, p in toc
               if not skip.match(t.strip()) and 1 <= p <= doc.page_count]
    if len(entries) >= 10:
        log.info("using %d PDF bookmarks", len(entries))
        return entries
    return None


# --------------------------------------------------------------------------
# Step 2: find and read the table of contents with vision
# --------------------------------------------------------------------------
def find_toc_pages(doc: pymupdf.Document, vision: Vision) -> list[int]:
    n = doc.page_count
    candidates = list(range(0, min(SCAN_FRONT, n)))
    back = [i for i in range(max(0, n - SCAN_BACK), n) if i not in candidates]
    candidates += back
    thumbs = [_render(doc[i], THUMB_DPI) for i in candidates]
    prompt = (
        f"These {len(thumbs)} images are pages from a music fake book, numbered "
        "1 to {n} in the order given. Identify which images are table-of-contents "
        "or song-index pages: pages that LIST many song titles together with page "
        "numbers. Sheet music pages, cover pages, and prose pages do not count.\n"
        'Reply with ONLY JSON: {"toc_images": [<image numbers>]}'
    ).replace("{n}", str(len(thumbs)))
    obj = _json_reply(vision.ask(thumbs, prompt, max_tokens=1000))
    picked = [candidates[i - 1] for i in obj.get("toc_images", [])
              if isinstance(i, int) and 1 <= i <= len(candidates)]
    if not picked:
        raise IndexingError(
            "No table of contents found in this book — automatic indexing "
            "needs a song list with page numbers.")
    log.info("TOC pages (0-based): %s", picked)
    return picked


def extract_entries(doc: pymupdf.Document, toc_pages: list[int],
                    vision: Vision) -> list[dict]:
    prompt = (
        "This image is a table-of-contents / index page from a music fake "
        "book. Extract EVERY song entry on the page. Entries may be in "
        "multiple columns; read each column top to bottom. Each entry has a "
        "song title and a page number.\n"
        'Reply with ONLY JSON: {"entries": [{"title": "...", "page": 123}, ...]}'
    )
    entries: list[dict] = []
    for p in toc_pages:
        obj = _json_reply(vision.ask([_render(doc[p], TOC_DPI)], prompt))
        for e in obj.get("entries", []):
            title = str(e.get("title", "")).strip()
            try:
                page = int(e.get("page"))
            except (TypeError, ValueError):
                continue
            if title and 0 < page < 2000:
                entries.append({"title": title, "printed": page})
    # dedupe identical (title, page) pairs across TOC pages
    seen = set()
    unique = []
    for e in entries:
        k = (norm_key(e["title"]), e["printed"])
        if k not in seen:
            seen.add(k)
            unique.append(e)
    if len(unique) < 5:
        raise IndexingError(
            "The table of contents could not be read reliably "
            f"(only {len(unique)} entries recognized).")
    log.info("extracted %d TOC entries", len(unique))
    return unique


# --------------------------------------------------------------------------
# Step 3: calibrate and verify the printed -> pdf page offset
# --------------------------------------------------------------------------
_SAMPLE_PROMPT = (
    "This is one page from a music fake book. Report the page number printed "
    "on the page itself (often in a corner) and the song title printed at the "
    "top, if any.\n"
    'Reply with ONLY JSON: {"printed_page_number": <int or null>, '
    '"title": "<title or null>"}'
)


def _read_page(doc: pymupdf.Document, idx0: int, vision: Vision) -> dict:
    return _json_reply(vision.ask([_render(doc[idx0], SAMPLE_DPI)],
                                  _SAMPLE_PROMPT, max_tokens=500))


def calibrate_offset(doc: pymupdf.Document, entries: list[dict],
                     vision: Vision) -> int:
    n = doc.page_count
    offsets = []
    for frac in (0.25, 0.45, 0.65, 0.85):
        idx0 = min(n - 1, max(0, int(n * frac)))
        obj = _read_page(doc, idx0, vision)
        printed = obj.get("printed_page_number")
        if isinstance(printed, int) and printed > 0:
            offsets.append((idx0 + 1) - printed)
    if not offsets:
        raise IndexingError(
            "No printed page numbers could be read from this book, so its "
            "pages can't be matched to the song list.")
    offset, votes = Counter(offsets).most_common(1)[0]
    if votes < 2 and len(offsets) > 1:
        raise IndexingError(
            "The book's printed page numbers are inconsistent — automatic "
            "indexing can't map them reliably.")

    # Verify: three TOC entries must actually appear where the offset says.
    picks = [entries[i] for i in
             sorted({0, len(entries) // 2, len(entries) - 1})]
    hits = 0
    checked = 0
    for e in picks:
        idx0 = e["printed"] + offset - 1
        if not (0 <= idx0 < n):
            continue
        checked += 1
        obj = _read_page(doc, idx0, vision)
        if obj.get("title") and _titles_match(e["title"], str(obj["title"])):
            hits += 1
    if checked and hits * 2 < checked:  # need at least half to verify
        raise IndexingError(
            "The song list's page numbers don't match the actual pages "
            f"(verified {hits}/{checked}).")
    log.info("offset %+d (votes %d/%d, verified %d/%d)",
             offset, votes, len(offsets), hits, checked)
    return offset


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------
def build_index(pdf_path: Path, book_key: str, label: str,
                vision: Vision | None = None) -> dict:
    """Index one uploaded book. Returns {"book": ..., "songs": ...}.

    Raises IndexingError with a user-facing message on failure.
    """
    doc = pymupdf.open(str(pdf_path))
    try:
        pages = doc.page_count
        entries = entries_from_bookmarks(doc)
        if entries is None:
            if vision is None:
                vision = Vision()
            toc_pages = find_toc_pages(doc, vision)
            raw = extract_entries(doc, toc_pages, vision)
            offset = calibrate_offset(doc, raw, vision)
            entries = [{"title": e["title"], "printed": e["printed"],
                        "pdf_page": e["printed"] + offset} for e in raw]
    finally:
        doc.close()

    songs: dict[str, dict] = {}
    dropped = 0
    for e in entries:
        if not (1 <= e["pdf_page"] <= pages):
            dropped += 1
            continue
        key = norm_key(e["title"])
        if not key:
            dropped += 1
            continue
        title = smart_title(e["title"]) if e["title"].isupper() else e["title"]
        song = songs.setdefault(key, {"title": title, "sources": []})
        src = {"book": book_key, "printed": e["printed"],
               "pdf_page": e["pdf_page"]}
        if src not in song["sources"]:
            song["sources"].append(src)

    out = [{"id": k, "title": s["title"], "sources": s["sources"]}
           for k, s in songs.items()]
    out.sort(key=lambda s: norm_key(s["title"]))
    if not out:
        raise IndexingError("No usable song entries were found in this book.")
    log.info("indexed %d songs (%d entries dropped)", len(out), dropped)
    return {
        "book": {"label": label, "pdf": pdf_path.name, "pages": pages},
        "songs": out,
    }
