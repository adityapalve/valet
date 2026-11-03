"""SQLite-backed record of every run, script snapshot and steering note."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
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
DEFAULT_TIMEOUT = 10.0      # seconds to wait for another process's write lock


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
    """All access goes through short-lived connections, and every query is read to the end
    and closed before the connection closes. Nothing is held between calls, so a long-lived
    process (the viewer) can never keep the database locked, on any Python implementation.
    (PyPy, unlike CPython, does not finalize a statement as soon as its cursor is dropped.)
    """

    def __init__(self, home: Path | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.home = home or find_home()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "scratch").mkdir(exist_ok=True)
        self.path = self.home / "valet.db"
        self.timeout = timeout
        with self._connect() as con:
            # WAL lets readers and the writer work at the same time. It is stored in the
            # file, so this only changes something the first time. Some filesystems (e.g.
            # network drives) refuse it; everything still works, with more lock waiting.
            self.journal_mode = _q(con, "PRAGMA journal_mode=WAL")[0][0]
            con.executescript(SCHEMA)
            cols = {r[1] for r in _q(con, "PRAGMA table_info(runs)")}
            if "trail" not in cols:  # added in 0.2: data trail of dataframe operations
                with _tx(con):
                    _x(con, "ALTER TABLE runs ADD COLUMN trail TEXT")
                    _x(con, "ALTER TABLE runs ADD COLUMN trail_warnings INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self):
        # isolation_level=None: no implicit transactions; writes use explicit BEGIN IMMEDIATE.
        con = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None,
                              check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _write(self):
        with self._connect() as con, _tx(con):
            yield con

    # ---- runs -------------------------------------------------------------
    def start_run(self, argv: list[str], cwd: str) -> int:
        with self._write() as con:
            return _x(con, "INSERT INTO runs (started, cwd, argv) VALUES (?, ?, ?)",
                      (time.time(), cwd, json.dumps(argv)))

    def snapshot(self, run_id: int, path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT:
                return False
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        h = hashlib.sha256(content.encode()).hexdigest()
        with self._write() as con:
            _x(con, "INSERT OR IGNORE INTO blobs VALUES (?, ?)", (h, content))
            _x(con, "INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (run_id, str(path.resolve()), h))
        return True

    def update_output(self, run_id: int, output: str) -> None:
        with self._write() as con:
            _x(con, "UPDATE runs SET output = ?, truncated = ? WHERE id = ?",
               (output[-MAX_OUTPUT:], int(len(output) > MAX_OUTPUT), run_id))

    def trail_path(self, run_id: int | str) -> Path:
        return trail_file(self.home, run_id)

    def ingest_trail(self, run_id: int) -> list[dict]:
        """Copy the run's trail file (written by the hook) into the database."""
        steps = read_trail_file(self.trail_path(run_id))
        if steps:
            n_warn = sum(len(st.get("warnings") or []) for st in steps)
            with self._write() as con:
                _x(con, "UPDATE runs SET trail = ?, trail_warnings = ? WHERE id = ?",
                   (json.dumps(steps), n_warn, run_id))
        return steps

    def finish_run(self, run_id: int, exit_code: int, output: str) -> None:
        with self._write() as con:
            started = _q(con, "SELECT started FROM runs WHERE id = ?", (run_id,))[0][0]
            _x(con, "UPDATE runs SET output = ?, truncated = ?, exit_code = ?, duration = ? WHERE id = ?",
               (output[-MAX_OUTPUT:], int(len(output) > MAX_OUTPUT), exit_code,
                time.time() - started, run_id))

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._connect() as con:
            rows = _q(con, "SELECT id, started, duration, cwd, argv, exit_code, trail_warnings "
                           "FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r, argv=json.loads(r["argv"])) for r in rows]

    def get_run(self, run_id: int) -> dict | None:
        with self._connect() as con:
            rows = _q(con, "SELECT * FROM runs WHERE id = ?", (run_id,))
            if not rows:
                return None
            r = rows[0]
            run = dict(r, argv=json.loads(r["argv"]))
            run["trail"] = json.loads(r["trail"]) if r["trail"] else []
            run["files"] = []
            for f in _q(con, "SELECT f.path, b.content FROM files f JOIN blobs b ON b.hash = f.hash "
                             "WHERE f.run_id = ? ORDER BY f.path", (run_id,)):
                prev_rows = _q(con, "SELECT f.run_id, b.content FROM files f JOIN blobs b ON b.hash = f.hash "
                                    "WHERE f.path = ? AND f.run_id < ? ORDER BY f.run_id DESC LIMIT 1",
                               (f["path"], run_id))
                prev = prev_rows[0] if prev_rows else None
                diff = None
                if prev is not None:
                    diff = "".join(difflib.unified_diff(
                        prev["content"].splitlines(keepends=True),
                        f["content"].splitlines(keepends=True),
                        fromfile=f"run {prev['run_id']}", tofile=f"run {run_id}"))
                run["files"].append({"path": f["path"], "content": f["content"],
                                     "prev_run": prev["run_id"] if prev else None, "diff": diff})
            run["notes"] = [dict(n) for n in _q(
                con, "SELECT id, created, text FROM notes WHERE delivered_run = ? ORDER BY id", (run_id,))]
        return run

    # ---- steering notes ---------------------------------------------------
    def add_note(self, text: str) -> int:
        with self._write() as con:
            return _x(con, "INSERT INTO notes (created, text) VALUES (?, ?)", (time.time(), text))

    def pending_notes(self) -> list[dict]:
        with self._connect() as con:
            return [dict(n) for n in _q(
                con, "SELECT id, created, text FROM notes WHERE delivered_run IS NULL ORDER BY id")]

    def take_notes(self, run_id: int) -> list[str]:
        """Claim all pending notes for this run and return their text (atomically)."""
        with self._write() as con:
            notes = _q(con, "SELECT id, text FROM notes WHERE delivered_run IS NULL ORDER BY id")
            for n in notes:
                _x(con, "UPDATE notes SET delivered_run = ? WHERE id = ?", (run_id, n["id"]))
        return [n["text"] for n in notes]

    def delete_note(self, note_id: int) -> None:
        with self._write() as con:
            _x(con, "DELETE FROM notes WHERE id = ? AND delivered_run IS NULL", (note_id,))

    # ---- health -----------------------------------------------------------
    def can_write(self, timeout: float = 2.0) -> bool:
        """True if a write lock can be taken within `timeout` seconds."""
        con = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE").close()
            con.execute("ROLLBACK").close()
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            con.close()


def trail_file(home: Path, key: int | str) -> Path:
    """Where the hook writes the data trail for a run (no database involved)."""
    d = home / "trails"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.jsonl"


def read_trail_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    steps = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            steps.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a line still being written
    return steps


def _q(con, sql, params=()):
    """Run a query, read every row, and close the cursor (so no statement stays active)."""
    cur = con.execute(sql, params)
    try:
        return cur.fetchall()
    finally:
        cur.close()


def _x(con, sql, params=()):
    """Run a write statement, close the cursor, return lastrowid."""
    cur = con.execute(sql, params)
    try:
        return cur.lastrowid
    finally:
        cur.close()


@contextmanager
def _tx(con):
    """Explicit write transaction: BEGIN IMMEDIATE takes the write lock up front."""
    _x(con, "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            _x(con, "ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    _x(con, "COMMIT")
