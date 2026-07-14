"""Build the merged song database (data/songs.json) for the Real Book Song Finder.

Sources:
  - MASTER Index.pdf        -> old Real Books Vol 1, 2, 3 (typed text, page numbers)
  - data/toc_book4.json     -> OCR of Hal Leonard Vol IV in-book contents
  - data/toc_book5.json     -> OCR of Fifth Edition in-book index
  - data/toc_book6.json     -> OCR of Sixth Edition in-book contents
  - Christmas book bookmarks -> exact PDF pages
  - Real-Book-Song-Finder.pdf -> canonical title spellings for OCR cleanup

Page offsets (verified visually at both ends of every book):
  pdf_page = printed_page + offset
"""
import difflib
import json
import re
import unicodedata
from pathlib import Path

import pymupdf

BOOKS_DIR = Path(r"C:\Users\stats\OneDrive\Desktop\Jazz Night School\Real_Books")
DATA_DIR = Path(__file__).parent / "data"

BOOKS = {
    "bk1": {"pdf": "Real Book-I.pdf", "label": "Real Book Vol 1 (original)", "offset": 8},
    "bk2": {"pdf": "Real-Book-2.pdf", "label": "Real Book Vol 2", "offset": 7},
    "bk3": {"pdf": "Real-Book-3.pdf", "label": "Real Book Vol 3", "offset": 5},
    "bk4": {"pdf": "Real-Book-4.pdf", "label": "Real Book Vol 4 (Hal Leonard)", "offset": 0},
    "bk5": {"pdf": "Real Book-5.pdf", "label": "Real Book 5th Edition", "offset": 12},
    "bk6": {"pdf": "Real-Book-6.pdf", "label": "Real Book 6th Edition", "offset": -1},
    "xmas": {"pdf": "Christmas-Real-Book.pdf", "label": "Christmas Real Book", "offset": 0},
}

# preferred display order of sources (most legible editions first)
BOOK_ORDER = ["bk6", "bk4", "bk5", "bk1", "bk2", "bk3", "xmas"]


def norm_key(title: str) -> str:
    """Normalize a title into a merge/search key."""
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"\(.*?\)", " ", t)          # drop parenthetical subtitles
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for art in ("the ", "a ", "an "):
        if t.startswith(art):
            t = t[len(art):]
    return t


def smart_title(text: str) -> str:
    """ALL-CAPS OCR title -> title case, keeping small words lowered."""
    small = {"a", "an", "and", "as", "at", "by", "de", "em", "for", "from",
             "in", "is", "of", "on", "or", "the", "to", "with", "you"}
    words = text.title().split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        out.append(lw if (0 < i < len(words) - 1 and lw in small) else w)
    return " ".join(out)


def fix_the_suffix(title: str) -> str:
    """MASTER Index writes 'Song Name (The)' -> 'The Song Name'."""
    m = re.match(r"^(.*?)\s*\((The|A|An)\)\s*$", title, re.I)
    if m:
        return f"{m.group(2)} {m.group(1)}".strip()
    return title


# --------------------------------------------------------------------------
# MASTER Index -> bk1 / bk2 / bk3 entries
# --------------------------------------------------------------------------
def parse_master_index() -> list[dict]:
    """Positional (word-coordinate) parse: 3 columns = Title | Book | Page."""
    doc = pymupdf.open(str(BOOKS_DIR / "MASTER Index.pdf"))
    entries: list[dict] = []
    skipped = 0
    for page in doc:
        words = page.get_text("words")  # x0, y0, x1, y1, text, ...
        # column boundaries from the header row, with sane fallbacks
        x_book = min((w[0] for w in words if w[4] == "Book" and w[1] < 80),
                     default=403.0)
        x_page = min((w[0] for w in words if w[4] == "Page" and w[1] < 80),
                     default=444.0)
        # group words into rows by y-center
        rows: list[list] = []
        for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
            yc = (w[1] + w[3]) / 2
            if rows and abs(rows[-1][0] - yc) < 5:
                rows[-1][1].append(w)
            else:
                rows.append([yc, [w]])

        stash = ""  # wrapped title fragment from a title-only row
        for _, rws in rows:
            rws.sort(key=lambda w: w[0])
            title_w = [w[4] for w in rws if w[0] < x_book - 12]
            book_w = [w[4] for w in rws if x_book - 12 <= w[0] < x_page - 8]
            page_w = [w[4] for w in rws if w[0] >= x_page - 8]
            title = " ".join(title_w).strip()
            if title.lower() in ("master index", "song title"):
                continue
            if not book_w and not page_w:
                if title:
                    stash = f"{stash} {title}".strip()
                continue
            full_title = fix_the_suffix(f"{stash} {title}".strip())
            stash = ""
            book_txt = "".join(book_w)
            page_txt = "".join(page_w)
            m = re.match(r"^realbk(\S*)$", book_txt, re.I)
            if not m:
                continue  # a book the user doesn't own
            if not (full_title and re.match(r"^\d{1,3}$", page_txt)):
                skipped += 1
                continue
            tail = m.group(1).lower()
            book = "bk2" if "2" in tail else "bk3" if "3" in tail else "bk1"
            entries.append({"book": book, "title": full_title,
                            "printed": int(page_txt)})
    doc.close()
    counts = {b: sum(1 for e in entries if e["book"] == b)
              for b in ("bk1", "bk2", "bk3")}
    print(f"MASTER Index: {len(entries)} entries {counts}, "
          f"{skipped} skipped (corrupt rows)")
    return entries


# --------------------------------------------------------------------------
# Song-Finder -> canonical titles for the Hal Leonard books
# --------------------------------------------------------------------------
def parse_song_finder() -> dict[str, list[str]]:
    """Return {code: [titles]} from the Hal Leonard Songfinder (codes 01..23)."""
    doc = pymupdf.open(str(BOOKS_DIR / "Real-Book-Song-Finder.pdf"))
    text = "\n".join(doc[i].get_text() for i in range(1, doc.page_count))
    doc.close()
    # join wrapped lines, then find "Title (01, 04)" patterns
    text = re.sub(r"\n(?![A-Z¡¿0-9\"'(])", " ", text)  # heal wrapped lowercase lines
    text = text.replace("\n", " \n")
    by_code: dict[str, list[str]] = {}
    for m in re.finditer(r"([^\n(]{2,90}?)\s*\(((?:\d{2})(?:,\s*\d{2})*)\)", text):
        title = m.group(1).strip(" .·\t\n-–")
        if len(title) < 2 or title.lower().startswith(("key", "unless")):
            continue
        for code in re.findall(r"\d{2}", m.group(2)):
            by_code.setdefault(code, []).append(title)
    counts = {c: len(v) for c, v in sorted(by_code.items())}
    print("Song-Finder titles per volume code:", counts)
    return by_code


def canonicalize(title: str, canon_pool: dict[str, str]) -> str:
    """Snap an OCR title to a canonical spelling when clearly the same song."""
    key = norm_key(title)
    if key in canon_pool:
        return canon_pool[key]
    matches = difflib.get_close_matches(key, canon_pool.keys(), n=1, cutoff=0.87)
    if matches:
        return canon_pool[matches[0]]
    return smart_title(title) if title.isupper() else title


# --------------------------------------------------------------------------
# OCR'd TOCs -> bk4 / bk5 / bk6 entries
# --------------------------------------------------------------------------
def repair_outliers(entries: list[dict], book: str) -> list[dict]:
    """Handle impossible OCR page values only.

    TOC page order genuinely jitters by a few pages (2-page tunes get moved),
    so small non-monotonicity is real data.  Only a page far outside its
    alphabetical neighborhood (>20 off) is an OCR misread: interpolate when
    the neighbors pin it exactly, otherwise drop the entry.
    """
    out = []
    for i, e in enumerate(entries):
        nbrs = [x["page"] for x in entries[max(0, i - 2):i + 3] if x is not e]
        nbrs.sort()
        med = nbrs[len(nbrs) // 2] if nbrs else e["page"]
        if abs(e["page"] - med) <= 20:
            out.append(e)
            continue
        prev = entries[i - 1]["page"] if i > 0 else None
        nxt = entries[i + 1]["page"] if i < len(entries) - 1 else None
        if prev is not None and nxt is not None and nxt - prev == 2:
            print(f"   {book}: repaired {e['title']!r} page {e['page']} -> {prev + 1}")
            e["page"] = prev + 1
            out.append(e)
        else:
            print(f"   {book}: dropped {e['title']!r} page {e['page']} (outlier)")
    return out


def clean_ocr_entries(data: list[dict], pool: dict[str, str],
                      book: str) -> list[dict]:
    """Fix common OCR damage in raw TOC entries before merging."""
    out: list[dict] = []
    for e in data:
        t = re.sub(r"\s+", " ", e["title"]).strip()
        # stray column-bleed digit glued to the front: "1 MISTY" -> "MISTY"
        t = re.sub(r"^[1lI][.,]?\s+(?=[A-Z]{2})", "", t)
        # two TOC lines merged with the first page number garbled ("3O1"):
        # "THE NIGHT HAS A THOUSAND EYES 3O1 A NIGHT IN TUNISIA" (page=302)
        m = re.match(r"^(.{3,}?)\s+([0-9OoIl]{2,3})\s+(.{4,})$", t)
        if m and re.search(r"\d", m.group(2)):
            num = int(m.group(2).replace("O", "0").replace("o", "0")
                      .replace("I", "1").replace("l", "1"))
            print(f"   {book}: split merged line -> ({m.group(1)!r}, {num}) + "
                  f"({m.group(3)!r}, {e['page']})")
            out.append({"title": m.group(1), "page": num})
            out.append({"title": m.group(3), "page": e["page"]})
            continue
        # two TOC lines merged with the first page number lost entirely:
        # "MISTY MIYAKO" (page=278).  Split only if BOTH halves are known
        # songs; the first half gets the preceding page (verify these!).
        if not difflib.get_close_matches(norm_key(t), pool.keys(), 1, 0.87):
            words = t.split()
            for cut in range(1, len(words)):
                a, b = " ".join(words[:cut]), " ".join(words[cut:])
                if (difflib.get_close_matches(norm_key(a), pool.keys(), 1, 0.92)
                        and difflib.get_close_matches(norm_key(b), pool.keys(), 1, 0.92)
                        and len(a) >= 4 and len(b) >= 4):
                    print(f"   {book}: split merged titles -> ({a!r}, "
                          f"{e['page'] - 1} GUESSED) + ({b!r}, {e['page']})")
                    out.append({"title": a, "page": e["page"] - 1})
                    out.append({"title": b, "page": e["page"]})
                    break
            else:
                out.append({"title": t, "page": e["page"]})
            continue
        out.append({"title": t, "page": e["page"]})
    return out


def parse_ocr_tocs(sf: dict[str, list[str]], master: list[dict]) -> list[dict]:
    canon_hl: dict[str, str] = {}
    for code in ("01", "04", "05", "13"):
        for t in sf.get(code, []):
            canon_hl.setdefault(norm_key(t), t)
    canon_old = {norm_key(e["title"]): e["title"] for e in master}

    out: list[dict] = []
    fixes = {("bk4", "COME DANCE WITHME"): 90}  # verified by eye on the TOC scan
    for book, fname, pool in [("bk4", "toc_book4.json", canon_hl),
                              ("bk5", "toc_book5.json", {**canon_hl, **canon_old}),
                              ("bk6", "toc_book6.json", canon_hl)]:
        data = json.loads((DATA_DIR / fname).read_text(encoding="utf-8"))
        data = [e for e in data if not re.match(r"^[\d\s.]+$", e["title"])]
        for e in data:
            e["page"] = fixes.get((book, e["title"]), e["page"])
        data = clean_ocr_entries(data, pool, book)
        # 5th-ed appendix tunes whose index numbers don't map to printed pages
        # (verified: printed p3 is A Fine Romance, not The Disguise)
        drops = {("bk5", "disguise"), ("bk5", "sphinx")}
        data = [e for e in data if (book, norm_key(e["title"])) not in drops]
        if book in ("bk4", "bk6"):
            data = repair_outliers(data, book)
        seen = set()
        for e in data:
            title = canonicalize(e["title"], pool)
            k = (norm_key(title), e["page"])
            if k in seen:
                continue
            seen.add(k)
            out.append({"book": book, "title": title, "printed": e["page"]})
        print(f"{book}: {len([o for o in out if o['book'] == book])} entries")
    return out


# --------------------------------------------------------------------------
# Christmas book bookmarks
# --------------------------------------------------------------------------
def parse_christmas() -> list[dict]:
    doc = pymupdf.open(str(BOOKS_DIR / "Christmas-Real-Book.pdf"))
    toc = doc.get_toc()
    doc.close()
    skip = re.compile(r"^(cover|preface|table of contents)", re.I)
    entries = [{"book": "xmas", "title": t, "printed": p}
               for _, t, p in toc if not skip.match(t)]
    print(f"xmas: {len(entries)} entries from bookmarks")
    return entries


# --------------------------------------------------------------------------
def main() -> None:
    master = parse_master_index()
    sf = parse_song_finder()
    ocr = parse_ocr_tocs(sf, master)
    xmas = parse_christmas()

    # verify page bounds against actual PDFs
    page_counts = {}
    for bid, b in BOOKS.items():
        doc = pymupdf.open(str(BOOKS_DIR / b["pdf"]))
        page_counts[bid] = doc.page_count
        doc.close()

    songs: dict[str, dict] = {}
    dropped = 0
    for e in master + ocr + xmas:
        book = BOOKS[e["book"]]
        pdf_page = e["printed"] + book["offset"]
        if not (1 <= pdf_page <= page_counts[e["book"]]):
            dropped += 1
            continue
        key = norm_key(e["title"])
        if not key:
            dropped += 1
            continue
        song = songs.setdefault(key, {"title": e["title"], "sources": []})
        # prefer nicely-cased titles over ALL CAPS for display
        if song["title"].isupper() and not e["title"].isupper():
            song["title"] = e["title"]
        src = {"book": e["book"], "printed": e["printed"], "pdf_page": pdf_page}
        if src not in song["sources"]:
            song["sources"].append(src)

    order = {b: i for i, b in enumerate(BOOK_ORDER)}
    out = []
    for key, s in songs.items():
        s["sources"].sort(key=lambda x: (order[x["book"]], x["printed"]))
        out.append({"id": key, "title": s["title"], "sources": s["sources"]})
    out.sort(key=lambda s: norm_key(s["title"]))

    db = {
        "books": {bid: {"label": b["label"], "pdf": b["pdf"],
                        "pages": page_counts[bid]} for bid, b in BOOKS.items()},
        "songs": out,
    }
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "songs.json").write_text(
        json.dumps(db, indent=1, ensure_ascii=False), encoding="utf-8")
    n_src = sum(len(s["sources"]) for s in out)
    print(f"\nWrote data/songs.json: {len(out)} unique songs, "
          f"{n_src} book locations, {dropped} dropped (out of range)")


if __name__ == "__main__":
    main()
