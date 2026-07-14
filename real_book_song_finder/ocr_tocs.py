"""OCR the in-book indexes/TOCs of Real Book 4, 5 (5th ed), and 6 (6th ed)."""
import json
import re
import sys

import pymupdf
from rapidocr_onnxruntime import RapidOCR

BASE = r"C:\Users\stats\OneDrive\Desktop\Jazz Night School\Real_Books"
OCR = RapidOCR()

HEADER_RE = re.compile(r"^[A-Z]\.?(\s*[-.]?\s*Cont\.?)?$", re.IGNORECASE)
SKIP_RE = re.compile(r"^(the\s+real\s+book|volume\s+\w+|index|contents?)$", re.IGNORECASE)
# title ......... 123  (allow OCR noise around dots)
PAGE_RE = re.compile(r"^(?P<title>.*?)[\s.·,_:;]*[\s.](?P<page>\d{1,3})\s*\.?$")


def ocr_page(doc: pymupdf.Document, pno0: int) -> list[dict]:
    """OCR one page; return regions with x/y centers."""
    pix = doc[pno0].get_pixmap(dpi=200)
    img = pix.tobytes("png")
    result, _ = OCR(img)
    regions = []
    for box, text, score in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        regions.append({
            "x0": min(xs), "x1": max(xs),
            "yc": (min(ys) + max(ys)) / 2,
            "text": text.strip(), "score": score,
            "width": pix.width,
        })
    return regions


def regions_to_lines(regions: list[dict], page_width: float,
                     split_by_x0: bool = False) -> list[str]:
    """Group OCR regions into text lines per column (2-column layout)."""
    mid = page_width / 2
    lines: list[str] = []
    for col in (0, 1):
        if split_by_x0:
            col_regs = [r for r in regions
                        if ((r["x0"] < page_width * 0.42) == (col == 0))]
        else:
            col_regs = [r for r in regions
                        if (((r["x0"] + r["x1"]) / 2 < mid) == (col == 0))]
        col_regs.sort(key=lambda r: r["yc"])
        groups: list[list[dict]] = []
        for r in col_regs:
            if groups and abs(groups[-1][-1]["yc"] - r["yc"]) < 18:
                groups[-1].append(r)
            else:
                groups.append([r])
        for g in groups:
            g.sort(key=lambda r: r["x0"])
            lines.append(" ".join(r["text"] for r in g))
    return lines


def parse_lines(lines: list[str]) -> list[tuple[str, int]]:
    """Parse TOC lines into (title, printed_page). Handles wrapped titles."""
    entries: list[tuple[str, int]] = []
    stash = ""  # wrapped title fragment waiting for its page number
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        bare = re.sub(r"[\s.·_]+$", "", line).strip()
        if HEADER_RE.match(bare) or SKIP_RE.match(bare):
            stash = ""
            continue
        m = PAGE_RE.match(line)
        if m and m.group("title").strip(" .·_-"):
            title = m.group("title").strip(" .·_-,")
            if stash:
                title = f"{stash} {title}".strip()
                stash = ""
            title = re.sub(r"[\s.·_]{2,}", " ", title).strip()
            if len(title) >= 2:
                entries.append((title, int(m.group("page"))))
        else:
            frag = bare.strip(" .·_-")
            # plausible wrapped-title fragment (not junk / stray numbers)
            if len(frag) >= 2 and not frag.isdigit():
                stash = f"{stash} {frag}".strip() if stash else frag
    return entries


def score(entries: list[tuple[str, int]]) -> int:
    """Prefer many entries, punish merged-garbage lines."""
    garbage = sum(1 for t, _ in entries
                  if len(t) > 45 or len(re.findall(r"\d{2,}", t)) >= 2)
    return len(entries) - 8 * garbage


def extract_book(pdf_name: str, start_pno1: int, out_name: str,
                 max_pages: int = 20, try_both: bool = False) -> None:
    doc = pymupdf.open(f"{BASE}\\{pdf_name}")
    all_entries: list[dict] = []
    pno1 = start_pno1
    while pno1 <= min(start_pno1 + max_pages - 1, doc.page_count):
        regions = ocr_page(doc, pno1 - 1)
        width = regions[0]["width"] if regions else 1700
        entries = parse_lines(regions_to_lines(regions, width, False))
        if try_both:
            alt = parse_lines(regions_to_lines(regions, width, True))
            if score(alt) > score(entries):
                entries = alt
        # drop garbage lines that survived
        entries = [(t, p) for t, p in entries
                   if len(t) <= 45 and len(re.findall(r"\d{2,}", t)) < 2]
        print(f"  {pdf_name} PDF p{pno1}: {len(regions)} regions -> "
              f"{len(entries)} entries", flush=True)
        if len(entries) < 8:  # no longer a TOC page
            break
        for title, page in entries:
            all_entries.append({"title": title, "page": page, "pdf_toc_page": pno1})
        pno1 += 1
    doc.close()
    with open(out_name, "w", encoding="utf-8") as f:
        json.dump(all_entries, f, indent=1, ensure_ascii=False)
    pages = [e["page"] for e in all_entries]
    print(f"{pdf_name}: {len(all_entries)} entries, printed pages "
          f"{min(pages)}..{max(pages)} -> {out_name}", flush=True)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "5"):
        extract_book("Real Book-5.pdf", 3, "toc_book5.json", try_both=True)
    if which in ("all", "4"):
        extract_book("Real-Book-4.pdf", 4, "toc_book4.json")
    if which in ("all", "6"):
        extract_book("Real-Book-6.pdf", 3, "toc_book6.json")
