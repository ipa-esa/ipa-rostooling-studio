#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ros_plot.py -- render RosTooling .rossystem models as ONE self-contained, interactive
HTML file (vanilla JS, no external libraries, opens as a local file:// with no network).

It REUSES the validated parser in scripts/rosmodel_lint.py (compose + node accessors) and
extends the interaction model of docs/system-overview.html (draggable absolute-positioned
boxes, SVG bezier edges with a shared arrowhead marker, click-to-isolate focus, checkbox
toggles, manual light/dark theme toggle, deterministic grid layout + Reset).

Four abstraction levels share the node layout for L1-L3 (switching never relayouts); L4 is a
separate bipartite dependency view resolved against assets/node_index.json (the catalogue).

Usage:
    ros_plot.py [FILE.rossystem ...] [--out PATH] [--recursive] [--open]
                [--json] [--check] [--no-catalogue]

With no file arguments, globs *.rossystem in the current directory (non-recursively unless
--recursive). Prints the absolute path of the generated HTML to stdout. Does NOT auto-open
unless --open is passed.
"""

import argparse
import glob
import json
import os
import platform
import re
import subprocess
import sys

# ----------------------------------------------------------------------------------------
# Import the validated linter from the same scripts/ directory. We drive its byte/layout and
# YAML-compose stages and its node accessors, but re-implement the arrow / connection parse
# ourselves (the linter's parse_arrow emits findings as a side effect).
# ----------------------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rosmodel_lint as L  # noqa: E402
import _studio_common as C  # noqa: E402


# Malformed-connections fallback: `-[a,b]` / `- [a, b]` / `-[a;b]` / `- [a; b]`, with optional brackets/semicolons.
_CONN_SCALAR_RE = re.compile(r"-\s*\[?\s*([^,;\]]+?)\s*[,;]\s*([^,;\]]+?)\s*\]?;?")

# The seven interaction kinds we band/colour by. `param` is a pseudo-kind for the parameters
# band; the six arrow kinds come straight from the grammar (RosSystem.xtext). Shared with
# ros_studio via _studio_common so the viewer and the editor never drift.
KIND_ORDER = C.KIND_ORDER
KIND_LABELS = C.KIND_LABELS


# ----------------------------------------------------------------------------------------
# Model extraction
# ----------------------------------------------------------------------------------------

def _sanitise_id(text):
    return re.sub(r"[^A-Za-z0-9_]", "_", text)


def _strip_quotes(text):
    stripped, _ = L.strip_literal_quotes(text)
    return stripped


def _parse_arrow(raw):
    """Re-implement RosSystem interface-arrow parse without emitting findings.
    Returns (kind, target, artifact, iface_name). kind is None when there is no '->'."""
    raw = (raw or "").strip()
    if "->" not in raw:
        return None, raw, None, raw
    prefix, target = raw.split("->", 1)
    prefix = prefix.strip()
    target = _strip_quotes(target.strip())
    if "::" in target:
        art, iface = target.split("::", 1)
    else:
        art, iface = None, target
    return prefix, target, art, iface


def _compose_root(path, use_catalogue):
    """Drive the linter's byte/layout + compose stages. Returns (linter, root_or_None,
    compose_error_or_None)."""
    linter = L.Linter(path, use_catalogue=use_catalogue)
    linter.kind = "rossystem"
    try:
        with open(path, "rb") as handle:
            linter.raw = handle.read()
    except (IOError, OSError) as exc:
        return linter, None, "cannot read file: %s" % exc
    linter.check_bytes_and_layout()
    if not L.HAVE_YAML:
        return linter, None, "PyYAML not available: %s" % L.YAML_IMPORT_ERROR
    ok = linter.compose()
    if not ok or linter.root is None:
        err = "YAML could not be composed"
        for f in linter.findings:
            if f.rule == "RM008" and f.severity == L.ERROR:
                err = f.message
                break
        return linter, None, err
    return linter, linter.root, None


def _linter_node_key_count(root):
    """Independent count of node keys the way rosmodel_lint.check_rossystem_nodes iterates
    them (mapping_items under 'nodes:', duplicates included). Used for the cross-check."""
    items = L.mapping_items(root)
    if not items:
        return 0
    _sys_key, sys_val = items[0]
    nodes_node = L.mapping_get(sys_val, "nodes")
    if not L.is_mapping(nodes_node):
        return 0
    return len([k for k, _ in L.mapping_items(nodes_node) if L.is_scalar(k)])


def extract_model(path, use_catalogue=True):
    """Build the plain-dict model a browser tab consumes. Never raises on a malformed file --
    it degrades to a header + banner with 0 nodes and a diagnostic."""
    abspath = os.path.abspath(path)
    model = {
        "file": abspath,
        "name": os.path.basename(abspath),
        "systemName": os.path.splitext(os.path.basename(abspath))[0],
        "fromFile": None,
        "nodes": [],
        "ghosts": [],
        "edges": [],
        "processes": [],
        "subSystems": [],
        "systemParams": [],
        "diagnostics": [],
        "catalogueAvailable": (use_catalogue and L.load_node_index() is not None),
        "nodeCount": 0,
        "edgeCount": 0,
        "connSource": "none",   # none | sequence | scalar-regex
    }

    linter, root, err = _compose_root(path, use_catalogue)
    if root is None:
        model["diagnostics"].append("Could not compose model: %s" % err)
        return model

    items = L.mapping_items(root)
    if not items:
        model["diagnostics"].append("Document has no root mapping.")
        return model

    sys_key, sys_val = items[0]
    if L.is_scalar(sys_key):
        model["systemName"] = sys_key.value

    if not L.is_mapping(sys_val):
        model["diagnostics"].append("System body is not a mapping; rendering header only.")
        return model

    node_index = L.load_node_index() if use_catalogue else None

    # ---- fromFile ---------------------------------------------------------------------
    from_file_node = L.mapping_get(sys_val, "fromFile")
    if L.is_scalar(from_file_node):
        model["fromFile"] = _strip_quotes(from_file_node.value)

    # ---- nodes + interface/parameter walk ---------------------------------------------
    interface_table = {}   # label -> [ {nodeId, kind, target} ]
    label_counts = {}
    nodes_node = L.mapping_get(sys_val, "nodes")
    if L.is_mapping(nodes_node):
        for key, val in L.mapping_items(nodes_node):
            if not L.is_scalar(key):
                continue
            label = key.value
            occ = label_counts.get(label, 0) + 1
            label_counts[label] = occ
            node_id = "n_%s__%d" % (_sanitise_id(label), occ)

            nrec = {
                "id": node_id,
                "label": label,
                "dupIndex": occ,
                "isDup": False,          # set below once we know the total
                "line": L.node_line(key),
                "from": None,
                "package": None,
                "nodeName": None,
                "namespace": None,
                "interfaces": [],
                "params": [],
                "resolved": False,
                "catalogueFile": None,
                "catalogueArtifact": None,
                "catalogueMissing": [],   # file interfaces absent from the catalogue entry
                "catalogueExtra": [],     # catalogue interfaces absent from the file
            }

            if L.is_mapping(val):
                from_node = L.mapping_get(val, "from")
                if L.is_scalar(from_node):
                    raw_from = _strip_quotes(from_node.value)
                    nrec["from"] = raw_from
                    if raw_from.count(".") >= 1:
                        pkg, node_name = raw_from.split(".", 1)
                        nrec["package"] = pkg
                        nrec["nodeName"] = node_name
                    else:
                        nrec["package"] = raw_from

                ns_node = L.mapping_get(val, "namespace")
                if L.is_scalar(ns_node):
                    nrec["namespace"] = _strip_quotes(ns_node.value)

                iface_node = L.mapping_get(val, "interfaces")
                if L.is_sequence(iface_node):
                    for item in iface_node.value:
                        if not L.is_mapping(item):
                            continue
                        for k, v in L.mapping_items(item):
                            if not (L.is_scalar(k) and L.is_scalar(v)):
                                continue
                            ilabel = k.value
                            kind, target, art, iface_name = _parse_arrow(v.value)
                            irec = {
                                "label": ilabel,
                                "kind": kind if kind in L.ARROW_ALL else "sub",
                                "rawKind": kind,
                                "target": target,
                                "artifact": art,
                                "ifaceName": iface_name,
                            }
                            nrec["interfaces"].append(irec)
                            interface_table.setdefault(ilabel, []).append(
                                {"nodeId": node_id, "kind": irec["kind"], "target": target})

                param_node = L.mapping_get(val, "parameters")
                if L.is_sequence(param_node):
                    for item in param_node.value:
                        if not L.is_mapping(item):
                            continue
                        pitems = L.mapping_items(item)
                        if not pitems:
                            continue
                        pk, pv = pitems[0]
                        if not L.is_scalar(pk):
                            continue
                        value_node = L.mapping_get(item, "value")
                        nrec["params"].append({
                            "label": pk.value,
                            "ref": pv.value if L.is_scalar(pv) else "",
                            "value": (value_node.value if L.is_scalar(value_node)
                                      else _param_list_literal(value_node)
                                      if L.is_sequence(value_node)
                                      else _node_repr(value_node)),
                        })

            # catalogue resolution against assets/node_index.json (keyed by package.node)
            if node_index is not None and nrec["from"] and nrec["from"].count(".") >= 1:
                entry = node_index.get(nrec["from"])
                if entry is not None:
                    nrec["resolved"] = True
                    nrec["catalogueFile"] = entry.get("file")
                    nrec["catalogueArtifact"] = entry.get("artifact")
                    cat_ifaces = entry.get("interfaces", {}) or {}
                    file_iface_names = set(i["ifaceName"] for i in nrec["interfaces"]
                                           if i["ifaceName"])
                    nrec["catalogueMissing"] = sorted(
                        n for n in file_iface_names if n not in cat_ifaces)
                    nrec["catalogueExtra"] = sorted(
                        n for n in cat_ifaces if n not in file_iface_names)

            model["nodes"].append(nrec)

    # flag duplicates (any label appearing more than once)
    for nrec in model["nodes"]:
        if label_counts.get(nrec["label"], 0) > 1:
            nrec["isDup"] = True

    node_ids = set(n["id"] for n in model["nodes"])

    # ---- connections: two-path extractor (sequence OR malformed scalar) ---------------
    conn_node = L.mapping_get(sys_val, "connections")
    raw_pairs = []
    if conn_node is None:
        model["connSource"] = "none"
    elif L.is_sequence(conn_node):
        model["connSource"] = "sequence"
        for item in conn_node.value:
            if L.is_sequence(item) and len(item.value) == 2:
                a, b = item.value
                if L.is_scalar(a) and L.is_scalar(b):
                    raw_pairs.append((_strip_quotes(a.value.rstrip(";")), _strip_quotes(b.value.rstrip(";"))))
            elif L.is_sequence(item) and len(item.value) == 1 and L.is_scalar(item.value[0]):
                txt = item.value[0].value
                for sep in (";", ","):
                    if sep in txt:
                        pts = [_strip_quotes(p.strip().rstrip(";")) for p in txt.split(sep, 1)]
                        if len(pts) == 2 and pts[0] and pts[1]:
                            raw_pairs.append((pts[0], pts[1]))
                            break
            elif L.is_scalar(item):
                txt = item.value
                for sep in (";", ","):
                    if sep in txt:
                        pts = [_strip_quotes(p.strip().rstrip(";")) for p in txt.split(sep, 1)]
                        if len(pts) == 2 and pts[0] and pts[1]:
                            raw_pairs.append((pts[0], pts[1]))
                            break
    elif L.is_scalar(conn_node):
        text = conn_node.value or ""
        matches = _CONN_SCALAR_RE.findall(text)
        if matches:
            model["connSource"] = "scalar-regex"
            for a, b in matches:
                raw_pairs.append((_strip_quotes(a.strip()), _strip_quotes(b.strip())))
            model["diagnostics"].append(
                "connections: block collapsed to a bare scalar (malformed '-[a,b]' form); "
                "recovered %d edge(s) by regex fallback." % len(matches))
        else:
            model["connSource"] = "none"  # empty / comment-only scalar -> zero edges, no error

    def _resolve(label, role, line_owner_diag):
        candidates = interface_table.get(label)
        if not candidates:
            ghost_id = "ghost__%s" % _sanitise_id(label)
            if not any(g["id"] == ghost_id for g in model["ghosts"]):
                model["ghosts"].append({"id": ghost_id, "label": label})
            model["diagnostics"].append(
                "connection %s endpoint '%s' matches no declared interface (dangling)."
                % (role, label))
            return ghost_id, None, False
        if len(candidates) > 1:
            owners = ", ".join(sorted(set(
                next((n["label"] for n in model["nodes"] if n["id"] == c["nodeId"]), c["nodeId"])
                for c in candidates)))
            model["diagnostics"].append(
                "connection %s endpoint '%s' is declared in %d nodes (%s); "
                "resolved to the first, marked unresolved."
                % (role, label, len(candidates), owners))
            return candidates[0]["nodeId"], candidates[0]["kind"], False
        return candidates[0]["nodeId"], candidates[0]["kind"], True

    for from_label, to_label in raw_pairs:
        from_id, from_kind, from_ok = _resolve(from_label, "from", None)
        to_id, _to_kind, to_ok = _resolve(to_label, "to", None)
        model["edges"].append({
            "id": "e%d" % len(model["edges"]),
            "fromLabel": from_label,
            "toLabel": to_label,
            "fromNode": from_id,
            "toNode": to_id,
            "kind": from_kind if from_kind in L.ARROW_ALL else "pub",
            "resolved": bool(from_ok and to_ok),
        })

    # ---- processes --------------------------------------------------------------------
    proc_node = L.mapping_get(sys_val, "processes")
    if L.is_mapping(proc_node):
        declared = set(n["label"] for n in model["nodes"])
        for pk, pv in L.mapping_items(proc_node):
            if not (L.is_scalar(pk) and L.is_mapping(pv)):
                continue
            proc = {"name": pk.value, "nodes": [], "threads": None, "missing": []}
            pnodes = L.mapping_get(pv, "nodes")
            if L.is_sequence(pnodes):
                for e in pnodes.value:
                    if L.is_scalar(e):
                        proc["nodes"].append(e.value)
                        if e.value not in declared:
                            proc["missing"].append(e.value)
            tnode = L.mapping_get(pv, "threads")
            if L.is_scalar(tnode):
                proc["threads"] = tnode.value
            model["processes"].append(proc)

    # ---- subSystems (scalar / sequence / mapping forms) -------------------------------
    sub_node = L.mapping_get(sys_val, "subSystems")
    sub_refs = []
    if L.is_scalar(sub_node) and sub_node.value.strip():
        sub_refs.append(_strip_quotes(sub_node.value.strip()))
    elif L.is_sequence(sub_node):
        for e in sub_node.value:
            if L.is_scalar(e):
                sub_refs.append(_strip_quotes(e.value))
    elif L.is_mapping(sub_node):
        for k, _ in L.mapping_items(sub_node):
            if L.is_scalar(k):
                sub_refs.append(k.value)
    for ref in sub_refs:
        model["subSystems"].append({"ref": ref, "crossTab": None})

    # ---- system-level parameters (mapping form OR node-style sequence) ----------------
    sys_params_node = L.mapping_get(sys_val, "parameters")
    if L.is_mapping(sys_params_node):
        for pk, pv in L.mapping_items(sys_params_node):
            if not L.is_scalar(pk):
                continue
            ptype = pdefault = pvalue = pns = None
            if L.is_mapping(pv):
                # `default:` and `value:` are DIFFERENT grammar slots and are kept apart.
                # `default:` belongs to the ParameterType ("ParameterStringType: 'String'
                # ('default:' default=ParameterString)?", Basics.xtext:77-80) -- indentation is
                # hidden whitespace, so `type: String` / `default: x` is one type expression.
                # `value:` is the Parameter's own optional slot (Basics.xtext:47). A file may
                # carry both, and folding them together would emit one of them into the other's
                # position.
                tnode = L.mapping_get(pv, "type")
                dnode = L.mapping_get(pv, "default")
                vnode = L.mapping_get(pv, "value")
                nsnode = L.mapping_get(pv, "ns")
                ptype = tnode.value if L.is_scalar(tnode) else None
                pns = nsnode.value if L.is_scalar(nsnode) else None
                pdefault = dnode.value if L.is_scalar(dnode) else (
                    _param_list_literal(dnode) if L.is_sequence(dnode) else
                    _node_repr(dnode) if dnode is not None else None)
                pvalue = vnode.value if L.is_scalar(vnode) else (
                    _param_list_literal(vnode) if L.is_sequence(vnode) else
                    _node_repr(vnode) if vnode is not None else None)
            model["systemParams"].append({"name": pk.value, "type": ptype,
                                          "default": pdefault, "value": pvalue, "ns": pns})
    elif L.is_sequence(sys_params_node):
        for item in sys_params_node.value:
            if not L.is_mapping(item):
                continue
            pitems = L.mapping_items(item)
            if not pitems:
                continue
            pk, pv = pitems[0]
            if not L.is_scalar(pk):
                continue
            vnode = L.mapping_get(item, "value")
            model["systemParams"].append({
                "name": pk.value,
                "type": pv.value if L.is_scalar(pv) else None,
                "default": None,
                "value": vnode.value if L.is_scalar(vnode) else None,
                "ns": None,
            })

    model["nodeCount"] = len(model["nodes"])
    model["edgeCount"] = len(model["edges"])
    return model


def _param_list_literal(node):
    """A ParameterList `value:`/`default:` sequence node, re-spelled as a bracketed literal
    with each element's own quoting preserved -- e.g. `["x,y", "z"]` (2 elements).

    Mirrors ros_studio._param_default_literal (which this module cannot import: ros_studio
    imports ros_plot, not the other way around), because the two need to agree exactly. Before
    this existed, every caller here fell back to _node_repr, which is fine for DISPLAY but
    silently corrupts a round-trip: it strips each element's quotes and rejoins with ", ",
    so a string element containing its own comma (`"x,y"`) becomes indistinguishable from two
    elements (`x`, `y`). That string is what `ros_studio._list_items` re-splits on later --
    correctly, given what it's handed -- so the comma that was DATA silently became a new
    element boundary, and the real language server still accepts the result. Confirmed via
    scratch repro: `value: ["x,y", "z"]` (2 elements) round-tripped as `['x', 'y', 'z']`
    (3 elements) before this fix.
    """
    if not L.is_sequence(node):
        return None
    parts = []
    for item in node.value:
        if not L.is_scalar(item):
            return None                       # a nested struct: no slot for it, carry nothing
        style = getattr(item, "style", None)
        v = item.value
        if style == '"' or (style == "'" and ("'" in v or "\\" in v)):
            parts.append('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"')
        elif style == "'":
            parts.append("'" + v + "'")
        else:
            parts.append(v)
    return "[" + ", ".join(parts) + "]" if parts else None


def _node_repr(node):
    """Compact display string for a non-scalar YAML value node (sequence/mapping)."""
    if node is None:
        return ""
    if L.is_scalar(node):
        return node.value
    if L.is_sequence(node):
        return "[%s]" % ", ".join(_node_repr(v) for v in node.value)
    if L.is_mapping(node):
        return "{%s}" % ", ".join(
            "%s: %s" % (k.value if L.is_scalar(k) else "?", _node_repr(v))
            for k, v in L.mapping_items(node))
    return ""


def link_subsystems(models):
    """Resolve subSystems references across loaded tabs by system name or file basename."""
    by_name = {}
    for i, m in enumerate(models):
        by_name.setdefault(m["systemName"], i)
        by_name.setdefault(os.path.splitext(m["name"])[0], i)
    for m in models:
        for sub in m["subSystems"]:
            ref = sub["ref"]
            key = os.path.splitext(os.path.basename(ref))[0]
            if ref in by_name:
                sub["crossTab"] = by_name[ref]
            elif key in by_name:
                sub["crossTab"] = by_name[key]


# ----------------------------------------------------------------------------------------
# HTML generation
# ----------------------------------------------------------------------------------------

def render_html(models, title="RosTooling system plot"):
    payload = json.dumps(
        {"title": title, "tabs": models, "kindOrder": KIND_ORDER, "kindLabels": KIND_LABELS},
        ensure_ascii=False)
    return (_HTML_TEMPLATE
            .replace("/*__THEME_BOOT__*/", C.THEME_BOOT_JS)
            .replace("/*__PALETTE_CSS__*/", C.PALETTE_CSS)
            .replace("/*__JS_PRIMITIVES__*/", C.JS_PRIMITIVES)
            .replace("__TITLE__", _html_escape(title))
            .replace("/*__DATA__*/null", payload))


def _html_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# The template is a complete standalone document. All CSS + JS are inlined; there are no
# external references of any kind. Data is injected at the /*__DATA__*/null marker.
_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script>/*__THEME_BOOT__*/</script>
<style>
/*__PALETTE_CSS__*/
  *{box-sizing:border-box;}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--body);
    font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:1400px;margin:0 auto;padding:1.6rem 1.2rem 4rem;display:flex;
    flex-direction:column;gap:1.1rem;}
  .topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;}
  h1{font-family:var(--display);font-size:clamp(1.3rem,3vw,1.8rem);line-height:1.1;font-weight:600;
    letter-spacing:-.01em;margin:0;}
  .eyebrow{font-family:var(--mono);font-size:.68rem;letter-spacing:.13em;text-transform:uppercase;
    color:var(--accent);margin-bottom:.35rem;}
  .themebtn{font-family:var(--mono);font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;
    color:var(--ink-2);background:var(--surface);border:1px solid var(--rule);border-radius:6px;
    padding:.42rem .7rem;cursor:pointer;white-space:nowrap;}
  .themebtn:hover{color:var(--ink);border-color:var(--ink-3);}
  code{font-family:var(--mono);font-size:.86em;background:var(--surface-2);padding:.08em .34em;
    border-radius:3px;color:var(--ink);word-break:break-word;}

  /* tab bar */
  .tabbar{display:flex;flex-wrap:wrap;gap:.35rem;border-bottom:1px solid var(--rule);padding-bottom:.1rem;}
  .tab{font-family:var(--body);font-size:.82rem;font-weight:600;color:var(--ink-2);background:transparent;
    border:1px solid transparent;border-bottom:none;border-radius:6px 6px 0 0;padding:.4rem .8rem;cursor:pointer;
    white-space:nowrap;}
  .tab:hover{color:var(--ink);}
  .tab.active{color:var(--ink);background:var(--surface);border-color:var(--rule);
    box-shadow:0 -1px 0 var(--accent) inset,0 2px 0 var(--surface);}

  .graph-shell{background:var(--surface);border:1px solid var(--rule);border-radius:10px;
    box-shadow:var(--shadow);overflow:hidden;}
  .graph-bar{display:flex;flex-wrap:wrap;align-items:center;gap:.55rem .95rem;padding:.6rem .85rem;
    border-bottom:1px solid var(--rule);background:var(--surface-2);}
  .seg{display:inline-flex;border:1px solid var(--rule);border-radius:7px;overflow:hidden;background:var(--surface);}
  .seg button{font-family:var(--body);font-size:.76rem;font-weight:600;color:var(--ink-2);background:transparent;
    border:none;border-right:1px solid var(--rule);padding:.34rem .7rem;cursor:pointer;}
  .seg button:last-child{border-right:none;}
  .seg button.on{background:var(--accent);color:#fff;}
  .graph-hint{font-size:.76rem;color:var(--ink-2);margin-right:auto;}
  .graph-hint b{color:var(--ink);font-weight:650;}
  .ctl{display:inline-flex;align-items:center;gap:.36rem;font-size:.76rem;color:var(--ink-2);cursor:pointer;}
  .ctl input{accent-color:var(--accent);width:14px;height:14px;cursor:pointer;}
  .btn{font-family:var(--body);font-size:.75rem;font-weight:600;color:var(--ink-2);background:var(--surface);
    border:1px solid var(--rule);border-radius:5px;padding:.3rem .65rem;cursor:pointer;}
  .btn:hover{color:var(--ink);border-color:var(--ink-3);}
  .graph-scroll{overflow:auto;max-height:78vh;}
  .graph{position:relative;margin:0;touch-action:none;background:
    radial-gradient(circle at 1px 1px, var(--rule-soft) 1px, transparent 0) 0 0/22px 22px;}
  .graph svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;}
  .collabel{position:absolute;font-family:var(--mono);font-size:.66rem;letter-spacing:.1em;
    text-transform:uppercase;color:var(--ink-3);white-space:nowrap;pointer-events:none;top:8px;}

  .node{position:absolute;box-sizing:border-box;background:var(--surface);color:var(--ink);
    border:1.5px solid var(--ink-3);border-radius:8px;cursor:grab;user-select:none;
    box-shadow:var(--shadow);font-family:var(--body);text-align:left;overflow:visible;
    transition:opacity .16s ease, box-shadow .16s ease;}
  .node:active{cursor:grabbing;}
  .node .nhead{padding:.42rem .55rem;display:flex;flex-direction:column;gap:.1rem;}
  .node .nt{font-size:.79rem;font-weight:650;line-height:1.2;word-break:break-word;}
  .node .ns{font-size:.66rem;line-height:1.25;opacity:.82;font-family:var(--mono);word-break:break-word;}
  .node .badge{position:absolute;top:-8px;right:-8px;font-family:var(--mono);font-size:.6rem;font-weight:700;
    background:var(--warn);color:#1a1205;border-radius:9px;padding:.02rem .35rem;box-shadow:var(--shadow);}
  .node.ghost{border-style:dashed;background:var(--dead-wash);border-color:var(--dead);opacity:.85;}
  .node.unresolved{border-color:var(--warn);}
  .node .catflag{font-size:.6rem;font-family:var(--mono);color:var(--warn);margin-top:.15rem;}
  .node .catok{font-size:.6rem;font-family:var(--mono);color:var(--accent-2);margin-top:.1rem;}

  /* port stubs (L2) */
  .ports{display:flex;flex-wrap:wrap;gap:.2rem;padding:.1rem .45rem .42rem;}
  .port{width:11px;height:11px;border-radius:3px;border:1px solid var(--pc,var(--ink-3));
    background:var(--pbg,var(--surface-2));}
  /* interface rows (L3) */
  .band{border-top:1px solid var(--rule-soft);padding:.22rem .5rem;}
  .band-h{font-family:var(--mono);font-size:.58rem;letter-spacing:.08em;text-transform:uppercase;
    color:var(--kc,var(--ink-3));font-weight:700;margin-bottom:.12rem;}
  .irow{display:flex;align-items:center;gap:.35rem;font-size:.66rem;line-height:1.35;padding:.02rem 0;}
  .idot{width:8px;height:8px;border-radius:2px;background:var(--kc,var(--ink-3));flex:0 0 auto;}
  .iname{font-weight:600;word-break:break-word;}
  .itarget{font-family:var(--mono);color:var(--ink-3);word-break:break-word;}
  .prow{font-size:.64rem;color:var(--ink-2);font-family:var(--mono);padding:.02rem 0;word-break:break-word;}
  .nsrow{font-size:.62rem;color:var(--ink-3);font-family:var(--mono);padding:.1rem .5rem;border-top:1px solid var(--rule-soft);}

  /* kind colouring hooks */
  .k-pub{--kc:var(--k-pub);--pc:var(--k-pub);--pbg:var(--k-pub-bg);}
  .k-sub{--kc:var(--k-sub);--pc:var(--k-sub);--pbg:var(--k-sub-bg);}
  .k-ss{--kc:var(--k-ss);--pc:var(--k-ss);--pbg:var(--k-ss-bg);}
  .k-sc{--kc:var(--k-sc);--pc:var(--k-sc);--pbg:var(--k-sc-bg);}
  .k-as{--kc:var(--k-as);--pc:var(--k-as);--pbg:var(--k-as-bg);}
  .k-ac{--kc:var(--k-ac);--pc:var(--k-ac);--pbg:var(--k-ac-bg);}
  .k-param{--kc:var(--k-param);--pc:var(--k-param);--pbg:var(--k-param-bg);}

  /* focus / filter */
  .graph.has-focus .node{opacity:.22;}
  .graph.has-focus .node.rel{opacity:1;}
  .graph.has-focus .node.foc{opacity:1;box-shadow:var(--shadow-lift);outline:2px solid var(--edge-hot);outline-offset:2px;}
  .graph.dragging .node{transition:none;}
  .node.kfaded{opacity:.28;}
  path.edge{fill:none;stroke:var(--ek,var(--edge));stroke-width:1.6;opacity:.62;
    transition:opacity .16s,stroke-width .16s;}
  path.edge.dangling{stroke-dasharray:5 4;}
  path.edge.unres{stroke-dasharray:2 3;}
  .graph.has-focus path.edge{opacity:.08;}
  .graph.has-focus path.edge.on{opacity:1;stroke-width:2.3;}
  path.edge.efaded{opacity:.06 !important;}
  .elabel{font-family:var(--mono);font-size:.6rem;fill:var(--ink-3);opacity:0;transition:opacity .16s;}
  .graph.has-focus .elabel.on{opacity:1;fill:var(--edge-hot);}

  /* legend */
  .legend{display:flex;flex-wrap:wrap;gap:.4rem .5rem;padding:.7rem .85rem;background:var(--surface-2);
    border-top:1px solid var(--rule);}
  .legend.hidden{display:none;}
  .lgk{display:inline-flex;align-items:center;gap:.36rem;font-size:.74rem;color:var(--ink-2);cursor:pointer;
    border:1px solid var(--rule);border-radius:20px;padding:.16rem .55rem;background:var(--surface);}
  .lgk:hover{border-color:var(--ink-3);}
  .lgk.off{opacity:.4;}
  .lgk .sw{width:13px;height:13px;border-radius:3px;background:var(--swc);border:1px solid var(--swc);}
  .lg-note{font-size:.72rem;color:var(--ink-3);display:inline-flex;align-items:center;gap:.5rem;
    margin-left:auto;}
  .lg-note .ls{width:22px;height:0;border-top:1.7px solid var(--edge);display:inline-block;}
  .lg-note .ls.dash{border-top-style:dashed;}

  /* side panels */
  .panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:.7rem;}
  .panel{background:var(--surface);border:1px solid var(--rule);border-radius:9px;padding:.75rem .9rem;
    display:flex;flex-direction:column;gap:.35rem;}
  .panel.hidden{display:none;}
  .panel h3{font-family:var(--body);font-size:.82rem;font-weight:700;margin:0;color:var(--ink);
    display:flex;align-items:center;gap:.4rem;}
  .panel .pk{font-family:var(--mono);font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;
    color:var(--ink-3);}
  .panel ul{margin:.1rem 0 0;padding-left:1.05rem;font-size:.75rem;color:var(--ink-2);
    display:flex;flex-direction:column;gap:.2rem;}
  .panel .diag{font-size:.72rem;color:var(--warn);}
  .banner{background:var(--warn-wash);border:1px solid var(--warn);border-radius:8px;padding:.6rem .8rem;
    font-size:.78rem;color:var(--ink);}
  .xlink{color:var(--accent);cursor:pointer;text-decoration:underline;text-underline-offset:2px;}
  .pill{display:inline-block;font-family:var(--mono);font-size:.6rem;letter-spacing:.04em;text-transform:uppercase;
    font-weight:700;padding:.08em .45em;border-radius:3px;}
  .pill.ok{background:var(--accent-wash);color:var(--accent-2);}
  .pill.warn{background:var(--warn-wash);color:var(--warn);}
  @media (prefers-reduced-motion: reduce){ *{animation:none !important;transition:none !important;} }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <div class="eyebrow">RosTooling · .rossystem plot</div>
      <h1 id="doctitle">__TITLE__</h1>
    </div>
    <button class="themebtn" id="themebtn" type="button" aria-label="Toggle colour theme">&#9680; Theme</button>
  </div>
  <div class="tabbar" id="tabbar"></div>
  <div id="tabhost"></div>
</div>
<script>
"use strict";
/*__JS_PRIMITIVES__*/
var DATA = /*__DATA__*/null;
(function(){
  var NS=STUDIO.NS;
  var KIND_ORDER = DATA.kindOrder, KIND_LABELS = DATA.kindLabels;
  var ARROW_KINDS = ["pub","sub","ss","sc","as","ac"];

  // layout constants
  var NW=196, GAPX=64, GAPY=52, PADX=30, PADY=40;
  var DEPS_NW=210, DEPS_COLGAP=320, DEPS_ROWH=64, DEPS_PAD=40;

  var el=STUDIO.el, svgEl=STUDIO.svgEl, txt=STUDIO.txt;

  var controllers = [];

  function buildTab(model, index){
    var host = el("div","tabpane");
    host.style.display = index===0 ? "block" : "none";

    // ---- header banner when compose failed / empty --------------------------------
    var topInfo = el("div");
    topInfo.style.marginBottom = ".7rem";
    if(model.nodeCount===0){
      var b = el("div","banner");
      b.textContent = "System “"+model.systemName+"” rendered with 0 nodes."
        + (model.fromFile ? " Declared launch file: "+model.fromFile+"." : "")
        + (model.diagnostics.length ? " See diagnostics below." : "");
      topInfo.appendChild(b);
    }
    host.appendChild(topInfo);

    var shell = el("div","graph-shell");
    // control bar
    var bar = el("div","graph-bar");
    var seg = el("div","seg");
    var levels = [["1","System"],["2","Interfaces"],["3","Full"],["4","Deps"]];
    var segBtns=[];
    levels.forEach(function(lv){
      var bt=el("button"); bt.textContent=lv[1]; bt.dataset.level=lv[0];
      seg.appendChild(bt); segBtns.push(bt);
    });
    bar.appendChild(seg);
    var hint=el("div","graph-hint");
    hint.innerHTML="<b>Drag</b> to rearrange · <b>click</b> a box to isolate · keys <b>1-4</b>";
    bar.appendChild(hint);
    var legToggle=el("label","ctl");
    var legCb=el("input"); legCb.type="checkbox"; legCb.checked=true;
    legToggle.appendChild(legCb); legToggle.appendChild(document.createTextNode(" Legend"));
    bar.appendChild(legToggle);
    var lblToggle=el("label","ctl");
    var lblCb=el("input"); lblCb.type="checkbox"; lblCb.checked=false;
    lblToggle.appendChild(lblCb); lblToggle.appendChild(document.createTextNode(" Edge labels"));
    bar.appendChild(lblToggle);
    var resetBtn=el("button","btn"); resetBtn.textContent="Reset layout";
    bar.appendChild(resetBtn);
    shell.appendChild(bar);

    var scroll=el("div","graph-scroll");
    var graph=el("div","graph");
    var svg=svgEl("svg"); svg.setAttribute("aria-hidden","true");
    STUDIO.makeArrowMarkers(svg, index);
    graph.appendChild(svg); scroll.appendChild(graph); shell.appendChild(scroll);

    // legend
    var legend=el("div","legend");
    var kindActive={}; KIND_ORDER.forEach(function(k){ kindActive[k]=true; });
    KIND_ORDER.forEach(function(k){
      var row=el("span","lgk"); row.dataset.kind=k;
      var sw=el("span","sw"); sw.style.setProperty("--swc","var(--k-"+k+")");
      row.appendChild(sw); row.appendChild(document.createTextNode(KIND_LABELS[k]));
      row.addEventListener("click",function(){
        kindActive[k]=!kindActive[k]; row.classList.toggle("off",!kindActive[k]); applyFilter();
      });
      legend.appendChild(row);
    });
    var note=el("span","lg-note");
    note.innerHTML='<span><span class="ls"></span> connection</span>'
      +'<span><span class="ls dash"></span> dangling / ambiguous</span>';
    legend.appendChild(note);
    shell.appendChild(legend);
    host.appendChild(shell);

    // ---- side panels ---------------------------------------------------------------
    var panels=el("div","panels"); panels.style.marginTop=".7rem";
    // meta panel
    var meta=el("div","panel");
    var mh=el("h3"); mh.textContent="System"; meta.appendChild(mh);
    var mkv=el("div"); mkv.style.fontSize=".76rem"; mkv.style.color="var(--ink-2)";
    mkv.innerHTML="<div><span class='pk'>name</span> "+esc(model.systemName)+"</div>"
      +"<div><span class='pk'>nodes</span> "+model.nodeCount+"</div>"
      +"<div><span class='pk'>connections</span> "+model.edgeCount
        +(model.connSource==="scalar-regex"?" <span class='pill warn'>regex fallback</span>":"")+"</div>"
      +"<div><span class='pk'>catalogue</span> "+(model.catalogueAvailable
        ?"<span class='pill ok'>available</span>":"<span class='pill warn'>unavailable</span>")+"</div>"
      +(model.fromFile?"<div><span class='pk'>fromFile</span> <code>"+esc(model.fromFile)+"</code></div>":"");
    meta.appendChild(mkv);
    panels.appendChild(meta);

    if(model.processes.length){
      var pp=el("div","panel"); var pph=el("h3"); pph.textContent="Processes"; pp.appendChild(pph);
      var pul=el("ul");
      model.processes.forEach(function(pr){
        var li=el("li");
        li.textContent=pr.name+": "+(pr.nodes.join(", ")||"(none)")
          +(pr.threads!=null?" · threads="+pr.threads:"");
        if(pr.missing.length){ var w=el("span","diag"); w.textContent=" ⚠ not in system: "+pr.missing.join(", "); li.appendChild(w); }
        pul.appendChild(li);
      });
      pp.appendChild(pul); panels.appendChild(pp);
    }
    if(model.subSystems.length){
      var sp=el("div","panel"); var sph=el("h3"); sph.textContent="Subsystems"; sp.appendChild(sph);
      var sul=el("ul");
      model.subSystems.forEach(function(sub){
        var li=el("li");
        li.appendChild(document.createTextNode(sub.ref+" "));
        if(sub.crossTab!=null){
          var a=el("span","xlink"); a.textContent="(open tab →)";
          a.addEventListener("click",function(){ activate(sub.crossTab); });
          li.appendChild(a);
        } else {
          var s=el("span"); s.style.color="var(--ink-3)"; s.textContent="(reference stub — not loaded)";
          li.appendChild(s);
        }
        sul.appendChild(li);
      });
      sp.appendChild(sul); panels.appendChild(sp);
    }
    if(model.systemParams.length){
      var yp=el("div","panel"); var yph=el("h3"); yph.textContent="System parameters"; yp.appendChild(yph);
      var yul=el("ul");
      model.systemParams.forEach(function(p){
        var li=el("li");
        li.textContent=p.name+(p.type?": "+p.type:"")+(p.default!=null&&p.default!==""?" = "+p.default:"");
        yul.appendChild(li);
      });
      yp.appendChild(yul); panels.appendChild(yp);
    }
    if(model.diagnostics.length){
      var dp=el("div","panel"); var dph=el("h3"); dph.textContent="Diagnostics (model facts)"; dp.appendChild(dph);
      var dul=el("ul");
      model.diagnostics.forEach(function(d){ var li=el("li","diag"); li.className=""; var s=el("span","diag"); s.textContent=d; li.appendChild(s); dul.appendChild(li); });
      dp.appendChild(dul); panels.appendChild(dp);
    }
    host.appendChild(panels);

    // =================================================================================
    //  node objects + positions
    // =================================================================================
    var nodes = model.nodes.map(function(n){ return {m:n, id:n.id, ghost:false}; });
    model.ghosts.forEach(function(g){ nodes.push({m:{label:g.label, interfaces:[], params:[], id:g.id}, id:g.id, ghost:true}); });
    var byId={}; nodes.forEach(function(nd){ byId[nd.id]=nd; });

    var gpos = STUDIO.gridPositions(nodes.length,
      {padx:PADX, pady:PADY, nw:NW, gapx:GAPX, rowh:220, gapy:GAPY});
    nodes.forEach(function(nd,i){
      nd.mainHome={x:gpos[i].x, y:gpos[i].y};
      nd.main={x:nd.mainHome.x, y:nd.mainHome.y};
    });
    // deps homes: node boxes left column, package boxes right column
    var packages={}, pkgList=[];
    model.nodes.forEach(function(n){
      var p=n.package||"(local)";
      if(!packages[p]){ packages[p]={name:p, nodeIds:[], resolved:false}; pkgList.push(packages[p]); }
      packages[p].nodeIds.push(n.id);
      if(n.resolved) packages[p].resolved=true;
    });
    var artifacts={}, artList=[];
    model.nodes.forEach(function(n){
      n.interfaces.forEach(function(i){
        if(i.artifact && !artifacts[i.artifact]){ artifacts[i.artifact]={name:i.artifact}; artList.push(artifacts[i.artifact]); }
      });
    });
    nodes.forEach(function(nd,i){ nd.depsHome={x:DEPS_PAD, y:DEPS_PAD+i*DEPS_ROWH}; nd.deps={x:nd.depsHome.x, y:nd.depsHome.y}; });
    pkgList.forEach(function(p,i){ p.home={x:DEPS_PAD+DEPS_COLGAP, y:DEPS_PAD+i*DEPS_ROWH}; p.x=p.home.x; p.y=p.home.y; p.id="pkg_"+i; });

    // build DOM: node boxes (L1-L3)
    nodes.forEach(function(nd){
      var box=el("div","node");
      box.style.width=NW+"px";
      box.setAttribute("tabindex","0"); box.setAttribute("role","button");
      box.setAttribute("aria-label", nd.m.label);
      if(nd.ghost) box.classList.add("ghost");
      nd.el=box; graph.appendChild(box);
    });
    // package boxes (L4)
    pkgList.forEach(function(p){
      var box=el("div","node"); box.style.width=DEPS_NW+"px"; box.style.display="none";
      var head=el("div","nhead");
      var t=el("div","nt"); t.textContent=p.name; head.appendChild(t);
      var s=el("div","ns"); s.textContent=(p.resolved?"in catalogue":"not in catalogue")+" · "+p.nodeIds.length+" node(s)"; head.appendChild(s);
      box.appendChild(head);
      if(!p.resolved) box.classList.add("unresolved");
      p.el=box; graph.appendChild(box);
    });

    // ---- edges (L1-L3 use model.edges; L4 uses node->package) ---------------------
    var edgeObjs = model.edges.map(function(e){
      var p=svgEl("path"); p.setAttribute("class","edge"); p.setAttribute("marker-end","url(#ah"+index+")");
      p.style.setProperty("--ek","var(--k-"+(ARROW_KINDS.indexOf(e.kind)>=0?e.kind:"pub")+")");
      if(!e.resolved) p.classList.add("unres");
      svg.appendChild(p);
      var lb=svgEl("text"); lb.setAttribute("class","elabel"); lb.setAttribute("text-anchor","middle");
      lb.textContent=e.fromLabel+" → "+e.toLabel; svg.appendChild(lb);
      return {e:e, path:p, label:lb};
    });
    var depEdges=[];
    pkgList.forEach(function(p){
      p.nodeIds.forEach(function(nid){
        var path=svgEl("path"); path.setAttribute("class","edge"); path.setAttribute("marker-end","url(#ah"+index+")");
        path.style.display="none"; svg.appendChild(path);
        depEdges.push({from:nid, pkg:p, path:path});
      });
    });

    // =================================================================================
    //  rendering per level
    // =================================================================================
    var level=1, focus=null;

    function renderNodeContent(nd){
      var box=nd.el; box.innerHTML="";
      box.classList.remove("k-pub","k-sub","k-ss","k-sc","k-as","k-ac","k-param","unresolved");
      var m=nd.m;
      var head=el("div","nhead");
      var t=el("div","nt"); t.textContent=m.label; head.appendChild(t);
      if(nd.ghost){ var g=el("div","ns"); g.textContent="(dangling endpoint)"; head.appendChild(g); box.appendChild(head); box.style.width=NW+"px"; return; }
      if(m.from){ var s=el("div","ns"); s.textContent=m.from; head.appendChild(s); }
      if(m.isDup){ var bd=el("span","badge"); bd.textContent="#"+m.dupIndex; box.appendChild(bd); }
      if(!m.resolved && m.from) box.classList.add("unresolved");
      box.appendChild(head);
      box.style.width=NW+"px";

      if(level===1){ return; }

      if(level===2){
        // grouped, coloured port stubs on the rim
        var ports=el("div","ports");
        KIND_ORDER.forEach(function(k){
          m.interfaces.filter(function(i){return i.kind===k;}).forEach(function(i){
            var d=el("div","port k-"+k); d.title=k+"-> "+i.label+" ("+(i.target||"")+")";
            d.dataset.kind=k; d.dataset.iface=i.label;
            ports.appendChild(d);
          });
        });
        m.params.forEach(function(p){ var d=el("div","port k-param"); d.title="param "+p.label; d.dataset.kind="param"; ports.appendChild(d); });
        box.appendChild(ports);
        return;
      }

      // level 3: full detail bands
      if(m.namespace){ var nsr=el("div","nsrow"); nsr.textContent="namespace: "+m.namespace; box.appendChild(nsr); }
      KIND_ORDER.forEach(function(k){
        if(k==="param") return;
        var list=m.interfaces.filter(function(i){return i.kind===k;});
        if(!list.length) return;
        var band=el("div","band k-"+k); band.dataset.kind=k;
        var bh=el("div","band-h"); bh.textContent=KIND_LABELS[k]+" ("+list.length+")"; band.appendChild(bh);
        list.forEach(function(i){
          var row=el("div","irow"); row.dataset.iface=i.label;
          var dot=el("span","idot"); row.appendChild(dot);
          txt(row,"iname",i.label);
          if(i.target){ txt(row,"itarget",i.target); }
          band.appendChild(row);
        });
        box.appendChild(band);
      });
      if(m.params.length){
        var pband=el("div","band k-param"); pband.dataset.kind="param";
        var ph=el("div","band-h"); ph.textContent="Parameters ("+m.params.length+")"; pband.appendChild(ph);
        m.params.forEach(function(p){
          var pr=el("div","prow"); pr.textContent=p.label+(p.value!=null&&p.value!==""?" = "+p.value:""); pband.appendChild(pr);
        });
        box.appendChild(pband);
      }
      if(m.catalogueMissing && m.catalogueMissing.length){
        var cf=el("div","catflag"); cf.style.padding=".15rem .5rem";
        cf.textContent="⚠ not in catalogue: "+m.catalogueMissing.join(", ");
        box.appendChild(cf);
      }
      if(m.resolved && m.catalogueExtra && m.catalogueExtra.length){
        var ce=el("div","catok"); ce.style.padding=".1rem .5rem .35rem";
        ce.textContent="catalogue also declares: "+m.catalogueExtra.join(", ");
        box.appendChild(ce);
      }
    }

    function anchorCenter(nd, ifaceLabel){
      // pick the DOM anchor for an edge endpoint given the current level
      var base = level===4 ? nd.deps : nd.main;
      var box=nd.el, cx=base.x+box.offsetWidth/2, cy=base.y+box.offsetHeight/2;
      if((level===2||level===3) && ifaceLabel){
        var sel = level===2 ? '.port[data-iface="'+cssEsc(ifaceLabel)+'"]' : '.irow[data-iface="'+cssEsc(ifaceLabel)+'"]';
        var child=box.querySelector(sel);
        if(child){ cx=base.x+child.offsetLeft+child.offsetWidth/2; cy=base.y+child.offsetTop+child.offsetHeight/2; }
      }
      return {x:cx, y:cy};
    }

    var bezier = STUDIO.bezier;

    function draw(){
      if(level===4){ drawDeps(); return; }
      edgeObjs.forEach(function(eo){
        var e=eo.e, A=byId[e.fromNode], B=byId[e.toNode];
        if(!A||!B){ eo.path.style.display="none"; if(eo.label) eo.label.style.display="none"; return; }
        eo.path.style.display=""; if(eo.label) eo.label.style.display="";
        var a=anchorCenter(A, e.fromLabel), b=anchorCenter(B, e.toLabel);
        eo.path.setAttribute("d", bezier(a.x,a.y,b.x,b.y));
        var dangling = A.ghost || B.ghost;
        eo.path.classList.toggle("dangling", dangling);
        if(eo.label){ eo.label.setAttribute("x",(a.x+b.x)/2); eo.label.setAttribute("y",(a.y+b.y)/2-5); }
      });
    }
    function drawDeps(){
      depEdges.forEach(function(de){
        var A=byId[de.from], P=de.pkg;
        var ax=A.deps.x+A.el.offsetWidth, ay=A.deps.y+A.el.offsetHeight/2;
        var bx=P.x, by=P.y+P.el.offsetHeight/2;
        de.path.setAttribute("d", bezier(ax,ay,bx,by));
      });
    }

    function sizeGraph(){
      var maxX=0,maxY=0;
      if(level===4){
        nodes.forEach(function(nd){ maxX=Math.max(maxX,nd.deps.x+nd.el.offsetWidth); maxY=Math.max(maxY,nd.deps.y+nd.el.offsetHeight); });
        pkgList.forEach(function(p){ maxX=Math.max(maxX,p.x+p.el.offsetWidth); maxY=Math.max(maxY,p.y+p.el.offsetHeight); });
      } else {
        nodes.forEach(function(nd){ maxX=Math.max(maxX,nd.main.x+nd.el.offsetWidth); maxY=Math.max(maxY,nd.main.y+nd.el.offsetHeight); });
      }
      var W=Math.max(maxX+PADX, 640), H=Math.max(maxY+PADX, 360);
      graph.style.width=W+"px"; graph.style.height=H+"px"; svg.setAttribute("viewBox","0 0 "+W+" "+H);
    }

    function place(){
      nodes.forEach(function(nd){
        var pos = level===4 ? nd.deps : nd.main;
        nd.el.style.left=pos.x+"px"; nd.el.style.top=pos.y+"px";
      });
      pkgList.forEach(function(p){ p.el.style.left=p.x+"px"; p.el.style.top=p.y+"px"; });
    }

    function setLevel(lv){
      level=lv;
      segBtns.forEach(function(bt){ bt.classList.toggle("on", +bt.dataset.level===lv); });
      var depsMode = lv===4;
      nodes.forEach(function(nd){ if(lv!==4) renderNodeContent(nd); nd.el.style.width=(lv===4?DEPS_NW:NW)+"px"; if(lv===4) renderDepsNode(nd); });
      pkgList.forEach(function(p){ p.el.style.display=depsMode?"":"none"; });
      depEdges.forEach(function(de){ de.path.style.display=depsMode?"":"none"; });
      edgeObjs.forEach(function(eo){ eo.path.style.display=depsMode?"none":""; if(eo.label) eo.label.style.display=depsMode?"none":""; });
      place(); sizeGraph(); draw(); applyFocus(); applyFilter();
    }
    function renderDepsNode(nd){
      var box=nd.el; box.innerHTML=""; box.classList.remove("k-pub","k-sub","k-ss","k-sc","k-as","k-ac","k-param");
      var m=nd.m;
      var head=el("div","nhead");
      var t=el("div","nt"); t.textContent=m.label; head.appendChild(t);
      var s=el("div","ns"); s.textContent=(m.from||"(no from)"); head.appendChild(s);
      box.appendChild(head);
      if(!nd.ghost){
        var flag=el("div","catflag"); flag.style.padding=".1rem .5rem .3rem";
        if(m.resolved){ flag.className="catok"; flag.style.padding=".1rem .5rem .3rem"; flag.textContent="pkg → "+(m.package||"?")+"  · resolved "+(m.catalogueFile||""); }
        else { flag.textContent="pkg → "+(m.package||"?")+(model.catalogueAvailable?"  · not in catalogue":"  · catalogue unavailable"); }
        box.appendChild(flag);
      }
    }

    // ---- focus (click to isolate) --------------------------------------------------
    function applyFocus(){
      if(!focus){
        graph.classList.remove("has-focus");
        nodes.forEach(function(nd){ nd.el.classList.remove("foc","rel"); });
        edgeObjs.forEach(function(eo){ eo.path.classList.remove("on"); if(eo.label) eo.label.classList.remove("on"); });
        return;
      }
      graph.classList.add("has-focus");
      var rel={}; rel[focus]=1;
      if(level!==4){
        edgeObjs.forEach(function(eo){
          var hit=(eo.e.fromNode===focus||eo.e.toNode===focus);
          eo.path.classList.toggle("on",hit); if(eo.label) eo.label.classList.toggle("on",hit);
          if(hit){ rel[eo.e.fromNode]=1; rel[eo.e.toNode]=1; }
        });
      } else {
        depEdges.forEach(function(de){ if(de.from===focus) rel[de.from]=1; });
      }
      nodes.forEach(function(nd){
        nd.el.classList.toggle("foc",nd.id===focus);
        nd.el.classList.toggle("rel",!!rel[nd.id]&&nd.id!==focus);
      });
    }

    // ---- kind filter (legend click) ------------------------------------------------
    function applyFilter(){
      var allOn = KIND_ORDER.every(function(k){return kindActive[k];});
      // edges by kind
      edgeObjs.forEach(function(eo){
        var k=eo.e.kind; var on = allOn || kindActive[k];
        eo.path.classList.toggle("efaded", !on);
      });
      // interface rows / ports / bands within nodes
      if(!allOn && (level===2||level===3)){
        nodes.forEach(function(nd){
          nd.el.querySelectorAll("[data-kind]").forEach(function(elem){
            var k=elem.dataset.kind; elem.style.opacity = kindActive[k]?"":"0.15";
          });
        });
      } else {
        nodes.forEach(function(nd){ nd.el.querySelectorAll("[data-kind]").forEach(function(elem){ elem.style.opacity=""; }); });
      }
    }

    // ---- dragging (shared pointer-capture primitive) -------------------------------
    function wireDrag(getPos, node){
      STUDIO.makeDraggable(node.el, {
        getPos:function(){ return getPos(node); },
        onStart:function(ev){ graph.classList.add("dragging"); ev.preventDefault(); },
        onMove:function(x,y){
          var pos=getPos(node); pos.x=x; pos.y=y;
          node.el.style.left=x+"px"; node.el.style.top=y+"px"; draw();
        },
        onEnd:function(wasClick){
          graph.classList.remove("dragging");
          if(wasClick && node.id!=null){ focus=(focus===node.id)?null:node.id; applyFocus(); }
        }
      });
    }
    nodes.forEach(function(nd){
      wireDrag(function(n){ return level===4?n.deps:n.main; }, nd);
      nd.el.addEventListener("keydown",function(ev){
        if(ev.key==="Enter"||ev.key===" "){ ev.preventDefault(); focus=(focus===nd.id)?null:nd.id; applyFocus(); }
        else if(ev.key==="Escape"){ focus=null; applyFocus(); }
      });
    });
    pkgList.forEach(function(p){ wireDrag(function(pp){ return pp; }, p); });

    graph.addEventListener("pointerdown",function(ev){ if(ev.target===graph||ev.target===svg){ focus=null; applyFocus(); } });

    // ---- controls ------------------------------------------------------------------
    segBtns.forEach(function(bt){ bt.addEventListener("click",function(){ setLevel(+bt.dataset.level); }); });
    legCb.addEventListener("change",function(){ legend.classList.toggle("hidden",!legCb.checked); });
    lblCb.addEventListener("change",function(){ graph.classList.toggle("show-elabels",lblCb.checked); syncLabels(); });
    function syncLabels(){ edgeObjs.forEach(function(eo){ if(eo.label) eo.label.style.opacity=(lblCb.checked&&level!==4)?"1":""; }); }
    resetBtn.addEventListener("click",function(){
      nodes.forEach(function(nd){ nd.main={x:nd.mainHome.x,y:nd.mainHome.y}; nd.deps={x:nd.depsHome.x,y:nd.depsHome.y}; });
      pkgList.forEach(function(p){ p.x=p.home.x; p.y=p.home.y; });
      focus=null; place(); sizeGraph(); draw(); applyFocus();
    });

    setLevel(1);

    return {
      host:host,
      focusKey:function(ev){
        if(ev.key>="1"&&ev.key<="4"){ setLevel(+ev.key); }
      }
    };
  }

  var esc=STUDIO.esc, cssEsc=STUDIO.cssEsc;

  // ---- tab wiring ------------------------------------------------------------------
  var tabbar=document.getElementById("tabbar");
  var tabhost=document.getElementById("tabhost");
  var tabBtns=[];
  function activate(i){
    controllers.forEach(function(c,j){ c.host.style.display = j===i?"block":"none"; });
    tabBtns.forEach(function(b,j){ b.classList.toggle("active", j===i); });
    activeIndex=i;
  }
  var activeIndex=0;

  DATA.tabs.forEach(function(model,i){
    var c=buildTab(model,i);
    controllers.push(c);
    tabhost.appendChild(c.host);
    var b=document.createElement("button"); b.className="tab"+(i===0?" active":"");
    b.textContent=model.systemName; b.title=model.file;
    b.addEventListener("click",function(){ activate(i); });
    tabbar.appendChild(b); tabBtns.push(b);
  });
  if(DATA.tabs.length<=1){ tabbar.style.display="none"; }

  document.addEventListener("keydown",function(ev){
    if(ev.target && /^(INPUT|TEXTAREA)$/.test(ev.target.tagName)) return;
    if(controllers[activeIndex]) controllers[activeIndex].focusKey(ev);
  });

  // manual theme toggle (standalone file -- no host to stamp data-theme)
  STUDIO.wireTheme(document.getElementById("themebtn"));
})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------

def _open_file(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        print("ros_plot: could not open %s: %s" % (path, exc), file=sys.stderr)


def _gather_inputs(args):
    if args.files:
        paths = []
        for pattern in args.files:
            expanded = [e for e in glob.glob(pattern, recursive=args.recursive)
                        if not os.path.isdir(e)]
            paths.extend(expanded if expanded else [pattern])
        return paths
    pattern = "**/*.rossystem" if args.recursive else "*.rossystem"
    found = sorted(e for e in glob.glob(pattern, recursive=args.recursive)
                   if not os.path.isdir(e))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ros_plot.py",
        description="Render RosTooling .rossystem models as one self-contained interactive HTML file.")
    parser.add_argument("files", nargs="*",
                        help="Model files to plot. With none, globs *.rossystem in the cwd.")
    parser.add_argument("--out", metavar="PATH", default=None,
                        help="Output HTML path (default: ros-plot.html next to the first input).")
    parser.add_argument("--recursive", action="store_true",
                        help="With no file args, glob *.rossystem recursively (default: cwd only).")
    parser.add_argument("--open", dest="open_after", action="store_true",
                        help="Open the generated HTML after writing (default: OFF; the path is always printed).")
    parser.add_argument("--no-catalogue", action="store_true",
                        help="Disable catalogue (assets/node_index.json) resolution for L4.")
    parser.add_argument("--json", action="store_true",
                        help="Print the extracted model(s) as JSON to stdout and exit (debug).")
    parser.add_argument("--check", action="store_true",
                        help="Cross-check node/connection counts against the linter tree and exit.")
    args = parser.parse_args(argv)

    paths = _gather_inputs(args)
    if not paths:
        where = os.getcwd()
        scope = "recursively under" if args.recursive else "in"
        print("ros_plot: no .rossystem files found %s %s.\n"
              "Pass file paths explicitly, or add --recursive to search subdirectories."
              % (scope, where), file=sys.stderr)
        return 2

    use_catalogue = not args.no_catalogue

    if args.check:
        rc = 0
        for path in paths:
            model = extract_model(path, use_catalogue=use_catalogue)
            _linter, root, err = _compose_root(path, use_catalogue)
            lint_nodes = _linter_node_key_count(root) if root is not None else 0
            ok = (model["nodeCount"] == lint_nodes)
            if not ok:
                rc = 1
            print("%-58s nodes=%d linter_nodes=%d edges=%d conn=%s %s"
                  % (os.path.basename(path), model["nodeCount"], lint_nodes,
                     model["edgeCount"], model["connSource"], "OK" if ok else "DIVERGENCE"))
            for d in model["diagnostics"]:
                print("    diag: %s" % d)
        return rc

    models = [extract_model(p, use_catalogue=use_catalogue) for p in paths]
    link_subsystems(models)

    if args.json:
        print(json.dumps(models, indent=2, ensure_ascii=False))
        return 0

    if args.out:
        out_path = os.path.abspath(args.out)
    else:
        out_path = os.path.join(os.path.dirname(os.path.abspath(paths[0])), "ros-plot.html")

    title = models[0]["systemName"] if len(models) == 1 else \
        "%d RosTooling systems" % len(models)
    html_doc = render_html(models, title=title)

    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(html_doc)

    print(out_path)
    for m in models:
        if m["diagnostics"]:
            print("  %s: %d diagnostic(s)" % (m["name"], len(m["diagnostics"])), file=sys.stderr)
            for d in m["diagnostics"]:
                print("    - %s" % d, file=sys.stderr)

    if args.open_after:
        _open_file(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
