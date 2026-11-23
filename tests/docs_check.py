#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docs_check.py -- hold the PROSE to the CODE.

    py tests/docs_check.py            # every tracked .md
    py tests/docs_check.py FILE ...   # just these

Why this exists
---------------
The skill and the toolchain are one product described in two places. `skills/ros-model/SKILL.md`
and the command docs tell an agent what the linter enforces, which files to read and which
examples to follow; `scripts/` decides what is actually true. Nothing connected the two, so the
prose drifted -- and drifted in the worst direction, because an agent BELIEVES it. Commit
04ddd6f had to correct twenty documentation claims that no longer described this repo, one of
which told an agent to delete values the grammar accepts.

Every check below is a class of drift that has actually happened here, not a hypothetical. The
point is not to police wording: it is that a fact restated in prose is a fact that can rot, so
each restatement that CAN be mechanically compared to its source is compared on every run.

What this cannot do
-------------------
Only mechanically checkable claims are checked. "RM065 fires when two nodes share a label" is
prose this file cannot verify -- that is what tests/roundtrip.py selftest and the oracle cases
are for. What it does cover is the boring half that rots silently: paths, identifiers, counts.

Exit 0 iff there are no ERRORs. WARNINGs are reported and do not fail the run.
"""

import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)

# Directories a repo-relative path in the docs can start with. Anchoring on this list (rather
# than "anything with a slash") is what keeps prose like "single for references/names" out of
# the path check -- that is not a path, it is a sentence.
TOP_DIRS = ("scripts", "tests", "assets", "skills", "commands", "agents",
            "references", "docs", "examples", "hooks", "research", "build")
PATH_RE = re.compile(
    r'(?<![\w./-])((?:' + "|".join(TOP_DIRS) + r')/[A-Za-z0-9_./-]*[A-Za-z0-9_-])')
RULE_RE = re.compile(r'\b(RM\d{3})\b')


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, cwd=ROOT).stdout


def tracked_markdown():
    out = _run(["git", "ls-files", "*.md"]).split()
    # build/ is vendored Xtext source with its own READMEs; it describes the language server,
    # not this repo, and is not ours to keep true.
    return [f for f in out if not f.startswith("build/")]


def _gitignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    return r.returncode == 0


def citable_paths(path):
    """Every path in a document that is a CLAIM about a file, skipping the ones that are not.

    A fenced block is often a directory TREE, whose indented lines are relative to the parent
    line above them, not to the repo:

        skills/ros-model/
          references/ros2-syntax.md     .ros2 + .ros grammar

    `references/ros2-syntax.md` is right there and wrong anywhere else, so an indented line
    inside a fence is drawn as structure and read as structure -- not checked as a path.
    """
    out, fenced = set(), False
    for line in open(path, encoding="utf-8", errors="replace"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced and line[:1] in (" ", "	"):
            continue
        out.update(PATH_RE.findall(line))
    return out


def check_paths(files, add):
    """Every repo path a doc cites must exist.

    A path may be repo-relative OR relative to the citing document -- SKILL.md writes
    `references/ros2-syntax.md` for a file that sits next to it -- so both are tried before a
    path is called missing.

    A path that exists but is GITIGNORED is a warning, not an error: it is true on the author's
    machine and absent from a fresh clone, which is the state a colleague or an agent starts
    from. `examples/` is exactly this case.
    """
    for f in files:
        base = os.path.dirname(f)
        for cited in sorted(citable_paths(os.path.join(ROOT, f))):
            p = cited.rstrip(".,);:`")
            # A bare directory, or a glob, is not a claim about one file.
            if "*" in p or "." not in os.path.basename(p):
                continue
            abs_repo = os.path.join(ROOT, p)
            abs_doc = os.path.join(ROOT, base, p)
            if os.path.exists(abs_repo):
                hit = p
            elif os.path.exists(abs_doc):
                hit = os.path.relpath(abs_doc, ROOT).replace(os.sep, "/")
            else:
                add("ERROR", f, "cites %s, which does not exist" % p)
                continue
            if _gitignored(hit):
                add("WARNING", f,
                    "cites %s, which is gitignored -- it is absent from a fresh clone, so a "
                    "reader or an agent following this will not find it" % p)


def check_rules(files, add):
    """Rule ids must exist, and rules must be documented.

    Both directions matter. A doc citing RM999 sends a reader looking for a rule that was
    renamed or removed; a rule cited nowhere is one an agent will never be told about, which is
    the same defect seen from the other end.
    """
    src = open(os.path.join(ROOT, "scripts", "rosmodel_lint.py"),
              encoding="utf-8").read()
    retired = retired_rules(src)
    implemented = set(re.findall(r'"(RM\d{3})"', src)) - retired
    cited = {}
    for f in files:
        text = open(os.path.join(ROOT, f), encoding="utf-8", errors="replace").read()
        for rid in set(RULE_RE.findall(text)):
            cited.setdefault(rid, set()).add(f)
    # A retired id may still be cited: history, and the README's table of retired rules.
    for rid in sorted(set(cited) - implemented - retired):
        add("ERROR", ", ".join(sorted(cited[rid])),
            "cites %s, which rosmodel_lint.py does not emit" % rid)
    # RM000 is the internal parse-failure id and is deliberately not written up.
    for rid in sorted(implemented - set(cited) - {"RM000"}):
        add("WARNING", "scripts/rosmodel_lint.py",
            "emits %s, which no document mentions" % rid)


def retired_rules(src):
    """The ids in rosmodel_lint.py's RETIRED_RULES: rules removed because the language server
    reports them. They are neither implemented nor counted, but may be cited."""
    m = re.search(r"^RETIRED_RULES = \((.*?)\)", src, re.S | re.M)
    return set(re.findall(r'"(RM\d{3})"', m.group(1))) if m else set()


def rule_count():
    """The number the docs mean by "N rules".

    NOT simply the count of RM### ids: RM000 is internal, and RM002B/RM008T are named variants
    with no numeric id of their own. Encoding the counting rule here is the point -- a naive
    count says 80 where the docs correctly say 81, and "fixing" the docs to match a naive count
    would have made them wrong.
    """
    src = open(os.path.join(ROOT, "scripts", "rosmodel_lint.py"), encoding="utf-8").read()
    numeric = set(re.findall(r'"(RM\d{3})"', src)) - {"RM000"} - retired_rules(src)
    variants = set(re.findall(r'"(RM\d{3}[A-Z])"', src))
    return len(numeric) + len(variants)


def oracle_cases():
    d = os.path.join(ROOT, "tests", "oracle", "cases")
    if not os.path.isdir(d):
        return 0
    return len([x for x in os.listdir(d)
                if os.path.isdir(os.path.join(d, x)) and not x.startswith("_")])


# The number NEAREST the marker, allowing the words and markdown emphasis that sit between them
# ("**81 rules**<!--@count:rules-->"). No digit may intervene, so the marker cannot bind to some
# other number earlier in the sentence.
COUNT_RE = re.compile(r'(\d+)[^<\d\r\n]{0,32}<!--@count:(\w+)-->')


def check_counts(files, add):
    """Counts quoted in prose must match what is on disk -- but ONLY where the prose says so.

    An earlier version of this check scanned for `N rules` and `N cases` anywhere. It found the
    two real staleness bugs and three false ones: a retrospective quoting the linter's size when
    a row was written, a dated record of a past oracle run, and the phrase "rules 13-14" in
    "§1.3 rules 13-14". Prose is not a data format, and a check that cries wolf gets switched
    off, so the contract is now EXPLICIT: a number is checked when, and only when, the author
    marks it.

        All 25 cases<!--@count:oracle_cases--> currently behave as documented

    The marker renders as nothing and reads as a promise: this number is derived, keep it true.
    A count with no marker is prose and is left alone -- including a deliberately historical one.
    """
    known = {"rules": rule_count(), "oracle_cases": oracle_cases()}
    seen = set()
    for f in files:
        for lineno, line in enumerate(
                open(os.path.join(ROOT, f), encoding="utf-8", errors="replace"), 1):
            for got, name in COUNT_RE.findall(line):
                seen.add(name)
                if name not in known:
                    add("ERROR", "%s:%d" % (f, lineno),
                        "marks a count named %r that docs_check.py cannot compute; add it to "
                        "`known` or drop the marker" % name)
                elif int(got) != known[name]:
                    add("ERROR", "%s:%d" % (f, lineno),
                        "says %s where %s is %d" % (got, name, known[name]))
    # A marker that no document uses is a check quietly doing nothing.
    for name in sorted(set(known) - seen):
        add("WARNING", "tests/docs_check.py",
            "computes %r but no document marks it, so nothing is being verified" % name)


def main(argv):
    files = argv[1:] or tracked_markdown()
    findings = []

    def add(sev, where, msg):
        findings.append((sev, where, msg))

    check_paths(files, add)
    check_rules(files, add)
    check_counts(files, add)

    errors = [f for f in findings if f[0] == "ERROR"]
    warnings = [f for f in findings if f[0] == "WARNING"]
    for sev, where, msg in sorted(errors) + sorted(warnings):
        print("  %-7s %s" % (sev, where))
        print("          %s" % msg)
    print("\n%d file(s): %d error(s), %d warning(s)"
          % (len(files), len(errors), len(warnings)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
