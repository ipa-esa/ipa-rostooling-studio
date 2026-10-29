#!/usr/bin/env python3
"""
extract_ros2_interfaces.py -- turn raw ROS 2 source into draft .ros2 node-interface
models, deterministically. No LLM, no regex guessing: Python nodes are read with the
stdlib `ast` module, C++ nodes with tree-sitter-cpp.

WHY THIS EXISTS: authoring a .ros2 by hand means reading every create_publisher /
create_subscription / create_service / create_client / action-server / declare_parameter
call in a package and transcribing it into the DSL. That transcription is mechanical
whenever the topic name and the message type are *literal* in the source, and it is a
judgement call whenever they are not (built from a parameter, an f-string, a loop
variable, a launch-time remap). This script does the mechanical half exactly and refuses
the other half: anything non-literal becomes a commented FLAG line in the file header,
with file:line, for a human or an LLM to resolve.

Phase 1 is node interfaces only. It does NOT emit a .rossystem -- inferring connections:
needs cross-package topic matching and architectural judgement (which of six nodes on
/cmd_vel are actually wired together), which is exactly the kind of guess this script
exists to avoid.

Usage:
    python extract_ros2_interfaces.py <path> [<path> ...] -o OUTDIR
        <path> is a ROS 2 package (a directory with package.xml) or any directory
        containing some; every package found underneath is extracted.

    Options:
      -o, --out DIR     where to write .ros2 files (default: ./ros_model_draft)
      --json FILE       also write the full extraction record, including everything
                        that was flagged rather than emitted
      --emit-qos        emit `qos: depth:` for literal integer QoS depths. OFF by
                        default: SKILL.md's pinned profile says emit depth: only when
                        the caller supplies a concrete value, and RM034 warns on it.
                        The depths are always in the --json record either way.
      --no-lint         skip the rosmodel_lint.py pass over the generated files
      --package-root DIR
                        extra directory to search for project-local .msg/.srv/.action
                        definitions (repeatable); the input paths are searched anyway

Exit status is 0 unless a generated file fails the linter with an ERROR.
"""

import argparse
import ast
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
TYPE_INDEX = os.path.join(PLUGIN_ROOT, "assets", "type_index.json")
LINTER = os.path.join(SCRIPT_DIR, "rosmodel_lint.py")

PY_EXT = (".py",)
CPP_EXT = (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h")

# DSL block names, in the conventional emission order (SKILL.md sec 9).
BLOCK_ORDER = [
    "publishers",
    "subscribers",
    "serviceservers",
    "serviceclients",
    "actionservers",
    "actionclients",
]

SKIP_DIRS = {
    ".git", ".svn", "build", "install", "log", "__pycache__",
    "node_modules", ".pytest_cache", ".mypy_cache", "venv", ".venv",
}


# Cheap prefilter: skip a file only when it cannot possibly contain a declaration we read.
# It must name EVERY call we look for. An earlier version tested only for "rclcpp" or
# "create_publisher", which silently skipped whole files -- three of moveit_ros_move_group's
# capability sources call create_service<> and never write the token "rclcpp" at all, so four
# real service servers vanished with no file, no flag and no line in the --json record. A
# prefilter that drops a declaration is worse than no prefilter.
INTERESTING_TOKENS = (
    "create_publisher", "create_subscription", "create_service", "create_client",
    "create_server", "declare_parameter", "declare_parameters", "auto_declare",
    "ActionServer", "ActionClient", "rclcpp", "rclpy", "create_node",
)

NOTE_EMPTY_LIST = ("source default is an empty list; the DSL list production needs at least "
                   "one element, so no default: is emitted")


def wrap_comment(text, indent="", width=96):
    """Wrap into `# ` comment lines. Comments live at column 0, above the model, so a
    long one never risks RM094 (a column-0 comment inside an indented block)."""
    import textwrap
    body = textwrap.wrap(text, width=width - len(indent) - 2,
                         break_long_words=False, break_on_hyphens=False) or [""]
    return "\n".join("# %s%s" % (indent, ln) for ln in body)


# --------------------------------------------------------------------------- model


class Interface:
    def __init__(self, block, name, type_ref, qos_depth, file, line):
        self.block = block
        self.name = name
        self.type_ref = type_ref        # 'pkg/msg/Type' or None when unresolved
        self.raw_type = None            # what the source actually wrote
        self.qos_depth = qos_depth
        self.file = file
        self.line = line

    def key(self):
        return (self.block, self.name)


class Param:
    def __init__(self, name, dsl_type, value, file, line, note=None):
        self.name = name
        self.dsl_type = dsl_type        # 'Double' | 'String' | ... | None
        # The DSL literal for the package's own declared default, or None when the
        # source's default has no legal form. It is emitted as `default:`, not `value:`:
        # SKILL.md rule 9 makes `default:` a member of ParameterType, and a compiled-in
        # declare_parameter()/generate_parameter_library default IS a default. That keeps
        # `value:` free for the DEPLOYED value, which belongs in the .rossystem.
        self.value = value
        self.note = note
        self.file = file
        self.line = line


class Flag:
    """Something real that was found but deliberately not emitted."""

    def __init__(self, kind, reason, file, line, snippet=""):
        self.kind = kind
        self.reason = reason
        self.file = file
        self.line = line
        self.snippet = snippet
        self.node = None                # NodeDraft whose declaration raised it, when known

    def as_comment(self, root):
        rel = os.path.relpath(self.file, root) if root else self.file
        head = "FLAG %s [%s:%d]" % (self.kind, rel.replace(os.sep, "/"), self.line)
        text = wrap_comment(head) + "\n" + wrap_comment(self.reason, "  ")
        if self.snippet:
            text += "\n" + wrap_comment("source: " + self.snippet, "  ")
        return text


class NodeDraft:
    def __init__(self, name, confident):
        self.name = name
        self.confident = confident      # was the node name a literal in the source?
        self.interfaces = OrderedDict()  # key -> Interface
        self.params = OrderedDict()      # name -> Param
        self.files = set()               # source files this node's declarations came from

    def add_interface(self, iface):
        self.interfaces.setdefault(iface.key(), iface)
        self.files.add(iface.file)

    def add_param(self, p):
        self.params.setdefault(p.name, p)
        self.files.add(p.file)


class PackageDraft:
    def __init__(self, name, path, build_type):
        self.name = name
        self.path = path
        self.build_type = build_type
        self.nodes = OrderedDict()       # node name -> NodeDraft
        self.fallback = None             # NodeDraft for calls with no node class
        self.fallbacks = []              # that bucket, split per build target (see below)
        self.target_node_names = {}      # build target (or None) -> literal node name
        self.flags = []
        self.notes = []
        self.languages = set()
        self.targets = {}                # source path -> build target (artifact) name

    def node_for(self, name, confident):
        if name not in self.nodes:
            self.nodes[name] = NodeDraft(name, confident)
        return self.nodes[name]

    def fallback_node(self):
        if self.fallback is None:
            self.fallback = NodeDraft(self.name, False)
        return self.fallback


# ----------------------------------------------------------------- package discovery


def find_packages(paths):
    """Every directory with a package.xml under any of `paths`."""
    found = OrderedDict()
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(os.path.join(p, "package.xml")):
            found.setdefault(p, True)
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if "package.xml" in filenames:
                found.setdefault(dirpath, True)
                dirnames[:] = []  # a package never nests another package
    return list(found)


def read_package_xml(pkg_dir):
    """(declared name, build_type). build_type is only a hint -- language is per-file."""
    path = os.path.join(pkg_dir, "package.xml")
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return os.path.basename(pkg_dir), None
    name_el = root.find("name")
    name = name_el.text.strip() if name_el is not None and name_el.text else os.path.basename(pkg_dir)
    bt = root.find("./export/build_type")
    build_type = bt.text.strip() if bt is not None and bt.text else None
    return name, build_type


def read_build_targets(pkg_dir):
    """{abs source path: build target name} from CMakeLists.txt / setup.py.

    SKILL.md rule 2a: package, artifact and node are three different names, and the
    artifact is *the executable to run*. That is what add_executable()/add_library()
    names -- deriving it from the node name instead collapses two of the three."""
    targets = {}
    cmake = os.path.join(pkg_dir, "CMakeLists.txt")
    if os.path.isfile(cmake):
        try:
            with open(cmake, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        project = ""
        if "project(" in text:
            project = text.split("project(", 1)[1].split(")")[0].split()[0].strip()
        for keyword in ("add_executable(", "add_library("):
            for chunk in text.split(keyword)[1:]:
                body = chunk.split(")")[0]
                tokens = [t.strip('"\'') for t in body.split()]
                if not tokens:
                    continue
                name = tokens[0].replace("${PROJECT_NAME}", project)
                for token in tokens[1:]:
                    if token in ("SHARED", "STATIC", "MODULE", "INTERFACE", "OBJECT"):
                        continue
                    candidate = os.path.join(pkg_dir, token)
                    if os.path.isfile(candidate):
                        targets[os.path.abspath(candidate)] = name
    setup = os.path.join(pkg_dir, "setup.py")
    if os.path.isfile(setup):
        try:
            with open(setup, encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (OSError, SyntaxError):
            tree = None
        for node in ast.walk(tree) if tree else []:
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if "=" not in node.value or ":" not in node.value:
                continue
            exe, _, rest = node.value.partition("=")
            module = rest.split(":")[0].strip().replace(".", os.sep)
            candidate = os.path.join(pkg_dir, module + ".py")
            if os.path.isfile(candidate):
                targets[os.path.abspath(candidate)] = exe.strip()
    return targets


def source_files(pkg_dir):
    out = []
    for dirpath, dirnames, filenames in os.walk(pkg_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in PY_EXT or ext in CPP_EXT:
                out.append(os.path.join(dirpath, fn))
    return out


def is_launch_or_setup(path):
    base = os.path.basename(path)
    return (
        base in ("setup.py", "conftest.py")
        or base.endswith(".launch.py")
        or ".launch." in base
        or os.sep + "launch" + os.sep in path
        or os.sep + "test" + os.sep in path
        or base.startswith("test_")
    )


# ----------------------------------------------------------------- type resolution


class TypeResolver:
    """Resolve 'pkg/msg/Type' against the vendored catalogue, then against local
    .msg/.srv/.action files found in the scanned tree. Never invents a spelling."""

    def __init__(self, search_roots):
        self.catalogue = {}
        try:
            with open(TYPE_INDEX, encoding="utf-8") as fh:
                self.catalogue = json.load(fh).get("types", {})
        except Exception:
            self.catalogue = {}
        self.local = {}  # 'pkg/msg/Type' -> abs path of the .msg file
        for root in search_roots:
            self._scan_local(root)

    def _scan_local(self, root):
        kinds = {".msg": "msg", ".srv": "srv", ".action": "action"}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            base = os.path.basename(dirpath)
            if base not in ("msg", "srv", "action"):
                continue
            pkg_dir = os.path.dirname(dirpath)
            if not os.path.isfile(os.path.join(pkg_dir, "package.xml")):
                continue
            pkg = read_package_xml(pkg_dir)[0]
            for fn in filenames:
                stem, ext = os.path.splitext(fn)
                kind = kinds.get(ext)
                if kind and kind == base:
                    self.local.setdefault("%s/%s/%s" % (pkg, kind, stem),
                                          os.path.join(dirpath, fn))

    def resolve(self, ref, report_root):
        """-> (status, comment). status in {'catalogue', 'local', 'unresolved'}."""
        if not ref:
            return "unresolved", "# TODO: type not literal in source"
        hit = self.catalogue.get(ref)
        if hit:
            return "catalogue", "# assets/roscommonobjects/%s" % hit["file"]
        path = self.local.get(ref)
        if path:
            rel = os.path.relpath(path, report_root) if report_root else path
            return "local", "# project-local, defined by %s" % rel.replace(os.sep, "/")
        return "unresolved", ("# TODO unresolved: %s is neither in assets/type_index.json "
                              "nor a .msg/.srv/.action in the scanned tree" % ref)


# ------------------------------------------------------------- .ros companion emitter
#
# A .ros2 whose type: refs are project-local is not a loadable model project on its own:
# the oracle resolves those refs through a companion .ros, and an unresolved ref is a
# linking-layer ERROR. TypeResolver already knows where every project-local .msg/.srv/
# .action lives, so emitting the companion is a transcription, not a new inference.
# SKILL.md sec 8b is the authority for the shape.

ROS_SCALARS = {
    "bool", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64",
    "uint64", "float32", "float64", "string", "byte", "char",
    "time", "duration", "Header",
}
# these three have no array form (SKILL.md sec 8b)
ROS_NO_ARRAY = {"time", "duration", "Header"}
MSG_SECTIONS = {"msg": ["message"], "srv": ["request", "response"],
                "action": ["goal", "result", "feedback"]}


def parse_msg_file(path, pkg, kind, flags):
    """A .msg/.srv/.action -> [[field, ...], ...], one list per '---'-separated section."""
    sections, current = [[]], 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.readlines()
    except OSError as exc:
        flags.append(Flag("msg", "could not read: %s" % exc, path, 1))
        return sections

    for lineno, raw in enumerate(raw_lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("---"):
            sections.append([])
            current += 1
            continue
        parts = line.split()
        if len(parts) < 2:
            flags.append(Flag("msg", "line is not '<type> <name>': %r" % line, path, lineno))
            continue
        typ, rest = parts[0], parts[1]

        # constant:  uint8 FAN_OFF=0   /   uint8 FAN_OFF = 0
        joined = " ".join(parts[1:])
        if "=" in joined:
            name, _, value = joined.partition("=")
            emitted = emit_ros_type(typ, pkg, path, lineno, flags)
            if emitted:
                val = value.strip()
                if typ in ("string", "wstring") and not (val.startswith(('"', "'")) and val.endswith(('"', "'"))):
                    if any(c.isspace() for c in val) or not val.isidentifier():
                        val = '"%s"' % val.replace('"', '\\"')
                sections[current].append("%s %s=%s" % (emitted, name.strip(), val))
            continue

        # plain field; a third token is a per-field default, which .ros cannot express
        if len(parts) > 2:
            flags.append(Flag(
                "msg", "field '%s' carries a default value (%s) -- the .ros grammar's "
                       "MessagePart has no default slot, so it is dropped"
                       % (rest, " ".join(parts[2:])), path, lineno))
        emitted = emit_ros_type(typ, pkg, path, lineno, flags)
        if emitted:
            sections[current].append("%s %s" % (emitted, rest))
    return sections


def emit_ros_type(typ, pkg, path, lineno, flags):
    """A .msg field type -> its .ros spelling, or None when it cannot be written."""
    base, is_array, bound = typ, False, ""
    if base.endswith("]"):
        head, _, inner = base[:-1].partition("[")
        base, is_array, bound = head, True, inner
    if bound:
        flags.append(Flag(
            "msg", "bounded/fixed array '%s' has no .ros form (bounded arrays are a "
                   "lexer error, SKILL.md sec 8b); field dropped" % typ, path, lineno))
        return None
    if base in ROS_SCALARS:
        if is_array and base in ROS_NO_ARRAY:
            flags.append(Flag(
                "msg", "'%s' has no array form in .ros; field dropped" % typ, path, lineno))
            return None
        return base + ("[]" if is_array else "")
    # a reference to another spec: bare short name means this package
    ref = base if "/" in base else "%s/msg/%s" % (pkg, base)
    if ref.count("/") == 1:                       # 'pkg/Type' -> 'pkg/msg/Type'
        left, _, right = ref.partition("/")
        ref = "%s/msg/%s" % (left, right)
    return '"%s"%s' % (ref, "[]" if is_array else "")


def emit_ros_package(pkg, specs, report_root, flags):
    """specs: {kind: {SpecName: path}} for one package -> the .ros file text."""
    # Body first: parsing is what discovers the losses, and they belong in the header of
    # the file itself. Printing them only to stderr means the record dies with the
    # terminal, leaving a file that silently differs from the .msg it came from --
    # exactly the drop SKILL.md's self-check 12 exists to prevent.
    body, mine = [], []
    for kind, block in (("msg", "msgs"), ("srv", "srvs"), ("action", "actions")):
        entries = specs.get(kind) or {}
        if not entries:
            continue
        body.append("  %s:" % block)
        for name in sorted(entries):
            sections = parse_msg_file(entries[name], pkg, kind, mine)
            body.append("    %s" % name)           # level 2, no trailing ':'
            for idx, keyword in enumerate(MSG_SECTIONS[kind]):
                body.append("      %s" % keyword)   # level 3
                for field in (sections[idx] if idx < len(sections) else []):
                    body.append("        %s" % field)  # level 4
    flags.extend(mine)

    lines = ["# GENERATED -- scripts/extract_ros2_interfaces.py",
             "# Transcribed from the package's own .msg/.srv/.action files."]
    for kind in ("msg", "srv", "action"):
        for name in sorted(specs.get(kind, {})):
            rel = os.path.relpath(specs[kind][name], report_root)
            lines.append("#   %s" % rel.replace(os.sep, "/"))
    if mine:
        lines.append("#")
        lines.append(wrap_comment(
            "DROPPED (%d) -- the .ros grammar cannot express these, so they are in the .msg "
            "above but not in this file:" % len(mine)))
        for flag in mine:
            rel = os.path.relpath(flag.file, report_root).replace(os.sep, "/")
            lines.append(wrap_comment("%s  [%s:%d]" % (flag.reason, rel, flag.line), "  "))
    lines.append("%s:" % pkg)
    return "\n".join(lines + body) + "\n"


def referenced_closure(resolver, referenced):
    """A .msg field can reference another project-local spec, which then also needs a body
    in the .ros -- otherwise the emitted reference is an unresolved link. Follow to a fixed
    point."""
    out, queue = set(), [r for r in referenced if r in resolver.local]
    while queue:
        ref = queue.pop()
        if ref in out:
            continue
        out.add(ref)
        pkg = ref.split("/", 1)[0]
        for section in parse_msg_file(resolver.local[ref], pkg, ref.split("/")[1], []):
            for field in section:
                if not field.startswith('"'):
                    continue
                inner = field[1:field.index('"', 1)]
                if inner in resolver.local and inner not in out:
                    queue.append(inner)
    return out


def emit_companion_msgs(resolver, out_dir, report_root, referenced):
    """Write one .ros per project-local package whose types `referenced` names."""
    by_pkg = {}
    for ref in sorted(referenced_closure(resolver, referenced)):
        path = resolver.local.get(ref)
        if not path:
            continue
        pkg, kind, name = ref.split("/", 2)
        by_pkg.setdefault(pkg, {}).setdefault(kind, {})[name] = path
    written, flags = [], []
    if by_pkg:
        os.makedirs(out_dir, exist_ok=True)
    for pkg, specs in sorted(by_pkg.items()):
        path = os.path.join(out_dir, "%s.ros" % pkg)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(emit_ros_package(pkg, specs, report_root, flags))
        written.append(path)
    return written, flags


# ------------------------------------------------------------------ python extractor


PY_METHOD_BLOCK = {
    "create_publisher": "publishers",
    "create_subscription": "subscribers",
    "create_service": "serviceservers",
    "create_client": "serviceclients",
}
# (type arg index, name arg index) for the rclpy methods above
PY_METHOD_ARGS = {
    "create_publisher": (0, 1),
    "create_subscription": (0, 1),
    "create_service": (0, 1),
    "create_client": (0, 1),
}
# rclpy.action.ActionServer(node, action_type, action_name, ...)
PY_ACTION_CTOR = {"ActionServer": "actionservers", "ActionClient": "actionclients"}


def py_literal(node):
    """Return the Python literal a node evaluates to, or a NOT_LITERAL sentinel."""
    try:
        return ast.literal_eval(node)
    except Exception:
        return _NOT_LITERAL


class _NotLiteral:
    def __repr__(self):
        return "<not-literal>"


_NOT_LITERAL = _NotLiteral()


def py_type_ref(node, imports):
    """Map an AST expression naming a message class to 'pkg/kind/Type', or None."""
    if isinstance(node, ast.Name):
        return imports.get(node.id)
    if isinstance(node, ast.Attribute):
        parts = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            parts.reverse()
            # std_msgs.msg.String  /  my_pkg.action.Fib
            if len(parts) >= 3 and parts[-2] in ("msg", "srv", "action"):
                return "%s/%s/%s" % (parts[-3], parts[-2], parts[-1])
    return None


def py_imports(tree):
    """{local name: 'pkg/kind/Type'} from `from pkg.msg import Type [as X]`."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            parts = node.module.split(".")
            if len(parts) == 2 and parts[1] in ("msg", "srv", "action"):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    out[alias.asname or alias.name] = "%s/%s/%s" % (
                        parts[0], parts[1], alias.name)
    return out


def call_arg(call, index, keyword_names):
    """Positional arg `index`, or the first matching keyword."""
    if index is not None and index < len(call.args):
        arg = call.args[index]
        if not isinstance(arg, ast.Starred):
            return arg
    for kw in call.keywords:
        if kw.arg in keyword_names:
            return kw.value
    return None


def py_param_dsl(value):
    """Python literal -> (DSL type keyword, DSL value literal) or (None, None)."""
    if isinstance(value, bool):
        return "Boolean", "true" if value else "false"
    if isinstance(value, int):
        return "Integer", str(value)
    if isinstance(value, float):
        text = repr(value)
        if "." not in text and "e" not in text and "E" not in text:
            text += ".0"
        return "Double", text
    if isinstance(value, str):
        return "String", '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    if isinstance(value, (list, tuple)):
        items = list(value)
        if not items:
            # The list production needs >=1 element, so `[]` is a parse error -- and a
            # quoted "[]" is a ParameterString, i.e. a lie (RM095). value:/default: is
            # optional on a .ros2 parameter, so the honest form is to omit it.
            return "Array[String]", None
        inner = {py_param_dsl(v)[0] for v in items}
        if len(inner) != 1 or None in inner:
            return None, None
        elem = inner.pop()
        rendered = [py_param_dsl(v)[1] for v in items]
        return "Array[%s]" % elem, "[%s]" % ", ".join(rendered)
    return None, None


class PyFileVisitor(ast.NodeVisitor):
    def __init__(self, pkg, path, imports, snippet_lines):
        self.pkg = pkg
        self.path = path
        self.imports = imports
        self.lines = snippet_lines
        self.class_stack = []
        self.func_stack = []
        self.node_classes = {}     # class name -> node name (literal) or None
        self.calls = []            # (owner class or None, ast.Call)
        self.call_funcs = {}       # id(ast.Call) -> enclosing function, for local names

    # -- structure

    def visit_ClassDef(self, node):
        for base in node.bases:
            base_name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)
            if base_name == "Node":
                self.node_classes[node.name] = self._node_name_of(node)
                break
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def _node_name_of(self, classdef):
        for sub in ast.walk(classdef):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            # super().__init__('name')  or  Node.__init__(self, 'name')
            if isinstance(fn, ast.Attribute) and fn.attr == "__init__":
                args = list(sub.args)
                if isinstance(fn.value, ast.Call) and getattr(fn.value.func, "id", "") == "super":
                    pass
                elif getattr(fn.value, "id", None) == "Node":
                    args = args[1:]
                else:
                    continue
                name_arg = args[0] if args else call_arg(sub, None, ("node_name",))
                if name_arg is not None:
                    val = py_literal(name_arg)
                    if isinstance(val, str):
                        return val
                return None
        return None

    def visit_FunctionDef(self, node):
        self.func_stack.append(node)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        self.calls.append((self.class_stack[-1] if self.class_stack else None, node))
        if self.func_stack:
            self.call_funcs[id(node)] = self.func_stack[-1]
        self.generic_visit(node)

    def local_assigns(self, call):
        """Local names the call's function assigns exactly once, as `x = <expr>`."""
        func = self.call_funcs.get(id(call))
        if func is None:
            return {}
        stores, simple = {}, {}
        for sub in ast.walk(func):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                stores[sub.id] = stores.get(sub.id, 0) + 1
            if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name) and sub.lineno < call.lineno):
                simple[sub.targets[0].id] = sub.value
        return {k: v for k, v in simple.items() if stores.get(k) == 1}

    # -- snippets

    def snippet(self, node):
        """The source text of `node`, collapsed to one line."""
        first = node.lineno
        last = getattr(node, "end_lineno", None) or first
        text = " ".join(ln.strip() for ln in self.lines[first - 1:last])
        return " ".join(text.split())[:140]


def extract_python(pkg, path, resolver, report_root):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        pkg.flags.append(Flag("parse", "python file did not parse: %s" % exc.msg, path,
                              exc.lineno or 1))
        return False
    if not any(tok in text for tok in INTERESTING_TOKENS):
        return False

    imports = py_imports(tree)
    visitor = PyFileVisitor(pkg, path, imports, text.splitlines())
    visitor.visit(tree)
    if not visitor.calls:
        return False

    pkg.languages.add("python")
    # module-level `Node('name')` / `rclpy.create_node('name')`
    for owner, call in visitor.calls:
        fname = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
        if fname == "create_node" and call.args:
            val = py_literal(call.args[0])
            if isinstance(val, str):
                visitor.node_classes.setdefault("<module>", val)

    named = {c: n for c, n in visitor.node_classes.items() if n}
    for cls, name in named.items():
        pkg.node_for(name, True)
    for cls, name in visitor.node_classes.items():
        if not name:
            pkg.flags.append(Flag(
                "node-name",
                "class %s subclasses rclpy Node but its node name is not a string "
                "literal (built at runtime); artifact falls back to the package name" % cls,
                path, 1))

    # `n = Node("demo_head")` / `self.host = Node("sim_bridge")` outside a Node subclass:
    # calls made on that handle belong to that node, not to the package-named fallback.
    handles = {}
    for sub in ast.walk(tree):
        if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                and isinstance(sub.value, ast.Call) and sub.value.args):
            fn = sub.value.func
            if (fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)) in (
                    "Node", "create_node"):
                val = py_literal(sub.value.args[0])
                if isinstance(val, str):
                    handles[ast.unparse(sub.targets[0])] = val

    found_any = False
    for owner, call in visitor.calls:
        fn = call.func
        handle = call.args[0] if (getattr(fn, "attr", getattr(fn, "id", None)) in PY_ACTION_CTOR
                                  and call.args) else getattr(fn, "value", None)
        name = handles.get(ast.unparse(handle)) if handle is not None else None
        target = pkg.node_for(name, True) if name else _py_target(pkg, owner, named)
        before = len(pkg.flags)
        if _handle_py_call(pkg, target, call, visitor, resolver, report_root, path):
            found_any = True
        for flag in pkg.flags[before:]:
            flag.node = target
    return found_any


def _py_target(pkg, owner, named):
    if owner and owner in named:
        return pkg.node_for(named[owner], True)
    if len(named) == 1:
        return pkg.node_for(next(iter(named.values())), True)
    return pkg.fallback_node()


def _handle_py_call(pkg, target, call, visitor, resolver, report_root, path):
    fn = call.func
    if isinstance(fn, ast.Attribute):
        fname = fn.attr
    elif isinstance(fn, ast.Name):
        fname = fn.id
    else:
        return False

    if fname in PY_METHOD_BLOCK:
        ti, ni = PY_METHOD_ARGS[fname]
        type_node = call_arg(call, ti, ("msg_type", "srv_type"))
        name_node = call_arg(call, ni, ("topic", "topic_name", "srv_name", "service_name"))
        return _record_interface(
            pkg, target, PY_METHOD_BLOCK[fname], name_node, type_node,
            _py_qos(call, fname), visitor, resolver, report_root, path, call)

    if fname in PY_ACTION_CTOR:
        # ActionServer(node, action_type, action_name, ...)
        type_node = call_arg(call, 1, ("action_type",))
        name_node = call_arg(call, 2, ("action_name",))
        return _record_interface(
            pkg, target, PY_ACTION_CTOR[fname], name_node, type_node,
            None, visitor, resolver, report_root, path, call)

    if fname == "declare_parameter":
        name_node = call_arg(call, 0, ("name",))
        val_node = call_arg(call, 1, ("value",))
        return _record_param(pkg, target, name_node, val_node, visitor, path, call)

    if fname == "declare_parameters":
        lst = call_arg(call, 1, ("parameters",))
        ns = py_literal(call_arg(call, 0, ("namespace",))) if call.args or call.keywords else ""
        prefix = ns + "." if isinstance(ns, str) and ns else ""
        if not isinstance(lst, (ast.List, ast.Tuple)):
            pkg.flags.append(Flag("parameter",
                                  "declare_parameters() argument is not a literal list",
                                  path, call.lineno, visitor.snippet(call)))
            return False
        ok = False
        for elt in lst.elts:
            if isinstance(elt, (ast.Tuple, ast.List)) and elt.elts:
                nn = elt.elts[0]
                vv = elt.elts[1] if len(elt.elts) > 1 else None
                ok |= _record_param(pkg, target, nn, vv, visitor, path, call,
                                    prefix=prefix)
            else:
                pkg.flags.append(Flag("parameter",
                                      "declare_parameters() entry is not a literal tuple",
                                      path, getattr(elt, "lineno", call.lineno),
                                      visitor.snippet(call)))
        return ok
    return False


def _py_qos(call, fname):
    idx = {"create_publisher": 2, "create_subscription": 3}.get(fname)
    if idx is None:
        return None
    node = call_arg(call, idx, ("qos_profile",))
    if node is None:
        return None
    val = py_literal(node)
    return val if isinstance(val, int) and not isinstance(val, bool) else None


def py_partial_name(node, assigns):
    """A runtime-built name as its literal pieces plus {expression} placeholders, e.g.
    ns + "/detections" with ns = f"/perception/{self.cam_name}" gives
    '/perception/{self.cam_name}/detections'. A local assigned once is followed; nothing is
    guessed. None when no literal piece is left to identify the name by."""
    parts = []

    def walk(n, depth):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            walk(n.left, depth)
            walk(n.right, depth)
        elif isinstance(n, ast.JoinedStr):
            for value in n.values:
                walk(value.value if isinstance(value, ast.FormattedValue) else value, depth)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            parts.append((True, n.value))
        elif isinstance(n, ast.Name) and n.id in assigns and depth < 3:
            walk(assigns[n.id], depth + 1)
        else:
            text = ast.unparse(n)
            parts.append((False, "{%s}" % "".join(c for c in text if c not in "'\" \t{}")))

    walk(node, 0)
    if not any(literal and any(c.isalnum() for c in text) for literal, text in parts):
        return None
    return "".join(text for _, text in parts)


def _record_interface(pkg, target, block, name_node, type_node, qos, visitor,
                      resolver, report_root, path, call):
    line = call.lineno
    snippet = visitor.snippet(call)
    if name_node is None or type_node is None:
        pkg.flags.append(Flag(block, "call is missing a literal name or type argument",
                              path, line, snippet))
        return False
    name = py_literal(name_node)
    if not isinstance(name, str):
        reason = ("name is not a string literal (built at runtime -- parameter, "
                  "f-string or variable); resolve by hand")
        partial = py_partial_name(name_node, visitor.local_assigns(call))
        ref = py_type_ref(type_node, visitor.imports) if partial else None
        if ref is not None:
            # The type is literal, so the port is real: keep it under the name's literal
            # pieces, each runtime piece shown as {its source expression}. Still flagged.
            reason += (". Kept as '%s' with its literal type %s; each {...} is a runtime "
                       "value, not part of the real name" % (partial, ref))
            target.add_interface(Interface(block, partial, ref, qos, path, line))
        pkg.flags.append(Flag(block, reason, path, line, snippet))
        return ref is not None
    ref = py_type_ref(type_node, visitor.imports)
    if ref is None:
        pkg.flags.append(Flag(
            block, "'%s' has a type that is not a resolvable class literal" % name,
            path, line, snippet))
        return False
    iface = Interface(block, name, ref, qos, path, line)
    target.add_interface(iface)
    return True


def _record_param(pkg, target, name_node, val_node, visitor, path, call, prefix=""):
    line = call.lineno
    snippet = visitor.snippet(call)
    name = py_literal(name_node) if name_node is not None else _NOT_LITERAL
    if not isinstance(name, str):
        pkg.flags.append(Flag("parameter", "parameter name is not a string literal",
                              path, line, snippet))
        return False
    name = prefix + name
    if val_node is None:
        # declare_parameter('x') with no default: the name is real, the type is not.
        pkg.flags.append(Flag(
            "parameter", "'%s' is declared with no default value, so its type cannot "
                         "be inferred from source" % name, path, line, snippet))
        return False
    val = py_literal(val_node)
    if val is _NOT_LITERAL:
        pkg.flags.append(Flag(
            "parameter", "'%s' has a non-literal default value" % name, path, line,
            snippet))
        return False
    dsl_type, dsl_val = py_param_dsl(val)
    if dsl_type is None:
        pkg.flags.append(Flag(
            "parameter", "'%s' default value has no DSL type (%r)" % (name, val),
            path, line, snippet))
        return False
    note = NOTE_EMPTY_LIST if (dsl_val is None and isinstance(val, (list, tuple))) else None
    target.add_param(Param(name, dsl_type, dsl_val, path, line, note))
    return True


# --------------------------------------------------------------------- cpp extractor


CPP_METHOD_BLOCK = {
    "create_publisher": "publishers",
    "create_subscription": "subscribers",
    "create_service": "serviceservers",
    "create_client": "serviceclients",
}
CPP_ACTION_BLOCK = {"create_server": "actionservers", "create_client": "actionclients"}

CPP_PRIMITIVE_PARAM = {
    "double": "Double", "float": "Double",
    "int": "Integer", "int64_t": "Integer", "int32_t": "Integer",
    "long": "Integer", "unsigned": "Integer", "uint8_t": "Integer",
    "size_t": "Integer",
    "bool": "Boolean",
    "std::string": "String", "string": "String",
    "std::vector<std::string>": "Array[String]",
    "std::vector<double>": "Array[Double]",
    "std::vector<bool>": "Array[Boolean]",
    "std::vector<int>": "Array[Integer]",
    "std::vector<int64_t>": "Array[Integer]",
}


def _load_cpp_parser():
    try:
        import tree_sitter_cpp
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    return Parser(Language(tree_sitter_cpp.language()))


def ts_text(src, node):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def ts_children(node, *types):
    return [c for c in node.children if c.type in types]


def ts_find(node, type_name, depth=6):
    """First descendant of `type_name`, breadth-limited."""
    stack = [(node, 0)]
    while stack:
        cur, d = stack.pop(0)
        if cur is not node and cur.type == type_name:
            return cur
        if d < depth:
            stack.extend((c, d + 1) for c in cur.children)
    return None


def cpp_callee(src, call):
    """-> (method name, [template arg texts]) for a call_expression, or (None, [])."""
    fn = call.child_by_field_name("function")
    if fn is None:
        return None, []
    # peel `rclcpp_action::create_server<T>` / `std::make_shared<T>`
    while fn.type == "qualified_identifier":
        last = fn.children[-1]
        if last.type in ("template_function", "identifier", "template_method",
                         "field_identifier", "qualified_identifier"):
            fn = last
        else:
            break
    if fn.type == "field_expression":
        fn = fn.child_by_field_name("field") or fn.children[-1]
    if fn.type == "dependent_name":  # `x->template f<T>()`
        fn = fn.children[-1]
    if fn.type in ("template_method", "template_function"):
        name_node = fn.children[0]
        targs = []
        talist = ts_find(fn, "template_argument_list", depth=1)
        if talist is not None:
            targs = [ts_text(src, c) for c in talist.children
                     if c.type not in ("<", ">", ",")]
        return ts_text(src, name_node), targs
    if fn.type in ("identifier", "field_identifier"):
        return ts_text(src, fn), []
    return None, []


# `rclcpp::Node::make_shared("x")` and friends. Only the qualifier matters: a user class
# called `make_shared` on itself is not a node factory.
NODE_FACTORY_TAILS = ("Node::make_shared", "Node::make_unique",
                      "LifecycleNode::make_shared", "LifecycleNode::make_unique")


def cpp_collect_free_node_names(src, root):
    """Literal node names for a Node constructed OUTSIDE a class -- typically in main().

    `class X : public rclcpp::Node` with `: Node("x")` is cpp_collect_node_classes' job.
    This covers the other common shape, the one every MoveIt-style demo uses:

        auto node = rclcpp::Node::make_shared("ssi_demo_node", options);
        auto node = std::make_shared<rclcpp::Node>("ssi_demo_node", options);

    There is no class to hang the name on, so it used to be ignored and the artifact fell
    back to the package name -- which a .rossystem's `from:` then inherited. The name is a
    plain string literal sitting in the source, so leaving it unread broke this script's
    one promise: everything literal is emitted.

    Returns the distinct literals found, sorted so a run is reproducible.
    """
    out = set()
    stack = [root]
    while stack:
        cur = stack.pop()
        stack.extend(cur.children)
        if cur.type != "call_expression":
            continue
        fn = cur.child_by_field_name("function")
        if fn is None:
            continue
        qualified = ts_text(src, fn).replace(" ", "")
        hit = any(qualified.endswith(tail) for tail in NODE_FACTORY_TAILS)
        if not hit:
            # std::make_shared<rclcpp::Node>(...) -- the type is a template argument.
            fname, targs = cpp_callee(src, cur)
            if fname in ("make_shared", "make_unique") and targs:
                hit = targs[0].replace(" ", "").split("::")[-1] in ("Node", "LifecycleNode")
        if not hit:
            continue
        args = cpp_args(cur)
        lit = cpp_string_literal(src, args[0]) if args else None
        if lit:
            out.add(lit)
    return sorted(out)


def cpp_call_namespace(src, call):
    """'rclcpp_action' for rclcpp_action::create_server<...>(...), else ''."""
    fn = call.child_by_field_name("function")
    if fn is not None and fn.type == "qualified_identifier":
        first = fn.children[0]
        if first.type == "namespace_identifier":
            return ts_text(src, first)
    return ""


def cpp_args(call):
    alist = call.child_by_field_name("arguments")
    if alist is None:
        return []
    return [c for c in alist.children if c.type not in ("(", ")", ",")]


def cpp_string_literal(src, node):
    """The string a node evaluates to, or None if it is not a pure literal."""
    if node is None:
        return None
    if node.type == "string_literal":
        content = ts_find(node, "string_content", depth=1)
        return ts_text(src, content) if content is not None else ""
    if node.type == "concatenated_string":
        parts = []
        for c in node.children:
            piece = cpp_string_literal(src, c)
            if piece is None:
                return None
            parts.append(piece)
        return "".join(parts)
    return None


def cpp_type_ref(text, aliases, depth=0):
    """'std_msgs::msg::Bool' (or an alias for it) -> 'std_msgs/msg/Bool'."""
    text = text.strip().lstrip(":")
    if text in aliases and depth < 8:
        return cpp_type_ref(aliases[text], aliases, depth + 1)
    parts = text.split("::")
    if len(parts) >= 3 and parts[-2] in ("msg", "srv", "action"):
        return "%s/%s/%s" % (parts[-3], parts[-2], parts[-1])
    return None


def cpp_collect_aliases(src, root, aliases):
    """using X = A::B::C;  and  typedef A::B::C X;"""
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type == "alias_declaration":
            name = cur.child_by_field_name("name")
            value = cur.child_by_field_name("type")
            if name is not None and value is not None:
                aliases[ts_text(src, name)] = ts_text(src, value)
        elif cur.type == "type_definition":
            decls = cur.children_by_field_name("declarator")
            typ = cur.child_by_field_name("type")
            if typ is not None:
                for d in decls:
                    if d.type == "type_identifier":
                        aliases[ts_text(src, d)] = ts_text(src, typ)
        stack.extend(cur.children)


def cpp_collect_node_classes(src, root, node_classes):
    """class X : public rclcpp::Node  ->  {'X': 'literal-name' or None}."""
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type in ("class_specifier", "struct_specifier"):
            name_node = cur.child_by_field_name("name")
            bases = ts_find(cur, "base_class_clause", depth=1)
            if name_node is not None and bases is not None:
                base_text = ts_text(src, bases)
                if "rclcpp::Node" in base_text or "rclcpp_lifecycle::LifecycleNode" in base_text:
                    cls = ts_text(src, name_node)
                    node_classes.setdefault(cls, None)
        elif cur.type == "field_initializer_list":
            for init in ts_children(cur, "field_initializer"):
                head = init.children[0]
                if head.type in ("field_identifier", "qualified_identifier") and \
                        ts_text(src, head).split("::")[-1] in ("Node", "LifecycleNode"):
                    alist = ts_find(init, "argument_list", depth=1)
                    if alist is not None:
                        for a in alist.children:
                            lit = cpp_string_literal(src, a)
                            if lit is not None:
                                owner = _cpp_enclosing_class(src, init)
                                if owner:
                                    node_classes[owner] = lit
                                break
        stack.extend(cur.children)


def _cpp_enclosing_class(src, node):
    """Class name owning this node: enclosing class_specifier, or the Ns::Class:: of
    an out-of-line definition."""
    cur = node
    while cur is not None:
        if cur.type in ("class_specifier", "struct_specifier"):
            n = cur.child_by_field_name("name")
            if n is not None:
                return ts_text(src, n)
        if cur.type == "function_definition":
            decl = cur.child_by_field_name("declarator")
            qual = _qualified_owner(src, decl)
            if qual:
                return qual
        cur = cur.parent
    return None


def _qualified_owner(src, decl, depth=0):
    """Ns::Class::method(...) -> 'Class'."""
    if decl is None or depth > 6:
        return None
    if decl.type == "function_declarator":
        return _qualified_owner(src, decl.child_by_field_name("declarator"), depth + 1)
    if decl.type in ("reference_declarator", "pointer_declarator"):
        for c in decl.children:
            got = _qualified_owner(src, c, depth + 1)
            if got:
                return got
        return None
    if decl.type == "qualified_identifier":
        parts = ts_text(src, decl).split("::")
        if len(parts) >= 2:
            return parts[-2]
    return None


def cpp_param_type(targs, args, src):
    """(DSL type, DSL value) from declare_parameter<T>(name, default)."""
    dsl = None
    if targs:
        dsl = CPP_PRIMITIVE_PARAM.get(targs[0].replace(" ", ""))
    default = args[1] if len(args) > 1 else None
    if default is None:
        return dsl, None
    lit = cpp_string_literal(src, default)
    if lit is not None:
        return (dsl or "String"), '"%s"' % lit.replace("\\", "\\\\").replace('"', '\\"')
    text = ts_text(src, default).strip()
    if default.type == "true" or text == "true":
        return (dsl or "Boolean"), "true"
    if default.type == "false" or text == "false":
        return (dsl or "Boolean"), "false"
    if default.type == "number_literal" or (
            default.type == "unary_expression" and ts_find(default, "number_literal", depth=1)):
        clean = text.rstrip("fFlLuU")
        if "." in clean or "e" in clean or "E" in clean:
            return (dsl or "Double"), clean
        if dsl == "Double":
            return "Double", clean + ".0"
        return (dsl or "Integer"), clean
    if default.type in ("compound_literal_expression", "initializer_list"):
        il = default if default.type == "initializer_list" else ts_find(default, "initializer_list", depth=2)
        if il is not None and not [c for c in il.children if c.type not in ("{", "}")]:
            return (dsl or "Array[String]"), None   # omit; see py_param_dsl
        if il is not None:
            items = [cpp_string_literal(src, c) for c in il.children
                     if c.type not in ("{", "}", ",")]
            if items and all(i is not None for i in items):
                return (dsl or "Array[String]"), "[%s]" % ", ".join('"%s"' % i for i in items)
    return dsl, None


def cpp_name_evidence(src, name_arg, owner, trees):
    """For a name built as literals + one identifier, gather what that identifier is
    actually given at every construction site VISIBLE in this package, and return the
    candidate names.

    This is EVIDENCE, never a declaration. Deciding whether the visible construction
    sites are all of them is a completeness claim a per-package parser cannot make --
    the class may also be built in another package, a test, or a loop -- so the caller
    still flags. Emitting a plausible subset would turn a visible unknown into an
    invisible one, which is strictly worse than saying nothing.
    """
    parts, names = [], set()
    stack = [name_arg]
    while stack:                      # flatten the concatenation, left to right
        node = stack.pop(0)
        if node.type == "binary_expression":
            stack = [c for c in node.children if c.type != "+"] + stack
            continue
        lit = cpp_string_literal(src, node)
        if lit is not None:
            parts.append(("lit", lit))
        elif node.type == "identifier":
            ident = ts_text(src, node)
            parts.append(("var", ident))
            names.add(ident)
        else:
            return None
    if len(names) != 1 or not owner:
        return None
    variable = names.pop()

    index = _cpp_ctor_param_index(src, owner, variable, trees)
    if index is None:
        return None
    values, incomplete = [], False
    for path, fsrc, tree in trees:
        for args in _cpp_construction_args(fsrc, tree, owner):
            if index >= len(args):
                incomplete = True
                continue
            lit = cpp_string_literal(fsrc, args[index])
            if lit is None:
                incomplete = True
            elif lit not in values:
                values.append(lit)
    if not values:
        return None
    values.sort()          # stable across runs; the tree walk order is not meaningful
    candidates = ["".join(v if kind == "lit" else value for kind, v in parts)
                  for value in values]
    return {"variable": variable, "values": values, "candidates": candidates,
            "sites": len(values), "incomplete": incomplete}


def _cpp_ctor_param_index(src, owner, variable, trees):
    """Position of `variable` in owner's constructor parameter list."""
    for path, fsrc, tree in trees:
        stack = [tree.root_node]
        while stack:
            cur = stack.pop()
            stack.extend(cur.children)
            if cur.type not in ("class_specifier", "struct_specifier"):
                continue
            name_node = cur.child_by_field_name("name")
            if name_node is None or ts_text(fsrc, name_node) != owner:
                continue
            for node in _descendants(cur, "function_declarator"):
                ident = node.child_by_field_name("declarator")
                if ident is None or ts_text(fsrc, ident) != owner:
                    continue
                plist = ts_find(node, "parameter_list", depth=1)
                if plist is None:
                    continue
                params = [c for c in plist.children if c.type == "parameter_declaration"]
                for i, param in enumerate(params):
                    if variable in ts_text(fsrc, param).split():
                        return i
                    inner = ts_find(param, "identifier", depth=4)
                    if inner is not None and ts_text(fsrc, inner) == variable:
                        return i
    return None


def _descendants(node, type_name, limit=4000):
    out, stack, seen = [], [node], 0
    while stack and seen < limit:
        cur = stack.pop()
        seen += 1
        if cur is not node and cur.type == type_name:
            out.append(cur)
        stack.extend(cur.children)
    return out


def _cpp_construction_args(src, tree, owner):
    """Argument lists of every visible construction of `owner`."""
    out = []
    stack = [tree.root_node]
    while stack:
        cur = stack.pop()
        stack.extend(cur.children)
        if cur.type == "new_expression":
            typ = cur.child_by_field_name("type")
            alist = ts_find(cur, "argument_list", depth=2)
            if typ is not None and ts_text(src, typ) == owner and alist is not None:
                out.append([c for c in alist.children if c.type not in ("(", ")", ",")])
            continue
        if cur.type != "call_expression":
            continue
        fname, targs = cpp_callee(src, cur)
        is_make = fname in ("make_unique", "make_shared") and targs and targs[0] == owner
        if is_make or fname == owner:
            out.append(cpp_args(cur))
    return out


def extract_cpp(pkg, paths, parser, resolver, report_root):
    """C++ is parsed package-at-a-time: `using` aliases and node classes live in the
    headers, the calls live in the .cpp."""
    trees = []
    aliases, node_classes = {}, {}
    for path in paths:
        try:
            with open(path, "rb") as fh:
                src = fh.read()
        except OSError:
            continue
        if not any(tok.encode() in src for tok in INTERESTING_TOKENS):
            continue
        tree = parser.parse(src)
        trees.append((path, src, tree))
        cpp_collect_aliases(src, tree.root_node, aliases)
        cpp_collect_node_classes(src, tree.root_node, node_classes)

    if not trees:
        return False
    pkg.languages.add("cpp")

    named = {c: n for c, n in node_classes.items() if n}
    for name in named.values():
        pkg.node_for(name, True)
    for cls, name in node_classes.items():
        if not name:
            pkg.flags.append(Flag(
                "node-name",
                "class %s derives from rclcpp::Node but its name is not a string "
                "literal in the constructor; artifact falls back to the package name" % cls,
                trees[0][0], 1))

    # A node built in main() belongs to the BUILD TARGET whose source declares it, not to
    # the package: one add_executable() is one executable with one main().
    seen = OrderedDict()          # build target -> (set of literal names, a file to cite)
    for path, src, tree in trees:
        key = pkg.targets.get(os.path.abspath(path))
        for lit in cpp_collect_free_node_names(src, tree.root_node):
            seen.setdefault(key, (set(), path))[0].add(lit)
    for key, (names, path) in seen.items():
        if len(names) == 1:
            pkg.target_node_names[key] = next(iter(names))
        else:
            pkg.flags.append(Flag(
                "node-name",
                "build target %s constructs rclcpp::Node with more than one literal name "
                "(%s), so which one this artifact runs under is not decidable from the "
                "source; the artifact keeps the package name"
                % (key or "(none)", ", ".join(sorted(names))), path, 1))

    found = False
    for path, src, tree in trees:
        found |= _cpp_walk(pkg, path, src, tree, aliases, named, resolver, report_root,
                           trees)
    return found


def _cpp_walk(pkg, path, src, tree, aliases, named, resolver, report_root, trees):
    found = False
    stack = [tree.root_node]
    while stack:
        cur = stack.pop()
        stack.extend(cur.children)
        if cur.type != "call_expression":
            continue
        fname, targs = cpp_callee(src, cur)
        if fname is None:
            continue
        ns = cpp_call_namespace(src, cur)
        args = cpp_args(cur)
        line = cur.start_point[0] + 1
        snippet = " ".join(ts_text(src, cur).split())[:140]
        owner = _cpp_enclosing_class(src, cur)
        target = _cpp_target(pkg, owner, named)
        before = len(pkg.flags)

        if fname in CPP_METHOD_BLOCK and not ns:
            block = CPP_METHOD_BLOCK[fname]
            found |= _cpp_record_iface(pkg, target, block, targs, args[0] if args else None,
                                       _cpp_qos(src, args, fname), src, path, line,
                                       snippet, aliases, owner, trees)
        elif ns == "rclcpp_action" and fname in CPP_ACTION_BLOCK:
            name_arg = _cpp_first_string_arg(src, args)
            found |= _cpp_record_iface(pkg, target, CPP_ACTION_BLOCK[fname], targs,
                                       name_arg, None, src, path, line, snippet, aliases,
                                       owner, trees)
        elif fname in ("declare_parameter", "auto_declare"):
            found |= _cpp_record_param(pkg, target, targs, args, src, path, line, snippet)
        elif fname == "declare_parameters":
            pkg.flags.append(Flag(
                "parameter",
                "declare_parameters() map form is not extracted deterministically; "
                "read the map by hand", path, line, snippet))
        for flag in pkg.flags[before:]:
            flag.node = target
    return found


def _cpp_target(pkg, owner, named):
    if owner and owner in named:
        return pkg.node_for(named[owner], True)
    if len(named) == 1:
        return pkg.node_for(next(iter(named.values())), True)
    return pkg.fallback_node()


def _cpp_first_string_arg(src, args):
    for a in args:
        if cpp_string_literal(src, a) is not None:
            return a
    for a in args:
        if ts_find(a, "string_literal", depth=4) is not None:
            return a  # dynamic; _cpp_record_iface will flag it
    return None


def _cpp_qos(src, args, fname):
    idx = {"create_publisher": 1, "create_subscription": 1}.get(fname)
    if idx is None or idx >= len(args):
        return None
    node = args[idx]
    if node.type == "number_literal":
        try:
            return int(ts_text(src, node))
        except ValueError:
            return None
    # rclcpp::QoS(10)
    if node.type == "call_expression" and "QoS" in ts_text(src, node):
        num = ts_find(node, "number_literal", depth=3)
        if num is not None:
            try:
                return int(ts_text(src, num))
            except ValueError:
                return None
    return None


def _cpp_record_iface(pkg, target, block, targs, name_arg, qos, src, path, line,
                      snippet, aliases, owner=None, trees=()):
    if not targs:
        pkg.flags.append(Flag(block, "call has no explicit template type argument",
                              path, line, snippet))
        return False
    ref = cpp_type_ref(targs[0], aliases)
    if name_arg is None:
        # Either the call really has no name argument, or the one it has is a variable or a
        # struct member rather than a string literal. The old wording asserted the first, which
        # sent a reader looking for the wrong thing on `create_client<T>(node, cfg.action_name)`.
        pkg.flags.append(Flag(
            block, "no name argument this parser can read: the call either has none, or passes "
                   "a variable/member rather than a string literal; resolve by hand",
            path, line, snippet))
        return False
    name = cpp_string_literal(src, name_arg)
    if name is None:
        reason = ("name is built at runtime (string concatenation, get_name(), or a "
                  "variable), not a literal; resolve by hand")
        evidence = cpp_name_evidence(src, name_arg, owner, trees)
        if evidence:
            reason += (". Built from %r, which %d construction site(s) of %s visible in "
                       "this package pass as: %s. CANDIDATES (evidence, NOT emitted -- "
                       "this parser cannot see construction sites outside the package, so "
                       "it cannot claim these are all of them): %s"
                       % (evidence["variable"], evidence["sites"], owner,
                          ", ".join(repr(v) for v in evidence["values"]),
                          ", ".join(evidence["candidates"])))
            if evidence["incomplete"]:
                reason += (". At least one construction site passes a non-literal, so the "
                           "list above is definitely incomplete")
        pkg.flags.append(Flag(block, reason, path, line, snippet))
        return False
    if ref is None:
        pkg.flags.append(Flag(
            block, "'%s' has template type %s, which does not resolve to a "
                   "pkg/kind/Type reference" % (name, targs[0]), path, line, snippet))
        return False
    iface = Interface(block, name, ref, qos, path, line)
    iface.raw_type = targs[0]
    target.add_interface(iface)
    return True


def _cpp_record_param(pkg, target, targs, args, src, path, line, snippet):
    if not args:
        return False
    name = cpp_string_literal(src, args[0])
    if name is None:
        pkg.flags.append(Flag("parameter", "parameter name is not a string literal",
                              path, line, snippet))
        return False
    dsl_type, dsl_val = cpp_param_type(targs, args, src)
    if dsl_type is None:
        pkg.flags.append(Flag(
            "parameter",
            "'%s' declared, but its type/default is not a literal this script can "
            "read (%s)" % (name, "no default" if len(args) < 2 else "non-literal default"),
            path, line, snippet))
        return False
    note = None
    if dsl_val is None:
        # The type is known; only the default has no legal form. Keep the declaration --
        # dropping it would lose a real parameter, and a .rossystem override needs it.
        note = (NOTE_EMPTY_LIST if dsl_type.startswith("Array[")
                else "source default is not a literal this script can read")
    target.add_param(Param(name, dsl_type, dsl_val, path, line, note))
    return True


# ------------------------------------------------------------------------- emission


def sanitize_package_name(name):
    """SKILL.md rule 2: the .ros2 root package name is [a-z0-9_] or it is an ERROR."""
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in name).lower()
    if not out or not (out[0].isalpha() or out[0] == "_"):
        out = "pkg_" + out
    return out


def sanitize_node_name(name):
    """SKILL.md rule 6: `node:` is RosNames -- never quoted, no '.', '-', '::', space."""
    return "".join(c if (c.isalnum() or c in "_/") else "_" for c in name)


def emit_package(pkg, resolver, report_root, emit_qos):
    lines = []
    rel = os.path.relpath(pkg.path, report_root) if report_root else pkg.path
    lines.append("# GENERATED DRAFT -- scripts/extract_ros2_interfaces.py")
    lines.append("# Source package: %s" % rel.replace(os.sep, "/"))
    lines.append("#   package.xml declares: %s%s" % (
        pkg.name, "  (build_type: %s)" % pkg.build_type if pkg.build_type else ""))
    lines.append("#   parsed as: %s" % (", ".join(sorted(pkg.languages)) or "none"))
    lines.append("#")
    lines.append("# Only declarations whose name AND type are literal in the source are")
    lines.append("# emitted. Everything the extractor could not read literally is listed")
    lines.append("# below as a FLAG for manual or LLM review -- it is NOT missing, it is")
    lines.append("# deliberately not guessed.")
    for note in pkg.notes:
        lines.append("#")
        lines.append(wrap_comment(note))
    # A durable record of how many items this extractor could not read. Resolving one
    # deletes its comment, so the live comment count alone cannot distinguish a finished
    # model from one whose flags were rewritten away. This line does not change. RM097
    # reports it alongside the live count.
    lines.append("# EXTRACTOR-FLAGS: %d" % len(pkg.flags))
    if pkg.flags:
        lines.append("#")
        for flag in sorted(pkg.flags, key=lambda f: (f.file, f.line)):
            lines.append(flag.as_comment(report_root))
    else:
        lines.append("#")
        lines.append("# (none -- every ROS call found in this package was literal.)")

    safe_pkg = sanitize_package_name(pkg.name)
    if safe_pkg != pkg.name:
        lines.append(wrap_comment(
            "NOTE: declared package name %r is not [a-z0-9_]; emitted as %r "
            "(SKILL.md rule 2)." % (pkg.name, safe_pkg)))
    lines.append("%s:" % safe_pkg)
    lines.append("  artifacts:")

    nodes = [n for n in pkg.nodes.values() if n.interfaces or n.params]
    nodes.extend(pkg.fallbacks)
    for node in sorted(nodes, key=lambda n: artifact_name(n, pkg.targets)):
        lines.extend(emit_node(node, resolver, report_root, emit_qos, pkg.targets))
    return "\n".join(lines) + "\n"


def resolve_fallback_artifacts(pkg):
    """Split the "no node class" bucket per BUILD TARGET, and name each from the source.

    Declarations that sit in no node class -- a call in main(), or on an injected
    get_node() handle -- were all collected in one bucket per package. Two things went
    wrong with that:

      * `add_executable()` names an executable, and three of them are three processes with
        three separate sets of interfaces. One bucket produced one artifact asserting that
        one executable serves all of them -- a FALSE claim, presented as fact, with no flag
        on it. Real case: `demo_node` carrying interfaces read from `demo_node.cpp`,
        `demo_node_pilz_ompl.cpp` and `demo_node_structured.cpp`, three different binaries.
      * `rclcpp::Node::make_shared("ssi_demo_node")` gives that bucket a real node name, and
        with one bucket per package there was nowhere to put a second one.

    Split ONLY when the declarations really do come from two or more different build
    targets. A package with one target, or with none the CMakeLists names, keeps exactly
    the shape it had -- which is every package this pair of scripts was developed against.

    Declarations from a file that belongs to no build target (a header, a
    generate_parameter_library YAML whose CMake target is a *_parameters helper rather than
    the library it feeds) stay with the single bucket when there is one. When there are
    several they are genuinely unattributable, and get their own bucket under the package
    name rather than being folded into whichever target sorts first.
    """
    bucket = pkg.fallback
    pkg.fallbacks = []
    if bucket is None or not (bucket.interfaces or bucket.params):
        return

    def owner(path):
        return pkg.targets.get(os.path.abspath(path))

    groups = OrderedDict()        # build target (or None) -> [("i"|"p", declaration)]
    for iface in bucket.interfaces.values():
        groups.setdefault(owner(iface.file), []).append(("i", iface))
    for param in bucket.params.values():
        groups.setdefault(owner(param.file), []).append(("p", param))

    targeted = [k for k in groups if k is not None]
    if len(targeted) < 2:
        name = pkg.target_node_names.get(targeted[0] if targeted else None)
        if name:
            bucket.name, bucket.confident = name, True
        pkg.fallbacks = [bucket]
        return

    for key, items in groups.items():
        name = pkg.target_node_names.get(key)
        draft = NodeDraft(name or pkg.name, bool(name))
        for kind, item in items:
            if kind == "i":
                draft.add_interface(item)
            else:
                draft.add_param(item)
        pkg.fallbacks.append(draft)


def artifact_name(node, targets):
    """The build target that owns this node's source, else the node name."""
    for path in sorted(node.files):
        hit = targets.get(os.path.abspath(path))
        if hit:
            return sanitize_node_name(hit)
    return sanitize_node_name(node.name)


def flag_artifact(flag, nodes, targets):
    """The emitted artifact a flag belongs to: the node whose call raised it, else the one
    node whose declarations come from the flag's file. None when that is not decidable. The
    package-named fallback collects calls from unrelated files, so it needs the file match."""
    owners = [n for n in nodes if os.path.abspath(flag.file) in {os.path.abspath(f) for f in n.files}]
    if flag.node in nodes and (flag.node.confident or flag.node in owners):
        return artifact_name(flag.node, targets)
    return artifact_name(owners[0], targets) if len(owners) == 1 else None


def emit_node(node, resolver, report_root, emit_qos, targets):
    artifact = artifact_name(node, targets)
    lines = ["    %s:" % artifact, "      node: %s" % sanitize_node_name(node.name)]
    for block in BLOCK_ORDER:
        entries = sorted((i for i in node.interfaces.values() if i.block == block),
                         key=lambda i: i.name)
        if not entries:
            continue
        lines.append("      %s:" % block)
        for iface in entries:
            status, comment = resolver.resolve(iface.type_ref, report_root)
            lines.append("        '%s':" % iface.name)
            lines.append("          type: '%s' %s" % (iface.type_ref, comment))
            if emit_qos and iface.qos_depth is not None:
                lines.append("          qos:")
                lines.append("            depth: %d" % iface.qos_depth)
    if node.params:
        lines.append("      parameters:")
        for name in sorted(node.params):
            p = node.params[name]
            lines.append("        '%s':" % p.name)
            lines.append("          type: %s%s"
                         % (p.dsl_type, "  # " + p.note if p.note else ""))
            if p.value is not None:
                # sibling of type:, immediately after it (SKILL.md rule 9)
                lines.append("          default: %s" % p.value)
    return lines


# ------------------------------------------------------------------------------ main


def run_linter(files):
    if not os.path.isfile(LINTER):
        return None
    try:
        proc = subprocess.run(
            [sys.executable, LINTER, "--json"] + files,
            capture_output=True, text=True, timeout=180)
        return json.loads(proc.stdout or "{}")
    except Exception as exc:
        print("  linter did not run: %s" % exc, file=sys.stderr)
        return None


# generate_parameter_library type keyword -> DSL ParameterType
GPL_TYPES = {
    "string": "String", "double": "Double", "int": "Integer", "bool": "Boolean",
    "string_array": "Array[String]", "double_array": "Array[Double]",
    "int_array": "Array[Integer]", "bool_array": "Array[Boolean]",
    "string_fixed_array": "Array[String]", "double_fixed_array": "Array[Double]",
    "int_fixed_array": "Array[Integer]", "bool_fixed_array": "Array[Boolean]",
}


_BOOL_WORDS = {"true": "true", "false": "false", "1": "true", "0": "false"}


def gpl_value(dsl_type, value):
    """A generate_parameter_library default_value -> a DSL literal for `dsl_type`."""
    if value is None:
        return None
    if dsl_type.startswith("Array["):
        if not isinstance(value, (list, tuple)):
            return None
        if not value:
            return None      # see py_param_dsl: omit rather than write a quoted "[]"
        elem = dsl_type[6:-1]
        parts = [gpl_value(elem, v) for v in value]
        return None if any(p is None for p in parts) else "[%s]" % ", ".join(parts)
    if dsl_type == "Boolean":
        # A YAML default_value is usually a real bool, but it can be the string "false" --
        # and a non-empty string is truthy, which would silently invert the parameter.
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.strip().lower() in _BOOL_WORDS:
            return _BOOL_WORDS[value.strip().lower()]
        return None
    if dsl_type == "Integer":
        # int(3.7) == 3 silently truncates; only an exact integer is an Integer.
        if isinstance(value, bool):
            return None
        if isinstance(value, float):
            # 3.0 is an exact integer; 3.7 would truncate silently.
            return str(int(value)) if value.is_integer() else None
        try:
            return str(int(str(value).strip()))
        except (TypeError, ValueError):
            return None
    if dsl_type == "Double":
        if isinstance(value, bool):
            return None
        try:
            text = repr(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
        return text if ("." in text or "e" in text or "E" in text) else text + ".0"
    if dsl_type == "String":
        if isinstance(value, (list, tuple, dict)):
            return None
        return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')
    return None


def read_gpl_yaml(pkg, path, target):
    """generate_parameter_library YAML -> parameters on `target`. The schema is
    `<root>: <name>: {type:, default_value:, ...}`, with nested maps as groups."""
    try:
        import yaml
    except ImportError:
        pkg.notes.append("NOTE: PyYAML is not installed, so this package's "
                         "generate_parameter_library declarations were not read.")
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = yaml.safe_load(fh)
    except Exception as exc:
        pkg.flags.append(Flag("parameter", "could not read the generate_parameter_library "
                                           "YAML: %s" % exc, path, 1))
        return
    if not isinstance(doc, dict) or len(doc) != 1:
        pkg.flags.append(Flag("parameter", "generate_parameter_library YAML does not have "
                                           "a single root key", path, 1))
        return

    def walk(node, prefix):
        for name, spec in sorted(node.items()):
            if not isinstance(spec, dict):
                continue
            full = prefix + name
            if name.startswith("__map_"):
                pkg.flags.append(Flag(
                    "parameter",
                    "'%s' is a generate_parameter_library dynamic map (%s): its real "
                    "parameter names are built at runtime from another parameter's value, "
                    "so they cannot be enumerated from source"
                    % (prefix.rstrip("."), name), path, 1))
                continue
            if "type" not in spec:
                walk(spec, full + ".")
                continue
            dsl_type = GPL_TYPES.get(str(spec.get("type")))
            if dsl_type is None:
                pkg.flags.append(Flag(
                    "parameter", "'%s' has generate_parameter_library type %r, which has "
                                 "no DSL equivalent" % (full, spec.get("type")), path, 1))
                continue
            raw = spec.get("default_value")
            value = gpl_value(dsl_type, raw)
            note = None
            if value is None and raw is not None:
                if isinstance(raw, (list, tuple)) and not raw:
                    note = NOTE_EMPTY_LIST
                else:
                    note = "source default %r has no legal %s literal" % (raw, dsl_type)
                    pkg.flags.append(Flag(
                        "parameter", "'%s' has default_value %r, which has no legal %s "
                                     "literal; it is declared without a default"
                                     % (full, raw, dsl_type), path, 1))
            target.add_param(Param(full, dsl_type, value, path, 1, note))

    walk(doc[next(iter(doc))], "")


def package_notes(pkg):
    """Read what the source declares outside declare_parameter(): a
    generate_parameter_library YAML is a real, literal declaration source."""
    cmake = os.path.join(pkg.path, "CMakeLists.txt")
    if not os.path.isfile(cmake):
        return
    try:
        with open(cmake, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return
    if "generate_parameter_library" not in text:
        return

    # generate_parameter_library(<target> <path/to/params.yaml> [<validators.hpp>])
    yamls = []
    for chunk in text.split("generate_parameter_library")[1:]:
        head = chunk.partition(")")[0]
        for token in head.replace("(", " ").split():
            token = token.strip('"\'')
            if token.endswith((".yaml", ".yml")):
                candidate = os.path.join(pkg.path, token)
                if os.path.isfile(candidate):
                    yamls.append(candidate)
                break
    if not yamls:
        pkg.notes.append(
            "NOTE: this package uses generate_parameter_library, but the YAML named in "
            "CMakeLists.txt's generate_parameter_library(...) call was not found on disk, "
            "so its parameters are NOT in the parameters: block below.")
        return

    target = pkg.fallback_node() if not pkg.nodes else next(iter(pkg.nodes.values()))
    for path in yamls:
        read_gpl_yaml(pkg, path, target)
    pkg.notes.append(
        "NOTE: the parameters below come from generate_parameter_library, not from "
        "declare_parameter() calls -- read from %s. Their values are that file's "
        "default_value entries; deployment overrides (a controllers.yaml, a launch file) "
        "are NOT here and belong in the .rossystem."
        % ", ".join(os.path.relpath(p, pkg.path).replace(os.sep, "/") for p in yamls))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Extract draft .ros2 node-interface models from ROS 2 source.")
    ap.add_argument("paths", nargs="+", help="ROS 2 package or directory of packages")
    ap.add_argument("-o", "--out", default="ros_model_draft", help="output directory")
    ap.add_argument("--json", dest="json_out", help="write the full extraction record here")
    ap.add_argument("--emit-qos", action="store_true",
                    help="emit qos: depth: for literal depths (off by default; RM034)")
    ap.add_argument("--no-lint", action="store_true", help="skip rosmodel_lint.py")
    ap.add_argument("--emit-msgs", metavar="DIR",
                    help="also write a companion .ros per project-local message package "
                         "that the generated models reference (transcribed from the "
                         "package's own .msg/.srv/.action files). Without this, a model "
                         "referencing project-local types is not loadable on its own.")
    ap.add_argument("--package-root", action="append", default=[],
                    help="extra root to search for project-local .msg/.srv/.action")
    args = ap.parse_args(argv)

    pkg_dirs = find_packages(args.paths)
    if not pkg_dirs:
        print("no package.xml found under: %s" % ", ".join(args.paths), file=sys.stderr)
        return 2

    report_root = os.path.commonpath([os.path.abspath(p) for p in args.paths]) \
        if len(args.paths) > 1 else os.path.abspath(args.paths[0])
    if os.path.isfile(os.path.join(report_root, "package.xml")):
        report_root = os.path.dirname(report_root)

    resolver = TypeResolver([os.path.abspath(p) for p in args.paths] +
                            [os.path.abspath(p) for p in args.package_root])
    cpp_parser = _load_cpp_parser()
    cpp_warned = False
    skipped_cpp = []

    os.makedirs(args.out, exist_ok=True)
    written, record = [], []

    for pkg_dir in sorted(pkg_dirs):
        name, build_type = read_package_xml(pkg_dir)
        pkg = PackageDraft(name, pkg_dir, build_type)
        pkg.targets = read_build_targets(pkg_dir)

        files = [f for f in source_files(pkg_dir) if not is_launch_or_setup(f)]
        py_files = [f for f in files if os.path.splitext(f)[1].lower() in PY_EXT]
        cpp_files = [f for f in files if os.path.splitext(f)[1].lower() in CPP_EXT]

        got = False
        for f in py_files:
            got |= extract_python(pkg, f, resolver, report_root)
        if cpp_files:
            if cpp_parser is None:
                skipped_cpp.append(pkg.name)
                if not cpp_warned:
                    print("tree_sitter / tree_sitter_cpp not installed -- C++ packages "
                          "are skipped. pip install tree_sitter tree_sitter_cpp",
                          file=sys.stderr)
                    cpp_warned = True
            else:
                got |= extract_cpp(pkg, cpp_files, cpp_parser, resolver, report_root)

        # after extraction: the generate_parameter_library reader needs to know which
        # node the package's calls landed on before it attaches parameters to one.
        before = sum(len(n.params) for n in pkg.nodes.values()) + (
            len(pkg.fallback.params) if pkg.fallback else 0)
        package_notes(pkg)
        after = sum(len(n.params) for n in pkg.nodes.values()) + (
            len(pkg.fallback.params) if pkg.fallback else 0)
        got |= after > before

        if not got and not pkg.flags:
            continue
        # Everything is collected; decide now how many artifacts the bucket really is.
        resolve_fallback_artifacts(pkg)
        if any(not n.confident for n in pkg.fallbacks):
            pkg.notes.append(
                "NOTE: no node name was found as a string literal in this package's "
                "source (its ROS calls sit on a get_node() handle, an injected node, or "
                "a plugin base class), so the artifact and node: below are named after "
                "the PACKAGE. The real runtime node name comes from the launch file or "
                "the controller_manager config -- confirm it before using this model.")

        nodes = [n for n in pkg.nodes.values() if n.interfaces or n.params]
        nodes.extend(pkg.fallbacks)

        out_path = None
        if nodes:
            out_path = os.path.join(args.out, "%s.ros2" % sanitize_package_name(pkg.name))
            with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(emit_package(pkg, resolver, report_root, args.emit_qos))
            written.append(out_path)
        else:
            # Every ROS call in this package was flagged: there is nothing the DSL can
            # legally hold (an artifact with no interfaces is not a useful model), so no
            # file is written -- but the flags must still reach the caller.
            print("  %s: no literal declarations; %d item(s) flagged, see --json"
                  % (pkg.name, len(pkg.flags)), file=sys.stderr)

        record.append({
            "package": pkg.name,
            "path": os.path.relpath(pkg.path, report_root),
            "build_type": pkg.build_type,
            "languages": sorted(pkg.languages),
            "file": os.path.relpath(out_path, os.getcwd()) if out_path else None,
            "nodes": [{
                "node": n.name,
                "artifact": artifact_name(n, pkg.targets),
                "node_name_from_source_literal": n.confident,
                "interfaces": [{
                    "block": i.block, "name": i.name, "type": i.type_ref,
                    "qos_depth": i.qos_depth,
                    "resolution": resolver.resolve(i.type_ref, report_root)[0],
                    "at": "%s:%d" % (os.path.relpath(i.file, report_root), i.line),
                } for i in n.interfaces.values()],
                "parameters": [{
                    "name": p.name, "type": p.dsl_type, "value": p.value,
                    "at": "%s:%d" % (os.path.relpath(p.file, report_root), p.line),
                } for p in n.params.values()],
            } for n in nodes],
            "flags": [{
                "kind": f.kind, "reason": f.reason,
                "at": "%s:%d" % (os.path.relpath(f.file, report_root), f.line),
                "source": f.snippet,
                "artifact": flag_artifact(f, nodes, pkg.targets),
            } for f in pkg.flags],
        })

    # Every scanned package's own .msg/.srv/.action, used by a node in this run or not: a
    # message-only package has no nodes, and its types were otherwise never written. A type the
    # vendored catalogue already holds is listed, not redefined (resolve() prefers the catalogue).
    scanned = {os.path.abspath(d) for d in pkg_dirs}
    interface_pkgs = {}
    for ref, path in sorted(resolver.local.items()):
        pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(path)))
        if pkg_dir in scanned:
            entry = interface_pkgs.setdefault(ref.split("/", 1)[0], {
                "path": os.path.relpath(pkg_dir, report_root), "defined": [], "catalogue": []})
            entry["defined"].append(ref)
            if ref in resolver.catalogue:
                entry["catalogue"].append(ref)

    if args.emit_msgs:
        referenced = {i["type"] for r in record for n in r["nodes"]
                      for i in n["interfaces"] if i["resolution"] == "local"}
        referenced |= {ref for e in interface_pkgs.values() for ref in e["defined"]
                       if ref not in resolver.catalogue}
        msg_files, msg_flags = emit_companion_msgs(
            resolver, args.emit_msgs, report_root, referenced)
        written.extend(msg_files)
        for flag in msg_flags:
            print("  companion .ros: %s (%s:%d)"
                  % (flag.reason, os.path.relpath(flag.file, report_root), flag.line),
                  file=sys.stderr)
        if msg_files:
            print("  %d companion .ros file(s) in %s" % (len(msg_files), args.emit_msgs))

    emitted = sum(len(n["interfaces"]) for r in record for n in r["nodes"])
    params = sum(len(n["parameters"]) for r in record for n in r["nodes"])
    flagged = sum(len(r["flags"]) for r in record)
    print("%d package(s) -> %d file(s) in %s" % (len(record), len(written), args.out))
    print("  %d interface(s), %d parameter(s) emitted; %d item(s) flagged for review"
          % (emitted, params, flagged))
    for name, e in sorted(interface_pkgs.items()):
        print("  %s defines %d type(s); %d already in the vendored catalogue (not redefined)"
              % (name, len(e["defined"]), len(e["catalogue"])))
    if skipped_cpp:
        # Loud, and in the primary output rather than only on stderr: a caller who reads
        # just the summary must not mistake a degraded run for a complete one.
        print("  INCOMPLETE: the C++ source of %d package(s) was NOT read (tree_sitter / "
              "tree_sitter_cpp missing), so their interfaces are absent and any model "
              "emitted for them is partial: %s"
              % (len(skipped_cpp), ", ".join(sorted(skipped_cpp))))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"packages": record, "interface_packages": interface_pkgs}, fh,
                      indent=2, sort_keys=True)
            fh.write("\n")
        print("  record: %s" % args.json_out)

    status = 0
    if written and not args.no_lint:
        result = run_linter(written)
        if result:
            errors = 0
            for finding in result.get("findings", []):
                if finding.get("severity") == "ERROR":
                    errors += 1
            counts = result.get("summary") or {}
            print("  rosmodel_lint: %s" % json.dumps(counts) if counts
                  else "  rosmodel_lint: %d ERROR(s)" % errors)
            if errors:
                status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
