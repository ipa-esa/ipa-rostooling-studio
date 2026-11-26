#!/usr/bin/env python3
"""
catalogue_query.py -- look up types, nodes and systems in the RosTooling catalogues.

WHY THIS EXISTS: the indexes (assets/type_index.json, assets/node_index.json and any shared
catalogue) are tens of kilobytes. An agent should ask for the few entries it needs instead of
reading them.

Usage:
    python catalogue_query.py type <text>            # matching "pkg/kind/Type" references
    python catalogue_query.py node <text>            # matching "pkg.node" references for from:
    python catalogue_query.py show <pkg.node>        # one node's artifact, file and interfaces
    python catalogue_query.py system <name>          # one catalogued system and what it exposes

Exit 0 with matches, 1 without.
"""

import argparse
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosmodel_lint as L  # noqa: E402


def search(keys, text, limit):
    """Exact name first, then substring matches, then close spellings."""
    low = text.lower()
    exact = [k for k in keys if k.lower() == low or k.lower().rsplit("/", 1)[-1] == low
             or k.lower().rsplit(".", 1)[-1] == low]
    part = [k for k in keys if low in k.lower() and k not in exact]
    found = sorted(exact) + sorted(part)
    if found:
        return found[:limit], len(found), False
    close = difflib.get_close_matches(text, keys, n=min(limit, 5), cutoff=0.6)
    return close, len(close), True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["type", "node", "show", "system"])
    ap.add_argument("text")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.what == "type":
        index, folder = L.load_type_index() or {}, "assets/roscommonobjects/"
    elif args.what == "system":
        index, folder = L.load_system_index() or {}, "assets/rosmodelscatalog/"
    else:
        index, folder = L.load_node_index() or {}, "assets/rosmodelscatalog/"
    if not index:
        print("catalogue index not available", file=sys.stderr)
        return 1

    if args.what in ("show", "system"):
        entry = index.get(args.text)
        if entry is None:
            names, _total, _close = search(list(index), args.text, 5)
            print("'%s' is not in the catalogue.%s" % (
                args.text, " Similar: " + ", ".join(names) if names else ""), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(entry, indent=2, sort_keys=True))
            return 0
        print("%s  (%s%s)" % (args.text, folder, entry.get("file")))
        if args.what == "show":
            if entry.get("artifact"):
                print("artifact: %s" % entry["artifact"])
            for name, kind in sorted((entry.get("interfaces") or {}).items()):
                print("  %-3s %s" % (kind, name))
        else:
            for label, node in sorted((entry.get("nodes") or {}).items()):
                exposed = node.get("interfaces") or {}
                print("  %s  from %s" % (label, node.get("from")))
                for name, kind in sorted(exposed.items()):
                    print("      %-3s %s" % (kind, name))
                if not exposed:
                    print("      (exposes no interfaces)")
        return 0

    names, total, close = search(list(index), args.text, args.limit)
    if args.json:
        print(json.dumps({"matches": {n: index[n].get("file") for n in names},
                          "total": total, "closeSpellingsOnly": close}, indent=2))
    else:
        if close and names:
            print("no match; similar spellings:")
        for name in names:
            print("%s  (%s%s)" % (name, folder, index[name].get("file")))
        if total > len(names):
            print("... %d more; narrow the search or raise --limit" % (total - len(names)))
        if not names:
            print("no match for '%s'" % args.text)
    return 0 if names and not close else 1


if __name__ == "__main__":
    sys.exit(main())
