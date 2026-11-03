"""valet data trail: loaded via PYTHONPATH into every Python process valet runs.

When (and only when) the script imports pandas, common DataFrame operations are wrapped
so that each call appends one JSON line to $VALET_TRAIL describing it: the operation,
its key parameters, input and output shapes, columns added or removed, the line of user
code that made the call (plus any `#` comment right above it), and warnings such as a
join that multiplied rows.

This file must stay stdlib-only and self-contained: it also runs inside environments
(e.g. `uv run --script`) where valet itself is not installed. Recording never raises
into user code.
"""

import functools
import importlib.util
import json
import linecache
import os
import sys
import threading

_TRAIL = os.environ.get("VALET_TRAIL")
_HERE = os.path.dirname(os.path.abspath(__file__))
_state = threading.local()
_lock = threading.Lock()


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


def _call_site():
    """First frame outside pandas/numpy/site-packages/this hook: (file, line, code, comment)."""
    f = sys._getframe(1)
    while f is not None:
        fn = f.f_code.co_filename
        if not (
            fn.startswith("<")
            or fn == __file__
            or os.sep + "site-packages" + os.sep in fn
            or os.sep + "pandas" + os.sep in fn
            or os.sep + "numpy" + os.sep in fn
        ):
            break
        f = f.f_back
    if f is None:
        return None
    fn, ln = f.f_code.co_filename, f.f_lineno
    code = linecache.getline(fn, ln).strip()
    comment = []
    i = ln - 1
    while i > 0 and len(comment) < 3:
        prev = linecache.getline(fn, i).strip()
        if not prev.startswith("#") or prev.startswith("# ///"):
            break
        comment.insert(0, prev.lstrip("#").strip())
        i -= 1
    return {"file": fn, "line": ln, "code": code[:240], "comment": " ".join(comment) or None}


def _shape(obj):
    s = getattr(obj, "shape", None)
    return list(s) if isinstance(s, tuple) else None


def _cols(obj):
    try:
        cols = getattr(obj, "columns", None)
        return [str(c) for c in cols] if cols is not None and len(cols) <= 500 else None
    except Exception:
        return None


def _frames_in(args, kwargs):
    """DataFrame/Series inputs among the call arguments (including lists, for concat)."""
    out = []
    for v in list(args) + list(kwargs.values()):
        if _shape(v) is not None and hasattr(v, "ndim"):
            out.append(v)
        elif isinstance(v, (list, tuple)) and v and all(_shape(x) is not None for x in v[:50]):
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
                f"({rows_in:,} → {rows_out:,}); join keys are not unique on the right"
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


def _record(op, args, kwargs, before, result, extra=None):
    site = _call_site()
    out_shape = _shape(result)
    if out_shape is None and before:  # inplace=True: the first input was mutated
        result, out_shape = None, before[0].get("after_shape")
    params = _params(op, args, kwargs)
    if extra:
        params.update(extra)
    rec = {
        "op": op,
        "params": params,
        "inputs": [{"shape": b["shape"], "cols": b["cols"]} for b in before],
        "output": out_shape,
        "site": site,
    }
    out_cols = _cols(result) if result is not None else None
    if op.startswith("groupby."):
        # keys move into the index, so added/removed columns would be misleading
        rec["cols_out"] = out_cols[:50] if out_cols is not None else None
    elif out_cols is not None and before and before[0]["cols"] is not None:
        first = set(before[0]["cols"])
        rec["cols_added"] = [c for c in out_cols if c not in first][:50]
        rec["cols_removed"] = [c for c in before[0]["cols"] if c not in set(out_cols)][:50]
    rec["warnings"] = _warnings(op, params, rec["inputs"], out_shape)
    _emit(rec)


def _wrap(owner, name, op=None, inputs=None):
    orig = getattr(owner, name, None)
    if orig is None or getattr(orig, "_valet", False):
        return
    op = op or name

    @functools.wraps(orig)
    def wrapper(*args, **kwargs):
        if getattr(_state, "busy", False):
            return orig(*args, **kwargs)
        try:
            frames = inputs(args, kwargs) if inputs else _frames_in(args, kwargs)
            if op not in ("merge", "join", "concat"):
                frames = frames[:1]
            before = [{"shape": _shape(f), "cols": _cols(f)} for f in frames]
        except Exception:
            frames, before = [], []
        _state.busy = True
        try:
            result = orig(*args, **kwargs)
        finally:
            _state.busy = False
        try:
            if result is None and frames:  # inplace
                before[0]["after_shape"] = _shape(frames[0])
                _record(op, args, kwargs, before, frames[0])
            else:
                _record(op, args, kwargs, before, result)
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
        if getattr(_state, "busy", False):
            return orig(*args, **kwargs)
        _state.busy = True
        try:
            result = orig(*args, **kwargs)
        finally:
            _state.busy = False
        try:
            target = args[1] if kind == "write" and len(args) > 1 else (args[0] if args else None)
            target = kwargs.get("path_or_buf", kwargs.get("path", kwargs.get("filepath_or_buffer", target)))
            obj = args[0] if kind == "write" else result
            _emit({
                "op": f"{kind}:{name.replace('read_', '').replace('to_', '')}",
                "params": {"source" if kind == "read" else "target": _short(target, 200)},
                "inputs": [],
                "output": _shape(obj),
                "cols": _cols(obj) if kind == "read" else None,
                "site": _call_site(),
                "warnings": [],
            })
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
    _wrap(pd, "merge")
    _wrap(pd, "concat")
    for name in ("read_csv", "read_parquet", "read_excel", "read_json", "read_sql",
                 "read_feather", "read_table", "read_pickle"):
        _wrap_io(pd, name, "read")
    for name in ("to_csv", "to_parquet", "to_excel", "to_json", "to_feather", "to_pickle", "to_sql"):
        _wrap_io(DF, name, "write")

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


def _wrap_groupby(G, name):
    orig = getattr(G, name, None)
    if orig is None or getattr(orig, "_valet", False):
        return

    @functools.wraps(orig)
    def wrapper(self, *args, **kwargs):
        if getattr(_state, "busy", False):
            return orig(self, *args, **kwargs)
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
            before = [{"shape": _shape(obj), "cols": _cols(obj)}]
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

    pandas_dir = os.sep + "pandas" + os.sep

    @functools.wraps(orig)
    def wrapper(self, key):
        # cheap path for the very common column access, and for pandas' internal calls
        if getattr(_state, "busy", False) or not _is_bool_mask(key[0] if loc and isinstance(key, tuple) and key else key) \
                or pandas_dir in sys._getframe(1).f_code.co_filename:
            return orig(self, key)
        _state.busy = True
        try:
            result = orig(self, key)
        finally:
            _state.busy = False
        try:
            k = key[0] if loc and isinstance(key, tuple) and key else key
            if _is_bool_mask(k) and _shape(result) is not None and getattr(result, "ndim", 0) == 2:
                frame = get_frame(self)
                before = [{"shape": _shape(frame), "cols": _cols(frame)}]
                _record("loc filter" if loc else "filter", (), {}, before, result)
        except Exception:
            pass
        return result

    wrapper._valet = True
    setattr(owner, name, wrapper)


class _PandasFinder:
    """Patches pandas right after it is first imported (never imports it ourselves)."""

    def find_spec(self, name, path=None, target=None):
        if name != "pandas":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec("pandas")
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def patched_exec(module):
            exec_module(module)
            try:
                _patch_pandas(module)
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
    if "pandas" in sys.modules:
        _patch_pandas(sys.modules["pandas"])
    else:
        sys.meta_path.insert(0, _PandasFinder())
_chain_next_sitecustomize()
