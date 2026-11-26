#!/usr/bin/env python3
"""
add_provenance.py -- say on every from: / type: line where the reference comes from.

A catalogue reference gets the vendored file ("# assets/rosmodelscatalog/navigation/amcl.ros2"),
a project-local one gets "# project-local, see <file>". Lines that already carry a comment are
left exactly as they are, and so is every other byte of the file. References that resolve to
neither are listed and left uncommented.

Usage:
    python add_provenance.py <model-file> [<model-file> ...] [--workspace <dir>] [--check]

--workspace  where project-local .ros / .ros2 files are (default: each model file's folder)
--check      write nothing; exit 1 if a line would change or a reference is unresolved
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_node_index as bni  # noqa: E402
import collect_deps  # noqa: E402
import rosmodel_lint as L  # noqa: E402

SKIP_DIRS = {".git", "__pycache__", "node_modules", "build", "install", "log", "src-gen"}

# from: only means a node reference in a .rossystem; type: only a spec reference in a .ros2.
LINE = {
    ".rossystem": re.compile(r"""^(?P<head>\s*from:\s*(?P<q>["']?)(?P<ref>[\w]+\.[\w/]+)(?P=q))[ \t]*(?P<eol>\r?\n?)$"""),
    ".ros2": re.compile(r"""^(?P<head>\s*type:\s*(?P<q>["']?)(?P<ref>\w+/(?:msg|srv|action)/\w+)(?P=q)(?:\[(?:<=)?\d*\])?)[ \t]*(?P<eol>\r?\n?)$"""),
}


def local_index(workspace):
    """({"pkg.node": path}, {"pkg/kind/Type": path}) for the .ros2 / .ros files under workspace."""
    nodes = {}
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if not name.endswith(".ros2"):
                continue
            path = os.path.join(dirpath, name)
            try:
                for pkg, _art, node, _ifaces in bni.extract_ros2_nodes(bni.compose_yaml(path)):
                    nodes.setdefault("%s.%s" % (pkg, node), path)
            except Exception as exc:  # one unreadable file must not hide the others
                print("skipped %s: %s" % (path, exc), file=sys.stderr)
    types, _packages = L.scan_type_roots([workspace])
    types = {ref: os.path.join(workspace, entry["file"]) for ref, entry in types.items()}
    return nodes, types


def without_comments(text):
    """The model lines that carry content: no comments, blank lines or line-ending differences."""
    kept = []
    for line in text.splitlines():
        quote = None
        for i, char in enumerate(line):
            if quote:
                quote = None if char == quote else quote
            elif char in "\"'":
                quote = char
            elif char == "#" and (i == 0 or line[i - 1].isspace()):
                line = line[:i]
                break
        if line.strip():
            kept.append(line.rstrip())
    return kept


def is_copy(path, ext, entry):
    """True when path says the same as the catalogue file that entry names (collect_deps.py copy)."""
    if ext == ".rossystem":
        shared, vendored = L.resolve_shared_node_roots(), collect_deps.NODE_CATALOG_ROOT
    else:
        shared, vendored = L.resolve_shared_type_roots(), collect_deps.TYPE_CATALOG_ROOT
    root, _ = collect_deps._resolve_catalog_file(entry["file"], shared, vendored)
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as mine, \
                open(os.path.join(root, entry["file"]), encoding="utf-8-sig",
                     errors="replace") as theirs:
            return without_comments(mine.read()) == without_comments(theirs.read())
    except OSError:
        return False


def comment_for(ref, ext, model_dir, local, catalogue):
    nodes, types = local
    path = (nodes if ext == ".rossystem" else types).get(ref)
    entry = catalogue.get(ref)
    folder = "assets/rosmodelscatalog/" if ext == ".rossystem" else "assets/roscommonobjects/"
    if path is not None:
        # A staged copy of the catalogue file (collect_deps.py) is still the catalogue's.
        if entry and is_copy(path, ext, entry):
            return folder + entry["file"]
        return "project-local, see %s" % os.path.relpath(path, model_dir).replace(os.sep, "/")
    return folder + entry["file"] if entry else None


def process(model, workspace, check):
    """(changed line numbers, unresolved (line, ref)) for one model file."""
    ext = os.path.splitext(model)[1]
    pattern = LINE.get(ext)
    if pattern is None:
        raise SystemExit("%s: expected a .ros2 or .rossystem file" % model)
    model_dir = os.path.dirname(os.path.abspath(model))
    local = local_index(os.path.abspath(workspace or model_dir))
    catalogue = (L.load_node_index() if ext == ".rossystem" else L.load_type_index()) or {}
    with open(model, "rb") as fh:
        lines = fh.read().decode("utf-8").splitlines(True)
    changed, unresolved = [], []
    for number, line in enumerate(lines, 1):
        match = pattern.match(line)
        if not match:
            continue
        note = comment_for(match.group("ref"), ext, model_dir, local, catalogue)
        if note is None:
            unresolved.append((number, match.group("ref")))
            continue
        lines[number - 1] = "%s # %s%s" % (match.group("head"), note, match.group("eol"))
        changed.append(number)
    if changed and not check:
        with open(model, "wb") as fh:
            fh.write("".join(lines).encode("utf-8"))
    return changed, unresolved


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("models", nargs="+")
    ap.add_argument("--workspace")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    dirty = False
    for model in args.models:
        changed, unresolved = process(model, args.workspace, args.check)
        print("%s: %d line(s) %s" % (model, len(changed),
                                     "would change" if args.check else "commented"))
        for number, ref in unresolved:
            print("  line %d: '%s' resolves to no catalogue or workspace file" % (number, ref))
        dirty = dirty or bool(unresolved) or (args.check and bool(changed))
    return 1 if dirty else 0


if __name__ == "__main__":
    sys.exit(main())
