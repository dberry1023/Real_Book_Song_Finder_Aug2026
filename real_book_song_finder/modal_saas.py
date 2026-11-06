"""Modal deployment of the multi-user Song Finder SaaS (beta).

Separate from the private single-password app (modal_app.py) — this one
runs with MULTI_USER=1: individual accounts, per-user book libraries
uploaded via Load My Books, and automatic indexing. No master book PDFs
are shipped: users upload their own copies (known books are recognized
by fingerprint and indexed from the bundled master index).

One-time setup:
    modal secret create songfinder-saas SESSION_SECRET=<random> \
        SIGNUP_CODE=<beta invite code> ANTHROPIC_API_KEY=<key> \
        CHORD_ADMIN_EMAILS=<your account email>
    # optional later, to turn on payments:
    #   STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET /
    #   STRIPE_PRICE_ANNUAL / STRIPE_PRICE_MONTHLY

Deploy:
    modal deploy modal_saas.py
"""
from pathlib import Path

import modal

app = modal.App("song-finder-saas")
vol = modal.Volume.from_name("songfinder-users", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("flask>=3.0.0", "pymupdf>=1.24.0", "anthropic>=0.40.0",
                 "stripe>=7.0.0")
    .env({
        "MULTI_USER": "1",
        "INDEX_SYNC": "1",          # threads don't survive scale-down
        "USERS_DB": "/userdata/users.db",
        "USER_DATA_ROOT": "/userdata/users",
        "CHORD_EDITS_PATH": "/userdata/chord_edits.json",
    })
    .add_local_python_source("server", "multiuser", "library", "index_book",
                             "chords")
    .add_local_dir("static", remote_path="/root/static")
    .add_local_file("data/songs.json", remote_path="/root/data/songs.json")
    .add_local_file("data/metadata.json", remote_path="/root/data/metadata.json")
)
if Path("data/chords.json").exists():
    image = image.add_local_file("data/chords.json",
                                 remote_path="/root/data/chords.json")


@app.function(
    image=image,
    volumes={"/userdata": vol},
    secrets=[modal.Secret.from_name("songfinder-saas")],
    memory=1024,
    timeout=600,          # unknown-book indexing runs inside the request
    max_containers=1,     # SQLite on the volume: keep a single writer
)
@modal.concurrent(max_inputs=20)
@modal.wsgi_app()
def web():
    from server import app as flask_app

    return flask_app
