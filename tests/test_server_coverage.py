# -*- coding: utf-8 -*-
"""The server-coverage fixture (tests/fixtures/server_coverage): one minimal case per
rosmodel_lint rule that copies a grammar or validator rule, with what the real language server
reported for it in expected.json. A case only proves something while it still trips its rule,
so this checks that against the linter; the server side is refreshed with tests/oracle/ask_oracle.py."""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import rosmodel_lint  # noqa: E402

CASES = os.path.join(HERE, "fixtures", "server_coverage")


def _findings(path):
    linter = rosmodel_lint.Linter(path)
    linter.run()
    return linter.findings


def test_base_is_clean():
    base = os.path.join(CASES, "base")
    for name in sorted(os.listdir(base)):
        errors = [(f.rule, f.message) for f in _findings(os.path.join(base, name))
                  if f.severity == rosmodel_lint.ERROR]
        assert not errors, (name, errors)


def test_every_case_trips_its_rule_and_has_a_server_verdict():
    with open(os.path.join(CASES, "expected.json")) as fh:
        expected = json.load(fh)["cases"]
    dirs = sorted(d for d in os.listdir(CASES) if os.path.isfile(os.path.join(CASES, d, "case.json")))
    assert dirs == sorted(expected), "expected.json and the case folders differ"
    missing = []
    for d in dirs:
        with open(os.path.join(CASES, d, "case.json")) as fh:
            case = json.load(fh)
        assert expected[d]["file"] == case["file"], d
        rules = {f.rule for f in _findings(os.path.join(CASES, d, case["file"]))}
        if case["rule"] not in rules:
            missing.append((d, case["rule"], sorted(rules)))
    assert not missing, missing
