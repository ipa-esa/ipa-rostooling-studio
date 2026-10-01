#!/usr/bin/env python3
"""studio_agent_checks.py -- the guard rails around Studio's background coding-agent jobs.

No real agent runs here (that costs money and needs a login): each check feeds the code the
shape a real run produced, or a fake "agent" that just prints, and asserts what Studio does.
  ARGV         codex runs outside git checkouts; claude loads no MCP servers and no project hooks.
  SUGGESTIONS  unknown source / non-https url / folder with no package.xml or outside the
               allowed folders -> moved to notInList with a reason, not a failed run.
  ECHO         the prompt's own example JSON, echoed in a transcript, never replaces the real
               validation error.
  APPLY        apply-agent rejects another source folder, flags the plan never reported, and a
               value that collides with an existing port; it never renames by a word match.
  PLACEHOLDER  a {expr} interface name is a gate WARNING and a linter RM097 warning.
Exit 0 = all pass.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import studio_runtime as R  # noqa: E402
import studio_server as S  # noqa: E402
import ros_studio  # noqa: E402

failures = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def argv_checks():
    codex = R._agent_exec_argv("codex", "codex", "p")
    check("--skip-git-repo-check" in codex and codex[codex.index("--sandbox") + 1] == "read-only",
          "codex: --skip-git-repo-check, sandbox stays read-only")
    claude = R._agent_exec_argv("claude", "claude", "p")
    check("--strict-mcp-config" in claude and "--mcp-config" not in claude
          and claude[claude.index("--setting-sources") + 1] == "user",
          "claude: no MCP servers, user settings only")


def suggestion_checks(tmp):
    pkg = tmp / "vendor" / "ws" / "src" / "good_pkg"
    pkg.mkdir(parents=True)
    (pkg / "package.xml").write_text("<package/>")
    empty = tmp / "vendor" / "empty"
    empty.mkdir()
    outside = tmp / "elsewhere" / "pkg"
    outside.mkdir(parents=True)
    (outside / "package.xml").write_text("<package/>")
    base = {"priority": "core", "fit": "f", "reason": "r"}
    doc = {"about": "a", "suggestions": [
        {**base, "id": "good", "name": "good", "source": "workspace", "path": str(tmp / "vendor" / "ws")},
        {**base, "id": "nopkg", "name": "nopkg", "source": "workspace", "path": str(empty)},
        {**base, "id": "far", "name": "far", "source": "workspace", "path": str(outside)},
        {**base, "id": "ssh", "name": "ssh", "source": "online", "url": "git@github.com:x/y.git"},
        {**base, "id": "web", "name": "web", "source": "online", "url": "https://github.com/x/y.git"},
        {**base, "id": "odd", "name": "odd", "source": "apt"}]}
    out = R.validate_suggestions(doc, [str(tmp / "vendor")])
    check([s["id"] for s in out["suggestions"]] == ["good", "web"], "suggestions: only usable entries kept")
    dropped = " | ".join(out.get("notInList", []))
    check(all(w in dropped for w in ("no package.xml", "outside the project", "not an https", "unknown source")),
          "suggestions: each dropped entry says why")


def echo_check(tmp):
    rt = R.StudioRuntime(ROOT, tmp / "store")
    ident = "e" * 32
    job = rt._jobs_root / ident
    job.mkdir(parents=True)
    start, end = R.SUGGESTION_MARKERS
    real = json.dumps({"about": "a", "suggestions": [{"id": "x", "name": "x", "source": "workspace",
                       "path": "/nonexistent/x", "priority": "core", "fit": "f", "reason": "r"}]})
    # A transcript that echoes the prompt (example JSON) and then prints a real but unusable block.
    script = "print(%r)" % "\n".join([start, R.SUGGESTION_EXAMPLE, end, "work...", start, real, end])
    record = {"jobId": ident, "kind": "package-suggestions", "projectRoot": str(tmp), "state": "queued",
              "argv": [sys.executable, "-c", script], "logPath": str(job / "output.log"),
              "resultPath": str(job / "suggestions.json"), "allowedRoots": [str(tmp)]}
    R._write_json(job / "record.json", record)
    rt._execute_agent_job(ident, str(tmp))
    done = json.loads((job / "record.json").read_text())
    check(done["state"] == "failed" and "no usable suggestions" in done.get("error", ""),
          "echo: the real block's error is kept (%r)" % done.get("error"))


class _Catalogue:
    def __init__(self, directory):
        self.directory = directory


def apply_checks(tmp):
    directory = tmp / "catalogue"
    (directory / "plans").mkdir(parents=True)
    flag = {"kind": "publishers", "reason": "name is not a string literal", "at": "pkg/a.py:9", "package": "pkg"}
    node = {"ifaces": [{"id": "i1", "name": "status", "kind": "pub", "type": "std_msgs/msg/String"},
                       {"id": "i2", "name": "beat", "kind": "pub", "type": "std_msgs/msg/Bool"}]}

    def plan(pid):
        p = {"projectId": "p", "version": "v1", "sourceRoot": str(tmp), "flags": [flag],
             "entries": [{"id": "g:v1:e", "package": "pkg", "name": "n", "node": json.loads(json.dumps(node)),
                          "sourcePaths": ["pkg/a.py"], "flags": [flag]}]}
        (directory / "plans" / (pid + ".json")).write_text(json.dumps(p))
        return pid

    def job(value, flags=None, root=None, source=None):
        f = dict(flags or flag, **({"source": source} if source else {}))
        return {"jobId": "j", "kind": "resolve-flags", "state": "passed", "provider": "claude",
                "sourceRoot": root or str(tmp), "flags": [f],
                "resolutions": [{"index": 0, "resolution": "traced", "value": value, "citation": "c"}]}

    cat = _Catalogue(directory)
    try:
        S.apply_agent_resolutions(cat, "p", plan("a" * 32), job("/x", root=str(tmp / "other")))
        check(False, "apply: another source folder is refused")
    except S.StudioError:
        check(True, "apply: another source folder is refused")
    r = S.apply_agent_resolutions(cat, "p", plan("b" * 32), job("/x", flags={**flag, "at": "pkg/a.py:99"}))
    check(not r["applied"] and r["skipped"][0]["reason"] == "flag is not in this preview",
          "apply: a flag the preview never reported is skipped")
    r = S.apply_agent_resolutions(cat, "p", plan("c" * 32), job("status"))
    check(not r["applied"] and "already has" in r["skipped"][0]["reason"], "apply: colliding name is skipped")
    r = S.apply_agent_resolutions(cat, "p", plan("d" * 32), job("/heartbeat", source='pub(Bool, beat)'))
    names = [f["name"] for f in json.loads((directory / "plans" / ("d" * 32 + ".json")).read_text())["entries"][0]["node"]["ifaces"]]
    check(len(r["applied"]) == 1 and "beat" in names and "/heartbeat" in names,
          "apply: a word in the source text does not rename an existing port")
    r = S.apply_agent_resolutions(cat, "p", plan("e" * 32), job("a//b/"))
    check(not r["applied"], "apply: 'a//b/' is not accepted as a ROS name")


def placeholder_checks():
    project = {"system": {"name": "s"}, "types": {}, "nodes": [{"id": "n1", "label": "pilot", "backing": "hand", "pkg": "probe", "node": "pilot", "params": [], "ifaces": [
        {"id": "i", "name": "/probe/{ns}/beat", "kind": "pub", "type": "std_msgs/msg/Bool"}]}], "connections": []}
    gate = ros_studio.validate_project(project)
    check(not gate["global"] and not gate["byNode"] and any("{ns}" in w for w in gate["warnings"]),
          "placeholder: gate warns, does not block")
    golden = ROOT / "tests/fixtures/extract/golden"
    lint = subprocess.run([sys.executable, str(ROOT / "scripts/rosmodel_lint.py"), "rosnodes/probe_pilot.ros2",
                           "probe.rossystem"], cwd=golden, capture_output=True, text=True).stdout
    check(lint.count("still holds a {...} placeholder") == 2, "placeholder: linter warns in .ros2 and .rossystem")


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d).resolve()
        argv_checks()
        suggestion_checks(tmp)
        echo_check(tmp)
        apply_checks(tmp)
        placeholder_checks()
    print("\n%d check(s) failed" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
