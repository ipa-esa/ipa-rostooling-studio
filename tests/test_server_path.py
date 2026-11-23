# -*- coding: utf-8 -*-
"""rosmodel_lint.py --server: the RosTooling language server's findings come back as RM100, and a
run that did not ask the server says so with RM101 (INFO for --server off, WARNING when auto finds
no usable server, ERROR when --server require does). A missing Java is reproduced anywhere by
pointing ROSMODEL_JAVA at a path that does not exist, as tests/oracle_gate.py does."""

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LINT = os.path.join(ROOT, "scripts", "rosmodel_lint.py")
CASES = os.path.join(HERE, "fixtures", "server_coverage")
BASE = os.path.join(CASES, "base", "probe_sys.rossystem")
BROKEN = os.path.join(CASES, "RM020", "probe_pkg.ros2")   # an unquoted type: a parse error for any server
NO_JAVA = os.path.join(os.sep, "does", "not", "exist", "java")

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ros_server  # noqa: E402


def lint(args, env_extra=None):
    env = dict(os.environ)
    env.pop("ROSMODEL_JAVA", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([sys.executable, LINT, "--json"] + args, capture_output=True, text=True,
                          env=env, timeout=300, cwd=ROOT)
    return proc.returncode, json.loads(proc.stdout)


def by_rule(payload, rule):
    return [f for f in payload["findings"] if f["rule"] == rule]


def test_off_never_asks_and_says_so():
    code, out = lint(["--server", "off", BASE])
    assert out["server"]["status"] == "OFF"
    assert [f["severity"] for f in by_rule(out, "RM101")] == ["INFO"]
    assert not by_rule(out, "RM100")
    assert code == 0


@pytest.mark.parametrize("mode,severity,exit_code", [("auto", "WARNING", 0), ("require", "ERROR", 1)])
def test_missing_java_is_reported_not_silent(mode, severity, exit_code):
    code, out = lint(["--server", mode, BASE], {"ROSMODEL_JAVA": NO_JAVA})
    assert out["server"]["status"] == "UNAVAILABLE"
    rm101 = by_rule(out, "RM101")
    assert [f["severity"] for f in rm101] == [severity]
    assert "java not found" in rm101[0]["message"]
    assert code == exit_code


def test_non_model_files_are_not_sent():
    findings, info = __import__("rosmodel_lint").server_findings(["notes.txt"], "auto")
    assert findings == [] and info["status"] == "NO_FILES"


@pytest.mark.skipif(not ros_server.preflight()[0],
                    reason="no usable language server here: %s" % ros_server.preflight()[1])
def test_server_findings_come_back_as_rm100_for_the_files_asked_about():
    code, out = lint(["--server", "require", BASE, BROKEN])
    assert out["server"]["status"] == "OK", out["server"]
    rm100 = by_rule(out, "RM100")
    assert rm100 and all(f["file"] == BROKEN for f in rm100), rm100
    assert any(f["severity"] == "ERROR" for f in rm100)
    assert not by_rule(out, "RM101")
    assert code == 1
