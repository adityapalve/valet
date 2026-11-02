import json
import os
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
