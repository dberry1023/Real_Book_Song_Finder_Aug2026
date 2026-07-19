"""Per-user book library management for multi-user mode.

Handles "Load My Books" uploads: validates PDFs, recognizes the seven
standard Real Books by SHA-256 fingerprint (instant indexing from the
master index, zero AI cost), stores unknown books as "pending" until the
automatic indexing pipeline (roadmap item 3) exists, and rebuilds each
user's private songs.json from their manifest.
"""
import hashlib
import json
import re
from pathlib import Path

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # largest known book is ~50 MB

# SHA-256 of the seven standard Real Book PDFs -> master-index book id.
KNOWN_BOOKS: dict[str, str] = {
    "b30bb8c3a19fbd3644af8d41e1bdbf7e2f1a526a97235af01a1f4b86738c5eef": "bk1",
    "f827d62821efd1244c46efa959ddf0c040fa273072f868fc5b94d7166a8fd500": "bk2",
    "eab2bf21560ee02452ea054000edc059d56706c2356c5e37595b6d0a03b2c9e3": "bk3",
    "dce0449562d9103decfc48cb147d75ea594c6875a1ebbabd18aa47331ec8a582": "bk4",
    "d83d10799caaa1940397fcf9595afba68e00255c7bbad8f4ebc6dffd9010bb05": "bk5",
    "c9bafa89c3821e039f39c78dfb5e608ebe0f38a1490e3d7315785b31f984a958": "bk6",
    "fc063b5343f692790fabbfb38e086003ecac2cc30746614ddb10696c8efcdb21": "xmas",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_pdf_name(original: str) -> str:
    """Reduce an uploaded filename to a safe basename ending in .pdf."""
    name = Path(original or "book.pdf").name
    name = re.sub(r"[^A-Za-z0-9 ._-]+", "_", name).strip(" .")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name or "book.pdf"


def load_manifest(user_dir: Path) -> list[dict]:
    p = user_dir / "library.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def save_manifest(user_dir: Path, manifest: list[dict]) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "library.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")


def custom_book_key(digest: str) -> str:
    """Book id for a user-indexed (non-standard) book."""
    return "u_" + digest[:8]


def custom_index_path(user_dir: Path, digest: str) -> Path:
    return user_dir / "indexes" / f"{digest}.json"


def rebuild_index(user_dir: Path, master_db: dict) -> None:
    """Write the user's songs.json from their manifest's indexed books.

    Known books come from the master index (whose songs already carry
    merged composer/key metadata); custom books come from their own
    per-book index files written by the indexing pipeline.
    """
    manifest = load_manifest(user_dir)
    owned = {e["book_id"] for e in manifest
             if e.get("book_id") and e.get("status") == "indexed"}
    books = {bid: master_db["books"][bid] for bid in owned
             if bid in master_db["books"]}
    merged: dict[str, dict] = {}
    for s in master_db["songs"]:
        sources = [src for src in s["sources"] if src["book"] in owned]
        if sources:
            entry = {k: v for k, v in s.items() if k != "sources"}
            entry["sources"] = sources
            merged[s["id"]] = entry

    # merge in the user's custom-indexed books
    for e in manifest:
        bid = e.get("book_id") or ""
        if e.get("status") != "indexed" or not bid.startswith("u_"):
            continue
        idx_path = custom_index_path(user_dir, e["sha256"])
        if not idx_path.exists():
            continue
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        books[bid] = idx["book"]
        for s in idx["songs"]:
            if s["id"] in merged:
                existing = merged[s["id"]]["sources"]
                existing.extend(src for src in s["sources"]
                                if src not in existing)
            else:
                merged[s["id"]] = {k: v for k, v in s.items()}

    songs = sorted(merged.values(), key=lambda s: s["id"])
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "songs.json").write_text(
        json.dumps({"books": books, "songs": songs}), encoding="utf-8")


def add_upload(user_dir: Path, tmp_path: Path, original_name: str,
               master_db: dict) -> tuple[dict | None, str | None]:
    """Ingest a validated uploaded PDF. Returns (manifest_entry, None) or
    (None, error_message). Caller is responsible for deleting tmp_path on
    error."""
    digest = sha256_file(tmp_path)
    manifest = load_manifest(user_dir)
    if any(e["sha256"] == digest for e in manifest):
        return None, "That book is already in your library."

    book_id = KNOWN_BOOKS.get(digest)
    books_dir = user_dir / "books"
    books_dir.mkdir(parents=True, exist_ok=True)

    if book_id:
        pdf_name = master_db["books"][book_id]["pdf"]
        entry = {
            "sha256": digest,
            "pdf": pdf_name,
            "book_id": book_id,
            "status": "indexed",
            "label": master_db["books"][book_id]["label"],
        }
    else:
        pdf_name = safe_pdf_name(original_name)
        # avoid clobbering an existing different file with the same name
        dest = books_dir / pdf_name
        n = 1
        while dest.exists():
            dest = books_dir / f"{Path(pdf_name).stem}_{n}.pdf"
            n += 1
        pdf_name = dest.name
        entry = {
            "sha256": digest,
            "pdf": pdf_name,
            "book_id": None,
            "status": "pending",
            "label": Path(original_name or pdf_name).stem,
        }

    tmp_path.replace(books_dir / pdf_name)
    manifest.append(entry)
    save_manifest(user_dir, manifest)
    rebuild_index(user_dir, master_db)
    return entry, None


def delete_book(user_dir: Path, digest: str, master_db: dict) -> bool:
    """Remove a book (by sha256) from the manifest, disk, and index."""
    manifest = load_manifest(user_dir)
    entry = next((e for e in manifest if e["sha256"] == digest), None)
    if entry is None:
        return False
    pdf_path = user_dir / "books" / entry["pdf"]
    if pdf_path.exists():
        pdf_path.unlink()
    custom_index_path(user_dir, digest).unlink(missing_ok=True)
    manifest.remove(entry)
    save_manifest(user_dir, manifest)
    rebuild_index(user_dir, master_db)
    return True
