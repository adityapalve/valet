"""What a run produced, and how to label it. Stdlib only.

- scan_outputs: files the run created or changed under its working directory (CSV/TSV get a
  table preview, images are shown, other data files are listed). Frames written through
  pandas/polars are captured richer by the hook; this catches everything else.
- derive_title: a readable name for a run, from --title, a leading comment or docstring.
- tidy_snippet: turns a `a; b; c` one-liner into one statement per line, only when the
  result is provably the same program (identical AST).
"""

from __future__ import annotations

import ast
import csv
import os
import time
from itertools import islice
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", ".valet", "node_modules", ".venv", "venv", "env", "__pycache__",
             "site-packages", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
             ".ipynb_checkpoints"}
TABULAR = {".csv", ".tsv"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
HTML = {".html", ".htm"}
OTHER_DATA = {".parquet", ".xlsx", ".xls", ".json", ".jsonl", ".ndjson", ".feather", ".arrow", ".ipc",
              ".pkl", ".pickle", ".h5", ".hdf5", ".db", ".sqlite", ".duckdb", ".pdf", ".svg"}
PREVIEW_ROWS, PREVIEW_COLS = 200, 60
EXACT_COUNT_LIMIT = 50_000_000      # bytes; bigger CSVs get an estimated row count
MAX_OUTPUTS = 20


def scan_outputs(root: str | Path, since: float, skip: set[str], budget_s: float = 1.5) -> list[dict]:
    """Data files and images under `root` modified at or after `since` (excluding `skip` paths)."""
    found: list[dict] = []
    deadline = time.monotonic() + budget_s
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if time.monotonic() > deadline:
            break
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in TABULAR and ext not in IMAGES and ext not in HTML and ext not in OTHER_DATA:
                continue
            path = os.path.abspath(os.path.join(dirpath, name))
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < since - 0.1 or path in skip or st.st_size == 0:
                continue
            kind = "image" if ext in IMAGES else "html" if ext in HTML else "file"
            art = {"type": kind, "path": path, "size": st.st_size}
            if ext in TABULAR:
                try:
                    art.update(preview_csv(Path(path)))
                    art["type"] = "file"
                    art["lib"] = "csv"
                except Exception as e:  # noqa: BLE001 - a weird file must not break the run
                    art["error"] = f"{type(e).__name__}: {e}"
            found.append(art)
            if len(found) >= MAX_OUTPUTS:
                return found
    return found


def preview_csv(path: Path) -> dict:
    """Header, first rows, row count, simple dtypes and empty-cell counts of a CSV/TSV."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(65536)
    text = head.decode("utf-8", errors="replace")
    if path.suffix.lower() == ".tsv":
        dialect: type[csv.Dialect] | csv.Dialect = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(text.split("\n", 20)[0] if text else ",", delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh, dialect)
        header = next(reader, [])
        ncols = len(header)
        cols = header[:PREVIEW_COLS]
        rows = [[_cell(v) for v in r[:PREVIEW_COLS]] for r in islice(reader, PREVIEW_ROWS)]
        n = len(rows)
        empties = [sum(1 for r in rows if i >= len(r) or r[i] == "") for i in range(len(cols))]
        estimated = False
        if n == PREVIEW_ROWS:
            if size <= EXACT_COUNT_LIMIT:
                for r in reader:
                    n += 1
                    for i in range(min(len(cols), len(r))):
                        if r[i] == "":
                            empties[i] += 1
            else:  # too big to count quickly: estimate from the average line length
                estimated = True
                n = int(size / (len(head) / max(1, head.count(b"\n")))) - 1
    return {
        "shape": [n, ncols], "columns": cols, "dtypes": [_infer(rows, i) for i in range(len(cols))],
        "nulls": None if estimated else empties, "rows": rows, "rows_estimated": estimated,
        "more_columns": max(0, ncols - PREVIEW_COLS),
    }


def _cell(v: str) -> str:
    return v if len(v) <= 40 else v[:39] + "…"


def _infer(rows: list[list[str]], i: int) -> str:
    vals = [r[i] for r in rows if i < len(r) and r[i] != ""]
    if not vals:
        return "empty"
    for typ, conv in (("int", int), ("float", float)):
        try:
            for v in vals:
                conv(v.replace(",", "") if typ == "float" else v)
            return typ
        except ValueError:
            continue
    if all(len(v) >= 8 and v[:4].isdigit() and v[4] in "-/" for v in vals):
        return "date"
    if {v.lower() for v in vals} <= {"true", "false"}:
        return "bool"
    return "str"


# ---- titles and snippets ------------------------------------------------------------------

def derive_title(script: Path | None, is_snippet: bool) -> str | None:
    """A readable name for a run: the script's leading comment or docstring; for snippets,
    otherwise their first meaningful statement."""
    if script is None:
        return None
    try:
        text = script.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#!") or "coding" in s[:20] and s.startswith("#"):
            continue
        if s.startswith("# ///"):
            continue
        if s.startswith("#"):
            body = s.lstrip("#").strip()
            if body and not body.startswith(("dependencies", "requires-python", "]")) and not body.startswith('"'):
                return body[:100]
            continue
        break
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    doc = ast.get_docstring(tree)
    if doc:
        return doc.strip().splitlines()[0][:100]
    if not is_snippet:
        return None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        src = ast.unparse(node)
        if src.startswith(("sys.path", "print(")):
            continue
        return src[:100]
    return None


def tidy_snippet(code: str) -> str:
    """`import x; y = f(); print(y)` -> one statement per line, if (and only if) that parses to
    exactly the same program. Anything else is returned unchanged."""
    if "\n" in code.strip() or ";" not in code:
        return code
    try:
        tree = ast.parse(code)
        tidy = ast.unparse(tree) + "\n"
        if ast.dump(ast.parse(tidy)) == ast.dump(tree):
            return tidy
    except (SyntaxError, ValueError, RecursionError):
        pass
    return code


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


__all__ = ["scan_outputs", "preview_csv", "derive_title", "tidy_snippet", "human_size"]


# ---- errors ---------------------------------------------------------------------------------

import re as _re

_FRAME_RE = _re.compile(r'^  File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?$')
_LIB_MARKERS = ("site-packages", "dist-packages", "<frozen", f"{os.sep}lib{os.sep}python", "\\Lib\\")
HINTS = {
    "ModuleNotFoundError": "The package isn't installed in the Python this ran with. For a scratch "
                           "script, declare it in a `# /// script` dependencies header.",
    "FileNotFoundError": "Paths are relative to the folder the run started in (shown above).",
    "ColumnNotFoundError": "Check the column name against the frame's columns (see the outputs / data trail).",
    "KeyError": "Often a missing column or dictionary key; check the exact name and case.",
    "SchemaError": "The data types don't match what the operation expects; check dtypes.",
    "MemoryError": "The data didn't fit in memory; filter or select columns earlier, or use a lazy query.",
}


def parse_error(output: str) -> dict | None:
    """The last Python traceback in a run's output: type, message, frames, and the user's frame."""
    lines = output.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("Traceback (most recent call last):")]
    if not starts:
        return None
    i = starts[-1] + 1
    frames = []
    while i < len(lines):
        m = _FRAME_RE.match(lines[i])
        if m:
            frames.append({"file": m["file"], "line": int(m["line"]), "func": m["func"] or "", "code": ""})
            i += 1
            # the source line, then optional ^^^^ markers
            while i < len(lines) and lines[i].startswith("    "):
                if not frames[-1]["code"] and lines[i].strip().strip("^~ "):
                    frames[-1]["code"] = lines[i].strip()
                i += 1
            continue
        if lines[i].startswith("  ") or not lines[i].strip():
            i += 1
            continue
        break
    rest = lines[i:]
    if not rest:
        return None
    head = rest[0]
    etype, _, msg = head.partition(": ") if ": " in head else (head.rstrip(":"), "", "")
    # message continues on following lines (polars, pandas often do this) until a blank line
    more = []
    for ln in rest[1:]:
        if not ln.strip() or ln.startswith("[valet]"):
            break
        more.append(ln)
    message = "\n".join([msg] + more).strip()
    short_type = etype.rsplit(".", 1)[-1].strip()
    user = None
    for idx in range(len(frames) - 1, -1, -1):
        f = frames[idx]["file"]
        # valet's own recording wrapper (valet/hook/...) is never the cause
        if "/valet/hook/" in f.replace("\\", "/"):
            frames[idx]["valet"] = True
            continue
        if not any(mk in f for mk in _LIB_MARKERS) and not f.startswith("<"):
            user = idx
            break
    return {"type": short_type, "qualified_type": etype.strip(), "message": message[:2000],
            "frames": frames[-30:], "user_frame": None if user is None else user - max(0, len(frames) - 30),
            "hint": HINTS.get(short_type),
            "traceback": "\n".join(lines[starts[-1]:i + 1 + len(more)])[-8000:]}
