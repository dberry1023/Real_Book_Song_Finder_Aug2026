"""Modal deployment of the Real Book Song Finder (private, password-gated).

One-time setup:
    modal secret create realbook-auth ACCESS_TOKEN=<password> SESSION_SECRET=<random>
    modal volume create real-books
    modal volume put real-books <each of the 7 PDFs> /

Deploy:
    modal deploy modal_app.py
"""
import modal

app = modal.App("real-book-finder")
vol = modal.Volume.from_name("real-books", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("flask>=3.0.0", "pymupdf>=1.24.0")
    .env({"BOOKS_DIR": "/books", "REQUIRE_AUTH": "1"})
    .add_local_python_source("server")
    .add_local_dir("static", remote_path="/root/static")
    .add_local_file("data/songs.json", remote_path="/root/data/songs.json")
    .add_local_file("data/metadata.json", remote_path="/root/data/metadata.json")
)


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
