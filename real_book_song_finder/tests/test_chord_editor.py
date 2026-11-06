"""Tests for the chord editor, page reading and page-number safety."""
import json
from types import SimpleNamespace

import pytest

import chords
import generate_chords

APP = {"X-Song-Finder": "1"}


def signed_in(server, email: str):
    """Create an account and return its signed-in test client."""
    client = server.app.test_client()
    client.post("/signup", data={"email": email, "password": "long-password-1"})
    return client


def edit(client, song: str, text: str, headers: dict | None = None):
    """Post one correction."""
    return client.post("/api/chords/edit", headers=APP if headers is None else headers,
                       json={"song": song, "text": text})


@pytest.mark.parametrize(
    ("bar", "expected"),
    [
        ("Dm7 G7", "Dm7 G7"),
        ("(1st ending) Dm7", "Dm7"),
        ("1. Cmaj7", "Cmaj7"),
        ("(Fmaj7/G)", "Fmaj7/G"),
        ("Gb7(13)", "Gb7(13)"),
        ("B07", "Bdim7"),
        ("Coda", "%"),
        ("Break", "%"),
        ("", "%"),
    ],
)
def test_clean_bar_keeps_only_chords(bar: str, expected: str) -> None:
    """Test that markers and words printed on the page are removed."""
    assert chords.clean_bar(bar) == expected


def test_parse_chart_text_reads_sections_and_bars() -> None:
    """Test the format the editor uses."""
    sections, problem = chords.parse_chart_text("[A] Dm7 G7 | Cmaj7\n\n[B] F7 | %")
    assert problem is None
    assert sections == [{"name": "A", "bars": ["Dm7 G7", "Cmaj7"]},
                        {"name": "B", "bars": ["F7", "%"]}]


def test_parse_chart_text_round_trips_with_chart_to_text() -> None:
    """Test that opening and saving a chart changes nothing."""
    original = [{"name": "A", "bars": ["C6/9", "F#m7b5 B7#9"]}]
    assert chords.parse_chart_text(chords.chart_to_text(original))[0] == original


@pytest.mark.parametrize(
    ("text", "word"),
    [("[A] Dm7 hello", "hello"), ("[A] Coda", "Coda"), ("", "at least one"), ("[A]", "no chords")],
)
def test_parse_chart_text_explains_problems(text: str, word: str) -> None:
    """Test that a mistake gets a message a person can act on."""
    sections, problem = chords.parse_chart_text(text)
    assert sections == [] and word in problem


def test_local_edit_applies_at_once_and_survives_a_restart(start_app) -> None:
    """Test that a fix on your own PC is saved and marked as a person's."""
    server = start_app()
    client = server.app.test_client()

    response = edit(client, "autumn leaves", "[A] Am7 D7 | Gmaj7")

    assert response.get_json() == {"published": True}
    data = client.get("/api/chords/lookup", query_string={"song": "autumn leaves"}).get_json()
    assert data["edited"] is True and data["reviewed"] is True
    assert data["sections"][0]["bars"] == ["Am7 D7", "Gmaj7"]
    saved = json.loads(server.CHORD_EDITS_PATH.read_text(encoding="utf-8"))
    assert saved["edits"]["autumn leaves"]["previous"][0]["bars"][0] == "Am7"
    assert chords.load_store(server.CHORDS_PATH)["autumn leaves"]["sections"][0]["bars"][0] == "Am7"


def test_edit_transposes_for_an_instrument(start_app) -> None:
    """Test that corrected chords still change with the instrument."""
    server = start_app()
    client = server.app.test_client()
    edit(client, "autumn leaves", "[A] Am7")
    data = client.get("/api/chords/lookup", query_string={
        "song": "autumn leaves", "instrument": "bb"}).get_json()
    assert data["sections"][0]["bars"] == ["Bm7"]


def test_edit_can_add_chords_to_a_song_with_none(start_app) -> None:
    """Test that a song with no saved chart can be given one."""
    server = start_app()
    client = server.app.test_client()
    assert client.get("/api/chords/lookup", query_string={"song": "blue bossa"}).get_json()["available"] is False
    assert edit(client, "blue bossa", "[A] Cm7 | Fm7").status_code == 200
    assert client.get("/api/chords/lookup", query_string={"song": "blue bossa"}).get_json()["available"] is True


def test_source_is_always_concert_pitch(start_app) -> None:
    """Test that the editor opens the chart in concert pitch."""
    server = start_app()
    text = server.app.test_client().get(
        "/api/chords/source", query_string={"song": "autumn leaves"}).get_json()["text"]
    assert text.startswith("[A] Am7 | D7")


def test_edit_refuses_bad_chords_and_outside_songs(start_app) -> None:
    """Test that the index limit also holds for edits."""
    server = start_app()
    client = server.app.test_client()
    response = edit(client, "autumn leaves", "[A] Am7 hello")
    assert response.status_code == 400 and "hello" in response.get_json()["message"]
    assert edit(client, "write me a poem", "[A] Am7").status_code == 404
    assert edit(client, "autumn leaves", "[A] Am7", headers={}).status_code == 403


def test_customer_edit_waits_for_approval(start_app) -> None:
    """Test that a customer's suggestion changes nothing until approved."""
    server = start_app(MULTI_USER="1", CHORD_ADMIN_EMAILS="owner@example.com")
    customer = signed_in(server, "customer@example.com")
    owner = signed_in(server, "owner@example.com")
    query = {"song": "autumn leaves"}
    original = customer.get("/api/chords/lookup", query_string=query).get_json()

    assert edit(customer, "autumn leaves", "[A] Am7").get_json() == {"published": False}
    assert customer.get("/api/chords/lookup", query_string=query).get_json() == original
    assert customer.get("/api/chords/pending").status_code == 403

    pending = owner.get("/api/chords/pending").get_json()["pending"]
    assert [p["by"] for p in pending] == ["customer@example.com"]
    url = f"/api/chords/pending/{pending[0]['id']}"
    assert customer.post(url, headers=APP, json={"action": "approve"}).status_code == 403
    assert owner.post(url, headers=APP, json={"action": "approve"}).status_code == 200

    data = customer.get("/api/chords/lookup", query_string=query).get_json()
    assert data["sections"][0]["bars"] == ["Am7"] and data["edited"] is True
    assert owner.get("/api/chords/pending").get_json()["pending"] == []


def test_rejected_suggestion_changes_nothing(start_app) -> None:
    """Test the reject path."""
    server = start_app(MULTI_USER="1", CHORD_ADMIN_EMAILS="owner@example.com")
    customer = signed_in(server, "customer@example.com")
    owner = signed_in(server, "owner@example.com")
    edit(customer, "autumn leaves", "[A] Am7")
    key = owner.get("/api/chords/pending").get_json()["pending"][0]["id"]
    owner.post(f"/api/chords/pending/{key}", headers=APP, json={"action": "reject"})
    assert server.CHORD_EDITS["edits"] == {} and server.CHORD_EDITS["pending"] == {}


def test_approved_edit_reaches_customers_with_reviewed_only(start_app) -> None:
    """Test that a correction counts as approved in the customer app."""
    server = start_app(MULTI_USER="1", CHORDS_REVIEWED_ONLY="1",
                       CHORD_ADMIN_EMAILS="owner@example.com")
    customer = signed_in(server, "customer@example.com")
    owner = signed_in(server, "owner@example.com")
    query = {"song": "autumn leaves"}
    assert customer.get("/api/chords/lookup", query_string=query).get_json()["available"] is False
    edit(owner, "autumn leaves", "[A] Am7")
    assert customer.get("/api/chords/lookup", query_string=query).get_json()["available"] is True


def test_editing_is_off_where_the_disk_does_not_persist(start_app) -> None:
    """Test the private cloud app."""
    server = start_app(LOCAL_LIBRARY="0")
    client = server.app.test_client()
    assert client.get("/api/chords/songs").get_json()["editing"]["enabled"] is False
    assert edit(client, "autumn leaves", "[A] Am7").status_code == 404


def message(reply: dict, stop: str = "end_turn") -> SimpleNamespace:
    """A stand-in for one model reply."""
    return SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="text", text=json.dumps(reply))],
        usage=SimpleNamespace(input_tokens=5000, output_tokens=500))


READING = {"title_on_page": "BLUE BOSSA", "printed_key": "Cm", "legible": True,
           "form": "16 bars", "confidence": "high",
           "sections": [{"name": "A", "bars": ["Cm7", "(1st", "Fm7"]}]}
SONG = {"id": "blue bossa", "title": "Blue Bossa", "key": "Cm"}
SOURCE = {"book": "bk6", "page": 49}


def test_page_entry_saves_a_good_reading_with_its_source() -> None:
    """Test a normal reading."""
    entry, problem, _ = generate_chords.page_entry(SONG, message(READING), "m", SOURCE)
    assert problem == ""
    assert entry["sections"][0]["bars"] == ["Cm7", "%", "Fm7"]
    assert (entry["source"], entry["source_book"], entry["source_page"]) == ("book", "bk6", 49)
    assert entry["review"] == "unreviewed"


def test_page_entry_refuses_a_page_showing_another_song() -> None:
    """Test that a wrong page number can't attach another tune's chords."""
    wrong = {**READING, "title_on_page": "Happy Little Sunbeam"}
    entry, problem, _ = generate_chords.page_entry(SONG, message(wrong), "m", SOURCE)
    assert entry is None and problem == generate_chords.WRONG_PAGE


def test_page_entry_refuses_an_unreadable_page() -> None:
    """Test that a page the model could not read is not saved."""
    entry, problem, _ = generate_chords.page_entry(
        SONG, message({**READING, "legible": False}), "m", SOURCE)
    assert entry is None and problem


def test_page_entry_refuses_a_page_with_no_chords() -> None:
    """Test that a chart with nothing but empty bars is not saved."""
    empty = {**READING, "sections": [{"name": "A", "bars": ["", "Coda", "1."]}]}
    entry, problem, _ = generate_chords.page_entry(SONG, message(empty), "m", SOURCE)
    assert entry is None and problem == "no chords"


def test_lookup_shows_which_book_a_chart_came_from(start_app) -> None:
    """Test that the source reaches the page label."""
    server = start_app()
    server.CHORDS["autumn leaves"].update(
        source="book", source_book="Real Book 6th Edition", source_page=39)
    data = server.app.test_client().get(
        "/api/chords/lookup", query_string={"song": "autumn leaves"}).get_json()
    assert data["source_book"] == "Real Book 6th Edition" and data["source_page"] == 39


def test_page_entry_uses_the_printed_key_when_ours_is_unknown() -> None:
    """Test that a reading fills in a missing key."""
    song = {**SONG, "key": "unknown"}
    entry, _, _ = generate_chords.page_entry(song, message(READING), "m", SOURCE)
    assert entry["key"] == "Cm"


@pytest.mark.parametrize(
    ("page", "expected"),
    [(10, 2), (12, 1), (13, 3), (30, 1)],
)
def test_page_span_follows_the_next_song(page: int, expected: int) -> None:
    """Test two-page songs, the three-page cap and the last song in a book."""
    starts = {"bk6": [10, 12, 13, 30]}
    assert generate_chords.page_span({"book": "bk6", "pdf_page": page}, starts) == expected


def test_page_todo_keeps_high_approved_read_and_flagged_songs_out() -> None:
    """Test which songs get read."""
    store = {"high": {"confidence": "high"}, "approved": {"review": "approved"},
             "read": {"source": "book"}, "low": {"confidence": "low"}}
    songs = dict.fromkeys(["high", "approved", "read", "low", "none", "flagged"], {})
    assert generate_chords.page_todo(songs, store, {"flagged"}) == ["low", "none"]


def test_book_order_prefers_typeset_books() -> None:
    """Test that handwritten books are used last."""
    assert generate_chords.book_order("bk6") < generate_chords.book_order("bk2")


def test_send_batches_splits_at_the_size_limit(tmp_path, monkeypatch) -> None:
    """Test that a large run becomes several batches, and all ids are kept."""
    monkeypatch.setattr(generate_chords, "BATCH_PATH", tmp_path / "batch.json")
    monkeypatch.setattr(generate_chords, "BATCH_MAX_BYTES", 100)
    sent: list[int] = []

    def create(requests):
        sent.append(len(requests))
        return SimpleNamespace(id=f"batch_{len(sent)}")

    client = SimpleNamespace(messages=SimpleNamespace(
        batches=SimpleNamespace(create=create)))
    ids = generate_chords.send_batches(client, ({"x": "y" * 40} for _ in range(5)), {})

    assert sent == [2, 2, 1]
    assert ids == ["batch_1", "batch_2", "batch_3"]
    assert json.loads((tmp_path / "batch.json").read_text())["batch_ids"] == ids
