"""Generate, test and review the saved chord charts.

Each song is sent to Claude once and the answer is saved in data/chords.json in
concert pitch. The web server reads that file and transposes in code, so people
using the app never cause an AI call.

    python generate_chords.py --test
        Try the test tunes on Opus 5.5 and Fable 5.1 and save the answers to
        data/chord_test/ so you can check them. Nothing goes in chords.json.

    python generate_chords.py --all --model claude-opus-5-5
        Generate every song that has no saved chart. Safe to stop and rerun.

    python generate_chords.py --batch-submit --model claude-opus-5-5
        The same at half price. Most batches finish within an hour, and the
        limit is 24 hours.

    python generate_chords.py --pages-submit --model claude-opus-5-5
        Read the chords from the printed pages, for every song not already
        rated high and not flagged with a bad page number. Add --sample 20 for
        a small trial. The first results back up the old charts.

    python generate_chords.py --batch-collect
        Save finished batches. Add --wait to keep checking until they end.

    python generate_chords.py --export-review
        Write data/chord_review.csv for checking in Excel, with the songs that
        most need a look first.

    python generate_chords.py --import-review
        Apply the review column (approved or rejected) from that file.
"""
import argparse
import base64
import csv
import datetime
import json
import logging
import os
import random
import shutil
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pymupdf

import chords
import index_book

APP_DIR = Path(__file__).parent
DATA = APP_DIR / "data"
CHORDS_PATH = DATA / "chords.json"
REVIEW_PATH = DATA / "chord_review.csv"
DECLINED_PATH = DATA / "chord_declined.json"
BATCH_PATH = DATA / "chord_batch.json"
TEST_DIR = DATA / "chord_test"
DECLINED = "declined"
WRONG_PAGE = "wrong page"
BATCH_DISCOUNT = 0.5
BATCH_POLL_SECONDS = 60
BATCH_MAX_BYTES = 200 * 1024 * 1024  # the API's limit is 256 MB per batch
MEMORY_BACKUP_PATH = DATA / "chords_from_memory_backup.json"
BOOKS_DIR = Path(os.environ.get(
    "BOOKS_DIR", r"C:\Users\stats\OneDrive\Desktop\Jazz Night School\Real_Books"))
PAGE_DPI = 200
PAGE_JPEG_QUALITY = 75
MAX_CHART_PAGES = 3
SAMPLE_SEED = 20261106

TEST_MODELS = ["claude-opus-5-5", "claude-fable-5-1"]
DEFAULT_EFFORT = "high"
# USD per million tokens (input, output).
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00),
          "claude-opus-5-5": (4.00, 20.00), "claude-fable-5-1": (10.00, 50.00)}
TEST_SONGS = [
    # Well known: easy to check by ear.
    "all the things you are", "autumn leaves", "blue bossa", "giant steps",
    "stella by starlight", "take the a train", "so what", "footprints",
    "there will never be another you", "misty", "body and soul",
    "alone together",
    # Less common: where a model is most likely to make things up.
    "across the alley from the alamo", "adios", "affirmation",
    "after you ve gone", "peri s scope", "inner urge", "joy spring",
    "moment s notice",
    # Added after a correct answer in the Claude app.
    "beautiful love",
]
MAX_TOKENS = 16000  # room for thinking as well as the chart

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["form", "confidence", "sections"],
    "properties": {
        "form": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "bars"],
                "properties": {
                    "name": {"type": "string"},
                    "bars": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

PAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title_on_page", "printed_key", "legible", "form", "confidence",
                 "sections"],
    "properties": {
        "title_on_page": {"type": "string"},
        "printed_key": {"type": "string"},
        "legible": {"type": "boolean"},
        "form": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "sections": SCHEMA["properties"]["sections"],
    },
}
READ_PROMPT = (
    "These are pages from a jazz fake book. The song should be \"{title}\". It starts "
    "on the first image. A later image may continue it, or may be a different song, "
    "so stop where this song ends.\n\n"
    "Read the chord symbols exactly as printed, in order, bar by bar, in the key "
    "printed. Do not use what you remember about the song. Each bar is one string "
    "holding only chord symbols separated by spaces, with no ending numbers, words or "
    "brackets around whole bars. Write minor as m even if a dash is printed, a "
    "triangle as maj7, a circled slash as m7b5 and a circle as dim7. Where a bar "
    "holds the chord before it, write that chord again so every bar is present. "
    "Keep printed section letters, name endings such as 1st ending, and note repeats "
    "in form. Leave out notes and lyrics.\n\n"
    "title_on_page is the title actually printed. Set legible to false if you could "
    "not read most chords. Set confidence to high only if you read every chord "
    "clearly, and to medium or low otherwise."
)

logger = logging.getLogger("generate_chords")


def build_prompt(song: dict) -> str:
    """Write the request for one song, using the composer and key we know."""
    composer = song.get("composer", "unknown")
    key = song.get("key", "unknown")
    lines = [f"Song: {song['title']}"]
    if composer != "unknown":
        lines.append(f"Composer: {composer}")
    if key != "unknown":
        lines.append(f"Key as printed in the Real Book: {key}")
    return "\n".join(lines) + (
        "\n\nGive the standard jazz lead-sheet chord changes for this song, in "
        "concert pitch, in the key above if one is given. Split it into the "
        "sections of its form, using names such as A, B, or Head. Each bar is "
        "one string holding that bar's chords separated by spaces, for example "
        '"Dm7 G7". Use plain chord symbols such as Cmaj7, F#m7b5, Bb7#9, C6/9 '
        "or Dm7/G, and repeat a chord rather than using a repeat sign. Write "
        "out every bar of the whole form.\n\n"
        "Give the version musicians most commonly play. Set confidence to high "
        "if you are sure of these changes, medium if parts may differ, and low "
        "if you are unsure. Return no sections only if you do not know the song."
    )


def ask_claude(client, model: str, song: dict,
               effort: str = DEFAULT_EFFORT) -> tuple[dict | None, str, dict]:
    """Ask Claude for one song's chart.

    Args:
        client: An anthropic.Anthropic client.
        model: Model id.
        song: A song with title, and composer and key if known.
        effort: How hard the model should think.

    Returns:
        (chart or None, problem text, token usage).
    """
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": build_prompt(song)}],
        extra_body={"output_config": output_config(effort)},
    )
    return parse_reply(response)


def output_config(effort: str) -> dict:
    """The effort and reply format used for every chart request."""
    return {"effort": effort,
            "format": {"type": "json_schema", "schema": SCHEMA}}


def parse_reply(response) -> tuple[dict | None, str, dict]:
    """Read one model reply, from a live call or a batch result.

    Returns:
        (chart or None, problem text, token usage).
    """
    usage = {"input": response.usage.input_tokens,
             "output": response.usage.output_tokens}
    if response.stop_reason != "end_turn":
        return None, f"stopped early: {response.stop_reason}", usage
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        chart = json.loads(text)
    except json.JSONDecodeError:
        return None, "reply was not valid JSON", usage
    problem = chords.validate_sections(chart.get("sections"))
    if problem:
        # Being told it does not know the song is a fair answer, not a failure.
        declined = chart.get("confidence") == "low" and problem != "empty bar"
        return None, DECLINED if declined else problem, usage
    return chart, "", usage


def cost(model: str, usage: dict) -> float:
    """Dollar cost of the tokens used."""
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (usage["input"] * price_in + usage["output"] * price_out) / 1_000_000


def load_songs() -> dict[str, dict]:
    """The Real Book index with verified composer and key merged in."""
    db = json.loads((DATA / "songs.json").read_text(encoding="utf-8"))
    meta = json.loads((DATA / "metadata.json").read_text(encoding="utf-8"))
    songs = {}
    for song in db["songs"]:
        info = meta.get(song["id"])
        if info and not info.get("page_mismatch"):
            song["composer"] = info.get("composer", "unknown")
            song["key"] = info.get("key", "unknown")
        songs[song["id"]] = song
    return songs


def make_client():
    """Create the Claude client, reading the key from the workspace .env."""
    import anthropic

    try:
        from dotenv import load_dotenv

        load_dotenv(APP_DIR.parent / ".env")
    except ImportError:
        pass
    return anthropic.Anthropic()


def to_entry(song: dict, chart: dict, model: str) -> dict:
    """Turn a model reply into a saved entry, awaiting review."""
    return {
        "title": song["title"],
        "key": song.get("key", "unknown"),
        "form": chart.get("form", ""),
        "confidence": chart.get("confidence", "low"),
        "sections": chart["sections"],
        "model": model,
        "generated": datetime.date.today().isoformat(),
        "review": "unreviewed",
    }


def run_test(client, songs: dict[str, dict], models: list[str],
             effort: str = DEFAULT_EFFORT) -> None:
    """Run the test tunes on each model and save the answers for checking."""
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    for model in models:
        results: dict[str, dict] = {}
        total = {"input": 0, "output": 0}
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {pool.submit(ask_claude, client, model, songs[i], effort): i
                    for i in TEST_SONGS}
            for job in as_completed(jobs):
                song_id = jobs[job]
                chart, problem, usage = job.result()
                total["input"] += usage["input"]
                total["output"] += usage["output"]
                results[song_id] = {"title": songs[song_id]["title"],
                                    "key": songs[song_id].get("key", "unknown"),
                                    "chart": chart, "problem": problem,
                                    "usage": usage}
                print(f"{model}  {song_id}: {problem or chart['confidence']}")
        path = TEST_DIR / f"{model}-{effort}.json"
        path.write_text(json.dumps(
            {"model": model, "usage": total, "cost": cost(model, total),
             "results": results}, indent=1), encoding="utf-8")
        print(f"{model}: {total} tokens, ${cost(model, total):.4f}, saved {path}")


def load_declined() -> set[str]:
    """Songs the model has said it does not know."""
    if DECLINED_PATH.exists():
        return set(json.loads(DECLINED_PATH.read_text(encoding="utf-8")))
    return set()


def run_all(client, songs: dict[str, dict], model: str, workers: int,
            limit: int | None, effort: str = DEFAULT_EFFORT) -> None:
    """Generate every song with no saved chart, saving as it goes.

    Songs the model declined are skipped on later runs, so they are paid for
    once. Delete data/chord_declined.json to try them again.
    """
    store = chords.load_store(CHORDS_PATH)
    declined = load_declined()
    todo = [s for i, s in songs.items()
            if i not in store and i not in declined][:limit]
    print(f"{len(store)} saved, {len(declined)} declined, "
          f"{len(todo)} to generate on {model}")
    lock = threading.Lock()
    total = {"input": 0, "output": 0}
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(ask_claude, client, model, s, effort): s for s in todo}
        for count, job in enumerate(as_completed(jobs), 1):
            song = jobs[job]
            try:
                chart, problem, usage = job.result()
            except Exception as err:  # keep going; a rerun picks these up
                logger.warning("%s failed: %s", song["id"], err)
                failed += 1
                continue
            with lock:
                total["input"] += usage["input"]
                total["output"] += usage["output"]
                if problem == DECLINED:
                    declined.add(song["id"])
                elif chart is None:
                    logger.warning("%s not saved: %s", song["id"], problem)
                    failed += 1
                else:
                    store[song["id"]] = to_entry(song, chart, model)
                if count % 25 == 0:
                    chords.save_store(CHORDS_PATH, store)
                    DECLINED_PATH.write_text(json.dumps(sorted(declined)),
                                             encoding="utf-8")
                    print(f"{count}/{len(todo)}  ${cost(model, total):.2f}")
    chords.save_store(CHORDS_PATH, store)
    DECLINED_PATH.write_text(json.dumps(sorted(declined)), encoding="utf-8")
    print(f"Done. {len(store)} saved, {len(declined)} declined, {failed} failed, "
          f"${cost(model, total):.2f} spent.")


def send_batches(client, requests: Iterable[dict], state: dict) -> list[str]:
    """Send requests in as many batches as the size limit needs, and save the ids.

    Args:
        client: An anthropic.Anthropic client.
        requests: The requests, which may be built one at a time.
        state: What collecting will need. The batch ids are added to it.

    Returns:
        The batch ids.
    """
    state["batch_ids"] = []
    group: list[dict] = []
    size = 0

    def flush() -> None:
        batch = client.messages.batches.create(requests=group)
        state["batch_ids"].append(batch.id)
        BATCH_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")
        print(f"Submitted batch {batch.id}: {len(group)} songs", flush=True)

    for request in requests:
        request_size = len(json.dumps(request))
        if group and size + request_size > BATCH_MAX_BYTES:
            flush()
            group, size = [], 0
        group.append(request)
        size += request_size
    if group:
        flush()
    return state["batch_ids"]


def batch_submit(client, songs: dict[str, dict], model: str, effort: str,
                 limit: int | None) -> list[str]:
    """Send every song with no saved chart as half-price batches.

    Returns:
        The batch ids, also saved in data/chord_batch.json so results can be
        collected later, even after a restart.
    """
    if BATCH_PATH.exists():
        sys.exit(f"A batch is waiting to be collected: see {BATCH_PATH}")
    store = chords.load_store(CHORDS_PATH)
    declined = load_declined()
    todo = [i for i in songs if i not in store and i not in declined][:limit]
    if not todo:
        sys.exit("Every song already has a saved chart.")
    # Batch ids allow no spaces, and song ids have them.
    names = {f"song-{n:04d}": song_id for n, song_id in enumerate(todo)}
    requests = [
        {"custom_id": name,
         "params": {"model": model, "max_tokens": MAX_TOKENS,
                    "output_config": output_config(effort),
                    "messages": [{"role": "user",
                                  "content": build_prompt(songs[song_id])}]}}
        for name, song_id in names.items()
    ]
    state = {"mode": "memory", "model": model, "effort": effort, "songs": names}
    return send_batches(client, requests, state)


def book_order(book_id: str) -> int:
    """Rank books for reading: typeset editions first, handwritten ones last."""
    order = ["bk6", "bk5", "bk4", "xmas", "bk1", "bk3", "bk2"]
    return order.index(book_id) if book_id in order else len(order)


def page_span(source: dict, starts: dict[str, list[int]]) -> int:
    """How many pages a chart may use, judged by where the next song starts."""
    later = [p for p in starts[source["book"]] if p > source["pdf_page"]]
    if not later:
        return 1
    return max(1, min(later[0] - source["pdf_page"], MAX_CHART_PAGES))


def page_todo(songs: dict[str, dict], store: dict[str, dict],
              flagged: set[str]) -> list[str]:
    """Songs to read from the page.

    Left out: the charts already rated high, any chart already read or checked
    by a person, and songs whose page number is flagged as unreliable.
    """
    todo = []
    for song_id in songs:
        entry = store.get(song_id, {})
        if song_id in flagged or entry.get("confidence") == "high":
            continue
        if entry.get("source") == "book" or entry.get("review") == "approved":
            continue
        todo.append(song_id)
    return todo


def render_page(pdf: Path, page: int) -> str:
    """One page as a greyscale JPEG in base64."""
    doc = pymupdf.open(str(pdf))
    try:
        pixmap = doc[page - 1].get_pixmap(dpi=PAGE_DPI, colorspace=pymupdf.csGRAY)
        data = pixmap.tobytes("jpeg", jpg_quality=PAGE_JPEG_QUALITY)
    finally:
        doc.close()
    return base64.standard_b64encode(data).decode()


def page_request(name: str, model: str, effort: str, song: dict,
                 source: dict, span: int, pdf: Path, pages_in_book: int) -> dict:
    """Build the request asking Claude to read one song's printed chords."""
    pages = range(source["pdf_page"],
                  min(source["pdf_page"] + span, pages_in_book + 1))
    content: list[dict] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": render_page(pdf, page)}}
        for page in pages
    ]
    content.append({"type": "text", "text": READ_PROMPT.format(title=song["title"])})
    return {"custom_id": name,
            "params": {"model": model, "max_tokens": MAX_TOKENS,
                       "output_config": {
                           "effort": effort,
                           "format": {"type": "json_schema", "schema": PAGE_SCHEMA}},
                       "messages": [{"role": "user", "content": content}]}}


def pages_submit(client, songs: dict[str, dict], model: str, effort: str,
                 sample: int | None) -> list[str]:
    """Send songs to be read from the printed page, as half-price batches.

    Args:
        client: An anthropic.Anthropic client.
        songs: The Real Book index.
        model: Model id.
        effort: How hard the model should think.
        sample: Read this many randomly chosen songs instead of all of them.

    Returns:
        The batch ids.
    """
    if BATCH_PATH.exists():
        sys.exit(f"A batch is waiting to be collected: see {BATCH_PATH}")
    db = json.loads((DATA / "songs.json").read_text(encoding="utf-8"))
    meta = json.loads((DATA / "metadata.json").read_text(encoding="utf-8"))
    flagged = {i for i, m in meta.items() if m.get("page_mismatch")}
    todo = page_todo(songs, chords.load_store(CHORDS_PATH), flagged)
    if sample:
        todo = sorted(random.Random(SAMPLE_SEED).sample(todo, sample))
    starts: dict[str, list[int]] = {}
    for song in songs.values():
        for src in song["sources"]:
            starts.setdefault(src["book"], []).append(src["pdf_page"])
    starts = {book: sorted(set(pages)) for book, pages in starts.items()}

    names = {f"song-{n:04d}": song_id for n, song_id in enumerate(todo)}
    picks: dict[str, dict] = {}

    def requests() -> Iterable[dict]:
        for name, song_id in names.items():
            song = songs[song_id]
            source = min(song["sources"], key=lambda s: book_order(s["book"]))
            book = db["books"][source["book"]]
            picks[song_id] = {"book": book["label"], "page": source["printed"]}
            yield page_request(name, model, effort, song, source,
                               page_span(source, starts), BOOKS_DIR / book["pdf"],
                               book["pages"])

    print(f"Reading {len(todo)} songs ({len(flagged)} skipped: flagged page numbers)")
    state = {"mode": "pages", "model": model, "effort": effort, "songs": names,
             "sources": picks}
    return send_batches(client, requests(), state)


def page_entry(song: dict, message, model: str,
               source: dict) -> tuple[dict | None, str, dict]:
    """Turn one page reading into a saved entry.

    A chart is saved only if the title printed on the page is this song, so a
    wrong page number can't attach another tune's chords to it.

    Returns:
        (entry or None, problem text, token usage).
    """
    usage = {"input": message.usage.input_tokens,
             "output": message.usage.output_tokens}
    if message.stop_reason != "end_turn":
        return None, f"stopped early: {message.stop_reason}", usage
    text = "".join(b.text for b in message.content if b.type == "text")
    try:
        read = json.loads(text)
    except json.JSONDecodeError:
        return None, "reply was not valid JSON", usage
    if not index_book._titles_match(song["title"], read.get("title_on_page", "")):
        return None, WRONG_PAGE, usage
    if not read.get("legible"):
        return None, "page could not be read", usage
    sections = [{"name": s["name"], "bars": [chords.clean_bar(b) for b in s["bars"]]}
                for s in read.get("sections", []) if s.get("bars")]
    problem = chords.validate_sections(sections)
    if problem:
        return None, problem, usage
    entry = to_entry(song, {"form": read.get("form", ""),
                            "confidence": read.get("confidence", "low"),
                            "sections": sections}, model)
    if entry["key"] == "unknown" and chords.is_chord_token(read.get("printed_key", "")):
        entry["key"] = read["printed_key"]
    entry.update({"source": "book", "source_book": source["book"],
                  "source_page": source["page"]})
    return entry, "", usage


def batch_collect(client, songs: dict[str, dict], wait: bool = False) -> bool:
    """Save finished batches' charts.

    Args:
        client: An anthropic.Anthropic client.
        songs: The Real Book index.
        wait: Keep checking until every batch ends.

    Returns:
        True if the results were saved, False if a batch is still running.
    """
    if not BATCH_PATH.exists():
        sys.exit("No batch is waiting. Submit one first.")
    state = json.loads(BATCH_PATH.read_text(encoding="utf-8"))
    while True:
        unfinished = 0
        for batch_id in state["batch_ids"]:
            batch = client.messages.batches.retrieve(batch_id)
            counts = batch.request_counts
            print(f"{batch.processing_status}: {counts.processing} processing, "
                  f"{counts.succeeded} succeeded, {counts.errored} errored",
                  flush=True)
            unfinished += batch.processing_status != "ended"
        if not unfinished:
            break
        if not wait:
            return False
        time.sleep(BATCH_POLL_SECONDS)

    from_pages = state["mode"] == "pages"
    if from_pages and CHORDS_PATH.exists() and not MEMORY_BACKUP_PATH.exists():
        shutil.copy2(CHORDS_PATH, MEMORY_BACKUP_PATH)
    store = chords.load_store(CHORDS_PATH)
    declined = load_declined()
    total = {"input": 0, "output": 0}
    tally = {"saved": 0, "declined": 0, "wrong page": 0, "failed": 0}
    for batch_id in state["batch_ids"]:
        for item in client.messages.batches.results(batch_id):
            song_id = state["songs"][item.custom_id]
            if item.result.type != "succeeded":
                logger.warning("%s: %s", song_id, item.result.type)
                tally["failed"] += 1
                continue
            message = item.result.message
            if from_pages:
                entry, problem, usage = page_entry(
                    songs[song_id], message, state["model"],
                    state["sources"][song_id])
            else:
                chart, problem, usage = parse_reply(message)
                entry = chart and to_entry(songs[song_id], chart, state["model"])
            total["input"] += usage["input"]
            total["output"] += usage["output"]
            if entry:
                store[song_id] = entry
                declined.discard(song_id)
                tally["saved"] += 1
            elif problem == DECLINED:
                declined.add(song_id)
                tally["declined"] += 1
            elif problem == WRONG_PAGE:
                logger.warning("%s: the page shows another title", song_id)
                tally["wrong page"] += 1
            else:
                logger.warning("%s not saved: %s", song_id, problem)
                tally["failed"] += 1
    chords.save_store(CHORDS_PATH, store)
    DECLINED_PATH.write_text(json.dumps(sorted(declined)), encoding="utf-8")
    BATCH_PATH.unlink()
    spent = cost(state["model"], total) * BATCH_DISCOUNT
    print(f"Done. {tally}. {len(store)} charts saved in total. ${spent:.2f} spent.")
    return True


def review_priority(song: dict) -> tuple[int, int]:
    """Sort key: songs with no verified data first, then the most widely printed."""
    unverified = "unknown" in (song.get("composer", "unknown"),
                               song.get("key", "unknown"))
    return (0 if unverified else 1, -len(song["sources"]))


def flatten(sections: list[dict]) -> str:
    """One readable line of chords for a spreadsheet cell."""
    return "  ".join(f"[{s['name']}] " + " | ".join(s["bars"]) for s in sections)


def export_review(songs: dict[str, dict]) -> int:
    """Write the review spreadsheet. Returns the number of rows."""
    store = chords.load_store(CHORDS_PATH)
    ordered = sorted((s for i, s in songs.items() if i in store),
                     key=review_priority)
    with open(REVIEW_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "title", "composer", "key", "books", "confidence",
                         "review", "form", "chords"])
        for song in ordered:
            entry = store[song["id"]]
            writer.writerow([song["id"], song["title"],
                             song.get("composer", "unknown"), entry["key"],
                             len(song["sources"]), entry["confidence"],
                             entry["review"], entry["form"],
                             flatten(entry["sections"])])
    return len(ordered)


def import_review() -> dict[str, int]:
    """Apply approved and rejected marks from the review spreadsheet."""
    store = chords.load_store(CHORDS_PATH)
    counts = {"approved": 0, "rejected": 0}
    with open(REVIEW_PATH, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            mark = row["review"].strip().lower()
            if mark in counts and row["id"] in store:
                store[row["id"]]["review"] = mark
                counts[mark] += 1
    chords.save_store(CHORDS_PATH, store)
    return counts


def main() -> None:
    """Run the command chosen on the command line."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--test", action="store_true")
    action.add_argument("--all", action="store_true")
    action.add_argument("--batch-submit", action="store_true")
    action.add_argument("--pages-submit", action="store_true")
    action.add_argument("--batch-collect", action="store_true")
    action.add_argument("--export-review", action="store_true")
    action.add_argument("--import-review", action="store_true")
    parser.add_argument("--model", help="Claude model id. Required with --all.")
    parser.add_argument("--effort", default=DEFAULT_EFFORT,
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--sample", type=int,
                        help="With --pages-submit, read this many random songs.")
    parser.add_argument("--wait", action="store_true",
                        help="With --batch-collect, wait for the batch to end.")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, help="Stop after this many songs.")
    args = parser.parse_args()

    songs = load_songs()
    if args.test:
        run_test(make_client(), songs,
                 [args.model] if args.model else TEST_MODELS, args.effort)
    elif args.all:
        if not args.model:
            sys.exit("--all needs --model, for example claude-opus-5-5")
        run_all(make_client(), songs, args.model, args.workers, args.limit,
                args.effort)
    elif args.batch_submit:
        if not args.model:
            sys.exit("--batch-submit needs --model, for example claude-opus-5-5")
        batch_submit(make_client(), songs, args.model, args.effort, args.limit)
    elif args.pages_submit:
        if not args.model:
            sys.exit("--pages-submit needs --model, for example claude-opus-5-5")
        pages_submit(make_client(), songs, args.model, args.effort, args.sample)
    elif args.batch_collect:
        batch_collect(make_client(), songs, args.wait)
    elif args.export_review:
        print(f"Wrote {export_review(songs)} songs to {REVIEW_PATH}")
    else:
        print(import_review())


if __name__ == "__main__":
    main()
