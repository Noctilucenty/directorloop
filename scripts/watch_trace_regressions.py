"""Bounded, debounced local regression watcher. Never runs model inference.

Runs an initial check and reruns after Python engine/fixture/test changes. Each
result is retained in append-only JSONL plus a redacted pytest output file.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOCUSED_TESTS = ("tests/unit/test_weave_trace_regression.py", "tests/unit/test_weave_online_checks.py")
EXTRA_INPUTS = (*FOCUSED_TESTS, "tests/fixtures/trace_regression_v1.json", "pyproject.toml",
                "scripts/weave_trace_regression.py", "scripts/weave_online_checks.py",
                "scripts/watch_trace_regressions.py", "scripts/regression_offline_pytest.py")


def source_snapshot(root=ROOT):
    paths = set((root / "directorloop").rglob("*.py"))
    paths.update(root / name for name in EXTRA_INPUTS)
    result = {}
    for path in sorted(paths):
        try:
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            result[str(path.relative_to(root))] = "missing"
    return result


def fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def changed_paths(before, after):
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def redact(text):
    for name, value in os.environ.items():
        if len(value) >= 8 and re.search(r"key|token|secret|password|credential", name, re.I):
            text = text.replace(value, "[redacted]")
    return re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|wandb_[A-Za-z0-9_-]{16,}|apikey_[A-Za-z0-9_-]{16,})", "[redacted]", text)


def append_event(path, event):
    row = {"at": datetime.now(UTC).isoformat(), "watcher_pid": os.getpid(), **event}
    with path.open("a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def stop_child(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def run_tests(log_dir, run_number, snapshot, changed, timeout):
    event_path = log_dir / "events.jsonl"
    append_event(event_path, {"event": "test_started", "run": run_number,
                             "source_sha256": fingerprint(snapshot), "source_files": len(snapshot),
                             "changed_paths": changed, "new_model_calls": 0})
    env = dict(os.environ)
    # No credential is needed by this test suite. Network is also blocked by a
    # dedicated pytest plugin before collection of the test modules.
    for name in list(env):
        if re.search(r"(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|SECRET|PASSWORD)", name, re.I):
            env.pop(name)
    env.update({"WEAVE_DISABLED": "true", "WANDB_MODE": "disabled", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    command = [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-p", "scripts.regression_offline_pytest",
               "-p", "pytest_asyncio.plugin", *FOCUSED_TESTS]
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    timed_out = False
    try:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_child(process)
            output, _ = process.communicate()
    finally:
        stop_child(process)
    output_path = log_dir / f"run-{run_number:04d}.log"
    output_path.write_text(redact(output))
    after = source_snapshot()
    result = {"event": "test_finished", "run": run_number, "passed": process.returncode == 0 and not timed_out,
              "exit_code": process.returncode, "timed_out": timed_out,
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "source_sha256": fingerprint(snapshot), "changed_during_run": changed_paths(snapshot, after),
              "output_file": output_path.name, "new_model_calls": 0}
    append_event(event_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=3600)
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--debounce-seconds", type=float, default=3)
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 3600 or not 0.1 <= args.poll_seconds <= 10 or not 0 <= args.debounce_seconds <= 30:
        parser.error("Duration must be 1–3600 seconds, poll 0.1–10 seconds, debounce 0–30 seconds")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.log_dir, 0o700)
    lock = (args.log_dir / "watcher.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("A watcher already owns this log directory")
    event_path = args.log_dir / "events.jsonl"
    if event_path.exists():
        parser.error("Use a new log directory to preserve prior runs")
    (args.log_dir / "watcher.pid").write_text(str(os.getpid()) + "\n")
    deadline = time.monotonic() + args.duration_seconds
    append_event(event_path, {"event": "watch_started", "duration_seconds": args.duration_seconds,
                             "focused_tests": list(FOCUSED_TESTS), "network_blocked": True})
    observed = source_snapshot()
    baseline = {}
    pending_since = time.monotonic() - args.debounce_seconds
    run_number = 0
    reason = "duration_complete"

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        while time.monotonic() < deadline:
            latest = source_snapshot()
            if latest != observed:
                observed, pending_since = latest, time.monotonic()
            if observed != baseline and time.monotonic() - pending_since >= args.debounce_seconds:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                run_number += 1
                run_tests(args.log_dir, run_number, observed, changed_paths(baseline, observed), min(120, remaining))
                baseline = observed
            time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        reason = "operator_stopped"
    finally:
        append_event(event_path, {"event": "watch_stopped", "reason": reason, "test_runs": run_number})
        lock.close()


if __name__ == "__main__":
    main()
