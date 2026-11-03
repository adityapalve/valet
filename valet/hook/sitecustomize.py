"""valet data trail: loaded via PYTHONPATH into every Python process valet runs.

When (and only when) the script imports pandas, common DataFrame operations are wrapped
so that each call appends one JSON line to $VALET_TRAIL describing it: the operation,
its key parameters, input and output shapes, columns added or removed, the line of user
code that made the call (plus any `#` comment right above it), and warnings such as a
join that multiplied rows.

Lineage: every frame valet sees gets a node id (tracked by object identity via weakrefs,
never by mutating the frame), so each step links its input nodes to its output node and
the viewer can draw the transformation tree. Each output node carries a small profile:
column dtypes, null counts and the first rows, for a Data Wrangler-style preview.

This file must stay stdlib-only and self-contained: it also runs inside environments
(e.g. `uv run --script`) where valet itself is not installed. Recording never raises
into user code.
"""

import ast
import functools
import importlib.util
import inspect
import json
import linecache
import os
import re
import sys
import threading
import weakref

_TRAIL = os.environ.get("VALET_TRAIL")
_HERE = os.path.dirname(os.path.abspath(__file__))
_state = threading.local()
_lock = threading.Lock()
_PROFILE = os.environ.get("VALET_TRAIL_PROFILE", "1") != "0"
_NULL_CELL_LIMIT = 5_000_000   # skip null counting on frames bigger than this
_PANDAS_DIR = os.sep + "pandas" + os.sep
_registry = {}                 # id(frame) -> (weakref, node id)
_labels = {}                   # node id -> variable name
_counter = [0]
# ops that matter for lineage but are not transformations worth a line of their own
_MINOR = {"sort", "cast", "with_row_index", "rechunk",           # polars
          "reset_index", "set_index", "sort_values", "sort_index", "copy", "astype",
          "rename_axis", "convert_dtypes", "reindex"}


def _emit(rec):
    try:
        with _lock, open(_TRAIL, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _short(v, limit=120):
    try:
        if isinstance(v, (list, tuple)) and len(v) > 12:
            v = list(v[:12]) + ["…"]
        if callable(v):
            s = getattr(v, "__name__", None) or repr(v)
        elif isinstance(v, os.PathLike):
            s = os.fspath(v)
        else:
            s = repr(v)
    except Exception:
        s = "<?>"
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _user_frame():
    """First frame outside pandas/numpy/site-packages/this hook."""
    f = sys._getframe(1)
    while f is not None:
        fn = f.f_code.co_filename
        if not (
            fn.startswith("<")
            or os.path.dirname(os.path.abspath(fn)) == _HERE   # this hook and valet_view
            or os.sep + "site-packages" + os.sep in fn
            or os.sep + "pandas" + os.sep in fn
            or os.sep + "numpy" + os.sep in fn
        ):
            break
        f = f.f_back
    return f


_STMT_STARTS = {}
_COMPOUND = tuple(getattr(ast, n) for n in ("If", "For", "AsyncFor", "While", "With", "AsyncWith", "Try",
                                             "TryStar", "FunctionDef", "AsyncFunctionDef", "ClassDef", "Match")
                  if hasattr(ast, n))


def _stmt_start(fn, ln):
    """First line of the simple statement containing line `ln` (parsed once per file)."""
    starts = _STMT_STARTS.get(fn)
    if starts is None:
        starts = {}
        try:
            src = "".join(linecache.getlines(fn))
            if len(src) < 2_000_000:
                for node in ast.walk(ast.parse(src)):
                    if isinstance(node, ast.stmt) and not isinstance(node, _COMPOUND):
                        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                            starts[line] = node.lineno
        except (SyntaxError, ValueError, RecursionError):
            pass
        _STMT_STARTS[fn] = starts
    return starts.get(ln, ln)


def _call_site(f=None):
    """(file, line, code, comment) of the user code that made the current call."""
    f = f or _user_frame()
    if f is None:
        return None
    fn, ln = f.f_code.co_filename, f.f_lineno
    code = linecache.getline(fn, ln).strip()
    start = _stmt_start(fn, ln)
    # a continuation line like `.agg(...)` says little; the statement's first line has the
    # assignment (`summary = (df.group_by(...)`), so show that, and name the result from it
    stmt = linecache.getline(fn, start).strip() if start != ln else code
    if start != ln and code.startswith((".", ")")):
        code = stmt
    comment = []
    # comments sit above the *statement*, which may start lines earlier (chained calls in parens)
    i = start - 1
    while i > 0 and len(comment) < 3:
        prev = linecache.getline(fn, i).strip()
        if not prev.startswith("#") or prev.startswith("# ///"):
            break
        comment.insert(0, prev.lstrip("#").strip())
        i -= 1
    return {"file": fn, "line": ln, "code": code[:240], "stmt": stmt[:240], "comment": " ".join(comment) or None}


def _shape(obj):
    s = getattr(obj, "shape", None)
    return list(s) if isinstance(s, tuple) else None


def _cols(obj):
    try:
        cols = getattr(obj, "columns", None)
        return [str(c) for c in cols] if cols is not None and len(cols) <= 500 else None
    except Exception:
        return None


# ---- lineage nodes --------------------------------------------------------------------

_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]*)?=(?!=)")
_RECV_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*[.\[]")


def _new_id():
    _counter[0] += 1
    return _counter[0]


def _register(obj):
    nid = _new_id()
    try:
        _registry[id(obj)] = (weakref.ref(obj), nid)
    except TypeError:
        pass
    return nid


def _lookup(obj):
    ent = _registry.get(id(obj))
    return ent[1] if ent is not None and ent[0]() is obj else None


def _cell(v):
    s = str(v)
    return s if len(s) <= 40 else s[:39] + "…"


def _profile(obj):
    """Small Data Wrangler-style summary: dtypes, null counts, first rows (≤40 columns)."""
    if not _PROFILE:
        return None
    if _lib(obj) == "polars":
        try:
            snap = _snapshot(obj, "polars", nrows=5)
            return {"columns": snap["columns"], "dtypes": snap["dtypes"], "more_columns": snap["more_columns"],
                    "nulls": snap.get("nulls"), "sample": snap["rows"]}
        except Exception:
            return None
    prev = getattr(_state, "busy", False)
    _state.busy = True
    try:
        df = obj.to_frame() if getattr(obj, "ndim", 2) == 1 else obj
        sub = df.iloc[:, :40]
        prof = {
            "columns": [str(c) for c in sub.columns],
            "dtypes": [str(t) for t in sub.dtypes],
            "more_columns": max(0, df.shape[1] - 40),
        }
        if df.shape[0] * sub.shape[1] <= _NULL_CELL_LIMIT:
            prof["nulls"] = [int(x) for x in sub.isna().sum().tolist()]
        prof["sample"] = [[_cell(v) for v in row] for row in sub.head(5).itertuples(index=False, name=None)]
        return prof
    except Exception:
        return None
    finally:
        _state.busy = prev


def _label_from_locals(obj, frame):
    try:
        for k, v in frame.f_locals.items():
            if v is obj and not k.startswith("_"):
                return k
    except Exception:
        pass
    return None


def _node_in(obj):
    """Node id for an input frame; frames valet has not seen become root ('source') nodes."""
    nid = _lookup(obj)
    if nid is not None:
        return nid
    nid = _register(obj)
    frame = _user_frame()
    label = (_label_from_locals(obj, frame) if frame else None) or (
        "series" if getattr(obj, "ndim", 2) == 1 else "frame")
    _labels[nid] = label
    _emit({"op": "source", "params": {}, "inputs": [], "in_nodes": [], "output": _shape(obj),
           "out_node": nid, "node": {"id": nid, "label": label, "profile": _profile(obj)},
           "site": None, "warnings": []})
    return nid


def _out_label(site, inplace, parent):
    site = site or {}
    code = site.get("code") or ""
    m = (None if inplace else _ASSIGN_RE.match(site.get("stmt") or code)) or _RECV_RE.match(code)
    return m.group(1) if m else _labels.get(parent) or "frame"


# ---- artifacts: frames the script writes to a file or prints --------------------------

_MAX_ARTIFACTS = int(os.environ.get("VALET_MAX_ARTIFACTS", "20"))
_PREVIEW_ROWS = 200
_PREVIEW_COLS = 60
_artifact_count = [0]
_LIB_DIRS = tuple(os.sep + d + os.sep for d in ("pandas", "polars", "numpy", "site-packages", "dist-packages"))


def _from_user_code(depth=2):
    """True if the frame `depth` levels up is the user's code (not a library or this hook)."""
    try:
        fn = sys._getframe(depth).f_code.co_filename
    except ValueError:
        return False
    return not (os.path.dirname(os.path.abspath(fn)) == _HERE or any(d in fn for d in _LIB_DIRS))


def _lib(obj):
    return type(obj).__module__.split(".")[0]


def _is_frame(v):
    return _shape(v) is not None and _lib(v) in ("pandas", "polars")


_PROFILE_ROWS = 1_000_000   # profiles of bigger frames use a seeded sample of this many rows
_HIST_BINS = 12


def _num(v):
    try:
        f = float(v)
        return None if f != f or f in (float("inf"), float("-inf")) else round(f, 6)
    except (TypeError, ValueError):
        return None


def _hist(lo, hi, counts):
    return {"lo": lo, "hi": hi, "counts": [int(c) for c in counts]}


def _profile_polars(df):
    import polars as pl
    sampled = df.height > _PROFILE_ROWS
    sub = df.sample(_PROFILE_ROWS, seed=0) if sampled else df
    out = []
    for c, dt in zip(sub.columns, sub.dtypes):
        p = {}
        try:
            s = sub.get_column(c)
            p["distinct"] = int(s.n_unique())
            if dt.is_numeric():
                nn = s.drop_nulls()
                if dt.is_float():
                    nn = nn.filter(nn.is_finite())
                if nn.len():
                    lo, hi = _num(nn.min()), _num(nn.max())
                    p.update(min=lo, max=hi, mean=_num(nn.mean()), median=_num(nn.median()))
                    if lo is not None and hi is not None and hi > lo:
                        b = ((nn.cast(pl.Float64) - lo) / (hi - lo) * _HIST_BINS).floor().clip(0, _HIST_BINS - 1).cast(pl.Int32)
                        counts = [0] * _HIST_BINS
                        for k, n in b.value_counts(name="__n").iter_rows():
                            counts[k] = n
                        p["hist"] = _hist(lo, hi, counts)
            elif dt.is_temporal():
                p.update(min=str(s.min()), max=str(s.max()))
            else:
                vc = s.drop_nulls().cast(pl.Utf8).value_counts(sort=True, name="__n").head(5)
                p["top"] = [[_cell(str(v)), int(n)] for v, n in vc.iter_rows()]
        except Exception:
            pass
        out.append(p)
    return out, sampled


def _profile_pandas(df):
    from pandas.api import types as pt
    sampled = len(df) > _PROFILE_ROWS
    sub = df.sample(_PROFILE_ROWS, random_state=0) if sampled else df
    out = []
    for i in range(sub.shape[1]):
        p = {}
        try:
            s = sub.iloc[:, i]
            p["distinct"] = int(s.nunique())
            if pt.is_numeric_dtype(s) and not pt.is_bool_dtype(s):
                nn = s.dropna()
                nn = nn[(nn != float("inf")) & (nn != float("-inf"))]
                if len(nn):
                    lo, hi = _num(nn.min()), _num(nn.max())
                    p.update(min=lo, max=hi, mean=_num(nn.mean()), median=_num(nn.median()))
                    if lo is not None and hi is not None and hi > lo:
                        b = (((nn.astype(float) - lo) / (hi - lo)) * _HIST_BINS).astype(int).clip(0, _HIST_BINS - 1)
                        counts = [0] * _HIST_BINS
                        for k, n in b.value_counts().items():
                            counts[int(k)] = int(n)
                        p["hist"] = _hist(lo, hi, counts)
            elif pt.is_datetime64_any_dtype(s):
                p.update(min=str(s.min()), max=str(s.max()))
            else:
                vc = s.dropna().astype(str).value_counts().head(5)
                p["top"] = [[_cell(str(v)), int(n)] for v, n in vc.items()]
        except Exception:
            pass
        out.append(p)
    return out, sampled


def _snapshot(obj, lib, nrows=_PREVIEW_ROWS, profile=False):
    """Shape, columns, dtypes, null counts and the first rows of a pandas or polars frame;
    with profile=True also per-column stats over the whole frame (Data Wrangler style)."""
    prev = getattr(_state, "busy", False)
    _state.busy = True
    try:
        if lib == "polars":
            cols = obj.columns[:_PREVIEW_COLS]
            sub = obj.select(cols) if len(obj.columns) > _PREVIEW_COLS else obj
            snap = {"shape": [obj.height, obj.width], "columns": [str(c) for c in cols],
                    "dtypes": [str(t) for t in sub.dtypes]}
            if obj.height * len(cols) <= _NULL_CELL_LIMIT:
                snap["nulls"] = [int(x) for x in sub.null_count().row(0)]
            snap["rows"] = [[_cell(v) for v in r] for r in sub.head(nrows).rows()]
            if profile and _PROFILE:
                snap["profile"], snap["profile_sampled"] = _profile_polars(sub)
        else:
            df = obj.to_frame() if getattr(obj, "ndim", 2) == 1 else obj
            shape = list(df.shape)
            # a meaningful index (e.g. after groupby) is data the reader wants to see
            reset = type(df.index).__name__ != "RangeIndex" or df.index.name is not None
            if reset:
                df = df.head(_PREVIEW_ROWS).reset_index()
            sub = df.iloc[:, :_PREVIEW_COLS]
            snap = {"shape": shape, "columns": [str(c) for c in sub.columns],
                    "dtypes": [str(t) for t in sub.dtypes]}
            if shape[0] * sub.shape[1] <= _NULL_CELL_LIMIT and len(df) == shape[0]:
                snap["nulls"] = [int(x) for x in sub.isna().sum().tolist()]
            snap["rows"] = [[_cell(v) for v in r] for r in sub.head(_PREVIEW_ROWS).itertuples(index=False, name=None)]
            if profile and _PROFILE:
                full = obj.to_frame() if getattr(obj, "ndim", 2) == 1 else obj
                if reset:  # the preview shows the index as columns: profile it too
                    full = (full if len(full) <= _PROFILE_ROWS else full.sample(_PROFILE_ROWS, random_state=0)).reset_index()
                snap["profile"], snap["profile_sampled"] = _profile_pandas(full.iloc[:, :_PREVIEW_COLS])
        snap["more_columns"] = max(0, snap["shape"][1] - len(snap["columns"]))
        return snap
    finally:
        _state.busy = prev


def _artifact(kind, obj, lib, target=None, title=None):
    """Record a frame the script wrote (kind="write") or printed (kind="print")."""
    if _artifact_count[0] >= _MAX_ARTIFACTS:
        return
    try:
        path = os.path.abspath(os.fspath(target)) if isinstance(target, (str, os.PathLike)) else None
        rec = {"kind": "artifact", "type": kind, "lib": lib, "path": path, "site": _call_site()}
        if title:
            rec["title"] = str(title)
        rec.update(_snapshot(obj, lib, profile=True))
        if kind == "write" and path:
            d = _row_diff(obj, lib, path)
            if d:
                rec["diff"] = d
        _artifact_count[0] += 1
        _emit(rec)
    except Exception:
        pass


def _row_diff(obj, lib, path):
    """Rows added/removed/changed vs the previous version of this output (see valet_rowdiff)."""
    valet_dir = os.environ.get("VALET_DIR")
    if not valet_dir:
        return None
    prev = getattr(_state, "busy", False)
    _state.busy = True
    try:
        import valet_rowdiff as rd
        if lib == "polars":
            if obj.height * max(1, obj.width) > rd.MAX_CELLS:
                return {"skipped": "too big to diff"}
            columns, rows = obj.columns, [[rd.cell(v) for v in r] for r in obj.rows()]
        else:
            df = obj.to_frame() if getattr(obj, "ndim", 2) == 1 else obj
            if df.shape[0] * max(1, df.shape[1]) > rd.MAX_CELLS:
                return {"skipped": "too big to diff"}
            if type(df.index).__name__ != "RangeIndex" or df.index.name is not None:
                df = df.reset_index()
            columns, rows = list(df.columns), [[rd.cell(v) for v in r] for r in df.itertuples(index=False, name=None)]
        run_id = os.environ.get("VALET_RUN_ID") or ""
        return rd.diff_and_save(valet_dir, path, columns, rows, int(run_id) if run_id.isdigit() else None, "frame")
    except Exception:
        return None
    finally:
        _state.busy = prev


def _wrap_print(cls, name, lib):
    """print(df) / str(df) from user code records the frame as an artifact."""
    orig = getattr(cls, name, None)
    if orig is None or getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(self, *args, **kwargs):
        if getattr(_state, "busy", False) or not _from_user_code():
            return orig(self, *args, **kwargs)
        _state.busy = True
        try:
            text = orig(self, *args, **kwargs)
        finally:
            _state.busy = False
        _artifact("print", self, lib)
        return text

    wrapper._valet = True
    setattr(cls, name, wrapper)


def _wrap_polars_write(DF, name):
    orig = getattr(DF, name, None)
    if orig is None or getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(self, *args, **kwargs):
        if getattr(_state, "busy", False) or not _from_user_code(2):
            return orig(self, *args, **kwargs)
        try:
            in_node = _node_in(self)
        except Exception:
            in_node = None
        _state.busy = True
        try:
            result = orig(self, *args, **kwargs)
        finally:
            _state.busy = False
        target = args[0] if args else kwargs.get("file", kwargs.get("path", kwargs.get("workbook")))
        _artifact("write", self, "polars", target)
        try:
            tgt = os.path.basename(os.fspath(target)) if isinstance(target, (str, os.PathLike)) else name
            nid = _new_id()
            _emit({"op": f"write:{name[len('write_'):]}", "params": {"target": _short(target, 200)}, "inputs": [],
                   "in_nodes": [in_node] if in_node else [], "output": _shape(self), "site": _call_site(),
                   "out_node": nid, "node": {"id": nid, "label": tgt, "sink": True}, "warnings": []})
        except Exception:
            pass
        return result

    wrapper._valet = True
    setattr(DF, name, wrapper)


def _pl_str(v):
    if _lib(v) == "polars":          # expressions / selectors read well as text
        s = str(v)
    elif isinstance(v, (list, tuple)):
        s = "[" + ", ".join(_pl_str(x) for x in v[:12]) + (", ..." if len(v) > 12 else "") + "]"
    elif isinstance(v, dict):
        s = "{" + ", ".join(f"{k}: {_pl_str(x)}" for k, x in list(v.items())[:12]) + "}"
    elif isinstance(v, str):
        s = v
    else:
        s = _short(v)
    return s if len(s) <= 160 else s[:159] + "..."


def _pl_params(sig, args, kwargs, op):
    """Parameters of a polars call by name (from its real signature), expressions as text."""
    if sig is None:
        return {}
    try:
        bound = sig.bind_partial(*args, **kwargs).arguments
    except TypeError:
        return {}
    out = {}
    for k, v in bound.items():
        if k in ("self", "other", "items") or _is_frame(v) or v is None or (isinstance(v, (tuple, list, dict)) and not v):
            continue
        if isinstance(v, dict) and k in ("named_exprs", "named_aggs", "named_by"):
            out[k.replace("named_", "")] = _pl_str(list(v))
            continue
        out[k] = _pl_str(v)
    if op in ("join", "join_asof"):
        out.setdefault("how", "inner")
    return out


def _out_names(exprs):
    names = []
    for e in exprs:
        for x in (e if isinstance(e, (list, tuple)) else [e]):
            try:
                names.append(x.meta.output_name())
            except Exception:
                names.append(str(x)[:40])
    return names


def _wrap_pl_groupby(G, name):
    orig = getattr(G, name, None)
    if orig is None or getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(self, *args, **kwargs):
        if getattr(_state, "busy", False) or not _from_user_code(2):
            return orig(self, *args, **kwargs)
        try:
            obj = getattr(self, "df", None)
            in_node = _node_in(obj) if obj is not None else None
        except Exception:
            obj, in_node = None, None
        _state.busy = True
        try:
            result = orig(self, *args, **kwargs)
        finally:
            _state.busy = False
        try:
            by = list(getattr(self, "by", ()) or ()) + list((getattr(self, "named_by", None) or {}).keys())
            extra = {"by": _pl_str(by)}
            aggs = _out_names(args) + list(kwargs)
            if aggs:
                extra["aggs"] = _pl_str(aggs)
            before = [{"shape": _shape(obj), "cols": _cols(obj), "node": in_node}]
            _record(f"group_by.{name}", (), {}, before, result, extra=extra, params={})
        except Exception:
            pass
        return result

    wrapper._valet = True
    setattr(G, name, wrapper)


def _wrap_pl_collect(LF):
    """LazyFrame.collect(): the result is a new frame; the (unoptimized) plan says how."""
    orig = getattr(LF, "collect", None)
    if orig is None or getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(self, *args, **kwargs):
        if getattr(_state, "busy", False) or not _from_user_code(2):
            return orig(self, *args, **kwargs)
        _state.busy = True
        try:
            result = orig(self, *args, **kwargs)
            try:
                plan = self.explain(optimized=False)
            except Exception:
                plan = ""
        finally:
            _state.busy = False
        if _is_frame(result):
            try:
                _record("collect", (), {}, [], result, params={"plan": _short_plan(plan)})
            except Exception:
                pass
        return result

    wrapper._valet = True
    setattr(LF, "collect", wrapper)


def _short_plan(plan):
    lines = [ln.strip() for ln in plan.splitlines() if ln.strip()]
    s = " <- ".join(lines[:8])
    return s if len(s) <= 300 else s[:299] + "..."


def _patch_polars(pl):
    DF = pl.DataFrame
    for name in ("write_csv", "write_parquet", "write_excel", "write_json", "write_ndjson",
                 "write_ipc", "write_avro", "write_delta", "write_database"):
        _wrap_polars_write(DF, name)
    _wrap_print(DF, "__str__", "polars")
    _wrap_print(DF, "__repr__", "polars")
    # transformations (and a few "minor" ones that keep the lineage tree connected)
    for name in ("filter", "join", "join_asof", "with_columns", "select", "drop", "unique", "pivot",
                 "unpivot", "explode", "drop_nulls", "fill_null", "fill_nan", "rename", "vstack", "hstack",
                 "sort", "cast", "with_row_index"):
        _wrap(DF, name, params_fn=_pl_params)
    _wrap(pl, "concat", params_fn=_pl_params)
    for name in ("read_csv", "read_parquet", "read_excel", "read_json", "read_ndjson", "read_ipc",
                 "read_database", "read_avro", "read_delta"):
        _wrap_io(pl, name, "read")
    try:
        from polars.dataframe.group_by import GroupBy
        for name in ("agg", "len", "count", "sum", "mean", "median", "min", "max", "first", "last",
                     "n_unique", "all"):
            _wrap_pl_groupby(GroupBy, name)
    except ImportError:
        pass
    _wrap_pl_collect(pl.LazyFrame)


def _frames_in(args, kwargs):
    """DataFrame/Series inputs among the call arguments (including lists, for concat)."""
    out = []
    for v in list(args) + list(kwargs.values()):
        if _is_frame(v):
            out.append(v)
        elif isinstance(v, (list, tuple)) and v and all(_is_frame(x) for x in v[:50]):
            out.extend(v[:50])
    return out


# ---- per-operation parameter extraction -----------------------------------------------

_PARAMS = {
    "merge": ("on", "left_on", "right_on", "how", "validate", "suffixes"),
    "join": ("on", "how", "lsuffix", "rsuffix"),
    "drop_duplicates": ("subset", "keep"),
    "dropna": ("subset", "how", "axis", "thresh"),
    "drop": ("columns", "labels", "index", "axis"),
    "pivot_table": ("index", "columns", "values", "aggfunc"),
    "pivot": ("index", "columns", "values"),
    "melt": ("id_vars", "value_vars", "var_name", "value_name"),
    "explode": ("column",),
    "concat": ("axis", "join", "ignore_index"),
    "fillna": ("value", "method", "axis"),
    "rename": ("columns", "index"),
}
# positional parameter names after the first input, for methods where they are common
_POSITIONAL = {
    "merge": ("right", "how", "on"),
    "join": ("other", "on", "how"),
    "drop_duplicates": ("subset", "keep"),
    "query": ("expr",),
    "explode": ("column",),
    "drop": ("labels",),
    "pivot_table": ("values", "index", "columns", "aggfunc"),
}
_DEFAULTS = {"merge": {"how": "inner"}, "join": {"how": "left"}}


def _params(op, args, kwargs):
    p = {}
    for name, val in zip(_POSITIONAL.get(op, ()), args[1:]):
        if name not in ("right", "other"):
            p[name] = val
    for name in _PARAMS.get(op, ()) + _POSITIONAL.get(op, ()):
        if name in kwargs and name not in ("right", "other"):
            p[name] = kwargs[name]
    for k, v in _DEFAULTS.get(op, {}).items():
        p.setdefault(k, v)
    if op == "assign":
        p["new"] = list(kwargs)
    return {k: _short(v) for k, v in p.items()}


def _warnings(op, params, inputs, out_shape):
    w = []
    if not inputs or out_shape is None:
        return w
    rows_in = inputs[0]["shape"][0] if inputs[0]["shape"] else None
    rows_out = out_shape[0]
    if op in ("merge", "join") and rows_in is not None:
        how = params.get("how", "").strip("'\"")
        if how in ("left", "inner") and rows_out > rows_in:
            w.append(
                f"join fan-out: {rows_out - rows_in:+,} rows vs the left input "
                f"({rows_in:,} -> {rows_out:,}); join keys are not unique on the right"
            )
        if how == "inner" and rows_out < rows_in:
            pct = 100 * (rows_in - rows_out) / rows_in if rows_in else 0
            w.append(f"inner join dropped {rows_in - rows_out:,} left rows ({pct:.1f}%) with no match")
    if op == "concat" and len(inputs) > 1 and params.get("axis", "0") in ("0", "'index'"):
        colsets = {tuple(i["cols"]) for i in inputs if i.get("cols") is not None}
        if len({frozenset(c) for c in colsets}) > 1:
            w.append("concat inputs have different columns; missing ones are filled with NaN")
    if rows_in and rows_out == 0:
        w.append("result is empty")
    return w


def _record(op, args, kwargs, before, result, extra=None, inplace=False, params=None):
    site = _call_site()
    out_shape = _shape(result)
    if out_shape is None and before:  # inplace=True: the first input was mutated
        result, out_shape = None, before[0].get("after_shape")
    params = dict(params) if params is not None else _params(op, args, kwargs)
    if extra:
        params.update(extra)
    rec = {
        "op": op,
        "params": params,
        "inputs": [{"shape": b["shape"], "cols": b["cols"]} for b in before],
        "in_nodes": [b.get("node") for b in before],
        "output": out_shape,
        "site": site,
    }
    if op in _MINOR:
        rec["minor"] = True
    if result is not None and out_shape is not None:
        nid = _register(result)
        label = _out_label(site, inplace, rec["in_nodes"][0] if rec["in_nodes"] else None)
        _labels[nid] = label
        rec["out_node"] = nid
        rec["node"] = {"id": nid, "label": label, "profile": _profile(result)}
    out_cols = _cols(result) if result is not None else None
    if op.startswith(("groupby.", "group_by.")):
        # keys move into the index, so added/removed columns would be misleading
        rec["cols_out"] = out_cols[:50] if out_cols is not None else None
    elif out_cols is not None and before and before[0]["cols"] is not None:
        first = set(before[0]["cols"])
        rec["cols_added"] = [c for c in out_cols if c not in first][:50]
        rec["cols_removed"] = [c for c in before[0]["cols"] if c not in set(out_cols)][:50]
    rec["warnings"] = _warnings(op, params, rec["inputs"], out_shape)
    _emit(rec)


_MULTI_INPUT = ("merge", "join", "join_asof", "concat", "vstack", "hstack")


def _wrap(owner, name, op=None, inputs=None, params_fn=None):
    orig = getattr(owner, name, None)
    if orig is None or getattr(orig, "_valet", False):
        return
    op = op or name
    try:
        sig = inspect.signature(orig) if params_fn else None
    except (TypeError, ValueError):
        sig = None

    @functools.wraps(orig)
    def wrapper(*args, **kwargs):
        # only calls made by the user's code; pandas/polars call their own methods internally
        if getattr(_state, "busy", False) or not _from_user_code(2):
            return orig(*args, **kwargs)
        try:
            frames = inputs(args, kwargs) if inputs else _frames_in(args, kwargs)
            if op not in _MULTI_INPUT:
                frames = frames[:1]
            before = [{"shape": _shape(f), "cols": _cols(f), "node": _node_in(f)} for f in frames]
        except Exception:
            frames, before = [], []
        _state.busy = True
        try:
            result = orig(*args, **kwargs)
        finally:
            _state.busy = False
        try:
            params = params_fn(sig, args, kwargs, op) if params_fn else None
            if result is None and frames:  # inplace
                before[0]["after_shape"] = _shape(frames[0])
                _record(op, args, kwargs, before, frames[0], inplace=True, params=params)
            else:
                _record(op, args, kwargs, before, result, params=params)
        except Exception:
            pass
        return result

    wrapper._valet = True
    setattr(owner, name, wrapper)


def _wrap_io(module, name, kind):
    orig = getattr(module, name, None)
    if orig is None or getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(*args, **kwargs):
        if getattr(_state, "busy", False) or not _from_user_code(2):
            return orig(*args, **kwargs)
        in_node = None
        if kind == "write" and args:
            try:
                in_node = _node_in(args[0])
            except Exception:
                pass
        _state.busy = True
        try:
            result = orig(*args, **kwargs)
        finally:
            _state.busy = False
        try:
            target = args[1] if kind == "write" and len(args) > 1 else (args[0] if args else None)
            for k in ("path_or_buf", "path", "filepath_or_buffer", "source", "file"):
                if k in kwargs:
                    target = kwargs[k]
                    break
            obj = args[0] if kind == "write" else result
            site = _call_site()
            rec = {
                "op": f"{kind}:{name.replace('read_', '').replace('to_', '')}",
                "params": {"source" if kind == "read" else "target": _short(target, 200)},
                "inputs": [],
                "in_nodes": [in_node] if in_node else [],
                "output": _shape(obj),
                "cols": _cols(obj) if kind == "read" else None,
                "site": site,
                "warnings": [],
            }
            if kind == "read" and _shape(result) is not None:
                nid = _register(result)
                _labels[nid] = _out_label(site, False, None)
                rec["out_node"] = nid
                rec["node"] = {"id": nid, "label": _labels[nid], "profile": _profile(result)}
            elif kind == "write":
                _artifact("write", obj, "pandas", target)
                nid = _new_id()
                tgt = os.path.basename(str(target)) if isinstance(target, (str, os.PathLike)) else name
                rec["out_node"] = nid
                rec["node"] = {"id": nid, "label": tgt, "sink": True}
            _emit(rec)
        except Exception:
            pass
        return result

    wrapper._valet = True
    setattr(module, name, wrapper)


def _is_bool_mask(key):
    dt = getattr(key, "dtype", None)
    return dt is not None and str(dt) in ("bool", "boolean")


def _patch_pandas(pd):
    DF = pd.DataFrame
    for name in ("merge", "join", "drop_duplicates", "dropna", "drop", "query", "pivot",
                 "pivot_table", "melt", "explode", "assign", "rename", "fillna"):
        _wrap(DF, name)
    for name in sorted(_MINOR):
        _wrap(DF, name)
    _wrap_setitem(DF)
    _wrap(pd, "merge")
    _wrap(pd, "concat")
    for name in ("read_csv", "read_parquet", "read_excel", "read_json", "read_sql",
                 "read_feather", "read_table", "read_pickle"):
        _wrap_io(pd, name, "read")
    for name in ("to_csv", "to_parquet", "to_excel", "to_json", "to_feather", "to_pickle", "to_sql"):
        _wrap_io(DF, name, "write")
    _wrap_print(DF, "__repr__", "pandas")

    # groupby aggregations: the input is the grouped frame, the op names the keys
    try:
        from pandas.core.groupby.generic import DataFrameGroupBy, SeriesGroupBy
    except ImportError:
        DataFrameGroupBy = SeriesGroupBy = None
    for G in (DataFrameGroupBy, SeriesGroupBy):
        if G is None:
            continue
        for name in ("agg", "aggregate", "sum", "mean", "median", "count", "size", "nunique",
                     "min", "max", "first", "last", "std", "apply"):
            _wrap_groupby(G, name)

    # boolean-mask row filters: df[mask] and df.loc[mask]
    _wrap_filter(DF, "__getitem__", lambda self: self)
    try:
        from pandas.core.indexing import _LocIndexer
        _wrap_filter(_LocIndexer, "__getitem__", lambda self: self.obj, loc=True)
    except ImportError:
        pass


def _wrap_setitem(DF):
    """df["col"] = ...: an in-place change, recorded as a new version of the same frame."""
    orig = DF.__setitem__
    if getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(self, key, value):
        if getattr(_state, "busy", False) or _PANDAS_DIR in sys._getframe(1).f_code.co_filename:
            return orig(self, key, value)
        try:
            cols = _cols(self)
            before = [{"shape": _shape(self), "cols": cols, "node": _node_in(self)}]
        except Exception:
            cols, before = None, []
        _state.busy = True
        try:
            orig(self, key, value)
        finally:
            _state.busy = False
        try:
            existed = cols is not None and str(key) in cols
            _record("replace column" if existed else "add column", (), {}, before, self,
                    extra={"column": _short(key)}, inplace=True)
        except Exception:
            pass

    wrapper._valet = True
    DF.__setitem__ = wrapper


def _wrap_groupby(G, name):
    orig = getattr(G, name, None)
    if orig is None or getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(self, *args, **kwargs):
        if getattr(_state, "busy", False) or _PANDAS_DIR in sys._getframe(1).f_code.co_filename:
            return orig(self, *args, **kwargs)
        try:
            obj = getattr(self, "obj", None)
            in_node = _node_in(obj) if obj is not None else None
        except Exception:
            in_node = None
        _state.busy = True
        try:
            result = orig(self, *args, **kwargs)
        finally:
            _state.busy = False
        try:
            obj = getattr(self, "obj", None)
            keys = getattr(self, "keys", None)
            extra = {"by": _short(keys)}
            if args:
                extra["func"] = _short(args[0])
            elif kwargs:
                extra["func"] = _short(kwargs)
            before = [{"shape": _shape(obj), "cols": _cols(obj), "node": in_node}]
            _record(f"groupby.{name}", (), {}, before, result, extra=extra)
        except Exception:
            pass
        return result

    wrapper._valet = True
    setattr(G, name, wrapper)


def _wrap_filter(owner, name, get_frame, loc=False):
    orig = getattr(owner, name)
    if getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(self, key):
        # cheap path for the very common column access, and for pandas' internal calls
        if getattr(_state, "busy", False) or not _is_bool_mask(key[0] if loc and isinstance(key, tuple) and key else key) \
                or _PANDAS_DIR in sys._getframe(1).f_code.co_filename:
            return orig(self, key)
        try:
            frame = get_frame(self)
            in_node = _node_in(frame)
        except Exception:
            frame, in_node = None, None
        _state.busy = True
        try:
            result = orig(self, key)
        finally:
            _state.busy = False
        try:
            if frame is not None and _shape(result) is not None and getattr(result, "ndim", 0) == 2:
                before = [{"shape": _shape(frame), "cols": _cols(frame), "node": in_node}]
                _record("loc filter" if loc else "filter", (), {}, before, result)
        except Exception:
            pass
        return result

    wrapper._valet = True
    setattr(owner, name, wrapper)


_PATCHERS = {"pandas": _patch_pandas, "polars": _patch_polars}


class _LibFinder:
    """Patches pandas / polars right after each is first imported (never imports them itself)."""

    def __init__(self, names):
        self.pending = set(names)
        self.resolving = False

    def find_spec(self, name, path=None, target=None):
        if name not in self.pending or self.resolving:
            return None
        self.resolving = True
        try:
            spec = importlib.util.find_spec(name)
        finally:
            self.resolving = False
        # Don't cross the library off yet: other libraries probe with find_spec("pandas")
        # without importing it (polars does). Only the actual import executes the module.
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def patched_exec(module):
            exec_module(module)
            self.pending.discard(name)
            if not self.pending and self in sys.meta_path:
                sys.meta_path.remove(self)
            try:
                _PATCHERS[name](module)
            except Exception:
                pass

        spec.loader.exec_module = patched_exec
        return spec


def _chain_next_sitecustomize():
    """We shadow any existing sitecustomize.py, so run the next one on sys.path ourselves."""
    for p in sys.path:
        d = os.path.abspath(p or ".")
        if d == _HERE:
            continue
        cand = os.path.join(d, "sitecustomize.py")
        if os.path.isfile(cand):
            try:
                spec = importlib.util.spec_from_file_location("_valet_next_sitecustomize", cand)
                spec.loader.exec_module(importlib.util.module_from_spec(spec))
            except Exception:
                pass
            return


if _TRAIL and os.environ.get("VALET_TRAIL_OFF") != "1":
    _later = []
    for _name, _patch in _PATCHERS.items():
        if _name in sys.modules:
            _patch(sys.modules[_name])
        else:
            _later.append(_name)
    if _later:
        sys.meta_path.insert(0, _LibFinder(_later))
_chain_next_sitecustomize()
