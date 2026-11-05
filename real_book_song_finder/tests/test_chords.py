"""Tests for chord transposition, chart checks and the review rules."""
import json
from types import SimpleNamespace

import pytest

import chords
import generate_chords


@pytest.mark.parametrize(
    ("chord", "semitones", "key", "expected"),
    [
        ("Cmaj7", 2, "D", "Dmaj7"),
        ("Bb7", 2, "C", "C7"),
        ("F#m7b5", 2, "A", "G#m7b5"),
        ("Am7", 9, "Db", "Gbm7"),
        ("Dm7/G", 2, "F", "Em7/A"),
        ("C6/9", 2, "D", "D6/9"),
        ("Bb7#9", 2, "C", "C7#9"),
        ("Ebmaj7#11", 7, "Bb", "Bbmaj7#11"),
        ("Cmaj7", 0, "C", "Cmaj7"),
        ("F#m7", 0, "E", "F#m7"),
        ("N.C.", 2, "D", "N.C."),
        ("%", 2, "D", "%"),
    ],
)
def test_transpose_chord_moves_root_and_bass_only(
    chord: str, semitones: int, key: str, expected: str
) -> None:
    """Test that transposing changes the notes and keeps the chord quality."""
    assert chords.transpose_chord(chord, semitones, key) == expected


@pytest.mark.parametrize(
    ("key", "semitones", "expected"),
    [("C", 2, "D"), ("Bb", 2, "C"), ("Eb", 9, "C"), ("Em", 2, "F#m"), ("Em", 9, "C#m"),
     ("F", 7, "C"), ("Cm", 9, "Am"), ("unknown", 2, "unknown")],
)
def test_transpose_key_returns_written_key(key: str, semitones: int, expected: str) -> None:
    """Test that keys move correctly and an unknown key is left alone."""
    assert chords.transpose_key(key, semitones) == expected


def test_transpose_chart_bb_trumpet_is_up_a_whole_step() -> None:
    """Test that a Bb instrument reads one whole step above concert."""
    entry = {"key": "Eb", "form": "AB",
             "sections": [{"name": "A", "bars": ["Ebmaj7", "Fm7 Bb7"]}]}
    written = chords.transpose_chart(entry, "bb")
    assert written["key"] == "F"
    assert written["concert_key"] == "Eb"
    assert written["sections"][0]["bars"] == ["Fmaj7", "Gm7 C7"]


def test_transpose_chart_eb_alto_is_up_a_major_sixth() -> None:
    """Test that an Eb instrument reads a major sixth above concert."""
    entry = {"key": "C", "sections": [{"name": "A", "bars": ["Cmaj7"]}]}
    written = chords.transpose_chart(entry, "eb")
    assert written["key"] == "A"
    assert written["sections"][0]["bars"] == ["Amaj7"]


def test_transpose_chart_bass_stays_at_concert_pitch() -> None:
    """Test that bass reads the same chords as concert."""
    entry = {"key": "C", "sections": [{"name": "A", "bars": ["Cmaj7"]}]}
    assert chords.transpose_chart(entry, "bass")["sections"][0]["bars"] == ["Cmaj7"]


def test_every_real_book_key_transposes_for_every_instrument() -> None:
    """Test that all keys found in metadata.json give a real key name."""
    keys = ["F", "C", "Bb", "Eb", "G", "Cm", "Fm", "Ab", "Dm", "Gm", "D", "Am", "Em",
            "Db", "Bbm", "A", "Bm", "E", "Ebm", "Gb", "F#m", "B", "C#m", "F#"]
    for key in keys:
        for _, semitones in chords.INSTRUMENTS.values():
            assert chords.transpose_key(key, semitones) != ""


@pytest.mark.parametrize(
    ("sections", "ok"),
    [
        ([{"name": "A", "bars": ["Dm7 G7", "Cmaj7", "%", "N.C."]}], True),
        ([], False),
        ("nope", False),
        ([{"name": "A", "bars": []}], False),
        ([{"name": "A", "bars": ["hello world"]}], False),
        ([{"name": "A", "bars": ["  "]}], False),
    ],
)
def test_validate_sections_accepts_only_real_charts(sections: object, ok: bool) -> None:
    """Test that a chart with anything but chords in it is refused."""
    assert (chords.validate_sections(sections) is None) is ok


@pytest.mark.parametrize(
    ("entry", "reviewed_only", "expected"),
    [
        (None, False, False),
        ({"review": "rejected", "confidence": "high"}, False, False),
        ({"review": "approved", "confidence": "low"}, True, True),
        ({"review": "unreviewed", "confidence": "high"}, False, True),
        ({"review": "unreviewed", "confidence": "high"}, True, False),
        ({"review": "unreviewed", "confidence": "medium"}, False, False),
        ({"review": "unreviewed", "confidence": "low"}, False, False),
    ],
)
def test_is_servable_follows_the_review_rules(
    entry: dict | None, reviewed_only: bool, expected: bool
) -> None:
    """Test which saved charts may be shown."""
    assert chords.is_servable(entry, reviewed_only) is expected


def test_load_store_skips_bad_charts(tmp_path) -> None:
    """Test that one damaged saved chart does not stop the rest loading."""
    path = tmp_path / "chords.json"
    good = {"sections": [{"name": "A", "bars": ["C7"]}]}
    path.write_text(json.dumps({"songs": {"good": good, "bad": {"sections": []}}}),
                    encoding="utf-8")
    assert list(chords.load_store(path)) == ["good"]


def test_load_store_missing_file_is_empty(tmp_path) -> None:
    """Test that the app runs before any chords have been generated."""
    assert chords.load_store(tmp_path / "none.json") == {}


def test_build_prompt_includes_known_composer_and_key() -> None:
    """Test that verified details are given to the model."""
    prompt = generate_chords.build_prompt(
        {"title": "Blue Bossa", "composer": "Kenny Dorham", "key": "Cm"})
    assert "Blue Bossa" in prompt and "Kenny Dorham" in prompt and "Cm" in prompt


def test_build_prompt_leaves_out_unknown_details() -> None:
    """Test that "unknown" is not passed off as a composer or key."""
    prompt = generate_chords.build_prompt(
        {"title": "Inner Urge", "composer": "unknown", "key": "unknown"})
    assert "Composer:" not in prompt and "Key as printed" not in prompt


def test_review_priority_puts_unverified_and_popular_songs_first() -> None:
    """Test the order of the review spreadsheet."""
    songs = [
        {"id": "plain", "composer": "A", "key": "C", "sources": [1]},
        {"id": "popular", "composer": "A", "key": "C", "sources": [1, 2, 3]},
        {"id": "unverified", "composer": "unknown", "key": "C", "sources": [1]},
    ]
    ordered = sorted(songs, key=generate_chords.review_priority)
    assert [s["id"] for s in ordered] == ["unverified", "popular", "plain"]


class _FakeMessages:
    """Stands in for the Claude API with a fixed reply."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply

    def create(self, **kwargs):
        class Block:
            type = "text"
            text = json.dumps(self.reply)

        class Usage:
            input_tokens = 600
            output_tokens = 300

        class Response:
            stop_reason = "end_turn"
            content = [Block()]
            usage = Usage()

        return Response()


class _FakeClient:
    def __init__(self, reply: dict) -> None:
        self.messages = _FakeMessages(reply)


def test_ask_claude_reports_a_declined_song() -> None:
    """Test that "I don't know this song" is not treated as an error."""
    client = _FakeClient({"form": "", "confidence": "low", "sections": []})
    chart, problem, _ = generate_chords.ask_claude(client, "m", {"title": "Rare Tune"})
    assert chart is None
    assert problem == generate_chords.DECLINED


def test_run_all_saves_charts_and_never_repays_for_a_declined_song(
    tmp_path, monkeypatch
) -> None:
    """Test that a rerun skips saved songs and songs the model declined."""
    monkeypatch.setattr(generate_chords, "CHORDS_PATH", tmp_path / "chords.json")
    monkeypatch.setattr(generate_chords, "DECLINED_PATH", tmp_path / "declined.json")
    calls = []
    def fake_ask(client, model, song, effort):
        calls.append(song["id"])
        if song["id"] == "rare":
            return None, generate_chords.DECLINED, {"input": 600, "output": 20}
        chart = {"form": "A", "confidence": "high",
                 "sections": [{"name": "A", "bars": ["C7"]}]}
        return chart, "", {"input": 600, "output": 300}
    monkeypatch.setattr(generate_chords, "ask_claude", fake_ask)
    songs = {"rare": {"id": "rare", "title": "Rare"},
             "famous": {"id": "famous", "title": "Famous", "key": "C"}}

    generate_chords.run_all(None, songs, "m", workers=2, limit=None)
    generate_chords.run_all(None, songs, "m", workers=2, limit=None)

    assert sorted(calls) == ["famous", "rare"]
    saved = chords.load_store(tmp_path / "chords.json")
    assert list(saved) == ["famous"]
    assert saved["famous"]["review"] == "unreviewed"


class _FakeBatches:
    """Stands in for the Batches API."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.status = "ended"

    def create(self, requests):
        self.sent = requests
        return SimpleNamespace(id="batch_1")

    def retrieve(self, batch_id):
        return SimpleNamespace(
            processing_status=self.status,
            request_counts=SimpleNamespace(processing=0, succeeded=2, errored=1))

    def results(self, batch_id):
        def message(reply: dict) -> SimpleNamespace:
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text=json.dumps(reply))],
                usage=SimpleNamespace(input_tokens=600, output_tokens=300))

        good = {"form": "A", "confidence": "high",
                "sections": [{"name": "A", "bars": ["C7"]}]}
        no = {"form": "", "confidence": "low", "sections": []}
        return [
            SimpleNamespace(custom_id="song-0000", result=SimpleNamespace(
                type="succeeded", message=message(good))),
            SimpleNamespace(custom_id="song-0001", result=SimpleNamespace(
                type="succeeded", message=message(no))),
            SimpleNamespace(custom_id="song-0002", result=SimpleNamespace(
                type="errored")),
        ]


@pytest.fixture
def batch_setup(tmp_path, monkeypatch):
    """Point the batch files at a temporary folder and return a fake client."""
    monkeypatch.setattr(generate_chords, "CHORDS_PATH", tmp_path / "chords.json")
    monkeypatch.setattr(generate_chords, "DECLINED_PATH", tmp_path / "declined.json")
    monkeypatch.setattr(generate_chords, "BATCH_PATH", tmp_path / "batch.json")
    client = SimpleNamespace(messages=SimpleNamespace(batches=_FakeBatches()))
    songs = {name: {"id": name, "title": name.title()}
             for name in ("take the a train", "rare tune", "third song")}
    return client, songs


def test_batch_submit_uses_ids_without_spaces(batch_setup) -> None:
    """Test that song ids with spaces are mapped to ids the API accepts."""
    client, songs = batch_setup
    generate_chords.batch_submit(client, songs, "m", "high", None)
    sent = client.messages.batches.sent
    assert [r["custom_id"] for r in sent] == ["song-0000", "song-0001", "song-0002"]
    assert sent[0]["params"]["output_config"]["effort"] == "high"
    assert generate_chords.BATCH_PATH.exists()


def test_batch_submit_refuses_a_second_batch_before_collecting(batch_setup) -> None:
    """Test that an uncollected batch can't be lost or paid for twice."""
    client, songs = batch_setup
    generate_chords.batch_submit(client, songs, "m", "high", None)
    with pytest.raises(SystemExit):
        generate_chords.batch_submit(client, songs, "m", "high", None)


def test_batch_collect_saves_charts_and_records_declines(batch_setup) -> None:
    """Test each outcome, and that a failed song is left to retry."""
    client, songs = batch_setup
    generate_chords.batch_submit(client, songs, "m", "high", None)

    assert generate_chords.batch_collect(client, songs) is True

    saved = chords.load_store(generate_chords.CHORDS_PATH)
    assert list(saved) == ["take the a train"]
    assert generate_chords.load_declined() == {"rare tune"}
    assert not generate_chords.BATCH_PATH.exists()
    generate_chords.batch_submit(client, songs, "m", "high", None)
    assert [r["custom_id"] for r in client.messages.batches.sent] == ["song-0000"]


def test_batch_collect_keeps_waiting_state_while_running(batch_setup) -> None:
    """Test that checking early changes nothing."""
    client, songs = batch_setup
    generate_chords.batch_submit(client, songs, "m", "high", None)
    client.messages.batches.status = "in_progress"
    assert generate_chords.batch_collect(client, songs) is False
    assert generate_chords.BATCH_PATH.exists()


def test_review_round_trip_marks_songs(tmp_path, monkeypatch) -> None:
    """Test that marks made in the spreadsheet reach the saved charts."""
    store_path = tmp_path / "chords.json"
    review_path = tmp_path / "review.csv"
    monkeypatch.setattr(generate_chords, "CHORDS_PATH", store_path)
    monkeypatch.setattr(generate_chords, "REVIEW_PATH", review_path)
    entry = {"key": "C", "form": "A", "confidence": "high", "review": "unreviewed",
             "sections": [{"name": "A", "bars": ["C7"]}]}
    chords.save_store(store_path, {"one": dict(entry), "two": dict(entry)})
    songs = {"one": {"id": "one", "title": "One", "sources": [1]},
             "two": {"id": "two", "title": "Two", "sources": [1]}}

    assert generate_chords.export_review(songs) == 2
    text = review_path.read_text(encoding="utf-8-sig").replace(
        "high,unreviewed", "high,approved", 1)
    review_path.write_text(text, encoding="utf-8-sig")

    assert generate_chords.import_review() == {"approved": 1, "rejected": 0}
    reviews = sorted(e["review"] for e in chords.load_store(store_path).values())
    assert reviews == ["approved", "unreviewed"]
