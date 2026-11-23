# -*- coding: utf-8 -*-
"""Tests for `rosmodel_lint.py --hook` (PostToolUse hook mode)."""

import io
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LINT = os.path.join(ROOT, "scripts", "rosmodel_lint.py")
BASE_SYS = os.path.join(HERE, "fixtures", "server_coverage", "base", "probe_sys.rossystem")

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import rosmodel_lint  # noqa: E402


def _run_hook_cli(event, env_extra=None):
    env = dict(os.environ)
    env["ROSMODEL_NO_CATALOGUE"] = "1"
    if env_extra:
        env.update(env_extra)
    stdin_data = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run(
        [sys.executable, LINT, "--hook"],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        cwd=ROOT,
    )


def test_clean_model_write_emits_validation_reminder(tmp_path):
    clean = tmp_path / "clean_pkg.ros2"
    clean.write_text(
        "clean_pkg:\n"
        "  artifacts:\n"
        "    talker:\n"
        "      node: talker\n"
        "      publishers:\n"
        "        'chatter':\n"
        "          type: 'std_msgs/msg/String'\n",
        encoding="utf-8",
    )
    proc = _run_hook_cli({"tool_input": {"file_path": str(clean)}})
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "decision" not in payload
    hook_out = payload["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PostToolUse"
    ctx = hook_out["additionalContext"]
    assert "not checked" in ctx
    assert "validate_ros_model" in ctx
    assert "rosmodel_lint.py --server require" in ctx


def test_error_still_blocks_and_includes_reminder(tmp_path):
    bad = tmp_path / "dup_key.ros2"
    bad.write_text(
        "dup_key:\n"
        "  artifacts:\n"
        "    talker:\n"
        "      node: talker\n"
        "      publishers:\n"
        "        'chatter':\n"
        "          type: 'std_msgs/msg/String'\n"
        "        'chatter':\n"
        "          type: 'std_msgs/msg/String'\n",
        encoding="utf-8",
    )
    proc = _run_hook_cli({"tool_input": {"file_path": str(bad)}})
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["decision"] == "block"
    assert "RM009" in payload["reason"]
    hook_out = payload["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PostToolUse"
    assert "validate_ros_model" in hook_out["additionalContext"]


def test_hook_never_starts_jvm(tmp_path):
    sentinel = tmp_path / "java_was_invoked"
    fake_java = tmp_path / "fake_java"
    fake_java.write_text(
        "#!/usr/bin/env sh\n"
        "touch '%s'\n"
        "exit 99\n" % sentinel,
        encoding="utf-8",
    )
    fake_java.chmod(0o755)
    fake_jar = tmp_path / "rostooling_extension-2.5.0.jar"
    fake_jar.write_text("", encoding="utf-8")

    proc = _run_hook_cli(
        {"tool_input": {"file_path": BASE_SYS}},
        {"ROSMODEL_JAVA": str(fake_java), "ROSMODEL_JAR": str(fake_jar)},
    )
    assert proc.returncode == 0, proc.stderr
    assert not sentinel.exists(), "hook must never invoke java/JVM"
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


@pytest.mark.parametrize(
    "stdin_payload",
    [
        "{not valid json",
        {},
        {"tool_input": {}},
        {"tool_input": {"file_path": "/tmp/not_a_model.py"}},
    ],
)
def test_hook_ignores_malformed_or_non_model_inputs_silently(stdin_payload):
    proc = _run_hook_cli(stdin_payload)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_hook_exits_zero_silently_on_internal_failure(monkeypatch, tmp_path, capsys):
    model = tmp_path / "boom.rossystem"
    model.write_text("boom:\n  nodes: {}\n", encoding="utf-8")

    def boom(self):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(rosmodel_lint.Linter, "run", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_input": {"file_path": str(model)}})))
    rc = rosmodel_lint.run_hook()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert "synthetic failure" in captured.err
