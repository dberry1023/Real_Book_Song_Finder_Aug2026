"""Shared test fixtures: the app in local or multi-user mode, on temporary folders."""
import importlib
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pymupdf
import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

ENV_NAMES = ["MULTI_USER", "USERS_DB", "USER_DATA_ROOT", "LOCAL_LIBRARY_DIR", "BOOKS_DIR",
             "CHORDS_PATH", "CHORDS_REVIEWED_ONLY", "CHORD_DAILY_LIMIT",
             "CHORD_EDITS_PATH", "CHORD_ADMIN_EMAILS",
             "LOCAL_LIBRARY", "ANTHROPIC_API_KEY", "INDEX_SYNC", "STRIPE_SECRET_KEY",
             "SESSION_SECRET", "ACCESS_TOKEN", "SIGNUP_CODE", "REQUIRE_AUTH"]

CHART = {
    "title": "Autumn Leaves", "key": "Em", "form": "AABC", "confidence": "high",
    "sections": [{"name": "A", "bars": ["Am7", "D7", "Gmaj7", "Cmaj7",
                                        "F#m7b5", "B7", "Em", "Em"]}],
    "model": "claude-haiku-5-5", "generated": "2026-11-05", "review": "unreviewed",
}


def make_pdf(path: Path, bookmarks: int = 12) -> Path:
    """Write a small PDF with one bookmarked page per song."""
    doc = pymupdf.open()
    for _ in range(bookmarks):
        doc.new_page()
    doc.set_toc([[1, f"Test Tune {i}", i] for i in range(1, bookmarks + 1)])
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def start_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable]:
    """Import the server fresh with the given settings and return it."""
    def start(**env: str):
        for name in ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        books = tmp_path / "master_books"
        books.mkdir(exist_ok=True)
        chords_path = tmp_path / "chords.json"
        chords_path.write_text(
            json.dumps({"songs": {"autumn leaves": CHART}}), encoding="utf-8")
        monkeypatch.setenv("BOOKS_DIR", str(books))
        monkeypatch.setenv("LOCAL_LIBRARY_DIR", str(tmp_path / "local_library"))
        monkeypatch.setenv("CHORDS_PATH", str(chords_path))
        monkeypatch.setenv("CHORD_EDITS_PATH", str(tmp_path / "chord_edits.json"))
        monkeypatch.setenv("USERS_DB", str(tmp_path / "users.db"))
        monkeypatch.setenv("USER_DATA_ROOT", str(tmp_path / "users"))
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        monkeypatch.setenv("ACCESS_TOKEN", "test-token")
        # A placeholder, so .env is not read for a real key and no test can
        # reach the API. Bookmarked test PDFs are indexed without any AI.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        for name in ("server", "library", "index_book", "chords", "multiuser"):
            sys.modules.pop(name, None)
        module = importlib.import_module("server")
        module.app.config["TESTING"] = True
        return module

    yield start
    for name in ("server", "library", "index_book", "chords", "multiuser"):
        sys.modules.pop(name, None)
