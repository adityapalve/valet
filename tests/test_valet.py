import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def valet(tmp_path, *args, stdin=None):
    env = dict(os.environ, VALET_HOME=str(tmp_path / ".valet"), VALET_PYTHON=sys.executable,
               PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "valet", *args], cwd=tmp_path, env=env,
                          input=stdin, capture_output=True, text=True, timeout=30)


def test_script_run_is_recorded_and_versioned(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text("print('hello')\n")
    r = valet(tmp_path, "py", "probe.py")
    assert r.returncode == 0
    assert "hello" in r.stdout and "[valet] run #1 · exit 0" in r.stdout

    script.write_text("import sys\nprint('v2')\nsys.exit(3)\n")
    r = valet(tmp_path, "py", "probe.py")
    assert r.returncode == 3  # exit code passes through

    show = valet(tmp_path, "show", "2").stdout
    assert "changes since run 1" in show
    assert "-print('hello')" in show and "+print('v2')" in show


def test_inline_and_heredoc_snippets_are_saved(tmp_path):
    valet(tmp_path, "py", "-c", "print(6*7)")
    valet(tmp_path, "py", "-", stdin="print('from stdin')\n")
    scratch = sorted((tmp_path / ".valet" / "scratch").glob("snippet-*.py"))
    assert [p.read_text() for p in scratch] == ["print(6*7)", "print('from stdin')\n"]
    assert "42" in valet(tmp_path, "show", "1").stdout


def test_note_is_delivered_once(tmp_path):
    valet(tmp_path, "note", "use the 2024 data only")
    first = valet(tmp_path, "run", "--", sys.executable, "-c", "print(1)").stdout
    assert "Message from the user" in first and "use the 2024 data only" in first
    second = valet(tmp_path, "run", "--", sys.executable, "-c", "print(1)").stdout
    assert "Message from the user" not in second


def test_missing_command(tmp_path):
    r = valet(tmp_path, "run", "--", "definitely-not-a-command-xyz")
    assert r.returncode == 127


def test_init_writes_rules(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop("VALET_HOME", None)
    subprocess.run([sys.executable, "-m", "valet", "init"], cwd=tmp_path, env=env, check=True,
                   capture_output=True)
    assert (tmp_path / ".amazonq" / "rules" / "valet.md").exists()
    assert ".valet/" in (tmp_path / ".gitignore").read_text()


def test_ui_api(tmp_path):
    valet(tmp_path, "py", "-c", "print('hi')")
    env = dict(os.environ, VALET_HOME=str(tmp_path / ".valet"), PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "valet", "ui", "--port", "8799", "--no-browser"],
                            cwd=tmp_path, env=env, stdout=subprocess.PIPE)
    try:
        proc.stdout.readline()  # wait for the startup line
        base = "http://127.0.0.1:8799"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        runs = json.load(opener.open(f"{base}/api/runs"))["runs"]
        assert runs[0]["id"] == 1
        detail = json.load(opener.open(f"{base}/api/runs/1"))
        assert "hi" in detail["output"] and detail["files"]
        req = urllib.request.Request(f"{base}/api/notes", data=b'{"text": "stop"}', method="POST")
        assert "id" in json.load(opener.open(req))
        assert "<title>valet</title>" in opener.open(base + "/").read().decode()
    finally:
        proc.terminate()
        proc.wait()


def _env_with_fake_uv(tmp_path):
    """PATH with a fake `uv` that just prints the arguments it was called with."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "uv"
    fake.write_text("#!/bin/sh\necho \"FAKE-UV $*\"\n")
    fake.chmod(0o755)
    env = dict(os.environ, VALET_HOME=str(tmp_path / ".valet"), PYTHONPATH=str(ROOT),
               PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    env.pop("VALET_PYTHON", None)
    return env


def _run(tmp_path, env, *args):
    return subprocess.run([sys.executable, "-m", "valet", *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30).stdout


def test_uv_runner_selection(tmp_path):
    env = _env_with_fake_uv(tmp_path)
    plain = tmp_path / "plain.py"
    plain.write_text("print('plain')\n")
    inline = tmp_path / "inline.py"
    inline.write_text('# /// script\n# dependencies = ["pandas"]\n# ///\nprint(1)\n')

    # inline deps -> uv run --script
    assert "FAKE-UV run --script inline.py" in _run(tmp_path, env, "py", "inline.py")
    # plain script outside a uv project -> regular python, uv not involved
    out = _run(tmp_path, env, "py", "plain.py")
    assert "plain" in out and "FAKE-UV" not in out
    # inside a uv project -> uv run python
    (tmp_path / "uv.lock").write_text("")
    assert "FAKE-UV run python plain.py" in _run(tmp_path, env, "py", "plain.py")
    # VALET_UV=0 opts out, VALET_PYTHON overrides everything
    assert "FAKE-UV" not in _run(tmp_path, dict(env, VALET_UV="0"), "py", "plain.py")
    assert "FAKE-UV" not in _run(tmp_path, dict(env, VALET_PYTHON=sys.executable), "py", "inline.py")


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
def test_real_uv_inline_script(tmp_path):
    env = dict(os.environ, VALET_HOME=str(tmp_path / ".valet"), PYTHONPATH=str(ROOT))
    env.pop("VALET_PYTHON", None)
    code = '# /// script\n# dependencies = []\n# ///\nimport sys\nprint("in", sys.prefix)\n'
    r = subprocess.run([sys.executable, "-m", "valet", "py", "-"], input=code, cwd=tmp_path,
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    show = subprocess.run([sys.executable, "-m", "valet", "show", "1"], cwd=tmp_path, env=env,
                          capture_output=True, text=True).stdout
    assert "uv run --script" in show and "in " in show


PIPELINE = '''import pandas as pd
events = pd.DataFrame({"event_id": [1, 1, 2, 3, 4], "user_id": [10, 10, 11, 12, 12]})
users = pd.DataFrame({"user_id": [10, 11, 12, 12], "region": ["N", "S", "E", "W"]})
# one row per event
events = events.drop_duplicates(subset=["event_id"])
events = events[events["event_id"] > 1]
# attach region
out = events.merge(users, on="user_id", how="left")
out.dropna(inplace=True)
print(out.groupby("region").size().to_dict())
'''


@pytest.mark.skipif(importlib.util.find_spec("pandas") is None, reason="pandas not installed")
def test_data_trail_records_pandas_steps(tmp_path):
    (tmp_path / "p.py").write_text(PIPELINE)
    r = valet(tmp_path, "py", "p.py")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dataframe steps recorded" in r.stdout
    assert "join fan-out" in r.stdout and "(p.py:8)" in r.stdout  # warning reaches the agent

    run = _run_json(tmp_path, 1)
    sources = [s for s in run["trail"] if s["op"] == "source"]
    steps = [s for s in run["trail"] if s["op"] != "source"]
    assert [s["op"] for s in steps] == ["drop_duplicates", "filter", "merge", "dropna", "groupby.size"]
    dedupe, filt, merge, dropna, _ = steps
    assert dedupe["params"]["subset"] == "['event_id']"
    assert dedupe["inputs"][0]["shape"] == [5, 2] and dedupe["output"] == [4, 2]
    assert dedupe["site"]["comment"] == "one row per event"
    assert filt["output"] == [3, 2] and filt["site"]["comment"] is None
    assert merge["output"] == [5, 3] and merge["cols_added"] == ["region"] and merge["warnings"]
    assert dropna["output"] == [5, 3]  # inplace is recorded via the mutated frame
    assert run["trail_warnings"] == 1


def test_trail_is_inert_without_pandas_and_chains_sitecustomize(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "sitecustomize.py").write_text("import builtins; builtins.CHAINED = True\n")
    env = dict(os.environ, VALET_HOME=str(tmp_path / ".valet"), VALET_PYTHON=sys.executable,
               PYTHONPATH=os.pathsep.join([str(ROOT), str(other)]))
    r = subprocess.run([sys.executable, "-m", "valet", "py", "-c",
                        "import builtins, sys; print('chained', getattr(builtins, 'CHAINED', False), "
                        "'pandas' in sys.modules)"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert "chained True False" in r.stdout  # existing sitecustomize ran; pandas not imported
    assert "dataframe steps" not in r.stdout


@pytest.mark.skipif(importlib.util.find_spec("pandas") is None, reason="pandas not installed")
def test_trail_can_be_turned_off(tmp_path):
    (tmp_path / "p.py").write_text(PIPELINE)
    env = dict(os.environ, VALET_HOME=str(tmp_path / ".valet"), VALET_PYTHON=sys.executable,
               PYTHONPATH=str(ROOT), VALET_TRAIL_OFF="1")
    r = subprocess.run([sys.executable, "-m", "valet", "py", "p.py"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "dataframe steps" not in r.stdout


def _run_json(tmp_path, run_id):
    sys.path.insert(0, str(ROOT))
    try:
        from valet.store import Store
        store = Store(tmp_path / ".valet")
        run = store.get_run(run_id)
        run["trail_warnings"] = store.list_runs(1)[0]["trail_warnings"]
        return run
    finally:
        sys.path.remove(str(ROOT))


@pytest.mark.skipif(importlib.util.find_spec("pandas") is None, reason="pandas not installed")
def test_lineage_tree_and_profiles(tmp_path):
    (tmp_path / "p.py").write_text(PIPELINE + 'out["big"] = out["event_id"] * 10\n')
    assert valet(tmp_path, "py", "p.py").returncode == 0
    trail = _run_json(tmp_path, 1)["trail"]
    by_out = {s["out_node"]: s for s in trail if s.get("out_node")}
    label = lambda nid: by_out[nid]["node"]["label"]

    # frames built in user code become labelled roots
    roots = [s for s in trail if s["op"] == "source"]
    assert sorted(s["node"]["label"] for s in roots) == ["events", "users"]

    merge = next(s for s in trail if s["op"] == "merge")
    left, right = merge["in_nodes"]
    assert by_out[left]["op"] == "filter" and label(left) == "events"
    assert by_out[right]["op"] == "source" and label(right) == "users"
    assert label(merge["out_node"]) == "out"

    # in-place ops and column assignment create new versions chained to the previous one
    dropna = next(s for s in trail if s["op"] == "dropna")
    assert dropna["in_nodes"] == [merge["out_node"]] and label(dropna["out_node"]) == "out"
    setcol = next(s for s in trail if s["op"] == "add column")
    assert setcol["params"]["column"] == "'big'" and setcol["cols_added"] == ["big"]
    assert setcol["in_nodes"] == [dropna["out_node"]]  # groupby read `out` but did not replace it

    prof = merge["node"]["profile"]
    assert prof["columns"] == ["event_id", "user_id", "region"]
    assert prof["nulls"] == [0, 0, 0] and len(prof["sample"]) == 5
