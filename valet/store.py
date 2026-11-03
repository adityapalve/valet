"""SQLite-backed record of every run, script snapshot and steering note."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started REAL NOT NULL,
    duration REAL,
    cwd TEXT NOT NULL,
    argv TEXT NOT NULL,          -- JSON list
    exit_code INTEGER,           -- NULL while running
    output TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT PRIMARY KEY,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    path TEXT NOT NULL,          -- absolute path
    hash TEXT NOT NULL REFERENCES blobs(hash),
    PRIMARY KEY (run_id, path)
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    created REAL NOT NULL,
    text TEXT NOT NULL,
    delivered_run INTEGER REFERENCES runs(id)
);
"""

MAX_OUTPUT = 2_000_000      # chars of output kept per run
MAX_SNAPSHOT = 1_000_000    # bytes; larger files are not snapshotted


def find_home(start: Path | None = None) -> Path:
    """Locate the .valet dir: $VALET_HOME, else nearest ancestor with .valet, else ./.valet."""
    if env := os.environ.get("VALET_HOME"):
        return Path(env)
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / ".valet").is_dir():
            return d / ".valet"
    return cur / ".valet"


class Store:
    def __init__(self, home: Path | None = None):
        self.home = home or find_home()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "scratch").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.home / "valet.db", timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "trail" not in cols:  # added in 0.2: data trail of dataframe operations
            self.db.execute("ALTER TABLE runs ADD COLUMN trail TEXT")
            self.db.execute("ALTER TABLE runs ADD COLUMN trail_warnings INTEGER NOT NULL DEFAULT 0")
            self.db.commit()

    # ---- runs -------------------------------------------------------------
    def start_run(self, argv: list[str], cwd: str) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (started, cwd, argv) VALUES (?, ?, ?)",
            (time.time(), cwd, json.dumps(argv)),
        )
        self.db.commit()
        return cur.lastrowid

    def snapshot(self, run_id: int, path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT:
                return False
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        h = hashlib.sha256(content.encode()).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (h, content))
        self.db.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (run_id, str(path.resolve()), h)
        )
        self.db.commit()
        return True

    def update_output(self, run_id: int, output: str) -> None:
        truncated = len(output) > MAX_OUTPUT
        self.db.execute(
            "UPDATE runs SET output = ?, truncated = ? WHERE id = ?",
            (output[-MAX_OUTPUT:], int(truncated), run_id),
        )
        self.db.commit()

    def trail_path(self, run_id: int) -> Path:
        d = self.home / "trails"
        d.mkdir(exist_ok=True)
        return d / f"{run_id}.jsonl"

    def ingest_trail(self, run_id: int) -> list[dict]:
        """Copy the run's trail file (written by the hook) into the database."""
        path = self.trail_path(run_id)
        if not path.exists():
            return []
        steps = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a line still being written
        n_warn = sum(len(st.get("warnings") or []) for st in steps)
        self.db.execute(
            "UPDATE runs SET trail = ?, trail_warnings = ? WHERE id = ?",
            (json.dumps(steps), n_warn, run_id),
        )
        self.db.commit()
        return steps

    def finish_run(self, run_id: int, exit_code: int, output: str) -> None:
        started = self.db.execute("SELECT started FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        self.update_output(run_id, output)
        self.db.execute(
            "UPDATE runs SET exit_code = ?, duration = ? WHERE id = ?",
            (exit_code, time.time() - started, run_id),
        )
        self.db.commit()

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, started, duration, cwd, argv, exit_code, trail_warnings FROM runs "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r, argv=json.loads(r["argv"])) for r in rows]

    def get_run(self, run_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            return None
        run = dict(r, argv=json.loads(r["argv"]))
        run["trail"] = json.loads(r["trail"]) if r["trail"] else []
        run["files"] = []
        for f in self.db.execute(
            "SELECT f.path, f.hash, b.content FROM files f JOIN blobs b ON b.hash = f.hash "
            "WHERE f.run_id = ? ORDER BY f.path",
            (run_id,),
        ):
            prev = self.db.execute(
                "SELECT f.run_id, b.content FROM files f JOIN blobs b ON b.hash = f.hash "
                "WHERE f.path = ? AND f.run_id < ? ORDER BY f.run_id DESC LIMIT 1",
                (f["path"], run_id),
            ).fetchone()
            diff = None
            if prev is not None:
                diff = "".join(
                    difflib.unified_diff(
                        prev["content"].splitlines(keepends=True),
                        f["content"].splitlines(keepends=True),
                        fromfile=f"run {prev['run_id']}",
                        tofile=f"run {run_id}",
                    )
                )
            run["files"].append(
                {
                    "path": f["path"],
                    "content": f["content"],
                    "prev_run": prev["run_id"] if prev else None,
                    "diff": diff,
                }
            )
        run["notes"] = [
            dict(n)
            for n in self.db.execute(
                "SELECT id, created, text FROM notes WHERE delivered_run = ? ORDER BY id", (run_id,)
            )
        ]
        return run

    # ---- steering notes ---------------------------------------------------
    def add_note(self, text: str) -> int:
        cur = self.db.execute("INSERT INTO notes (created, text) VALUES (?, ?)", (time.time(), text))
        self.db.commit()
        return cur.lastrowid

    def pending_notes(self) -> list[dict]:
        return [
            dict(n)
            for n in self.db.execute(
                "SELECT id, created, text FROM notes WHERE delivered_run IS NULL ORDER BY id"
            )
        ]

    def take_notes(self, run_id: int) -> list[str]:
        """Claim all pending notes for this run and return their text."""
        notes = self.pending_notes()
        if notes:
            self.db.executemany(
                "UPDATE notes SET delivered_run = ? WHERE id = ? AND delivered_run IS NULL",
                [(run_id, n["id"]) for n in notes],
            )
            self.db.commit()
        return [n["text"] for n in notes]

    def delete_note(self, note_id: int) -> None:
        self.db.execute("DELETE FROM notes WHERE id = ? AND delivered_run IS NULL", (note_id,))
        self.db.commit()
