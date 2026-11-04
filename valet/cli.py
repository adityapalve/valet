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

from .hook.valet_rowdiff import summary as rowdiff_summary
from .outputs import derive_title, human_size, parse_error, scan_outputs, tidy_snippet
from .store import Store, find_home, read_trail_file, trail_file

RULES = """\
# Running code: use valet

The user watches every command you run through `valet` in a live viewer and may
send you instructions through it. Follow these rules for this project:

- Run Python with `valet py`, never bare `python`:
  - a script: `valet py path/to/script.py arg1 arg2`
  - a quick snippet: `valet py -c "..."`, or pipe the code in: in bash
    `valet py - <<'EOF' ... EOF`, in PowerShell `@' ... '@ | valet py -`
- Give every run a short title saying what it's for, so the user can follow
  along: `valet py --title "Clean survey data" script.py`. Anything longer than one
  or two statements belongs in a script file in `.valet/scratch/`, one statement
  per line, not a long `-c` one-liner joined with `;`.
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
  valet records every pandas and polars step with its shapes and shows your comment next
  to it, so the user can follow how the data changed.
- If the output contains `[valet] Data warnings` (e.g. a join that multiplied
  rows), investigate and fix or explain it before moving on.
- To show the user a chart, use valet's chart helper instead of writing HTML or
  saving image files (nothing to install; works in any `valet py` run):
  ```
  import valet_view as vv
  vv.chart(df, x="month", y="revenue", kind="line", color="region", title="Revenue by month")
  vv.chart(df, x="minutes", kind="hist")          # also: bar, point, area, box, heatmap
  vv.vega({...vega-lite spec...}, data=df)        # anything custom
  vv.table(df, "Top 20 users")                    # a titled table
  vv.key("output/clean.csv", ["id"])              # how to match rows between runs
  ```
  `df` can be pandas, polars or a list of dicts. Aggregate first: charts use at
  most 5,000 rows. Each call prints `[valet] chart: <title>` when it worked.
- When a decision really depends on the user's judgment (which records to exclude,
  which of two plausible definitions to use), ask instead of guessing:
  `if vv.ask("Drop the 2007 season?", ["yes", "no"], default="no") == "yes": ...`
  It shows the question in the viewer and waits (up to 2 minutes) only if the user
  is watching; otherwise it returns the default at once. Always give a sensible
  default, ask at most a couple of questions per run, and never ask inside a loop.
  The output says whether the user answered or the default was used.
- When you write an output file, the run summary says how its rows changed since
  the last run, e.g. `(+12 -3 ~41 rows vs run #7)`. If that's not what you
  intended, say so and investigate before moving on.
- Print compact output: `df.head()`, `df.shape`, `df.describe()`, never whole
  dataframes or files. The user sees printed and written dataframes as tables
  in the viewer (up to 200 rows), so `print(df.head(20))` is plenty.
- If the output ends with a `[valet] Message from the user` block, treat it as a
  direct instruction from the user that takes priority over your current plan.
- The viewer (`valet ui`) is for the user. Don't start or stop it unless they ask;
  if they do, `valet ui` returns immediately (it runs in the background) and
  `valet ui --stop` stops it.
- Tracebacks may include frames in `valet/hook/sitecustomize.py` (`wrapper`):
  that's valet recording the call, not the cause. Look at your own frame above it.
- Never delete or edit files inside `.valet/`. If valet prints `[valet] recording
  unavailable`, your command still ran normally; run `valet doctor` and tell the
  user what it reports. Don't try to kill processes or remove lock files.
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


class Recorder:
    """Best-effort recording around a command. A database problem (e.g. another process
    holding a lock) is reported once and recording stops for the rest of the run, but the
    command itself always runs, its output always streams, and its exit code passes through.
    """

    def __init__(self, home: Path | None = None):
        self.error: str | None = None
        self.store: Store | None = None
        timeout = float(os.environ.get("VALET_DB_TIMEOUT", "5"))
        try:
            self.store = Store(home, timeout=timeout)
            self.home = self.store.home
        except Exception as e:  # noqa: BLE001 - recording must never break the command
            self.home = home or find_home()
            (self.home / "scratch").mkdir(parents=True, exist_ok=True)
            self._fail(e)

    def _fail(self, e: Exception) -> None:
        if self.error is None:
            self.error = f"{type(e).__name__}: {e}"
            sys.stderr.write(f"[valet] recording unavailable ({self.error}); the command runs "
                             f"normally but is not recorded. Run `valet doctor` for details.\n")
            sys.stderr.flush()

    def call(self, method: str, *args, default=None):
        if self.store is None or self.error:
            return default
        try:
            return getattr(self.store, method)(*args)
        except Exception as e:  # noqa: BLE001
            self._fail(e)
            return default


def _write_out(text: str) -> None:
    """Write text to stdout as bytes in the terminal's encoding, never raising."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        sys.stdout.buffer.write(text.encode(enc, errors="replace"))
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        _silence_stdout()


def _silence_stdout() -> None:
    """The reader went away (e.g. `| head`): send further output, and the final flush, nowhere."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:
        pass


def execute(rec: Recorder, argv: list[str], snapshot: list[Path] | None = None,
            title: str | None = None) -> int:
    """Run argv, tee output to our stdout, record what we can. Returns the exit code."""
    cwd = os.getcwd()
    started = time.time()
    run_id = rec.call("start_run", argv, cwd, title)

    # Snapshot explicit files plus any argument that is an existing text file.
    if run_id is not None:
        for p in snapshot or []:
            rec.call("snapshot", run_id, p)
        for a in argv[1:]:
            if not a.startswith("-") and len(a) < 4096 and os.path.isfile(a):
                rec.call("snapshot", run_id, Path(a))

    trail = trail_file(rec.home, run_id if run_id is not None else f"unrecorded-{os.getpid()}")
    env = dict(os.environ, PYTHONUNBUFFERED="1", VALET_RUN_ID=str(run_id or ""), VALET_DIR=str(rec.home))
    if os.environ.get("VALET_TRAIL_OFF") != "1":
        # the hook dir holds a sitecustomize.py that records dataframe operations
        hook = str(Path(__file__).parent / "hook")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [hook, os.environ.get("PYTHONPATH")]))
        env["VALET_TRAIL"] = str(trail)
    chunks: list[str] = []
    out = sys.stdout.buffer
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except OSError as e:
        msg = f"valet: cannot run {argv[0]!r}: {e}\n"
        sys.stderr.write(msg)
        if run_id is not None:
            rec.call("finish_run", run_id, 127, msg)
        return 127

    last_flush = time.monotonic()
    reader_gone = False
    code: int
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            if not reader_gone:
                try:
                    out.write(data)
                    out.flush()
                except (BrokenPipeError, OSError):   # e.g. `valet py x.py | head`: keep recording
                    reader_gone = True
                    _silence_stdout()
            chunks.append(data.decode("utf-8", errors="replace"))
            if run_id is not None and time.monotonic() - last_flush > 1.0:  # live view
                rec.call("update_output", run_id, "".join(chunks))
                rec.call("ingest_trail", run_id)
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
    records = read_trail_file(trail)
    steps = [r for r in records if r.get("kind") != "artifact"]
    captured = [r for r in records if r.get("kind") == "artifact"]
    notes: list[str] = []
    outputs: list[dict] = []
    if run_id is not None:
        rec.call("finish_run", run_id, code, output)
        if code != 0:
            rec.call("set_error", run_id, parse_error(output))
        rec.call("ingest_trail", run_id)
        # files the run created or changed that the hook didn't already capture
        if os.environ.get("VALET_SCAN_OUTPUTS", "1") != "0":
            known = {a["path"] for a in captured if a.get("path")} | {str(p.resolve()) for p in snapshot or []}
            outputs = scan_outputs(cwd, started, known, valet_dir=str(rec.home), run_id=run_id)
            if outputs:
                rec.call("set_artifacts", run_id, outputs, ("file", "image", "html"))
        notes = rec.call("take_notes", run_id, default=[])
    trail.unlink(missing_ok=True)

    # Plain ASCII from here on: the agent reads this through whatever encoding its terminal has.
    trailer = f"\n[valet] run #{run_id} | exit {code}" if run_id is not None and not rec.error \
        else f"\n[valet] exit {code} | not recorded (run `valet doctor`)"
    real = [st for st in steps if st["op"] != "source" and not st.get("minor")]
    if real:
        trailer += f" | {len(real)} dataframe steps recorded"
    written = [a for a in captured if a.get("type") == "write" and a.get("path")] + \
              [a for a in outputs if a["type"] in ("file", "image", "html")]
    if written:
        def describe(a: dict) -> str:
            name = os.path.relpath(a["path"], cwd) if a["path"].startswith(cwd) else a["path"]
            if a.get("shape"):
                change = rowdiff_summary(a.get("diff"))
                return f"{name} ({a['shape'][0]:,} x {a['shape'][1]}{'; ' + change if change else ''})"
            return f"{name} ({human_size(a['size'])})" if a.get("size") else name
        shown = ", ".join(describe(a) for a in written[:4])
        trailer += f"\n[valet] outputs: {shown}" + (f" and {len(written) - 4} more" if len(written) > 4 else "")
    warnings = [
        f"  - {w}  ({Path(st['site']['file']).name}:{st['site']['line']})" if st.get("site") else f"  - {w}"
        for st in steps for w in st.get("warnings") or []
    ]
    if warnings:
        trailer += "\n[valet] Data warnings (check these before continuing):\n" + "\n".join(warnings[:10])
    if notes:
        trailer += "\n[valet] Message from the user (they are watching this run):\n"
        trailer += "".join(f"  - {n}\n" for n in notes)
    _write_out(trailer.rstrip("\n") + "\n")
    return code


def _take_title(args: list[str]) -> tuple[str | None, list[str]]:
    """Pull a leading `--title TEXT` (or `--title=TEXT`) off the argument list."""
    if args and args[0] == "--title" and len(args) > 1:
        return args[1], args[2:]
    if args and args[0].startswith("--title="):
        return args[0].split("=", 1)[1], args[1:]
    return None, args


def cmd_py(store: Recorder, args: list[str]) -> int:
    title, args = _take_title(args)
    if not args:
        sys.exit("usage: valet py [--title TEXT] SCRIPT [ARGS...] | valet py -c CODE | valet py - < code")
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
        # one statement per line, so the snippet is readable in the viewer (same program, checked)
        script.write_text(tidy_snippet(code), encoding="utf-8")
        print(f"[valet] saved snippet to {script}", flush=True)
        title = title or derive_title(script, is_snippet=True)
        return execute(store, [*python_cmd(script), str(script), *rest], snapshot=[script], title=title)
    first = Path(args[0])
    title = title or (derive_title(first, is_snippet=False) if first.suffix == ".py" else None)
    return execute(store, [*python_cmd(first), *args], title=title)


def cmd_run(store: Recorder, args: list[str]) -> int:
    title, args = _take_title(args)
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        sys.exit("usage: valet run [--title TEXT] -- COMMAND [ARGS...]")
    return execute(store, args, title=title)


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
    return " x ".join(f"{n:,}" for n in shape)


def format_step(st: dict) -> str:
    node = st.get("node") or {}
    if st["op"] == "source":
        return f"source {node.get('label', '?')}  {fmt_shape(st.get('output'))}"
    params = ", ".join(f"{k}={v}" for k, v in (st.get("params") or {}).items())
    ins = " + ".join(fmt_shape(i.get("shape")) for i in st.get("inputs") or [])
    line = f"{st['op']}({params})  {ins + ' -> ' if ins else ''}{fmt_shape(st.get('output'))}"
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


def cmd_doctor() -> int:
    import platform

    from . import __version__
    from .ui import ping, read_info

    print(f"valet {__version__} on {platform.python_implementation()} {platform.python_version()} ({sys.executable})")
    home = find_home()
    if not home.exists():
        print(f"store:    {home} does not exist yet (run `valet init` in your project)")
        return 0
    print(f"store:    {home}")
    ok = True
    try:
        store = Store(home, timeout=2)
        note = "" if store.journal_mode == "wal" else " (WAL unavailable on this drive; works, with more waiting)"
        print(f"database: journal mode {store.journal_mode}{note}")
        if store.can_write(2):
            print("database: OK, writable")
        else:
            ok = False
            print("database: LOCKED - another process is holding the write lock")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"database: cannot open ({type(e).__name__}: {e})")
    info = read_info(home)
    if ping(info, home):
        print(f"viewer:   running on http://127.0.0.1:{info['port']}/ (pid {info['pid']})")
    elif info:
        print(f"viewer:   recorded as pid {info.get('pid')} but not responding (stale record)")
    else:
        print("viewer:   not running")
    if not ok:
        print("""
What to do:
  - If a viewer is running, stop it: valet ui --stop
  - A viewer started by valet older than 0.4 can hold the lock until it exits, and
    may not be stoppable that way. End that python process, or log out / restart.
    Newer viewers never hold the lock and stop by themselves when idle.
  - Don't delete files in .valet/ while a process has them open.""")
    return 0 if ok else 1


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
    for stream in (sys.stdout, sys.stderr):  # e.g. a cp1252 pipe on Windows: never crash on output
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    # `py` and `run` pass everything after them through untouched.
    if argv and argv[0] in ("py", "run"):
        rec = Recorder()
        return (cmd_py if argv[0] == "py" else cmd_run)(rec, argv[1:])

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
    ui = sub.add_parser("ui", help="start the live viewer in the background (or --stop / --status)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--stop", action="store_true", help="stop the viewer for this project")
    ui.add_argument("--status", action="store_true", help="is the viewer running?")
    ui.add_argument("--foreground", action="store_true", help="run in this terminal (ctrl-c to stop)")
    ui.add_argument("--idle", type=float, default=30.0,
                    help="minutes without requests before the viewer exits by itself (0 = never)")
    sub.add_parser("doctor", help="check the database and viewer, and explain any problem")
    a = p.parse_args(argv)

    if a.cmd == "init":
        return cmd_init()
    if a.cmd == "doctor":
        return cmd_doctor()
    if a.cmd == "ui":
        from . import ui as viewer

        if a.stop:
            return viewer.stop(find_home())
        if a.status:
            return viewer.status(find_home())
        store = Store()
        if a.foreground:
            return viewer.serve(store, a.port, open_browser=not a.no_browser, idle=a.idle)
        return viewer.start(store.home, a.port, open_browser=not a.no_browser, idle=a.idle)
    store = Store()
    if a.cmd == "note":
        return cmd_note(store, a.text)
    if a.cmd == "log":
        return cmd_log(store, a.n)
    if a.cmd == "show":
        return cmd_show(store, a.id, a.full)
    return 1


if __name__ == "__main__":
    sys.exit(main())
