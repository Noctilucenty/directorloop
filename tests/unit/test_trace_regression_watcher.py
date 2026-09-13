import json
import sys

from scripts import watch_trace_regressions as watcher


def test_debounce_runs_initial_and_latest_stable_revision_only(tmp_path, monkeypatch):
    clock = [0.0]
    runs = []

    def snapshot():
        return {"engine.py": "A" if clock[0] < 2 else "B" if clock[0] < 4 else "C"}

    monkeypatch.setattr(watcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(watcher.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(watcher, "source_snapshot", snapshot)
    monkeypatch.setattr(watcher, "run_tests", lambda log, number, source, changed, timeout: runs.append(source))
    monkeypatch.setattr(watcher.signal, "signal", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["watch", "--log-dir", str(tmp_path), "--duration-seconds", "10"])
    watcher.main()
    assert runs == [{"engine.py": "A"}, {"engine.py": "C"}]
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[-1]["reason"] == "duration_complete"
    assert events[-1]["test_runs"] == 2


def test_file_add_delete_and_content_changes_are_detected(tmp_path):
    engine = tmp_path / "directorloop"
    engine.mkdir()
    file = engine / "guard.py"
    file.write_text("original")
    before = watcher.source_snapshot(tmp_path)
    file.write_text("changed")
    (engine / "new.py").write_text("new")
    after = watcher.source_snapshot(tmp_path)
    assert watcher.changed_paths(before, after) == ["directorloop/guard.py", "directorloop/new.py"]
    file.unlink()
    assert "directorloop/guard.py" in watcher.changed_paths(after, watcher.source_snapshot(tmp_path))


def test_logs_redact_environment_credentials_and_key_patterns(monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "private-test-value-0123")
    assert watcher.redact("private-test-value-0123 wandb_1234567890abcdefghijkl") == "[redacted] [redacted]"


def test_failed_test_run_is_preserved_without_network_or_retry(tmp_path, monkeypatch):
    root = tmp_path / "project"
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!{sys.executable}\nimport sys\nprint('deliberate failure')\nsys.exit(3)\n")
    python.chmod(0o700)
    log = tmp_path / "log"
    log.mkdir()
    monkeypatch.setattr(watcher, "ROOT", root)
    monkeypatch.setattr(watcher, "source_snapshot", lambda: {"guard.py": "sha"})
    result = watcher.run_tests(log, 1, {"guard.py": "sha"}, [], 5)
    assert result["passed"] is False
    assert result["exit_code"] == 3
    assert (log / "run-0001.log").read_text() == "deliberate failure\n"
    events = [json.loads(line) for line in (log / "events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["test_started", "test_finished"]
    assert events[-1]["passed"] is False
