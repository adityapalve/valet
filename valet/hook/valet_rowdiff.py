"""Row-level diff of an output table against its previous version. Stdlib only.

Both the in-process hook (frames written through pandas/polars) and the CLI's file scan (CSV
files written any other way) use this. Each output path keeps one fingerprint in
.valet/fingerprints/: its columns, key and rows (as strings), so the next run can say which rows
were added, removed or changed, even though the file itself has been overwritten.
"""

import hashlib
import json
import os
from collections import Counter

MAX_CELLS = int(os.environ.get("VALET_DIFF_MAX_CELLS", "3000000"))   # bigger tables aren't diffed
SAMPLES = 50                                                          # example rows of each kind
CELL = 200                                                            # chars kept per cell
NULLS = ("", "None", "null", "NaN", "nan", "NaT")


def cell(v):
    s = "" if v is None else str(v)
    return s if len(s) <= CELL else s[:CELL]


def fingerprint_path(valet_dir, path):
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()
    return os.path.join(valet_dir, "fingerprints", digest + ".json")


def key_override(valet_dir, path):
    """Key columns set with valet_view.key(path, [...]), if any."""
    try:
        with open(os.path.join(valet_dir, "keys.json"), encoding="utf-8") as fh:
            keys = json.load(fh)
        return keys.get(os.path.normcase(os.path.abspath(path)))
    except (OSError, ValueError):
        return None


def set_key(valet_dir, path, columns):
    f = os.path.join(valet_dir, "keys.json")
    try:
        with open(f, encoding="utf-8") as fh:
            keys = json.load(fh)
    except (OSError, ValueError):
        keys = {}
    keys[os.path.normcase(os.path.abspath(path))] = list(columns)
    os.makedirs(valet_dir, exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, indent=1)


def _unique(columns, rows, key):
    if not all(c in columns for c in key):
        return False
    idx = [columns.index(c) for c in key]
    seen = set()
    for r in rows:
        k = tuple(r[i] for i in idx)
        if k in seen or any(v in NULLS for v in k):
            return False
        seen.add(k)
    return True


def choose_key(columns, rows, override=None, old=None):
    """Columns that identify a row: the override if set; else last run's key if it still works;
    else the shortest run of leading columns (up to 4) that is present and unique in this version
    *and* the previous one (a key unique in only one would pair up the wrong rows)."""
    if override and all(c in columns for c in override):
        return list(override)
    both = [(columns, rows)] + ([(old["columns"], old["rows"])] if old else [])
    if old and old.get("key") and all(_unique(c, r, old["key"]) for c, r in both):
        return list(old["key"])
    for k in range(1, min(4, len(columns)) + 1):
        if rows and all(_unique(c, r, columns[:k]) for c, r in both):
            return list(columns[:k])
    return None


def diff_and_save(valet_dir, path, columns, rows, run_id, source):
    """Compare `rows` (lists of strings) with the previous fingerprint of `path` from the same
    source ("frame" or "csv"), save the new fingerprint, and return the diff (or None)."""
    columns = [str(c) for c in columns]
    if not valet_dir or len(rows) * max(1, len(columns)) > MAX_CELLS:
        return {"skipped": "too big to diff"} if valet_dir else None
    fp = fingerprint_path(valet_dir, path)
    old = None
    try:
        with open(fp, encoding="utf-8") as fh:
            old = json.load(fh)
        if old.get("source") != source:
            old = None   # e.g. pandas wrote it last time, a CSV scan this time: formats differ
    except (OSError, ValueError):
        pass
    key = choose_key(columns, rows, key_override(valet_dir, path), old)
    result = compare(old, columns, rows, key) if old else None
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"run_id": run_id, "source": source, "path": os.path.abspath(path),
                   "columns": columns, "key": key, "rows": rows}, fh)
    os.replace(tmp, fp)
    return result


def compare(old, columns, rows, key):
    ocols = old["columns"]
    added_cols = [c for c in columns if c not in ocols]
    removed_cols = [c for c in ocols if c not in columns]
    out = {"vs_run": old.get("run_id"), "key": key, "cols_added": added_cols, "cols_removed": removed_cols}
    if key and all(c in ocols for c in key):
        ni = [columns.index(c) for c in key]
        oi = [ocols.index(c) for c in key]
        new_map = {tuple(r[i] for i in ni): r for r in rows}
        old_map = {tuple(r[i] for i in oi): r for r in old["rows"]}
        common = [(c, ocols.index(c), columns.index(c)) for c in columns if c in ocols and c not in key]
        added = [k for k in new_map if k not in old_map]
        removed = [k for k in old_map if k not in new_map]
        changed, per_col = [], Counter()
        for k, r in new_map.items():
            o = old_map.get(k)
            if o is None:
                continue
            cells = [[c, o[j], r[i]] for c, j, i in common if o[j] != r[i]]
            if cells:
                changed.append((k, cells))
                per_col.update(c for c, _, _ in cells)
        out.update({
            "added": len(added), "removed": len(removed), "changed": len(changed),
            "unchanged": len(new_map) - len(added) - len(changed),
            "col_changes": dict(per_col.most_common(30)),
            "samples": {
                "added": [{"key": list(k), "row": new_map[k]} for k in added[:SAMPLES]],
                "removed": [{"key": list(k), "row": old_map[k]} for k in removed[:SAMPLES]],
                "changed": [{"key": list(k), "cells": cells} for k, cells in changed[:SAMPLES]],
            },
            "columns": columns, "old_columns": ocols,
        })
    else:  # no key: compare the rows as a multiset (a changed row = one removed + one added)
        if added_cols or removed_cols:
            out.update({"note": "columns changed and there is no key to match rows by"})
            return out
        new_c, old_c = Counter(map(tuple, rows)), Counter(map(tuple, old["rows"]))
        added, removed = new_c - old_c, old_c - new_c
        out.update({
            "added": sum(added.values()), "removed": sum(removed.values()), "changed": None,
            "unchanged": sum((new_c & old_c).values()),
            "samples": {"added": [{"row": list(r)} for r in list(added)[:SAMPLES]],
                        "removed": [{"row": list(r)} for r in list(removed)[:SAMPLES]], "changed": []},
            "columns": columns, "old_columns": ocols,
        })
    return out


def summary(d):
    """One ASCII line for the agent, e.g. '+12 -3 ~41 rows vs run #7'."""
    if not d or d.get("vs_run") is None or "added" not in d:
        return ""
    bits = [f"+{d['added']:,}", f"-{d['removed']:,}"]
    if d.get("changed") is not None:
        bits.append(f"~{d['changed']:,}")
    if not d["added"] and not d["removed"] and not d.get("changed"):
        return f"no row changes vs run #{d['vs_run']}"
    return " ".join(bits) + f" rows vs run #{d['vs_run']}"
