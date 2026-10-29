#!/usr/bin/env python3
"""
build_node_index.py -- index every pkg.node declared in assets/rosmodelscatalog/'s
.ros2 files into assets/node_index.json, plus a human-browsable
references/node-catalogue.md. Also indexes every .rossystem composition found there --
its system name plus, per node, its 'from:' and exactly the interfaces it declares --
so a 'subSystems:' reference can be resolved without re-parsing the target file, and so
rosmodel_lint.py can tell whether a given subsystem actually exposes anything to wire
into (some, like turtlebot3_navigation2.rossystem, declare zero interfaces on every
node and are consequently NOT usable via subSystems: no matter how correct the
reference is -- see snappy-dreaming-harbor.md point 3).

WHY THIS EXISTS: the node-side counterpart to build_type_index.py. An agent authoring
a .rossystem has no way to check that a `from: "pkg.node"` or an arrow/parameter target
`"artifact::interface"` refers to something real versus a plausible-but-invented stock
node -- this is exactly the mistake made when nav2_amcl/nav2_bt_navigator/nav2_planner/
nav2_controller were invented as package names for examples/turtlebot2_navigation.rossystem
(the real catalog names are amcl/bt_navigator/planner_server/controller_server, no `nav2_`
prefix). This index closes that gap (consumed by rosmodel_lint.py's RM084-087 and by
collect_deps.py).

Reuses rosmodel_lint.py's YAML-composition helpers (is_mapping/mapping_items/mapping_keys/
mapping_get/is_scalar), the same ones Linter.check_ros2* uses, rather than writing a second
.ros2 reader.

Usage:
    python build_node_index.py
        Writes assets/node_index.json and references/node-catalogue.md.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rosmodel_lint import (  # noqa: E402
    ARROW_ALL, HAVE_YAML, is_mapping, is_scalar, is_sequence, mapping_items, mapping_keys,
    mapping_get, normalise_bare_subsystems, resolve_shared_node_roots, yaml,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
CATALOG_ROOT = os.path.join(PLUGIN_ROOT, "assets", "rosmodelscatalog")
INDEX_PATH = os.path.join(PLUGIN_ROOT, "assets", "node_index.json")
CATALOGUE_MD_PATH = os.path.join(PLUGIN_ROOT, "references", "node-catalogue.md")

INTERFACE_BLOCKS = {
    "publishers": "pub", "subscribers": "sub",
    "serviceservers": "ss", "serviceclients": "sc",
    "actionservers": "as", "actionclients": "ac",
}


def compose_yaml(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    # A multi-entry `subSystems:` block is legal RosSystem and uncomposable YAML; without
    # this the whole file silently returns None, so a catalogued system would vanish from
    # the index and load_local_system would report the reference unresolved. Same normaliser
    # the linter uses, so the two cannot disagree about what parses.
    text, _ = normalise_bare_subsystems(text)
    try:
        return yaml.compose(text)
    except Exception:
        return None


def extract_ros2_nodes(root):
    """Yield (package_name, artifact_name, node_name, interfaces) for one parsed .ros2 file.

    interfaces is {interface_name: kind} where kind is one of pub/sub/ss/sc/as/ac.
    Mirrors Linter.check_ros2/check_ros2_artifacts/check_ros2_artifact_body's traversal
    without the diagnostics -- assumes well-formed input (the vendored catalog).
    """
    if not is_mapping(root):
        return
    items = mapping_items(root)
    if not items:
        return
    pkg_key, pkg_val = items[0]
    if not is_scalar(pkg_key) or not is_mapping(pkg_val):
        return
    pkg_name = pkg_key.value

    artifacts = mapping_get(pkg_val, "artifacts")
    if not is_mapping(artifacts):
        return

    for art_key, art_val in mapping_items(artifacts):
        if not is_scalar(art_key) or not is_mapping(art_val):
            continue
        artifact_name = art_key.value
        node_val = mapping_get(art_val, "node")
        node_name = node_val.value if is_scalar(node_val) else artifact_name

        interfaces = {}
        for block_key, block_val in mapping_items(art_val):
            if not is_scalar(block_key):
                continue
            kind = INTERFACE_BLOCKS.get(block_key.value)
            if kind is None or not is_mapping(block_val):
                continue
            for iface_key, _iface_val in mapping_items(block_val):
                if is_scalar(iface_key):
                    interfaces[iface_key.value] = kind

        yield pkg_name, artifact_name, node_name, interfaces


def extract_rossystem_connections(sys_val):
    """[[fromLabel, toLabel], ...] for one parsed .rossystem, or [] when it declares none.

    A subsystem's own `connections:` are irrelevant to what it EXPOSES -- checkIfInterfaceInSystem
    reads only the `interfaces:` blocks -- so nothing in emission or validation needs them. They
    are indexed for the studio's subsystem views: without them, expanding or opening a catalogued
    subsystem can only draw disconnected cards.

    Every vendored .rossystem currently has an empty list here (turtlebot.rossystem, the largest
    at 3 nodes / 7 interfaces, declares no connections at all). That is the files' truth, not a
    parsing gap.
    """
    out = []
    conn_seq = mapping_get(sys_val, "connections")
    if not is_sequence(conn_seq):
        return out
    for item in conn_seq.value:
        if not is_sequence(item) or len(item.value) != 2:
            continue
        a, b = item.value
        if is_scalar(a) and is_scalar(b):
            out.append([a.value, b.value])
    return out


def extract_rossystem_system(root):
    """Return (system_name, {node_label: {"from": str|None, "interfaces": {name: kind}}},
    has_own_subsystems, connections) for one parsed .rossystem file, or
    (None, None, False, []) if it doesn't look like one.

    Mirrors RosSystemValidator.xtend's checkIfInterfaceInSystem: a node's connectable
    interfaces are exactly what its own 'interfaces:' block declares -- NEVER derived
    from the .ros2 'from:' points at. A node with no 'interfaces:' block (e.g. every
    node in the vendored turtlebot3_navigation2.rossystem) legitimately has zero here;
    that is not a parsing gap, it is why that particular subsystem exposes nothing to
    a 'subSystems:' referencer (see snappy-dreaming-harbor.md point 3).

    has_own_subsystems flags a system that itself declares 'subSystems:' -- referencing
    ONE of these from a third file would be two levels of nesting, and
    checkIfInterfaceInSystem casts unconditionally to RosNode one level down, so the
    real validator throws ClassCastException on it (RM061). rosmodel_lint.py's RM091
    surfaces this before a caller finds out from an oracle stack trace.
    """
    if not is_mapping(root):
        return None, None, False, []
    items = mapping_items(root)
    if not items:
        return None, None, False, []
    sys_key, sys_val = items[0]
    if not is_scalar(sys_key) or not is_mapping(sys_val):
        return None, None, False, []

    has_own_subsystems = mapping_get(sys_val, "subSystems") is not None
    conns = extract_rossystem_connections(sys_val)
    nodes_val = mapping_get(sys_val, "nodes")
    if not is_mapping(nodes_val):
        return sys_key.value, {}, has_own_subsystems, conns

    nodes = {}
    for node_key, node_val in mapping_items(nodes_val):
        if not is_scalar(node_key) or not is_mapping(node_val):
            continue
        from_val = mapping_get(node_val, "from")
        from_str = from_val.value if is_scalar(from_val) else None

        interfaces = {}
        iface_seq = mapping_get(node_val, "interfaces")
        if is_sequence(iface_seq):
            for item in iface_seq.value:
                if not is_mapping(item):
                    continue
                for label_key, arrow_val in mapping_items(item):
                    if not is_scalar(label_key) or not is_scalar(arrow_val):
                        continue
                    kind = arrow_val.value.strip().split("->", 1)[0].strip()
                    if kind in ARROW_ALL:
                        interfaces[label_key.value] = kind

        nodes[node_key.value] = {"from": from_str, "interfaces": interfaces}

    return sys_key.value, nodes, has_own_subsystems, conns


def resolve_catalog_roots():
    """Prefer shared ~/.rostooling/catalogue_repos (or ROSMODEL_CATALOGUE_DIR) when present,
    falling back to assets/rosmodelscatalog/ when absent or empty."""
    shared = resolve_shared_node_roots()
    if shared:
        return shared, shared[0]
    return [CATALOG_ROOT], "assets/rosmodelscatalog/"


def scan_node_roots(roots):
    """Index .ros2 and .rossystem files across one or more roots; returns (nodes, systems)."""
    nodes = {}       # "pkg.node" -> {"file", "artifact", "interfaces"}
    systems = []      # [{"file": ...}] -- .rossystem retrieval references
    visited_files = set()

    for root_base in roots:
        if not os.path.isdir(root_base):
            continue
        for root_dir, dirs, files in os.walk(root_base):
            dirs[:] = [d for d in sorted(dirs) if d not in (".git", "__pycache__", "node_modules")]
            for fname in sorted(files):
                fpath = os.path.join(root_dir, fname)
                rp = os.path.realpath(fpath)
                if rp in visited_files:
                    continue
                visited_files.add(rp)
                rel = os.path.relpath(fpath, root_base).replace(os.sep, "/")

                if fname.endswith(".rossystem"):
                    if not HAVE_YAML:
                        systems.append({"file": rel})
                        continue
                    root = compose_yaml(fpath)
                    sys_name, sys_nodes, has_own_sub, sys_conns = (
                        (None, None, False, []) if root is None
                        else extract_rossystem_system(root))
                    entry = {"file": rel}
                    if sys_name is not None:
                        entry["system"] = sys_name
                        entry["nodes"] = sys_nodes
                        entry["hasOwnSubsystems"] = has_own_sub
                        entry["connections"] = sys_conns
                    systems.append(entry)
                    continue
                if not fname.endswith(".ros2"):
                    continue
                if not HAVE_YAML:
                    continue

                root = compose_yaml(fpath)
                if root is None:
                    continue

                for pkg_name, artifact_name, node_name, interfaces in extract_ros2_nodes(root):
                    key = "%s.%s" % (pkg_name, node_name)
                    if key in nodes:
                        continue  # first file wins; collisions are rare and not silently overwritten
                    nodes[key] = {"file": rel, "artifact": artifact_name, "interfaces": interfaces}

    return nodes, systems


def main():
    roots, source_label = resolve_catalog_roots()
    nodes, systems = scan_node_roots(roots)

    payload = {
        "_meta": {
            "generated_by": "scripts/build_node_index.py",
            "source": source_label,
            "node_count": len(nodes),
            "system_count": len(systems),
        },
        "nodes": nodes,
        "_systems": sorted(systems, key=lambda s: s["file"]),
    }
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")

    lines = [
        "# Node catalogue (generated -- do not hand-edit)",
        "",
        "Generated by `scripts/build_node_index.py` from `assets/rosmodelscatalog/`. "
        "%d nodes, %d system compositions. Regenerate after `scripts/sync_catalogue.sh`, "
        "never edit this file directly." % (len(nodes), len(systems)),
        "",
        "## Nodes (`from: \"pkg.node\"`)",
        "",
        "| pkg.node | Source file | Artifact | Interfaces (kind: name) |",
        "|---|---|---|---|",
    ]
    for key in sorted(nodes):
        entry = nodes[key]
        ifaces = ", ".join("%s: %s" % (k, n) for n, k in sorted(entry["interfaces"].items()))
        lines.append("| `%s` | `%s` | `%s` | %s |"
                      % (key, entry["file"], entry["artifact"], ifaces or "-"))

    lines += [
        "",
        "## System compositions (`subSystems: \"<system>\"` targets)",
        "",
        "Reusable via `subSystems:` only for the nodes whose Interfaces column is non-empty --",
        "a node with no `interfaces:` declared in the source `.rossystem` exposes nothing to a",
        "referencer, even though `from:` still resolves (see `references/node-catalogue.md`'s",
        "generator, `build_node_index.py`, and `snappy-dreaming-harbor.md` point 3 for why).",
        "",
        "| system | Source file | Node | from: | Interfaces (kind: name) |",
        "|---|---|---|---|---|",
    ]
    for sysinfo in sorted(systems, key=lambda s: s["file"]):
        # "nodes" absent means extract_rossystem_system genuinely couldn't parse this file
        # (not a mapping, no items, etc.) -- NOT the same as "parsed fine, zero nodes:",
        # which is a real, valid, if unusual, .rossystem. `not sysinfo.get("nodes")` was
        # truthy-false on both an empty dict and a missing key, mislabelling the latter
        # case as "unparsed" (review-caught bug).
        if "nodes" not in sysinfo:
            lines.append("| - | `%s` | *(unparsed)* | - | - |" % sysinfo["file"])
            continue
        sys_nodes = sysinfo["nodes"]
        if not sys_nodes:
            lines.append("| `%s` | `%s` | *(no nodes: block)* | - | - |"
                          % (sysinfo.get("system", "?"), sysinfo["file"]))
            continue
        for node_label in sorted(sys_nodes):
            node_entry = sys_nodes[node_label]
            ifaces = ", ".join(
                "%s: %s" % (k, n) for n, k in sorted(node_entry["interfaces"].items()))
            lines.append("| `%s` | `%s` | `%s` | `%s` | %s |" % (
                sysinfo.get("system", "?"), sysinfo["file"], node_label,
                node_entry["from"] or "-", ifaces or "-"))

    os.makedirs(os.path.dirname(CATALOGUE_MD_PATH), exist_ok=True)
    with open(CATALOGUE_MD_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    print("Indexed %d nodes, %d systems -> %s" % (len(nodes), len(systems), INDEX_PATH))
    print("Wrote catalogue table -> %s" % CATALOGUE_MD_PATH)


if __name__ == "__main__":
    main()
