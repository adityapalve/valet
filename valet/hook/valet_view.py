"""Show things in the valet viewer from a script run through valet. Nothing to install:
valet puts this module on the import path of every run.

    import valet_view as vv
    vv.chart(df, x="month", y="revenue", kind="line", color="region", title="Revenue by month")
    vv.chart(df, x="minutes", kind="hist")
    vv.vega({...})              # any Vega-Lite spec (https://vega.github.io/vega-lite/)
    vv.table(df, "Top users")   # a dataframe as a table in the viewer

`df` can be a pandas or polars DataFrame or a list of dicts. Charts are drawn by the viewer
(interactive, with tooltips) from up to `max_rows` rows of data; aggregate first for big data.
Stdlib only. Outside a valet run, calls print a note and do nothing.
"""

import datetime as _dt
import json
import math
import os
import sys

__all__ = ["chart", "vega", "table", "key"]

KINDS = ("bar", "line", "point", "area", "hist", "box", "heatmap", "tick")


def chart(data, x, y=None, kind="bar", color=None, title=None, agg=None, sort=None, size=None,
          tooltip=None, max_rows=5000, width=None, height=None):
    """A common chart from a dataframe.

    kind:  bar | line | point | area | tick | hist (x only) | box (x groups, y values) |
           heatmap (x, y, color = the value)
    agg:   aggregate y per x, e.g. "sum", "mean", "count", "median" (bar/line/area/point)
    sort:  for bar charts, "x", "-x", "y" or "-y" (e.g. "-y" = biggest bar first)
    """
    if kind not in KINDS:
        raise ValueError(f"valet_view.chart: kind must be one of {', '.join(KINDS)}")
    # Counts, histograms and aggregates are computed here over *all* rows, so the chart is exact
    # no matter how big the data is; only the (small) result goes to the viewer.
    pre = _preaggregate(data, kind, x, y, color, agg)
    if pre is not None:
        spec, n_total = pre
        return _emit_chart(spec, title or _default_title(kind, x, y, agg), len(spec["data"]["values"]),
                           n_total, aggregated=True)
    rows, total = _records(data, max_rows)
    types = _field_types(rows)
    enc = {}

    def field(name, **extra):
        if name not in types and rows:
            raise ValueError(f"valet_view.chart: no column {name!r}; columns are {list(types)}")
        return {"field": name, "type": types.get(name, "nominal"), **extra}

    if kind == "hist":
        mark = {"type": "bar", "tooltip": True}
        enc["x"] = {**field(x), "bin": {"maxbins": 40}, "type": "quantitative"}
        enc["y"] = {"aggregate": "count", "type": "quantitative", "title": "count"}
        if color:
            enc["color"] = field(color)
    elif kind == "box":
        mark = {"type": "boxplot", "extent": 1.5}
        enc["x"] = field(x)
        enc["y"] = {**field(y), "type": "quantitative"}
        if color:
            enc["color"] = field(color)
    elif kind == "heatmap":
        mark = {"type": "rect", "tooltip": True}
        enc["x"] = {**field(x), "type": "ordinal"}
        enc["y"] = {**field(y), "type": "ordinal"}
        enc["color"] = {**field(color), "type": "quantitative"} if color else {"aggregate": "count", "type": "quantitative"}
    else:
        mark = {"type": kind, "tooltip": True}
        if kind == "line":
            mark["point"] = len(rows) <= 200
        enc["x"] = field(x)
        if y is None:
            enc["y"] = {"aggregate": "count", "type": "quantitative", "title": "count"}
        else:
            enc["y"] = {**field(y), "type": "quantitative"}
            if agg:
                enc["y"]["aggregate"] = agg
        if color:
            enc["color"] = field(color)
        if size:
            enc["size"] = field(size)
        if kind == "bar" and sort:
            desc = sort.startswith("-")
            key = sort.lstrip("-")
            enc["x"]["sort"] = {"encoding": key, "order": "descending" if desc else "ascending"} \
                if key in ("x", "y") else sort
        if kind == "bar" and color:
            # side by side, not stacked: stacked means/medians would add up to nonsense
            enc["xOffset"] = {"field": color}
        if kind == "bar" and types.get(x) == "nominal" and len({r.get(x) for r in rows}) > 25:
            # many categories: horizontal bars read better
            enc["x"], enc["y"] = enc["y"], enc["x"]
            if "xOffset" in enc:
                enc["yOffset"] = enc.pop("xOffset")
    if kind in ("point", "line", "tick"):
        # zero baselines matter for bars and areas; for points and lines they squash the data
        for ch in ("x", "y"):
            if enc.get(ch, {}).get("type") == "quantitative" and "aggregate" not in enc[ch]:
                enc[ch]["scale"] = {"zero": False}
    if tooltip:
        enc["tooltip"] = [field(t) for t in tooltip]
    spec = {"mark": mark, "encoding": enc, "data": {"values": rows}}
    if width:
        spec["width"] = width
    if height:
        spec["height"] = height
    return _emit_chart(spec, title or _default_title(kind, x, y, agg), len(rows), total)


def vega(spec, data=None, title=None, max_rows=5000):
    """Any Vega-Lite spec (a dict). If `data` is given it becomes the spec's data."""
    spec = dict(spec)
    total = None
    if data is not None:
        rows, total = _records(data, max_rows)
        spec["data"] = {"values": rows}
    n = len(spec.get("data", {}).get("values", []) or [])
    return _emit_chart(spec, title or spec.get("title"), n, total if total is not None else n)


def table(data, title=None):
    """Show a dataframe as a table in the viewer (same as printing it, but with a title)."""
    hook = _hook()
    if hook is None:
        return None
    lib = "polars" if type(data).__module__.split(".")[0] == "polars" else "pandas"
    hook._artifact("print", data, lib, None, title)
    print(f"[valet] table: {title or 'dataframe'}", flush=True)
    return None


def key(path, columns):
    """The columns that identify a row of the output file `path`, used to match rows between
    runs ("which rows changed?"). valet guesses a key otherwise; set it when the guess is wrong."""
    valet_dir = os.environ.get("VALET_DIR")
    if not valet_dir:
        print("[valet] valet_view: not running under `valet py`; nothing set", flush=True)
        return None
    import valet_rowdiff
    valet_rowdiff.set_key(valet_dir, path, [str(c) for c in columns])
    print(f"[valet] key for {path}: {', '.join(map(str, columns))}", flush=True)
    return None


# ---- internals -----------------------------------------------------------------------------

def _hook():
    mod = sys.modules.get("sitecustomize")
    if not os.environ.get("VALET_TRAIL") or mod is None or not hasattr(mod, "_emit"):
        print("[valet] valet_view: not running under `valet py`; nothing shown", flush=True)
        return None
    return mod


def _emit_chart(spec, title, n, total, aggregated=False):
    hook = _hook()
    if hook is None:
        return spec
    spec.setdefault("$schema", "https://vega.github.io/schema/vega-lite/v6.json")
    if title and "title" not in spec:
        spec["title"] = title
    hook._emit({"kind": "artifact", "type": "chart", "title": title, "spec": _jsonable(spec),
                "rows_used": n, "rows_total": total, "aggregated": aggregated, "site": hook._call_site()})
    more = f" (aggregated from {total:,} rows)" if aggregated else \
        f" (first {n:,} of {total:,} rows)" if total and total > n else ""
    print(f"[valet] chart: {title or 'untitled'}{more}", flush=True)
    return spec


AGGS = {
    "sum": sum, "count": len, "min": min, "max": max,
    "mean": lambda v: sum(v) / len(v) if v else None,
    "median": lambda v: (sorted(v)[len(v) // 2] if len(v) % 2 else sum(sorted(v)[len(v) // 2 - 1:len(v) // 2 + 1]) / 2) if v else None,
}


def _preaggregate(data, kind, x, y, color, agg):
    """Exact counts / bins / aggregates over the full data, or None if the chart plots raw rows."""
    counting = kind == "hist" or (kind in ("bar", "line", "area", "point", "heatmap") and (y is None or agg))
    if kind == "heatmap":
        counting = color is None or agg is not None
    if not counting:
        return None
    if agg and agg not in AGGS:
        raise ValueError(f"valet_view.chart: agg must be one of {', '.join(AGGS)}")
    cols = _columns(data, [c for c in (x, y, color) if c])
    total = len(cols[x])
    if kind == "hist":
        vals = [v for v in cols[x] if isinstance(v, (int, float)) and not isinstance(v, bool)
                and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))]
        if not vals:
            return None
        distinct = set(vals)
        if all(float(v).is_integer() for v in distinct) and len(distinct) <= 40:   # one bar per value
            counts = {}
            for v in vals:
                counts[int(v)] = counts.get(int(v), 0) + 1
            rows = [{x: k, "count": n} for k, n in sorted(counts.items())]
            enc = {"x": {"field": x, "type": "ordinal", "sort": "ascending"},
                   "y": {"field": "count", "type": "quantitative"}}
        else:
            lo, hi = min(vals), max(vals)
            step = _nice_step((hi - lo) / 30 if hi > lo else 1)
            start = math.floor(lo / step) * step
            nbins = max(1, int(math.floor((hi - start) / step)) + 1)
            counts = [0] * nbins
            for v in vals:
                counts[min(nbins - 1, int((v - start) // step))] += 1
            rows = [{"bin_start": round(start + i * step, 10), "bin_end": round(start + (i + 1) * step, 10),
                     "count": n} for i, n in enumerate(counts)]
            enc = {"x": {"field": "bin_start", "type": "quantitative", "title": x, "bin": {"binned": True, "step": step}},
                   "x2": {"field": "bin_end"}, "y": {"field": "count", "type": "quantitative"}}
        enc["tooltip"] = [{"field": k, "type": "quantitative" if k != x else "ordinal"} for k in rows[0]]
        return {"mark": {"type": "bar", "tooltip": True}, "encoding": enc, "data": {"values": rows}}, total

    keys = [k for k in (x, color if kind != "heatmap" else y) if k] if kind != "heatmap" else [x, y]
    groups = {}
    for i in range(total):
        key = tuple(_value(cols[k][i]) for k in keys)
        groups.setdefault(key, []).append(cols[y][i] if (y and kind != "heatmap") else
                                          cols[color][i] if (kind == "heatmap" and color) else 1)
    fn = AGGS[agg or ("sum" if kind == "heatmap" and color else "count")]
    vname = (y if kind != "heatmap" else color) if agg or (kind == "heatmap" and color) else "count"
    rows = []
    for key, vals in groups.items():
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)
                and not (isinstance(v, float) and math.isnan(v))]
        rows.append({**dict(zip(keys, key)), vname: fn(nums) if fn is not len else len(vals)})
    types = _field_types(rows)
    enc = {"x": {"field": x, "type": "ordinal" if kind == "heatmap" else types.get(x, "nominal")}}
    if kind == "heatmap":
        enc["y"] = {"field": y, "type": "ordinal"}
        enc["color"] = {"field": vname, "type": "quantitative"}
        mark = {"type": "rect", "tooltip": True}
    else:
        enc["y"] = {"field": vname, "type": "quantitative", "title": f"{agg} of {y}" if agg else "count"}
        if color:
            enc["color"] = {"field": color, "type": types.get(color, "nominal")}
            if kind == "bar":   # side by side, not stacked (see above)
                enc["xOffset"] = {"field": color}
        mark = {"type": kind, "tooltip": True}
        if kind == "line":
            mark["point"] = len(rows) <= 200
    return {"mark": mark, "encoding": enc, "data": {"values": rows}}, total


def _nice_step(raw):
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _columns(data, names):
    """Full columns as Python lists (pandas, polars, dict of lists or list of dicts)."""
    mod = type(data).__module__.split(".")[0]
    if mod in ("polars", "pandas"):
        df = data
        if mod == "pandas":
            df = data.to_frame() if getattr(data, "ndim", 2) == 1 else data
            if type(df.index).__name__ != "RangeIndex" or df.index.name is not None:
                df = df.reset_index()
        missing = [n for n in names if n not in df.columns]
        if missing:
            raise ValueError(f"valet_view.chart: no column {missing[0]!r}; columns are {list(df.columns)}")
        return {n: (df[n].to_list() if mod == "polars" else df[n].tolist()) for n in names}
    if isinstance(data, dict):
        return {n: list(data[n]) for n in names}
    rows = list(data)
    if rows and any(n not in rows[0] for n in names):
        raise ValueError(f"valet_view.chart: no column {[n for n in names if n not in rows[0]][0]!r}")
    return {n: [r.get(n) for r in rows] for n in names}


def _records(data, max_rows):
    mod = type(data).__module__.split(".")[0]
    if mod == "polars":
        total = data.height
        rows = data.head(max_rows).to_dicts()
    elif mod == "pandas":
        df = data.to_frame() if getattr(data, "ndim", 2) == 1 else data
        if type(df.index).__name__ != "RangeIndex" or df.index.name is not None:
            df = df.reset_index()
        total = len(df)
        rows = df.head(max_rows).to_dict("records")
    elif isinstance(data, dict):  # dict of columns
        cols = list(data)
        total = len(data[cols[0]]) if cols else 0
        rows = [{c: data[c][i] for c in cols} for i in range(min(total, max_rows))]
    else:
        rows = list(data)
        total = len(rows)
        rows = rows[:max_rows]
    return [{str(k): _value(v) for k, v in r.items()} for r in rows], total


def _value(v):
    if v is None:
        return None
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if hasattr(v, "isoformat"):  # pandas Timestamp
        try:
            return v.isoformat()
        except Exception:
            pass
    if hasattr(v, "item"):  # numpy scalars
        try:
            return _value(v.item())
        except Exception:
            pass
    return str(v)


def _field_types(rows):
    types = {}
    for r in rows[:500]:
        for k, v in r.items():
            if v is None or types.get(k) == "nominal":
                continue
            if isinstance(v, bool) or isinstance(v, str) and not _looks_temporal(v):
                t = "nominal"
            elif isinstance(v, (int, float)):
                t = "quantitative"
            else:
                t = "temporal"
            if types.get(k) not in (None, t):
                t = "nominal"
            types[k] = t
    for r in rows[:1]:
        for k in r:
            types.setdefault(k, "nominal")
    return types


def _looks_temporal(s):
    return len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit()


def _default_title(kind, x, y, agg):
    if kind == "hist":
        return f"Distribution of {x}"
    if y is None:
        return f"Count by {x}"
    return f"{agg + ' of ' if agg else ''}{y} by {x}"


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))
