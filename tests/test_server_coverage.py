# -*- coding: utf-8 -*-
"""The server-coverage fixture (tests/fixtures/server_coverage): one minimal case per check that
rosmodel_lint.py used to copy from the grammar or the validator, with what the real language
server reported for it in expected.json. Those checks were removed from the linter (cut, step 2):
the server reports every case, so the linter leaves them to it. Refresh the server side with
tests/oracle/ask_oracle.py; regenerate the cases with make_cases.py in that folder."""

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


def _expected():
    with open(os.path.join(CASES, "expected.json")) as fh:
        return json.load(fh)["cases"]


def test_base_is_clean():
    base = os.path.join(CASES, "base")
    for name in sorted(os.listdir(base)):
        errors = [(f.rule, f.message) for f in _findings(os.path.join(base, name))
                  if f.severity == rosmodel_lint.ERROR]
        assert not errors, (name, errors)


def test_every_case_is_reported_by_the_server():
    expected = _expected()
    dirs = sorted(d for d in os.listdir(CASES) if os.path.isfile(os.path.join(CASES, d, "case.json")))
    assert dirs == sorted(expected), "expected.json and the case folders differ"
    # RM053's model is valid (no fromFile: means the generator writes the bringup package).
    silent = sorted(d for d, v in expected.items() if not v["server"] and "note" not in v)
    assert not silent, "cases the server does not report: %s" % silent


def test_the_linter_leaves_every_case_to_the_server():
    still_raised = []
    for d, v in sorted(_expected().items()):
        with open(os.path.join(CASES, d, "case.json")) as fh:
            rule = json.load(fh)["rule"]
        errors = [f for f in _findings(os.path.join(CASES, d, v["file"]))
                  if f.rule == rule and f.severity == rosmodel_lint.ERROR]
        if errors:
            still_raised.append((d, [f.message for f in errors]))
    assert not still_raised, still_raised
