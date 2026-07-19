"""User accounts for the multi-user cloud mode (MULTI_USER=1).

SQLite store with werkzeug password hashing. The local single-user app
never imports behavior from here beyond what server.py gates behind
MULTI_USER, so Start_Song_Finder.bat is unaffected.
"""
import datetime
import re
import sqlite3
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   email TEXT UNIQUE NOT NULL,
                   pw_hash TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )"""
        )
        # additive migrations for databases created by earlier versions
        cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        for col, ddl in [("plan", "TEXT NOT NULL DEFAULT 'free'"),
                         ("stripe_customer_id", "TEXT"),
                         ("stripe_subscription_id", "TEXT")]:
            if col not in cols:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")


def create_user(db_path: Path, email: str, password: str) -> tuple[int | None, str | None]:
    """Create a user. Returns (user_id, None) or (None, error_message)."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return None, "Please enter a valid email address."
    if len(password or "") < MIN_PASSWORD_LEN:
        return None, f"Password must be at least {MIN_PASSWORD_LEN} characters."
    try:
        with _connect(db_path) as con:
            cur = con.execute(
                "INSERT INTO users (email, pw_hash, created_at) VALUES (?, ?, ?)",
                (
                    email,
                    generate_password_hash(password),
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )
            return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "That email is already registered — try signing in."


def verify_user(db_path: Path, email: str, password: str) -> tuple[int | None, str | None]:
    """Check credentials. Returns (user_id, email) on success, (None, None) otherwise."""
    email = (email or "").strip().lower()
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT id, email, pw_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    if row and check_password_hash(row["pw_hash"], password or ""):
        return row["id"], row["email"]
    return None, None


def get_email(db_path: Path, user_id: int) -> str | None:
    with _connect(db_path) as con:
        row = con.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["email"] if row else None


# ---------------------------------------------------------------- billing --
def get_plan(db_path: Path, user_id: int) -> str:
    with _connect(db_path) as con:
        row = con.execute("SELECT plan FROM users WHERE id = ?",
                          (user_id,)).fetchone()
    return row["plan"] if row else "free"


def set_plan(db_path: Path, user_id: int, plan: str) -> None:
    with _connect(db_path) as con:
        con.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))


def set_stripe_ids(db_path: Path, user_id: int,
                   customer_id: str | None, subscription_id: str | None) -> None:
    with _connect(db_path) as con:
        con.execute(
            "UPDATE users SET stripe_customer_id = ?, "
            "stripe_subscription_id = ? WHERE id = ?",
            (customer_id, subscription_id, user_id))


def get_stripe_customer(db_path: Path, user_id: int) -> str | None:
    with _connect(db_path) as con:
        row = con.execute("SELECT stripe_customer_id FROM users WHERE id = ?",
                          (user_id,)).fetchone()
    return row["stripe_customer_id"] if row else None


def find_by_stripe_customer(db_path: Path, customer_id: str) -> int | None:
    if not customer_id:
        return None
    with _connect(db_path) as con:
        row = con.execute("SELECT id FROM users WHERE stripe_customer_id = ?",
                          (customer_id,)).fetchone()
    return row["id"] if row else None
