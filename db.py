"""Storage for client requests: one row per intake form submission, in a SQLite file.

The web app adds requests and the worker builds them; both go through these functions so
neither has to write SQL. Statuses move like this:

    queued -> building -> ready_for_review -> approved
                       --> failed            =-> rejected
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    company       TEXT NOT NULL,
    website       TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    facts         TEXT NOT NULL DEFAULT '{}',   -- form answers as JSON, such as {"pricing": "..."}
    files         TEXT NOT NULL DEFAULT '[]',   -- paths of uploaded files as JSON
    status        TEXT NOT NULL DEFAULT 'queued',
    progress      TEXT NOT NULL DEFAULT '',     -- latest message from the build
    error         TEXT,
    agent_id      TEXT,
    folder_id     TEXT,
    facts_doc_id  TEXT,
    file_doc_ids  TEXT NOT NULL DEFAULT '[]'
)
"""

JSON_COLUMNS = {"facts", "files", "file_doc_ids"}
# Columns update() may change. Column names cannot be passed as SQL parameters, so they are
# checked against this list instead of being trusted.
UPDATABLE = {"status", "progress", "error", "agent_id", "folder_id", "facts_doc_id", "file_doc_ids", "facts", "files"}


@contextmanager
def connect():

    """Open the database, save the changes if the block succeeds (or undo them if it raises),
    and always close it. Creates the table the first time."""
    
    db = sqlite3.connect(DB_PATH, timeout=30)  # wait up to 30 s if another process is writing
    db.row_factory = sqlite3.Row  # rows behave like dicts: row["company"]
    try:
        db.execute(SCHEMA)
        with db:  # commit on success, roll back on error
            yield db
    finally:
        db.close()


def to_dict(row):
    if row is None:
        return None
    request = dict(row)
    for column in JSON_COLUMNS:
        request[column] = json.loads(request[column])
    return request


def add_request(company, website, contact_email, facts=None, files=None):
    """Save a new request with status 'queued' and return its id."""
    now = time.time()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO requests (created_at, updated_at, company, website, contact_email, facts, files)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, now, company, website, contact_email, json.dumps(facts or {}), json.dumps(files or [])),
        )
        return cursor.lastrowid


def get_request(request_id):
    with connect() as db:
        return to_dict(db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone())


def list_requests():
    """All requests, newest first."""
    with connect() as db:
        return [to_dict(row) for row in db.execute("SELECT * FROM requests ORDER BY id DESC")]


def claim_next():
    """Mark the oldest queued request as 'building' and return it, or None if nothing is waiting.

    BEGIN IMMEDIATE locks the database for writing before reading, so if two workers ever run
    at once they cannot both pick the same request."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM requests WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        db.execute("UPDATE requests SET status = 'building', updated_at = ? WHERE id = ?", (time.time(), row["id"]))
    return get_request(row["id"])


def update(request_id, **fields):
    """Change some columns of one request, for example update(3, status="failed", error="...")."""
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError(f"Cannot update {', '.join(sorted(unknown))}")
    values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in fields.items()}
    values["updated_at"] = time.time()
    assignments = ", ".join(f"{column} = ?" for column in values)
    with connect() as db:
        db.execute(f"UPDATE requests SET {assignments} WHERE id = ?", (*values.values(), request_id))
