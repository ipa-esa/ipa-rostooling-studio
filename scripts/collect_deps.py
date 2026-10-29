#!/usr/bin/env python3
"""
collect_deps.py -- resolve a model's type:/from:/arrow references against the vendored
catalogues (assets/type_index.json, assets/node_index.json) and copy the winning
catalogue files into an oracle case directory, replacing the manual "figure out which
_deps files to hand-copy" step.

WHY THIS EXISTS: tests/oracle/cases/_deps/ is a hand-carried, confirmed-stale snapshot
(see assets/roscommonobjects/PROVENANCE.md) that someone has to remember to update by
hand. rosmodel_lint.py's RM083/RM087 already compute exactly which catalogue files a
model needs -- this script just runs the linter and acts on that.

Usage:
    python collect_deps.py <model-file> [<model-file> ...] <case-dir>
        Lints each model file with the catalogue enabled, then copies every catalogue
        file its RM083/RM087 findings named into <case-dir> (creating it if needed).
        This is a TRANSITIVE closure, not just the models' own direct references: a
        copied catalogue file (e.g. lifecycle_msgs.ros) can itself reference further
        catalogue types in its own field bodies (e.g. builtin_interfaces/msg/Time) that
        none of the given model files ever named directly -- those get pulled in too,
        recursively, until nothing new is found. A .rossystem model's resolved
        subSystems: entries are staged and re-linted the same way: the referenced
        .rossystem file is copied in and its own from:/type:/subSystems: references are
        followed recursively, so reusing e.g. turtlebot.rossystem also pulls in
        turtlebot3_node.ros2, hlds_laser_publisher.ros2, robot_state_publisher.ros2 and
        their .ros type deps without the caller naming any of them.
"""

import filecmp
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rosmodel_lint import (  # noqa: E402
    Linter,
    resolve_shared_node_roots,
    resolve_shared_type_roots,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
TYPE_CATALOG_ROOT = os.path.join(PLUGIN_ROOT, "assets", "roscommonobjects")
NODE_CATALOG_ROOT = os.path.join(PLUGIN_ROOT, "assets", "rosmodelscatalog")


def _resolve_catalog_file(rel, shared_roots, fallback_root):
    for r in shared_roots:
        cand = os.path.join(r, rel)
        if os.path.isfile(cand):
            return r, cand
    return fallback_root, os.path.join(fallback_root, rel)


def collect(model_paths, case_dir):
    """Fixpoint closure, not just a direct-reference lookup: a copied catalogue file (e.g.
    lifecycle_msgs.ros) can itself reference further catalogue types in its own message
    field bodies (e.g. builtin_interfaces/msg/Time) that none of the ORIGINAL model_paths
    ever named directly. Re-lint every newly-copied catalogue file the same way the
    original models were linted, and keep going until nothing new is found."""
    type_files, node_files = set(), set()
    processed = set()
    worklist = list(model_paths)
    unparseable = []

    shared_type_roots = resolve_shared_type_roots()
    shared_node_roots = resolve_shared_node_roots()

    while worklist:
        path = worklist.pop()
        if path in processed:
            continue
        processed.add(path)

        linter = Linter(path, use_catalogue=True)
        findings = linter.run()
        if any(f.rule == "RM008" for f in findings):
            # A parse failure means needed_type_files/needed_node_files were never computed --
            # this model's real dependencies are silently missing from the closure below, not
            # genuinely zero. Report it instead of letting "No catalogue files needed" imply
            # the model is self-contained when it was never actually read.
            unparseable.append(path)

        for rel in linter.needed_type_files:
            if rel not in type_files:
                type_files.add(rel)
                _, resolved_path = _resolve_catalog_file(rel, shared_type_roots, TYPE_CATALOG_ROOT)
                worklist.append(resolved_path)
        for rel in linter.needed_node_files:
            if rel not in node_files:
                node_files.add(rel)
                _, resolved_path = _resolve_catalog_file(rel, shared_node_roots, NODE_CATALOG_ROOT)
                worklist.append(resolved_path)

    os.makedirs(case_dir, exist_ok=True)
    # Snapshot what was already on disk BEFORE this run staged anything -- a project's own
    # input file (e.g. a caller-supplied turtlesim.ros sitting next to a .ros2 that references
    # it) can share a basename with a vendored catalogue file, and shutil.copyfile below would
    # silently replace the project's file with the catalogue's, changing what gets validated
    # without any message. Anything present here is untouchable by stage() below.
    preexisting = set(os.listdir(case_dir)) if os.path.isdir(case_dir) else set()
    copied = []
    claimed = {}  # basename -> (root, rel) that already claimed it, for collision detection

    def stage(root, rel, kind):
        basename = os.path.basename(rel)
        src = os.path.join(root, rel)
        dst = os.path.join(case_dir, basename)
        if basename in preexisting:
            if os.path.isfile(dst) and filecmp.cmp(src, dst, shallow=False):
                # Byte-identical to what we'd stage -- almost certainly this same catalogue
                # file left over from a prior run of this script. Nothing to do, and nothing
                # to warn about; re-running this script must stay idempotent.
                claimed[basename] = (root, rel)
                copied.append((kind, rel, dst))
                return
            print("WARNING: '%s' already exists in %s with DIFFERENT content than the "
                  "catalogue file %s -- NOT overwritten. That's either a real project file "
                  "this run must not clobber, or a stale copy from an earlier catalogue "
                  "version; delete it yourself first if it's the latter."
                  % (basename, case_dir, rel), file=sys.stderr)
            return
        prior = claimed.get(basename)
        if prior is not None and prior != (root, rel):
            # ask_oracle.py's MODEL_GLOBS is a flat, non-recursive glob of case_dir, so two
            # catalogue files from different subdirectories that happen to share a basename
            # (real cases exist: robot_state_publisher.ros2, turtlebot3_laserscan.ros2, ... --
            # see `find assets/rosmodelscatalog -name '*.ros2' -o -name '*.rossystem' | xargs
            # -n1 basename | sort | uniq -d`) cannot both be staged. Silently letting the
            # second overwrite the first would stage the WRONG file under a name the model's
            # own reference resolution already committed to via node_index.json -- refuse
            # instead of guessing which one the caller actually needs.
            print("WARNING: basename collision -- '%s' is claimed by both %s and %s; "
                  "skipping the second (rename or stage into separate case dirs)."
                  % (basename, prior[1], rel), file=sys.stderr)
            return
        claimed[basename] = (root, rel)
        shutil.copyfile(src, dst)
        copied.append((kind, rel, dst))

    for rel in sorted(type_files):
        root_used, _ = _resolve_catalog_file(rel, shared_type_roots, TYPE_CATALOG_ROOT)
        stage(root_used, rel, "type")

    for rel in sorted(node_files):
        kind = "system" if rel.endswith(".rossystem") else "node"
        root_used, _ = _resolve_catalog_file(rel, shared_node_roots, NODE_CATALOG_ROOT)
        stage(root_used, rel, kind)

    return copied, unparseable


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print(__doc__)
        return 1

    *model_paths, case_dir = argv
    missing = [p for p in model_paths if not os.path.isfile(p)]
    if missing:
        print("Model file(s) not found: %s" % ", ".join(missing), file=sys.stderr)
        return 1

    copied, unparseable = collect(model_paths, case_dir)

    status = 0
    if unparseable:
        print("ERROR: %d model file(s) failed to parse (RM008) -- their real catalogue "
              "dependencies were never computed, so this run's closure is INCOMPLETE, not "
              "necessarily empty: %s" % (len(unparseable), ", ".join(unparseable)),
              file=sys.stderr)
        status = 1

    if not copied:
        print("No catalogue files needed (nothing resolved, or catalogue indexes missing)."
              if not unparseable else
              "No catalogue files staged from the files that DID parse.")
    else:
        for kind, rel, dst in copied:
            print("%-5s %-55s -> %s" % (kind, rel, dst))
        print("Copied %d file(s) into %s" % (len(copied), case_dir))

    return status


if __name__ == "__main__":
    sys.exit(main())
