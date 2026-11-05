"""Modal deployment of the Real Book Song Finder (private, password-gated).

One-time setup:
    modal secret create realbook-auth ACCESS_TOKEN=<password> SESSION_SECRET=<random>
    modal volume create real-books
    modal volume put real-books <each of the 7 PDFs> /

Deploy:
    modal deploy modal_app.py
"""
from pathlib import Path

import modal

app = modal.App("real-book-finder")
vol = modal.Volume.from_name("real-books", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("flask>=3.0.0", "pymupdf>=1.24.0")
    # LOCAL_LIBRARY=0: the container disk does not persist, so no LOAD or DELETE.
    .env({"BOOKS_DIR": "/books", "REQUIRE_AUTH": "1", "LOCAL_LIBRARY": "0"})
    .add_local_python_source("server", "library", "index_book", "chords")
    .add_local_dir("static", remote_path="/root/static")
    .add_local_file("data/songs.json", remote_path="/root/data/songs.json")
    .add_local_file("data/metadata.json", remote_path="/root/data/metadata.json")
)
if Path("data/chords.json").exists():
    image = image.add_local_file("data/chords.json",
                                 remote_path="/root/data/chords.json")


@app.function(
    image=image,
    volumes={"/books": vol},
    secrets=[modal.Secret.from_name("realbook-auth")],
    memory=1024,
    timeout=120,
)
@modal.concurrent(max_inputs=20)  # the render lock serializes PyMuPDF anyway
@modal.wsgi_app()
def web():
    from server import app as flask_app

    return flask_app
