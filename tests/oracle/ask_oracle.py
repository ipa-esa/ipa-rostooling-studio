#!/usr/bin/env python3
"""
ask_oracle.py — feed model files to the REAL RosTooling language server and report what it says.

Everything else in this plugin validates against our own reimplementation of RosTooling's rules.
This script asks the actual toolchain. It is the only component here that does.

Usage:
    py ask_oracle.py <case-dir> [<case-dir> ...]
    py ask_oracle.py --all            # every directory under cases/

Each case directory is opened as its own LSP workspace root so cases cannot interfere
(two files declaring the same package would otherwise cross-contaminate).

Requires Java 19+. Finding Java and the jar, the version check and the LSP client live in
scripts/ros_server.py, shared with rosmodel_lint.py --server; this file is the oracle's CLI.
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "scripts"))

import ros_server as RS  # noqa: E402


def _cli_jar_override() -> str:
    for i, a in enumerate(sys.argv):
        if a == "--jar" and i + 1 < len(sys.argv):
            return sys.argv[i + 1].strip()
        if a.startswith("--jar="):
            return a.split("=", 1)[1].strip()
    return ""


# --jar wins over ROSMODEL_JAR, which wins over ROSMODEL_ORACLE and the discovered jar.
JAR_OVERRIDE = _cli_jar_override() or RS.JAR_OVERRIDE


def _results_path(is_partial_run=False):
    """Where to write the run's verdict.

    Default is tests/oracle/results.json for full test runs (--all), but NOT when that
    file has uncommitted changes or when running a subset of cases. A partial run writes
    to results.local.json so it never accidentally truncates the 19-case regression baseline.
    Pass --results PATH to force a specific location.
    """
    for i, a in enumerate(sys.argv):
        if a == "--results" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1])
        if a.startswith("--results="):
            return Path(a.split("=", 1)[1])

    default = HERE / "results.json"
    if is_partial_run:
        alt = default.with_name("results.local.json")
        print(f"NOTE: Partial test run. Verdict written to {alt.name} to preserve the\n"
              f"      checked-in regression baseline in {default.name}.\n"
              f"      Pass --all to run all cases, or --results to override.",
              file=sys.stderr)
        return alt

    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", str(default)],
            cwd=str(HERE), capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        dirty = ""                      # not a git checkout, or git unavailable
    if not dirty:
        return default

    alt = default.with_name("results.local.json")
    print(f"NOTE: {default.name} has uncommitted changes, so it was left alone.\n"
          f"      This run's verdict went to {alt.name} instead.\n"
          f"      Commit or stash that file, or pass --results, to write it directly.",
          file=sys.stderr)
    return alt


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args = []
    skip_next = False
    for a in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a == "--jar":
            skip_next = True
            continue
        if a.startswith("--jar="):
            continue
        if not a.startswith("-"):
            args.append(a)

    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    # One gate for both callers. `--preflight` answers "could the oracle run?" without running
    # it -- ros_studio.py asks that before committing to a 45-second initialize wait, and needs
    # the REASON verbatim to put in front of the user rather than a bare exit code.
    ok, reason = RS.preflight(JAR_OVERRIDE)
    if "--preflight" in sys.argv:
        print(reason if ok else ("FATAL: " + reason), file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 3
    if not ok:
        print("FATAL: " + reason, file=sys.stderr)
        return 2
    if JAR_OVERRIDE:
        print(f"oracle language server JAR override -> {JAR_OVERRIDE}", file=sys.stderr)
    elif RS.ORACLE_MODE == "legacy":
        print("oracle mode: LEGACY (stale 2024 grammar — QoS fields will not parse)",
              file=sys.stderr)

    if "--all" in sys.argv or not args:
        dirs = sorted(d for d in (HERE / "cases").iterdir() if d.is_dir())
    else:
        dirs = [Path(a).resolve() for a in args]

    print(f"java   {RS.JAVA}")
    print(f"cases  {len(dirs)}   (jar chosen per case by file extension)\n")

    results = []
    for d in dirs:
        print(f"== {d.name} " + "=" * max(0, 56 - len(d.name)))
        r = RS.run_case(d, verbose=verbose, jar_override=JAR_OVERRIDE)
        results.append(r)

        if r["status"] != "OK":
            print(f"   STATUS: {r['status']}")
            for line in r.get("stderr", [])[-12:]:
                print(f"   | {line}")
            print()
            continue

        errs = [d_ for d_ in r["diagnostics"] if d_["severity"] == "ERROR"]
        warns = [d_ for d_ in r["diagnostics"] if d_["severity"] == "WARNING"]
        verdict = "REJECTED" if errs else "ACCEPTED"
        print(f"   {verdict}  —  {len(errs)} error(s), {len(warns)} warning(s)   [{r.get('jar','?')}]")
        for d_ in r["diagnostics"][:14]:
            print(f"     {d_['severity']:<8} line {d_['line']:<4} {d_['message'][:100]}")
        if len(r["diagnostics"]) > 14:
            print(f"     ... {len(r['diagnostics']) - 14} more")
        print()

    is_partial = not ("--all" in sys.argv or not args)
    out = _results_path(is_partial_run=is_partial)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"full results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
