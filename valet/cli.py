"""valet: make your coding agent's scratch scripts and terminal runs durable, visible and steerable.

The agent runs things through `valet` instead of straight in the terminal:

    valet py analysis.py --limit 10     # run a python script (snapshotted)
    valet py -c "import pandas; ..."    # inline code is saved to .valet/scratch/ first
    valet py - <<'EOF' ... EOF          # so is a heredoc
    valet run -- pytest -x tests/       # any other command

Output streams to the terminal exactly as before, so the agent's own terminal tool
still sees it. Meanwhile every run, the exact version of every script it touched and
the full output are recorded in .valet/valet.db, and `valet ui` shows them live.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .store import Store

RULES = """\
# Running code: use valet

The user watches every command you run through `valet` in a live viewer and may
send you instructions through it. Follow these rules for this project:

- Run Python with `valet py`, never bare `python`:
  - a script: `valet py path/to/script.py arg1 arg2`
  - a quick snippet: `valet py -c "..."`, or a heredoc: `valet py - <<'EOF' ... EOF`
- Run other commands you want the user to see (tests, data tools, CLIs) with
  `valet run -- <command> <args>`.
- Put throwaway scripts in `.valet/scratch/`. Edit and re-run the same file as
  you iterate instead of creating `script_v2.py`, `script_v3.py`; valet keeps
  every version.
- If a scratch script needs a package that isn't installed, do NOT `pip install`
  it. Declare it at the top of the script and valet will run it with uv in a
  cached environment:
  ```
  # /// script
  # dependencies = ["pandas", "pyarrow"]
  # ///
  ```
  (A script with this header runs isolated from the project's environment, so
  only use it for standalone scratch scripts.)
- Before each dataframe transformation (merge, filter, dedupe, groupby…), write a
  one-line `#` comment saying *why*, e.g. `# keep the latest event per user`.
  valet records every pandas step with its shapes and shows your comment next
  to it, so the user can follow how the data changed.
- If the output contains `[valet] Data warnings` (e.g. a join that multiplied
  rows), investigate and fix or explain it before moving on.
- Print compact output: `df.head()`, `df.shape`, `df.describe()`, never whole
  dataframes or files.
- If the output ends with a `[valet] Message from the user` block, treat it as a
  direct instruction from the user that takes priority over your current plan.
"""


def python_exe() -> str:
    return (
        os.environ.get("VALET_PYTHON")
        or shutil.which("python")
        or shutil.which("python3")
        or sys.executable
    )


def has_inline_deps(script: Path) -> bool:
    """True if the script declares PEP 723 inline metadata (`# /// script`)."""
    try:
        with script.open(encoding="utf-8") as fh:
            return any(line.rstrip() == "# /// script" for line in fh)
    except (OSError, UnicodeDecodeError):
        return False


def in_uv_project(start: Path) -> bool:
    cur = start.resolve()
    return any((d / "uv.lock").is_file() for d in (cur, *cur.parents))


def python_cmd(script: Path) -> list[str]:
    """How to launch a python script.

    $VALET_PYTHON wins if set. Otherwise, when uv is installed:
      - a script with PEP 723 inline deps runs as `uv run --script` (isolated, cached env)
      - inside a uv project (uv.lock found) it runs as `uv run python` (the project's .venv)
    and in every other case the `python` on PATH is used.
    """
    if os.environ.get("VALET_PYTHON"):
        return [os.environ["VALET_PYTHON"]]
    uv = shutil.which("uv")
    if uv and os.environ.get("VALET_UV", "1") != "0":
        if has_inline_deps(script):
            return [uv, "run", "--script"]
        if in_uv_project(Path.cwd()):
            return [uv, "run", "python"]
    return [python_exe()]


def execute(store: Store, argv: list[str], snapshot: list[Path] | None = None) -> int:
    """Run argv, tee output to our stdout, record everything. Returns the exit code."""
    cwd = os.getcwd()
    run_id = store.start_run(argv, cwd)

    # Snapshot explicit files plus any argument that is an existing text file.
    for p in snapshot or []:
        store.snapshot(run_id, p)
    for a in argv[1:]:
        if not a.startswith("-") and len(a) < 4096 and os.path.isfile(a):
            store.snapshot(run_id, Path(a))

    env = dict(os.environ, PYTHONUNBUFFERED="1", VALET_RUN_ID=str(run_id))
    if os.environ.get("VALET_TRAIL_OFF") != "1":
        # the hook dir holds a sitecustomize.py that records dataframe operations
        hook = str(Path(__file__).parent / "hook")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [hook, os.environ.get("PYTHONPATH")]))
        env["VALET_TRAIL"] = str(store.trail_path(run_id))
    chunks: list[str] = []
    out = sys.stdout.buffer
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except OSError as e:
        msg = f"valet: cannot run {argv[0]!r}: {e}\n"
        sys.stderr.write(msg)
        store.finish_run(run_id, 127, msg)
        return 127

    last_flush = time.monotonic()
    code: int
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            out.write(data)
            out.flush()
            chunks.append(data.decode("utf-8", errors="replace"))
            if time.monotonic() - last_flush > 1.0:  # live output for the viewer
                store.update_output(run_id, "".join(chunks))
                store.ingest_trail(run_id)
                last_flush = time.monotonic()
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
        chunks.append("\n[valet] interrupted\n")
        code = code if code else 130

    output = "".join(chunks)
    store.finish_run(run_id, code, output)
    steps = store.ingest_trail(run_id)
    store.trail_path(run_id).unlink(missing_ok=True)

    trailer = f"\n[valet] run #{run_id} · exit {code}"
    real = [st for st in steps if st["op"] != "source" and not st.get("minor")]
    if real:
        trailer += f" · {len(real)} dataframe steps recorded"
    warnings = [
        f"  - {w}  ({Path(st['site']['file']).name}:{st['site']['line']})" if st.get("site") else f"  - {w}"
        for st in steps for w in st.get("warnings") or []
    ]
    if warnings:
        trailer += "\n[valet] Data warnings (check these before continuing):\n" + "\n".join(warnings[:10])
    notes = store.take_notes(run_id)
    if notes:
        trailer += "\n[valet] Message from the user (they are watching this run):\n"
        trailer += "".join(f"  - {n}\n" for n in notes)
    out.write((trailer.rstrip("\n") + "\n").encode())
    out.flush()
    return code


def cmd_py(store: Store, args: list[str]) -> int:
    if not args:
        sys.exit("usage: valet py SCRIPT [ARGS...] | valet py -c CODE | valet py - < code")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args[0] in ("-c", "-"):
        if args[0] == "-c":
            if len(args) < 2:
                sys.exit("valet py -c: missing code")
            code, rest = args[1], args[2:]
        else:
            code, rest = sys.stdin.read(), args[1:]
        n = 1
        script = store.home / "scratch" / f"snippet-{stamp}-{n}.py"
        while script.exists():
            n += 1
            script = store.home / "scratch" / f"snippet-{stamp}-{n}.py"
        script.write_text(code, encoding="utf-8")
        print(f"[valet] saved snippet to {script}", flush=True)
        return execute(store, [*python_cmd(script), str(script), *rest], snapshot=[script])
    return execute(store, [*python_cmd(Path(args[0])), *args])


def cmd_run(store: Store, args: list[str]) -> int:
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        sys.exit("usage: valet run -- COMMAND [ARGS...]")
    return execute(store, args)


def cmd_note(store: Store, text: str) -> int:
    nid = store.add_note(text)
    print(f"queued note #{nid}; it will be shown to the agent at the end of its next run")
    return 0


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def cmd_log(store: Store, limit: int) -> int:
    for r in reversed(store.list_runs(limit)):
        status = "…" if r["exit_code"] is None else r["exit_code"]
        dur = f"{r['duration']:.1f}s" if r["duration"] is not None else ""
        cmd = " ".join(r["argv"])
        print(f"#{r['id']:<4} {fmt_time(r['started'])}  exit {status!s:<3} {dur:>7}  {cmd[:100]}")
    return 0


def cmd_show(store: Store, run_id: int | None, full: bool) -> int:
    if run_id is None:
        runs = store.list_runs(1)
        if not runs:
            sys.exit("no runs recorded yet")
        run_id = runs[0]["id"]
    r = store.get_run(run_id)
    if r is None:
        sys.exit(f"no run #{run_id}")
    print(f"# run {r['id']}  {fmt_time(r['started'])}  exit {r['exit_code']}  in {r['cwd']}")
    print(f"$ {' '.join(r['argv'])}\n")
    for f in r["files"]:
        if f["diff"] and not full:
            print(f"--- {f['path']} (changes since run {f['prev_run']})")
            print(f["diff"])
        elif f["diff"] == "" and not full:
            print(f"--- {f['path']} (unchanged since run {f['prev_run']})\n")
        else:
            print(f"--- {f['path']}")
            print(f["content"])
    if r["trail"]:
        print("--- data trail")
        for st in r["trail"]:
            print("  " + format_step(st))
        print()
    print("--- output" + (" (truncated)" if r["truncated"] else ""))
    print(r["output"])
    for n in r["notes"]:
        print(f"--- note delivered: {n['text']}")
    return 0


def fmt_shape(shape) -> str:
    if not shape:
        return "?"
    return " × ".join(f"{n:,}" for n in shape)


def format_step(st: dict) -> str:
    node = st.get("node") or {}
    if st["op"] == "source":
        return f"source {node.get('label', '?')}  {fmt_shape(st.get('output'))}"
    params = ", ".join(f"{k}={v}" for k, v in (st.get("params") or {}).items())
    ins = " + ".join(fmt_shape(i.get("shape")) for i in st.get("inputs") or [])
    line = f"{st['op']}({params})  {ins + ' → ' if ins else ''}{fmt_shape(st.get('output'))}"
    if st.get("cols_added"):
        line += f"  +cols {st['cols_added']}"
    if st.get("cols_removed"):
        line += f"  -cols {st['cols_removed']}"
    if node.get("label") and not node.get("sink"):
        line += f"  => {node['label']}"
    if st.get("site"):
        line += f"  @{Path(st['site']['file']).name}:{st['site']['line']}"
    for w in st.get("warnings") or []:
        line += f"\n    ! {w}"
    return line


def cmd_init() -> int:
    root = Path.cwd()
    store = Store(root / ".valet")
    rules = root / ".amazonq" / "rules" / "valet.md"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text(RULES, encoding="utf-8")
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if ".valet/" not in lines:
        with gi.open("a") as fh:
            fh.write(("\n" if lines and lines[-1] else "") + ".valet/\n")
    print(f"created {store.home}/ and {rules.relative_to(root)}")
    print("start the viewer with: valet ui")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `py` and `run` pass everything after them through untouched.
    if argv and argv[0] in ("py", "run"):
        store = Store()
        return (cmd_py if argv[0] == "py" else cmd_run)(store, argv[1:])

    p = argparse.ArgumentParser(prog="valet", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("py", help="run a python script / snippet through valet")
    sub.add_parser("run", help="run any command through valet: valet run -- CMD ...")
    sub.add_parser("init", help="set up .valet/ and Amazon Q rules in this project")
    n = sub.add_parser("note", help="queue a message for the agent's next run")
    n.add_argument("text")
    lg = sub.add_parser("log", help="list recorded runs")
    lg.add_argument("-n", type=int, default=30)
    sh = sub.add_parser("show", help="show a run: script diffs and output")
    sh.add_argument("id", type=int, nargs="?")
    sh.add_argument("--full", action="store_true", help="full scripts instead of diffs")
    ui = sub.add_parser("ui", help="start the live viewer")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "init":
        return cmd_init()
    store = Store()
    if a.cmd == "note":
        return cmd_note(store, a.text)
    if a.cmd == "log":
        return cmd_log(store, a.n)
    if a.cmd == "show":
        return cmd_show(store, a.id, a.full)
    if a.cmd == "ui":
        from .ui import serve

        return serve(store, a.port, open_browser=not a.no_browser)
    return 1


if __name__ == "__main__":
    sys.exit(main())
