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

    python generate_chords.py --batch-collect
        Save a finished batch. Add --wait to keep checking until it ends.

    python generate_chords.py --export-review
        Write data/chord_review.csv for checking in Excel, with the songs that
        most need a look first.

    python generate_chords.py --import-review
        Apply the review column (approved or rejected) from that file.
"""
import argparse
import csv
import datetime
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import chords

APP_DIR = Path(__file__).parent
DATA = APP_DIR / "data"
CHORDS_PATH = DATA / "chords.json"
REVIEW_PATH = DATA / "chord_review.csv"
DECLINED_PATH = DATA / "chord_declined.json"
BATCH_PATH = DATA / "chord_batch.json"
TEST_DIR = DATA / "chord_test"
DECLINED = "declined"
BATCH_DISCOUNT = 0.5
BATCH_POLL_SECONDS = 60

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


def batch_submit(client, songs: dict[str, dict], model: str, effort: str,
                 limit: int | None) -> str:
    """Send every song with no saved chart as one half-price batch.

    Returns:
        The batch id, also saved in data/chord_batch.json so results can be
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
    batch = client.messages.batches.create(requests=requests)
    BATCH_PATH.write_text(json.dumps(
        {"batch_id": batch.id, "model": model, "effort": effort, "songs": names},
        indent=1), encoding="utf-8")
    print(f"Submitted batch {batch.id}: {len(requests)} songs on {model}")
    return batch.id


def batch_collect(client, songs: dict[str, dict], wait: bool = False) -> bool:
    """Save a finished batch's charts.

    Args:
        client: An anthropic.Anthropic client.
        songs: The Real Book index.
        wait: Keep checking until the batch ends.

    Returns:
        True if the results were saved, False if the batch is still running.
    """
    if not BATCH_PATH.exists():
        sys.exit("No batch is waiting. Submit one with --batch-submit.")
    state = json.loads(BATCH_PATH.read_text(encoding="utf-8"))
    while True:
        batch = client.messages.batches.retrieve(state["batch_id"])
        counts = batch.request_counts
        print(f"{batch.processing_status}: {counts.processing} processing, "
              f"{counts.succeeded} succeeded, {counts.errored} errored", flush=True)
        if batch.processing_status == "ended":
            break
        if not wait:
            return False
        time.sleep(BATCH_POLL_SECONDS)

    store = chords.load_store(CHORDS_PATH)
    declined = load_declined()
    total = {"input": 0, "output": 0}
    tally = {"saved": 0, "declined": 0, "failed": 0}
    for item in client.messages.batches.results(state["batch_id"]):
        song_id = state["songs"][item.custom_id]
        if item.result.type != "succeeded":
            logger.warning("%s: %s", song_id, item.result.type)
            tally["failed"] += 1
            continue
        chart, problem, usage = parse_reply(item.result.message)
        total["input"] += usage["input"]
        total["output"] += usage["output"]
        if problem == DECLINED:
            declined.add(song_id)
            tally["declined"] += 1
        elif chart is None:
            logger.warning("%s not saved: %s", song_id, problem)
            tally["failed"] += 1
        else:
            store[song_id] = to_entry(songs[song_id], chart, state["model"])
            tally["saved"] += 1
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
    action.add_argument("--batch-collect", action="store_true")
    action.add_argument("--export-review", action="store_true")
    action.add_argument("--import-review", action="store_true")
    parser.add_argument("--model", help="Claude model id. Required with --all.")
    parser.add_argument("--effort", default=DEFAULT_EFFORT,
                        choices=["low", "medium", "high", "xhigh", "max"])
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
    elif args.batch_collect:
        batch_collect(make_client(), songs, args.wait)
    elif args.export_review:
        print(f"Wrote {export_review(songs)} songs to {REVIEW_PATH}")
    else:
        print(import_review())


if __name__ == "__main__":
    main()
