# valet

Coding agents like Amazon Q love to write a throwaway script, run it in the terminal,
tweak it, and run it again. Everything in that loop is ephemeral: the script gets
overwritten, the output scrolls away, and you can't redirect the agent until its turn ends.

**valet** is a tiny wrapper the agent runs its code *through*. Output still streams to
the terminal exactly as before, so the agent's own terminal tool sees it, and on top of that:

- **Every run is recorded**: command, working directory, exit code, duration, full output.
- **Every script version is kept**: each run snapshots the files it executed, so you can
  diff run #7's `inspect.py` against run #6's even though the agent overwrote it.
- **Inline code becomes durable**: `valet py -c "..."` and heredocs are saved to
  `.valet/scratch/` before they run.
- **You can steer**: type a message in the viewer (or `valet note "..."`), and it is appended
  to the output of the agent's *next* run as `[valet] Message from the user`. The agent's
  rules tell it to treat that as a direct instruction.
- **A transformation tree for pandas**: every DataFrame transformation is recorded with its shape
  before and after, the keys it used, the line of code, and the `#` comment above it explaining
  why, and linked into a tree of which frame came from which. Click any node for a Data
  Wrangler-style preview (column types, % missing, sample rows). Joins that multiply rows are
  flagged, to you *and* to the agent.
- **A live viewer**: `valet ui` serves a local page showing the run timeline, script
  diffs, the data trail, streaming output and queued notes.

![viewer](docs/viewer.png)

It has zero dependencies (stdlib Python ≥ 3.9, SQLite) and makes no network calls apart from
the viewer on `127.0.0.1`, so it works behind a corporate VPN or proxy.

## Install

```bash
uv tool install git+https://github.com/adityapalve/valet
```

This puts `valet` on your PATH in its own isolated environment, so the agent's terminal finds it
whatever venv is active. (`pipx install …` or `pip install …` work too. For a private repo use
`git+ssh://git@github.com/adityapalve/valet`.) Upgrade with `uv tool upgrade valet`.

### No install: run from a clone

valet only uses the standard library, so nothing needs building. This is the route to use if
your network or endpoint policy blocks installers (e.g. uv or pip building wheels in a temp folder):

```bash
git clone git@github.com:adityapalve/valet.git ~/tools/valet
```

Then add the clone's `bin/` folder to your `PATH`. It has `valet` (macOS, Linux, Git Bash) and
`valet.cmd` (Windows cmd and PowerShell); both run `bin/_launch.py` with any Python ≥ 3.9 on your
PATH (`VALET_LAUNCH_PYTHON` overrides it). To update, `git pull`.

## Use with Amazon Q (or any agent with a terminal tool)

In your project:

```bash
valet init     # creates .valet/, adds it to .gitignore, writes .amazonq/rules/valet.md
valet ui       # starts the viewer in the background and opens http://127.0.0.1:8765
```

The rules file tells the agent to run Python as `valet py ...` and other commands as
`valet run -- ...`, keep scratch scripts in `.valet/scratch/` and edit them in place rather than
making `_v2` copies, keep output compact, and obey `[valet]` messages. For agents other than Q,
paste the same text (`valet/cli.py: RULES`) into their rules or `AGENTS.md`.

| command | what it does |
| --- | --- |
| `valet py script.py args…` | run a Python script (snapshotted) |
| `valet py -c "code"` / `valet py - <<'EOF'` | save the snippet to `.valet/scratch/`, then run it |
| `valet run -- cmd args…` | run any command; arguments that are files get snapshotted |
| `valet note "text"` | queue a message for the agent's next run |
| `valet log` / `valet show [id] [--full]` | browse runs from the terminal |
| `valet ui` | start the live viewer in the background (running it again just prints the URL) |
| `valet ui --stop` / `--status` | stop the viewer / check whether it's running |
| `valet doctor` | check the database and viewer, and say what to do about any problem |

### Data trail and transformation tree

When a script run through valet imports pandas, valet records each transformation, with no
change to the code, and draws them as a tree. Each node is a version of a DataFrame, named after
the variable it was assigned to (`events v1 → v2 → v3`). Each edge is the operation that
produced it, and merges draw a dashed edge from the second input:

![data trail](docs/trail.png)

- **Recorded:** `read_*` / `to_*`, `merge` / `join` / `concat`, `drop_duplicates`, `dropna`,
  `drop`, `query`, boolean-mask filters (`df[mask]`, `df.loc[mask]`), `assign`, `rename`,
  `fillna`, `pivot` / `pivot_table` / `melt` / `explode`, column assignment (`df["x"] = …`), and
  groupby aggregations. `reset_index`, `sort_values`, `astype`, `copy` and similar are recorded
  as small "minor" nodes so the tree stays connected. Each step
  stores its parameters (dedupe `subset`, join `on` / `how`, groupby keys…), input and output
  shapes, columns added and removed, and the calling line.
- **The why:** the rules ask the agent to put a one-line `#` comment above each
  transformation. valet shows that comment as the step's description, so the trail reads like
  "one row per event_id: 5,300 → 5,000 rows".
- **Lineage:** frames are tracked by object identity (weak references; the frames themselves are
  never modified), so a step's inputs link to the nodes that produced them. Frames valet didn't
  see being created (e.g. built with `pd.DataFrame(...)`) become root nodes named after their
  variable.
- **Previews:** each node stores the first 5 rows, column dtypes and null counts (≤40 columns;
  nulls skipped above 5M cells). This is a copy of your data in `.valet/` (which `valet init`
  gitignores). Set `VALET_TRAIL_PROFILE=0` to record shapes only.
- **Warnings:** a left or inner join that *adds* rows (duplicate keys on the right), an inner join
  that drops rows, concat inputs with mismatched columns, or a step that empties the frame. These
  are also printed at the end of the run as `[valet] Data warnings`, so the agent sees them too.
- **How:** valet puts a stdlib-only `sitecustomize.py` on the child's `PYTHONPATH`. It patches
  pandas only once the script imports it, records only top-level calls (not pandas' internal
  ones), chains to any existing `sitecustomize`, and never raises into your code. It also works
  under `uv run --script`. Overhead is a shape lookup per call. Turn it off with
  `VALET_TRAIL_OFF=1`.

### Which Python runs the script (and where packages come from)

valet itself has no dependencies. The scripts it runs get their packages from:

1. **`$VALET_PYTHON`**, if set: always used.
2. **PEP 723 inline dependencies** (when uv is installed): a script with a header like
   ```python
   # /// script
   # dependencies = ["pandas", "pyarrow"]
   # ///
   ```
   runs as `uv run --script`, in an isolated environment that uv builds once and caches. The
   rules tell the agent to do this instead of `pip install`, so the dependencies are part of the
   script, and valet's snapshot of the script records them too.
3. **A uv project** (a `uv.lock` above the current directory): scripts run with `uv run python`,
   so they use the project's synced `.venv`.
4. Otherwise, **`python` / `python3` on `PATH`**, so an active venv is respected.

Set `VALET_UV=0` to turn off the uv behaviour. `$VALET_HOME` overrides where the `.valet/` store lives.

## Viewer lifecycle and troubleshooting

- `valet ui` returns immediately: the server runs as a background process. It's safe to run
  from an agent's terminal, and running it twice reuses the viewer that's already up.
- The viewer **stops by itself** after 30 minutes without requests (`--idle N` to change, `0`
  for never). An open tab keeps it alive.
- `valet ui --stop` asks the viewer to shut itself down over HTTP, so it works even if the process
  was started in a context you aren't allowed to kill (e.g. an agent's sandboxed terminal).
  `valet ui --foreground` runs it attached to your terminal instead (ctrl-c stops it).
- **Recording never blocks your command.** If the database can't be written within a few seconds
  (`VALET_DB_TIMEOUT`, default 5), valet prints `[valet] recording unavailable`, runs the command
  anyway with output and exit code intact, and stops recording for that run.
- `valet doctor` reports the Python in use, the database's journal mode and whether it's writable,
  and the viewer's state, with next steps if something's wrong.

**`database is locked` with valet < 0.4:** older versions kept a database connection open and
relied on CPython finishing a query as soon as its result was dropped. Under PyPy (and in some
error paths) queries stayed open, so the viewer held a write lock for as long as it ran. Since
0.4, every operation uses its own short-lived connection, reads every query to the end, and
closes it. The test suite checks this under PyPy too. If an old viewer is still running, end
that python process (or log out), then update.

## Design notes

**Why a CLI wrapper rather than an MCP server?** The agent's built-in terminal tool can't be
hooked from outside, and an MCP server depends on the IDE extension allowing MCP (it may be
disabled by org policy). Every agent can run a shell command, so a wrapper plus a rules file
works everywhere, today.

**Why notes arrive at the end of a run.** You can't inject text into the agent's chat mid-turn,
but a tool result is something it is guaranteed to read. Attaching steering notes to the next
command's output is the most reliable channel available.

## Roadmap

- **Persistent kernel mode**: `valet py --kernel script.py` executes in a long-lived IPython
  kernel (via `jupyter_client` over ZMQ, not a Jupyter server), so state like a loaded dataframe
  survives between runs, and you can attach to the same kernel yourself
  (`jupyter console --existing`). Rich outputs (tables, plots) would go to the viewer while the
  agent gets a text summary.
- **Kernel helpers ("code mode")**: preload `h.peek(df)`, `h.plot(...)`, `h.ask("…")` into the
  kernel so the agent reaches the viewer through code.
- **Approve gate**: optionally hold a run until you click Run in the viewer, or edit the
  script before it runs (must stay inside the agent's command timeout).
- **Replay ("durable" state)**: valet already stores every script version in run order, so a
  crashed or reset session (or kernel) can be rebuilt with `valet replay --until 12`. Optionally,
  checkpoint DataFrames to parquet at chosen steps so replay can resume from there.
- **Data trail, more**: polars support; null-rate and key-uniqueness checks on join keys;
  `df.loc[mask, col] = …` updates; collapsing long runs of column assignments; a tree that spans
  runs (follow `report.csv` from the script that wrote it into the one that reads it).
- **Optional MCP front-end** over the same store, for agents/IDEs where MCP is available.
- **Workspace diffs**: also record files the scripts *wrote*, not just the ones they ran.
  Zed's DeltaDB (operation-level history, private beta as of late 2026) is worth watching here.
