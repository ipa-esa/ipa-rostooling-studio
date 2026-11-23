#!/usr/bin/env python3
"""
extract_rossystem.py -- turn a ROS 2 launch file into a draft .rossystem, deterministically.
Phase 2 of the pair; scripts/extract_ros2_interfaces.py is Phase 1.

WHY THIS EXISTS: a .ros2 says what a node TYPE is -- read from the source. A .rossystem says
what INSTANCES run and how they are configured -- and that lives in the launch file and the
controller config, not in the C++/Python source. Phase 1 owns the first, this owns the second,
and neither edits the other's files.

That split is also what makes the launch file's overrides harmless. A .rossystem node is

    "<instance label>":
      from: "<package>.<node>"

where the label is free text and only `from:` is a cross-reference. So a launch file that runs
`Node(package="unitree_g1_loco_motion", name="g1_loco_motion")` over a source that says
`Node("Loco_motion")` needs no rename anywhere: the label carries the deployment name, `from:`
carries the type. Likewise a controller plugin spawned three times is three labels sharing one
`from:`. Nothing in Phase 1's output is rewritten, so re-running Phase 1 can never silently
break this file's references.

The launch file is parsed with `ast` and NEVER executed. The same literal-only discipline as
Phase 1 applies: a node whose package/executable is a computed substitution, a parameter whose
value is a Command(...) or an EnvironmentVariable, a node that resolves to no known model --
each becomes a `# FLAG` comment in the emitted file rather than a guess. Same-file
`LaunchConfiguration("x")` values ARE resolved, one hop, to their own
`DeclareLaunchArgument(default_value=...)` literal, and disclosed as launch-argument defaults.

NOT emitted, deliberately:
  * `connections:` -- SKILL.md hard rule 4: never synthesise a connection the source did not
    declare. MatchPortMsgs compares types with Xtend `!==` (object identity), so a connection
    inferred from matching names is an unverifiable claim that can hard-error. Candidate pairs
    are computed, printed and written to the --json record for a human or an LLM to accept one
    at a time.
  * `processes:`, `subSystems:`, `namespace:` -- nothing in a launch file implies them.

Usage:
    python extract_rossystem.py <launch-file> --models DIR -o OUT.rossystem

    Options:
      --models DIR          directory of Phase 1 .ros2 files (required)
      -o, --out FILE        output .rossystem path (required)
      --workspace DIR       ROS 2 source tree, for locating packages named by
                            FindPackageShare and for the fromFile: package prefix
                            (default: inferred from the launch file's own package)
      --controllers-file F  controller config; by default it is resolved from the launch
                            file's own controllers_file launch argument
      --system-name NAME    system name (default: the launch file's stem)
      --json FILE           write the full record, including flags and connection candidates
      --no-lint             skip the rosmodel_lint.py pass

Exit status is non-zero if a node was flagged rather than emitted, or if the linter reports an
ERROR -- a partial model is a result, not a success.
"""

import argparse
import ast
import json
import os
import subprocess
import sys
from collections import OrderedDict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from extract_ros2_interfaces import (  # noqa: E402
    find_packages, read_package_xml, wrap_comment, Flag,
)

PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
NODE_INDEX = os.path.join(PLUGIN_ROOT, "assets", "node_index.json")
CATALOGUE_ROOT = os.path.join(PLUGIN_ROOT, "assets", "rosmodelscatalog")
LINTER = os.path.join(SCRIPT_DIR, "rosmodel_lint.py")

# .ros2 block name -> .rossystem arrow prefix
ARROW = OrderedDict([
    ("publishers", "pub"), ("subscribers", "sub"),
    ("serviceservers", "ss"), ("serviceclients", "sc"),
    ("actionservers", "as"), ("actionclients", "ac"),
])
# node_index.json records kinds already abbreviated
INDEX_KIND_TO_BLOCK = {v: k for k, v in ARROW.items()}
# from -> to, the only three legal connection pairings (rossystem-syntax.md sec 6)
CONNECTABLE = {"pub": "sub", "ss": "sc", "as": "ac"}

# SKILL.md "When not to use": a robot description NEVER reaches a model inlined. This is an
# explicit name guard, not a side effect of Command(...) being non-literal, so that making the
# resolver smarter later cannot start inlining URDF.
DESCRIPTION_PARAMS = {
    "robot_description", "robot_description_semantic",
    "robot_description_kinematics", "robot_description_planning",
}


class NotLiteral(object):
    def __init__(self, why):
        self.why = why

    def __repr__(self):
        return "<not-literal: %s>" % self.why


class PkgShare(object):
    """FindPackageShare("x") -- a package's share dir, resolved against the workspace."""

    def __init__(self, pkg):
        self.pkg = pkg


class Lit(object):
    """A resolved literal plus where it came from."""

    def __init__(self, value, prov="literal"):
        self.value = value
        self.prov = prov


# --------------------------------------------------------------------- .ros2 reading


class Ros2File(object):
    """The subset of a .ros2 this script needs: artifacts, their node, interfaces, params."""

    def __init__(self, package, path):
        self.package = package
        self.path = path
        self.artifacts = OrderedDict()   # artifact -> {"node":, "ifaces":, "params":}


def _unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def parse_ros2(path):
    """Tolerant indentation read of a .ros2 (ours or a catalogue file)."""
    package, artifact, block, entry = None, None, None, None
    out = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.rstrip("\n").replace("\t", "  ")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        code = body.split(" #")[0].rstrip()
        if indent == 0 and code.endswith(":"):
            package = _unquote(code[:-1])
            out = Ros2File(package, path)
            artifact = block = entry = None
        elif out is None:
            continue
        elif indent == 4 and code.endswith(":"):
            artifact = _unquote(code[:-1])
            out.artifacts[artifact] = {"node": artifact, "ifaces": OrderedDict(),
                                       "params": OrderedDict()}
            block = entry = None
        elif indent == 6 and artifact:
            if code.startswith("node:"):
                out.artifacts[artifact]["node"] = _unquote(code.split(":", 1)[1])
            elif code.endswith(":"):
                block = _unquote(code[:-1])
                entry = None
        elif indent == 8 and artifact and block and code.endswith(":"):
            entry = _unquote(code[:-1])
            if block == "parameters":
                out.artifacts[artifact]["params"][entry] = {"type": None}
            elif block in ARROW:
                out.artifacts[artifact]["ifaces"][(block, entry)] = {"type": None}
        elif indent == 10 and artifact and block and entry:
            key, _, value = code.partition(":")
            key, value = key.strip(), value.strip()
            if block == "parameters" and key == "type":
                out.artifacts[artifact]["params"][entry]["type"] = value
            elif block in ARROW and key == "type":
                out.artifacts[artifact]["ifaces"][(block, entry)]["type"] = _unquote(value)
    return out


class ModelIndex(object):
    """Everything a from:/arrow target can resolve against: Phase 1's output first, then the
    vendored catalogue (SKILL.md sec 8c)."""

    def __init__(self, models_dir):
        self.local = OrderedDict()     # package -> Ros2File
        self.catalogue = {}
        self.cat_files = {}
        if models_dir and os.path.isdir(models_dir):
            for fn in sorted(os.listdir(models_dir)):
                if fn.endswith(".ros2"):
                    parsed = parse_ros2(os.path.join(models_dir, fn))
                    if parsed:
                        self.local[parsed.package] = parsed
        try:
            with open(NODE_INDEX, encoding="utf-8") as fh:
                self.catalogue = json.load(fh).get("nodes", {})
        except Exception:
            self.catalogue = {}

    def resolve_package(self, package, executable=None):
        """-> (Ros2File, source label, note) or (None, None, None).

        A catalogue package name is not always the real ROS package name: the launch file
        runs package="moveit_ros_move_group", while the catalogue models that node under
        package "move_group". Falling back to the executable finds it -- but the resulting
        from: names the CATALOGUE's package, not the launch file's, so the caller has to
        say so rather than let the substitution pass silently."""
        if package in self.local:
            return self.local[package], "project", None
        hits = [(k, v) for k, v in self.catalogue.items() if k.split(".", 1)[0] == package]
        note = None
        if not hits and executable:
            hits = [(k, v) for k, v in self.catalogue.items()
                    if k.split(".", 1)[1] == executable or v.get("artifact") == executable]
            if hits:
                note = ("the launch file runs package %r, but no model declares that "
                        "package; matched on executable %r instead, against catalogue "
                        "package %r. If the node is emitted below, its from: names the "
                        "CATALOGUE's package, not the launch file's"
                        % (package, executable, hits[0][0].split(".", 1)[0]))
        if not hits:
            return None, None, None
        key, entry = hits[0]
        path = os.path.join(CATALOGUE_ROOT, entry["file"])
        if path not in self.cat_files:
            self.cat_files[path] = parse_ros2(path)
        parsed = self.cat_files[path]
        if parsed is None:
            return None, None, None
        return parsed, "catalogue:" + entry["file"], note

    def artifact_for_node(self, model, node_name=None, executable=None):
        """Pick the artifact to reference. A .ros2 usually has exactly one.

        Order matters: the node name is the strongest evidence, but a launch entry often has
        no name= at all, and then the executable is the next-strongest -- SKILL.md rule 2a says
        the artifact IS the executable to run, so `executable="demo_node"` naming the artifact
        `demo_node` is a direct hit, not an inference. Without that step a package with more
        than one artifact and no name= resolved to nothing and the node was dropped from the
        system entirely.
        """
        if node_name:
            for art, data in model.artifacts.items():
                if data["node"] == node_name:
                    return art, data
        if executable:
            for art, data in model.artifacts.items():
                if art == executable:
                    return art, data
        if len(model.artifacts) == 1:
            art = next(iter(model.artifacts))
            return art, model.artifacts[art]
        return None, None


# ------------------------------------------------------------------- launch reading


LAUNCH_NODE_CALLS = {"Node", "LifecycleNode", "ComposableNode"}
CONTAINER_KWARGS = ("actions", "composable_node_descriptions", "nodes")


class LaunchFile(object):
    def __init__(self, path):
        self.path = path
        self.assignments = {}
        self.launch_args = {}
        self.nodes = []          # ordered NodeSpec
        self.flags = []
        self.dropped = []        # things with no DSL slot, for the report
        self.reassigned = set()  # names assigned more than once


class NodeSpec(object):
    def __init__(self, call, kwargs, launch_file, order):
        self.call = call
        self.kwargs = kwargs
        self.launch_file = launch_file
        self.order = order


def kw(call, *names):
    for keyword in call.keywords:
        if keyword.arg in names:
            return keyword.value
    return None


def call_name(node):
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return None


def read_launch(path):
    """Parse one launch file. Returns a LaunchFile with nodes in LaunchDescription order."""
    lf = LaunchFile(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        tree = ast.parse(text, filename=path)
    except (OSError, SyntaxError) as exc:
        lf.flags.append(Flag("launch", "launch file did not parse: %s" % exc, path, 1))
        return lf

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    # ast.walk is breadth-first, so "last seen" is not "last in the file".
                    # Keep the latest by line number, and say so when a name is assigned
                    # more than once -- branches make the real value undecidable here.
                    previous = lf.assignments.get(target.id)
                    if previous is not None:
                        if getattr(previous, "lineno", 0) > node.lineno:
                            continue
                        lf.reassigned.add(target.id)
                    lf.assignments[target.id] = node.value
        elif isinstance(node, ast.Call) and call_name(node) == "DeclareLaunchArgument":
            name_node = node.args[0] if node.args else kw(node, "name")
            try:
                name = ast.literal_eval(name_node)
            except Exception:
                continue
            if isinstance(name, str):
                default = kw(node, "default_value")
                if default is None and len(node.args) > 1:
                    default = node.args[1]
                lf.launch_args[name] = default

    # the ordered node list is whatever LaunchDescription([...]) contains
    desc = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node) == "LaunchDescription":
            desc = node.args[0] if node.args else None
            break
    order = [0]
    seen = set()
    if desc is None:
        lf.flags.append(Flag(
            "launch", "no LaunchDescription([...]) call found; every Node(...) in the file "
                      "is treated as launched, which may over-report", path, 1))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and call_name(node) in LAUNCH_NODE_CALLS:
                _add_node(lf, node, order, seen)
    else:
        _walk_actions(lf, desc, order, seen, 0)
    return lf


def _add_node(lf, call, order, seen):
    if id(call) in seen:
        return
    seen.add(id(call))
    lf.nodes.append(NodeSpec(call, {k.arg: k.value for k in call.keywords}, lf, order[0]))
    order[0] += 1


def _walk_actions(lf, expr, order, seen, depth):
    """Walk a LaunchDescription list, resolving names and descending into containers."""
    if depth > 12 or expr is None:
        return
    if isinstance(expr, ast.Name):
        return _walk_actions(lf, lf.assignments.get(expr.id), order, seen, depth + 1)
    if isinstance(expr, (ast.List, ast.Tuple)):
        for element in expr.elts:
            _walk_actions(lf, element, order, seen, depth + 1)
        return
    if isinstance(expr, ast.Call):
        name = call_name(expr)
        if name in LAUNCH_NODE_CALLS:
            _add_node(lf, expr, order, seen)
            return
        if name == "ExecuteProcess":
            lf.dropped.append(
                "ExecuteProcess(...) -- a raw process, not a ROS node action; the DSL has "
                "no node to model it with")
            return
        if name == "IncludeLaunchDescription":
            lf.flags.append(Flag(
                "launch", "IncludeLaunchDescription(...) is not followed -- any node it "
                          "brings up is missing from this model", lf.path, expr.lineno))
            return
        for kwarg in CONTAINER_KWARGS:
            inner = kw(expr, kwarg)
            if inner is not None:
                if name == "TimerAction":
                    period = kw(expr, "period")
                    try:
                        lf.dropped.append(
                            "TimerAction period=%s (bring-up delay; the DSL has no slot "
                            "for it)" % ast.literal_eval(period))
                    except Exception:
                        lf.dropped.append("TimerAction with a non-literal period")
                _walk_actions(lf, inner, order, seen, depth + 1)
                return
        for arg in expr.args:
            if isinstance(arg, (ast.List, ast.Tuple)):
                _walk_actions(lf, arg, order, seen, depth + 1)


# ------------------------------------------------------------- expression resolution


def resolve(expr, lf, depth=0, prov="literal"):
    """A launch expression -> Lit / PkgShare / NotLiteral. Never executes anything."""
    if depth > 12:
        return NotLiteral("expression nested too deep")
    if expr is None:
        return NotLiteral("missing")
    if isinstance(expr, ast.Name):
        target = lf.assignments.get(expr.id)
        if target is None:
            return NotLiteral("variable %r has no literal assignment" % expr.id)
        if expr.id in lf.reassigned:
            return NotLiteral("variable %r is assigned more than once, so which value "
                              "reaches here is not decidable without executing the file"
                              % expr.id)
        return resolve(target, lf, depth + 1, prov)
    if isinstance(expr, (ast.Constant, ast.Dict, ast.List, ast.Tuple)):
        if isinstance(expr, ast.Constant):
            return Lit(expr.value, prov)
        if isinstance(expr, ast.Dict):
            out = OrderedDict()
            for k, v in zip(expr.keys, expr.values):
                key = resolve(k, lf, depth + 1, prov)
                if not isinstance(key, Lit):
                    return NotLiteral("dict key is not literal")
                out[key.value] = resolve(v, lf, depth + 1, prov)
            return Lit(out, prov)
        items = [resolve(e, lf, depth + 1, prov) for e in expr.elts]
        return Lit(items, prov)
    if isinstance(expr, ast.Call):
        name = call_name(expr)
        if name == "LaunchConfiguration":
            try:
                arg = ast.literal_eval(expr.args[0])
            except Exception:
                return NotLiteral("LaunchConfiguration with a non-literal name")
            if arg not in lf.launch_args:
                return NotLiteral("LaunchConfiguration(%r) has no DeclareLaunchArgument "
                                  "in this file" % arg)
            default = lf.launch_args[arg]
            if default is None:
                return NotLiteral("launch argument %r has no default_value" % arg)
            return resolve(default, lf, depth + 1, "launch-arg default (%s)" % arg)
        if name == "TextSubstitution":
            return resolve(kw(expr, "text"), lf, depth + 1, prov)
        if name == "FindPackageShare":
            try:
                return PkgShare(ast.literal_eval(expr.args[0]) if expr.args
                                else ast.literal_eval(kw(expr, "package")))
            except Exception:
                return NotLiteral("FindPackageShare with a non-literal package")
        if name == "PathJoinSubstitution":
            inner = resolve(expr.args[0] if expr.args else None, lf, depth + 1, prov)
            if not isinstance(inner, Lit) or not isinstance(inner.value, list):
                return NotLiteral("PathJoinSubstitution over a non-literal list")
            return Lit(inner.value, prov)          # caller joins; may contain a PkgShare
        if name == "ParameterValue":
            return NotLiteral("ParameterValue(...) wraps a computed substitution")
        if name in ("Command", "EnvironmentVariable", "PythonExpression",
                    "AnonName", "ThisLaunchFileDir"):
            return NotLiteral("%s(...) is evaluated at launch time" % name)
        return NotLiteral("%s(...) is not a literal" % (name or "call"))
    return NotLiteral("unsupported expression")


def as_text(res, workspace_pkgs):
    """A resolved value -> a filesystem path string, or None."""
    if isinstance(res, Lit) and isinstance(res.value, str):
        return res.value
    if isinstance(res, Lit) and isinstance(res.value, list):
        parts = []
        for item in res.value:
            if isinstance(item, PkgShare):
                path = workspace_pkgs.get(item.pkg)
                if not path:
                    return None
                parts.append(path)
            elif isinstance(item, Lit) and isinstance(item.value, str):
                parts.append(item.value)
            else:
                return None
        return os.path.join(*parts) if parts else None
    return None


# ------------------------------------------------------------------- DSL formatting


_BOOL_WORDS = {"true": "true", "false": "false", "1": "true", "0": "false"}


def dsl_value(declared_type, value):
    """Render a Python value as a .rossystem `value:` literal for the .ros2's declared type."""
    if declared_type is None:
        declared_type = ""
    if declared_type.startswith("Array[") or declared_type.startswith("List["):
        if not isinstance(value, (list, tuple)):
            return None
        if not value:
            return None                      # `[]` is a parse error; caller flags it
        elem = declared_type[declared_type.index("[") + 1:-1]
        parts = [dsl_value(elem, v) for v in value]
        return None if any(p is None for p in parts) else "[%s]" % ", ".join(parts)
    if isinstance(value, (list, tuple, dict)):
        return None
    if declared_type == "Boolean":
        # A launch argument's default_value is ALWAYS a string, so the truthiness of
        # `value` is not the answer: the string "false" is truthy and would silently
        # invert the parameter. Only an actual bool or an explicit true/false token
        # counts; anything else is refused and flagged.
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.strip().lower() in _BOOL_WORDS:
            return _BOOL_WORDS[value.strip().lower()]
        return None
    if declared_type == "Integer":
        if isinstance(value, bool):
            return None
        try:
            return str(int(str(value).strip()))
        except (TypeError, ValueError):
            return None
    if declared_type == "Double":
        if isinstance(value, bool):
            return None
        try:
            text = repr(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
        return text if ("." in text or "e" in text or "E" in text) else text + ".0"
    if declared_type == "String":
        return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')
    # Declared type unknown. rossystem-syntax.md sec 5: a RosParameter carries no type:,
    # so the .ros2 is the only place to read it -- and if it could not be read, the string
    # "false" and the boolean false are indistinguishable here. Refuse rather than guess.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return None


def sanitize_system_name(name):
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in name).lower()
    return out or "system"


# ----------------------------------------------------------------------- extraction


class SystemDraft(object):
    def __init__(self, name):
        self.name = name
        self.nodes = []          # ordered dicts
        self.flags = []
        self.dropped = []
        self.labels = set()
        self.artifact_uses = {}   # (package, artifact) -> how many instances reference it

    def unique_label(self, base):
        label = base
        if label not in self.labels:
            self.labels.add(label)
            return label
        n = 2
        while "%s_%d" % (label, n) in self.labels:
            n += 1
        label = "%s_%d" % (label, n)
        self.labels.add(label)
        return label


def controller_type_map(controllers_file, problems=None):
    """controllers.yaml -> {instance: implementation package}, plus per-instance params.

    Failure here is not cosmetic: without the type: map every ros2_control spawner looks like a
    controller with no declared type and is skipped, so a missing PyYAML would silently remove
    four of this system's ten nodes AND misattribute it to the config. Report, never swallow.
    """
    types, params = {}, {}
    if problems is None:
        problems = []
    if not controllers_file or not os.path.isfile(controllers_file):
        return types, params
    try:
        import yaml
    except ImportError:
        problems.append("PyYAML is not installed, so the controller config could not be read; "
                        "every controller instance it declares is therefore MISSING from this "
                        "model. pip install pyyaml")
        return types, params
    try:
        with open(controllers_file, encoding="utf-8", errors="replace") as fh:
            doc = yaml.safe_load(fh)
    except Exception as exc:
        problems.append("the controller config %s could not be parsed (%s), so every "
                        "controller instance it declares is MISSING from this model"
                        % (controllers_file, exc))
        return types, params
    if not isinstance(doc, dict):
        problems.append("the controller config %s is not a mapping, so no controller instance "
                        "could be resolved" % controllers_file)
        return types, params
    cm = (doc.get("controller_manager") or {}).get("ros__parameters") or {}
    for name, spec in cm.items():
        if isinstance(spec, dict) and isinstance(spec.get("type"), str):
            types[name] = spec["type"].split("/")[0]
    for name, spec in doc.items():
        if name == "controller_manager" or not isinstance(spec, dict):
            continue
        inner = spec.get("ros__parameters")
        if isinstance(inner, dict):
            params[name] = inner
    return types, params


def build_system(launch_files, index, workspace_pkgs, controllers_file, system_name):
    sys_draft = SystemDraft(system_name)
    problems = []
    ctrl_types, ctrl_params = controller_type_map(controllers_file, problems)
    for problem in problems:
        sys_draft.flags.append(Flag("controller-config", problem,
                                    controllers_file or "<none>", 1))

    for lf in launch_files:
        sys_draft.flags.extend(lf.flags)
        for item in lf.dropped:
            sys_draft.dropped.append("%s: %s" % (os.path.basename(lf.path), item))

    for lf in launch_files:
        for spec in lf.nodes:
            _emit_node(sys_draft, spec, lf, index, workspace_pkgs, ctrl_types, ctrl_params)
    assign_interface_labels(sys_draft)
    return sys_draft


def assign_interface_labels(sys_draft):
    """Interface labels must be unique across the file (connections resolve them by name).
    When one artifact backs several instances -- three spawns of one controller plugin --
    every instance offers the same port names, so the instance label goes in front. A bare
    numeric suffix would be unique but would not say which instance owns which port."""
    counts = {}
    for node in sys_draft.nodes:
        for iface in node["interfaces"]:
            counts[iface["label"]] = counts.get(iface["label"], 0) + 1
    for node in sys_draft.nodes:
        for iface in node["interfaces"]:
            base = iface["label"]
            if counts[base] > 1:
                # two nodes offering the same port name: a bare numeric suffix would be
                # unique but would not say which node owns which port.
                base = "%s.%s" % (node["label"], base)
            iface["label"] = sys_draft.unique_label(base)


def _lit_str(res):
    return res.value if isinstance(res, Lit) and isinstance(res.value, str) else None


def _emit_node(sys_draft, spec, lf, index, workspace_pkgs, ctrl_types, ctrl_params):
    line = spec.call.lineno
    package = _lit_str(resolve(spec.kwargs.get("package"), lf))
    executable = _lit_str(resolve(spec.kwargs.get("executable")
                                  or spec.kwargs.get("node_executable"), lf))
    node_name = _lit_str(resolve(spec.kwargs.get("name")
                                 or spec.kwargs.get("node_name"), lf))

    if not package:
        sys_draft.flags.append(Flag(
            "node", "a launched node's package= is not a string literal, so it cannot be "
                    "resolved to a model", lf.path, line))
        return
    cond = spec.kwargs.get("condition")
    if cond is not None:
        try:
            cond_text = " ".join(ast.unparse(cond).split())
        except Exception:
            cond_text = "<condition>"
        sys_draft.flags.append(Flag(
            "node", "node '%s' is launched under condition=%s, so whether it runs at all is "
                    "decided at launch time; it IS included below, which is only right when "
                    "that condition holds"
                    % (node_name or executable or package, cond_text), lf.path, line))
    if spec.kwargs.get("remappings") is not None:
        sys_draft.flags.append(Flag(
            "node", "node in package '%s' declares remappings=, which rename its topics at "
                    "launch time; the interfaces below are the UNREMAPPED source names"
                    % package, lf.path, line))

    # ros2_control spawner: the instance is the controller named in arguments
    controller_instance = None
    if package == "controller_manager" and executable == "spawner":
        args = resolve(spec.kwargs.get("arguments"), lf)
        names = []
        if isinstance(args, Lit) and isinstance(args.value, list):
            for item in args.value:
                text = _lit_str(item)
                if text is None:
                    continue
                if text.startswith("-"):
                    break
                names.append(text)
        if not names:
            sys_draft.flags.append(Flag(
                "node", "a controller_manager spawner has no literal controller name in "
                        "arguments=, so the controller it starts is unknown", lf.path, line))
            return
        controller_instance = names[0]
        if len(names) > 1:
            sys_draft.dropped.append(
                "spawner arguments beyond the controller name: %s" % ", ".join(names[1:]))
        impl = ctrl_types.get(controller_instance)
        if not impl:
            sys_draft.flags.append(Flag(
                "node", "controller '%s' is spawned but the controller config does not give "
                        "it a type:, so its implementation package is unknown"
                        % controller_instance, lf.path, line))
            return
        package, node_name = impl, controller_instance

    label_base = node_name or controller_instance or executable or package
    model, source, note = index.resolve_package(package, executable)
    if note:
        sys_draft.flags.append(Flag("node", "node '%s': %s" % (label_base, note),
                                    lf.path, line))
    if model is None:
        sys_draft.flags.append(Flag(
            "node", "launched node '%s' (package '%s') resolves to neither a model in "
                    "--models nor assets/node_index.json, so it is NOT in this system -- an "
                    "unresolved from: is a linking ERROR that would stop the whole file "
                    "loading" % (label_base, package), lf.path, line))
        return

    # One call: artifact_for_node already falls through node name -> executable -> the
    # sole artifact, so the old second call with node_name=None could never add anything.
    artifact, data = index.artifact_for_node(model, node_name, executable)
    if artifact is None:
        sys_draft.flags.append(Flag(
            "node", "package '%s' has %d artifacts and none matches node name %r, so the "
                    "from: target is ambiguous" % (package, len(model.artifacts), node_name),
            lf.path, line))
        return

    # a controller instance name that is not the catalogued node name would produce a
    # from: that does not link -- catch it rather than emit a broken reference
    if controller_instance and source.startswith("catalogue") \
            and data["node"] != controller_instance:
        sys_draft.flags.append(Flag(
            "node", "controller instance '%s' is spawned from catalogued package '%s', whose "
                    "node is named '%s'; from: \"%s.%s\" would not link"
                    % (controller_instance, package, data["node"], package,
                       controller_instance), lf.path, line))
        return

    label = sys_draft.unique_label(label_base)
    entry = {
        "label": label,
        "from": "%s.%s" % (model.package, data["node"]),
        "from_comment": _from_comment(source, model),
        "interfaces": [],
        "parameters": [],
        "artifact": artifact,
        "package": model.package,
        "launched_at": "%s:%d" % (os.path.basename(lf.path), line),
    }

    for (block, name), info in data["ifaces"].items():
        entry["interfaces"].append({
            "label": "%s_%s" % (name, ARROW[block]), "kind": ARROW[block],
            "target": "%s::%s" % (artifact, name), "type": info.get("type"),
        })

    _emit_params(sys_draft, entry, spec, lf, data, ctrl_params.get(controller_instance or ""),
                 line)
    sys_draft.nodes.append(entry)


def _from_comment(source, model):
    if source == "project":
        return "# project-local, see %s" % os.path.basename(model.path)
    return "# assets/rosmodelscatalog/%s" % source.split(":", 1)[1]


def _emit_params(sys_draft, entry, spec, lf, data, controller_overrides, line):
    """Node-level parameter overrides: launch parameters=[{...}] and the controller config."""
    declared = data["params"]
    pending = OrderedDict()

    params = resolve(spec.kwargs.get("parameters"), lf)
    if isinstance(params, Lit) and isinstance(params.value, list):
        for index, item in enumerate(params.value):
            if not (isinstance(item, Lit) and isinstance(item.value, dict)):
                # a whole entry of the list -- often an entire config dict, e.g.
                # moveit_config.to_dict() -- that this reader cannot expand. Dropping it
                # silently would hide every parameter it carries.
                why = item.why if isinstance(item, NotLiteral) else "not a literal dict"
                sys_draft.flags.append(Flag(
                    "parameter", "node '%s': entry %d of parameters= is %s, so every "
                                 "parameter it carries is missing from this model"
                                 % (entry["label"], index, why), lf.path, line))
                continue
            for key, val in item.value.items():
                pending[key] = (val, "launch file")
    elif spec.kwargs.get("parameters") is not None and not isinstance(params, Lit):
        sys_draft.flags.append(Flag(
            "parameter", "node '%s' has a parameters= list that is not literal (%s); its "
                         "overrides are not in this model"
                         % (entry["label"], getattr(params, "why", "?")), lf.path, line))

    for key, val in (controller_overrides or {}).items():
        pending[key] = (Lit(val, "controller config"), "controller config")

    for name, (res, origin) in pending.items():
        if name in DESCRIPTION_PARAMS:
            sys_draft.flags.append(Flag(
                "parameter", "'%s' on node '%s' is a robot description; SKILL.md forbids "
                             "inlining a robot description into a model, so it is omitted "
                             "regardless of whether it is literal"
                             % (name, entry["label"]), lf.path, line))
            continue
        if not isinstance(res, Lit):
            sys_draft.flags.append(Flag(
                "parameter", "'%s' on node '%s' is not literal (%s), so its deployed value "
                             "is unknown" % (name, entry["label"], getattr(res, "why", "?")),
                lf.path, line))
            continue
        if name not in declared:
            sys_draft.flags.append(Flag(
                "parameter", "the %s sets '%s' on node '%s', but %s does not declare that "
                             "parameter -- \"%s::%s\" would not link, so the override is "
                             "omitted" % (origin, name, entry["label"], entry["package"],
                                          entry["artifact"], name), lf.path, line))
            continue
        rendered = dsl_value(declared[name].get("type"), _plain(res.value))
        if rendered is None:
            sys_draft.flags.append(Flag(
                "parameter", "'%s' on node '%s' has value %r, which has no legal form for "
                             "declared type %s (an empty list cannot be written as [])"
                             % (name, entry["label"], _plain(res.value),
                                declared[name].get("type")), lf.path, line))
            continue
        entry["parameters"].append({
            "label": name, "target": "%s::%s" % (entry["artifact"], name),
            "value": rendered, "origin": origin, "prov": res.prov,
        })


def _plain(value):
    """Unwrap nested Lit objects produced by resolving a list/dict."""
    if isinstance(value, Lit):
        return _plain(value.value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return OrderedDict((k, _plain(v)) for k, v in value.items())
    return value


# ---------------------------------------------------------------- candidate wiring


def connection_candidates(sys_draft):
    """Name+type matches, for a human to accept. NEVER emitted (SKILL.md hard rule 4)."""
    out = []
    by_name = {}
    label_counts = {}
    for node in sys_draft.nodes:
        for iface in node["interfaces"]:
            lbl = iface["label"]
            label_counts[lbl] = label_counts.get(lbl, 0) + 1
            target_name = iface["target"].split("::", 1)[1]
            by_name.setdefault(target_name, []).append((node, iface))
    for name, entries in sorted(by_name.items()):
        for from_node, from_iface in entries:
            to_kind = CONNECTABLE.get(from_iface["kind"])
            if not to_kind:
                continue
            for to_node, to_iface in entries:
                if to_node is from_node or to_iface["kind"] != to_kind:
                    continue
                if from_iface["type"] != to_iface["type"]:
                    continue
                from_label = from_iface["label"]
                to_label = to_iface["label"]
                from_conn = ("%s.%s" % (from_node["label"], from_label)
                             if label_counts.get(from_label, 0) > 1 else from_label)
                to_conn = ("%s.%s" % (to_node["label"], to_label)
                           if label_counts.get(to_label, 0) > 1 else to_label)
                out.append({
                    "from": from_conn, "to": to_conn,
                    "from_interface": from_label, "to_interface": to_label,
                    "topic": name, "type": from_iface["type"],
                    "from_node": from_node["label"], "to_node": to_node["label"],
                })
    return out


# ------------------------------------------------------------------------ emission


def emit(sys_draft, from_file, from_file_abs, launch_files, models_dir, candidates):
    lines = ["# GENERATED DRAFT -- scripts/extract_rossystem.py",
             "# Launch file(s) read (parsed with ast, never executed):"]
    for lf in launch_files:
        lines.append("#   %s" % lf.path)
    lines.append("# Node interfaces read from: %s" % models_dir)
    lines.append("#")
    lines.append(wrap_comment(
        "A node label below is the DEPLOYMENT instance name (the launch file's name=, or a "
        "controller instance); from: is the node TYPE as the .ros2 declares it. They differ "
        "on purpose and neither file was rewritten to make them match."))
    lines.append("#")
    lines.append(wrap_comment(
        "connections: is deliberately absent. SKILL.md hard rule 4 forbids synthesising a "
        "connection the source did not declare, and MatchPortMsgs compares types by object "
        "identity, so a pairing inferred from matching names is unverifiable. %d candidate "
        "pair(s) were computed and are listed in the run's --json record; accept them one at "
        "a time." % len(candidates)))
    catalogued = sorted({n["label"] for n in sys_draft.nodes
                         if n["from_comment"].startswith("# assets/")})
    if catalogued:
        lines.append("#")
        lines.append(wrap_comment(
            "CAUTION -- these nodes resolved to the vendored catalogue rather than to a "
            "model built from this project's own source: %s. Their interfaces: below are "
            "the catalogue's general model of that node, which can be broader or narrower "
            "than what this deployment actually uses (the catalogued rviz2, for instance, "
            "is Nav2-specific). They resolve and validate; whether each port is really "
            "present here is not something a launch file can tell you."
            % ", ".join(catalogued)))
    for item in sorted(set(sys_draft.dropped)):
        lines.append(wrap_comment("DROPPED (no DSL slot): %s" % item))
    # See extract_ros2_interfaces.py's emit_package for why this stamp exists.
    lines.append("# EXTRACTOR-FLAGS: %d" % len(sys_draft.flags))
    for flag in sys_draft.flags:
        lines.append(flag.as_comment(None))

    lines.append("%s:" % sys_draft.name)
    lines.append('  fromFile: "%s" # on disk: %s' % (from_file, from_file_abs))
    lines.append("  nodes:")
    for node in sys_draft.nodes:
        lines.append('    "%s":' % node["label"])
        lines.append('      from: "%s" %s' % (node["from"], node["from_comment"]))
        if node["interfaces"]:
            lines.append("      interfaces:")
            ordered = sorted(node["interfaces"],
                             key=lambda i: (list(ARROW.values()).index(i["kind"]), i["label"]))
            for iface in ordered:
                lines.append('        - "%s": %s-> "%s"'
                             % (iface["label"], iface["kind"], iface["target"]))
        if node["parameters"]:
            lines.append("      parameters:")
            for param in sorted(node["parameters"], key=lambda p: p["label"]):
                lines.append('        - "%s": "%s"' % (param["label"], param["target"]))
                note = param["origin"]
                if param["prov"].startswith("launch-arg default"):
                    note += ", %s -- overridable on the command line" % param["prov"]
                lines.append("          value: %s # from the %s" % (param["value"], note))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- main


def package_of(path, workspace_pkgs_by_dir):
    """The package a file belongs to: nearest ancestor with a package.xml."""
    current = os.path.dirname(os.path.abspath(path))
    while True:
        if current in workspace_pkgs_by_dir:
            return workspace_pkgs_by_dir[current], current
        parent = os.path.dirname(current)
        if parent == current:
            return None, None
        current = parent


def run_linter(files):
    if not os.path.isfile(LINTER):
        return None
    try:
        proc = subprocess.run([sys.executable, LINTER, "--json", "--server", "off"] + files,
                              capture_output=True, text=True, timeout=180)
        return json.loads(proc.stdout or "{}")
    except Exception as exc:
        print("  linter did not run: %s" % exc, file=sys.stderr)
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Extract a draft .rossystem from ROS 2 launch files.")
    ap.add_argument("launch", nargs="+", help="launch file(s) to read")
    ap.add_argument("--models", required=True, help="directory of Phase 1 .ros2 files")
    ap.add_argument("-o", "--out", required=True, help="output .rossystem path")
    ap.add_argument("--workspace", help="ROS 2 source tree (for FindPackageShare and the "
                                        "fromFile: package prefix)")
    ap.add_argument("--controllers-file", help="controller config YAML")
    ap.add_argument("--system-name", help="system name (default: launch file stem)")
    ap.add_argument("--json", dest="json_out", help="write the full record here")
    ap.add_argument("--no-lint", action="store_true")
    args = ap.parse_args(argv)

    workspace = args.workspace
    if not workspace:
        current = os.path.dirname(os.path.abspath(args.launch[0]))
        while current != os.path.dirname(current):
            if os.path.isfile(os.path.join(current, "package.xml")):
                workspace = os.path.dirname(current)
                break
            current = os.path.dirname(current)
        workspace = workspace or os.path.dirname(os.path.abspath(args.launch[0]))

    pkg_dirs = find_packages([workspace])
    workspace_pkgs, by_dir = {}, {}
    for pkg_dir in pkg_dirs:
        name = read_package_xml(pkg_dir)[0]
        workspace_pkgs.setdefault(name, pkg_dir)
        by_dir[pkg_dir] = name

    launch_files = [read_launch(p) for p in args.launch]
    index = ModelIndex(args.models)

    controllers_file = args.controllers_file
    if not controllers_file:
        for lf in launch_files:
            for key in ("controllers_file", "controller_file", "controllers"):
                if key in lf.launch_args:
                    path = as_text(resolve(lf.launch_args[key], lf), workspace_pkgs)
                    if path and os.path.isfile(path):
                        controllers_file = path
                        break
            if controllers_file:
                break

    name = args.system_name or os.path.basename(args.launch[0]).split(".")[0]
    sys_draft = build_system(launch_files, index, workspace_pkgs, controllers_file,
                             sanitize_system_name(name))

    if controllers_file:
        print("controller config: %s" % controllers_file)
    else:
        print("controller config: NOT FOUND -- controller instances cannot be resolved; "
              "pass --controllers-file", file=sys.stderr)

    # fromFile: rung 2 -- a real launch file on disk, written package-relative
    primary = os.path.abspath(args.launch[0])
    pkg_name, pkg_dir = package_of(primary, by_dir)
    if pkg_name:
        rel = os.path.relpath(primary, pkg_dir).replace(os.sep, "/")
        from_file = "%s/%s" % (pkg_name, rel)
    else:
        from_file = "TODO_PACKAGE/launch/TODO.launch.py"
        sys_draft.flags.append(Flag(
            "fromFile", "the launch file is not inside a package with a package.xml, so its "
                        "package-relative path is unknown; the sentinel is emitted instead",
            primary, 1))

    candidates = connection_candidates(sys_draft)

    if not sys_draft.nodes:
        # `nodes:` with nothing under it emits no BEGIN token, so the indentation lexer
        # almost certainly rejects it -- and a system with no nodes models nothing either
        # way. Writing one produces a file that passes the linter while saying nothing,
        # which is worse than writing none.
        print("%s: NO MODEL WRITTEN -- no node resolved from this launch file." % args.out,
              file=sys.stderr)
        for item in sorted(set(sys_draft.dropped)):
            print("  it does contain: %s" % item, file=sys.stderr)
        for flag in sys_draft.flags:
            print("  %s" % flag.reason, file=sys.stderr)
        if not sys_draft.dropped and not sys_draft.flags:
            print("  It declares no launch_ros Node(...) actions at all.", file=sys.stderr)
        return 1

    text = emit(sys_draft, from_file, primary, launch_files, args.models, candidates)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)

    n_if = sum(len(n["interfaces"]) for n in sys_draft.nodes)
    n_p = sum(len(n["parameters"]) for n in sys_draft.nodes)
    print("%s -> %d node(s), %d interface(s), %d parameter override(s); %d flagged"
          % (args.out, len(sys_draft.nodes), n_if, n_p, len(sys_draft.flags)))
    print("  %d connection candidate(s) computed, 0 emitted (hard rule 4)" % len(candidates))
    for cand in candidates:
        print("    %s -> %s   [%s : %s]"
              % (cand["from_node"], cand["to_node"], cand["topic"], cand["type"]))

    if args.json_out:
        record = {
            "system": sys_draft.name,
            "fromFile": from_file,
            "launch_files": [lf.path for lf in launch_files],
            "controllers_file": controllers_file,
            "nodes": sys_draft.nodes,
            "connection_candidates": candidates,
            "dropped": sorted(set(sys_draft.dropped)),
            "flags": [{"kind": f.kind, "reason": f.reason,
                       "at": "%s:%d" % (f.file, f.line)} for f in sys_draft.flags],
        }
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh, indent=2, default=str)
            fh.write("\n")
        print("  record: %s" % args.json_out)

    status = 1 if sys_draft.flags else 0
    if not args.no_lint:
        result = run_linter([args.out])
        if result:
            counts = result.get("summary") or {}
            print("  rosmodel_lint: %s" % json.dumps(counts))
            if counts.get("errors"):
                status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
