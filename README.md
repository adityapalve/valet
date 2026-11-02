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
- **A live viewer**: `valet ui` serves a local page showing the run timeline, script
  diffs, streaming output and queued notes.

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

## Use with Amazon Q (or any agent with a terminal tool)

In your project:

```bash
valet init     # creates .valet/, adds it to .gitignore, writes .amazonq/rules/valet.md
valet ui       # opens the viewer at http://127.0.0.1:8765
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
| `valet ui [--port N]` | live viewer |

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
- **Optional MCP front-end** over the same store, for agents/IDEs where MCP is available.
- **Workspace diffs**: also record files the scripts *wrote*, not just the ones they ran.
  Zed's DeltaDB (operation-level history, private beta as of late 2026) is worth watching here.
