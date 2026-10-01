#!/usr/bin/env python3
"""
rosmodel_lint.py -- static linter for RosTooling .ros, .ros2 and .rossystem model files.

Work item E1, CoreSense x Humanoid (Fraunhofer IPA).

Checks a model file against three authorities, in this order of precedence:

  1. docs/grammar-subset.md     -- the PINNED oracle vocabulary (ros2 LS JAR 3.0.0.202408011124).
                                  Anything outside it is a hard parse error against the oracle.
  2. research/validator-rules.md -- RosValidator.xtend / RosSystemValidator.xtend constraints,
                                  with their real ERROR / WARNING severities.
  3. emission-profile.md        -- the normative house formatting style.

Every finding carries a stable rule id (RMnnn). See scripts/README.md for the id table and the
cross-reference to the validator method each rule mirrors.

Dependencies: Python 3.8+, standard library, and PyYAML (optional -- without it the linter still
runs all byte/layout checks and reports the structural checks as skipped).

Usage:
    python rosmodel_lint.py <file>...
    python rosmodel_lint.py --json <file>...
    python rosmodel_lint.py --min-severity WARNING <file>...

A .ros file is NOT parsed as YAML. Its message bodies are token streams -- `float32 x` under an
un-colonned `Pose` -- which PyYAML folds into one multi-line plain scalar, silently losing every
field. The .ros branch therefore runs its own indentation-stack parser (`parse_ros_indent`),
modelled directly on Xtext's AbstractIndentationTokenSource: any increase in indent opens a
BEGIN, any decrease closes ENDs. Absolute column values are never assumed -- the corpus uses
3/4/5/6/7/9-space steps and the real parser accepts all of them.

Exit status: 1 if any ERROR-severity finding was produced, else 0.
"""

import argparse
import difflib
import glob
import io
import json
import os
import re
import sys

try:
    import yaml
    HAVE_YAML = True
    YAML_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover - environment dependent
    yaml = None
    HAVE_YAML = False
    YAML_IMPORT_ERROR = str(exc)


# --------------------------------------------------------------------------------------
# Severities
# --------------------------------------------------------------------------------------

ERROR = "ERROR"
WARNING = "WARNING"
INFO = "INFO"

SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


# --------------------------------------------------------------------------------------
# Catalogue indexes (assets/type_index.json, assets/node_index.json) -- built by
# build_type_index.py / build_node_index.py from the vendored assets/roscommonobjects/
# and assets/rosmodelscatalog/. Loaded lazily and cached at module level; missing files
# degrade to "catalogue checks produce nothing" rather than an error, so a stripped-down
# install without assets/ still runs every other rule normally.
# --------------------------------------------------------------------------------------

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "assets")
_TYPE_INDEX_PATH = os.path.join(_ASSETS_DIR, "type_index.json")
_NODE_INDEX_PATH = os.path.join(_ASSETS_DIR, "node_index.json")
_type_index_cache = {"loaded": False, "value": None}
_node_index_cache = {"loaded": False, "value": None}


def load_type_index():
    if not _type_index_cache["loaded"]:
        _type_index_cache["loaded"] = True
        try:
            with open(_TYPE_INDEX_PATH, encoding="utf-8") as f:
                _type_index_cache["value"] = json.load(f).get("types")
        except (IOError, OSError, ValueError):
            _type_index_cache["value"] = None
    return _type_index_cache["value"]


def load_node_index():
    if not _node_index_cache["loaded"]:
        _node_index_cache["loaded"] = True
        try:
            with open(_NODE_INDEX_PATH, encoding="utf-8") as f:
                _node_index_cache["value"] = json.load(f).get("nodes")
        except (IOError, OSError, ValueError):
            _node_index_cache["value"] = None
    return _node_index_cache["value"]


_system_index_cache = {"loaded": False, "value": None}


def load_system_index():
    """{"<system name or file basename>": {"file", "nodes": {label: {"from", "interfaces"}},
    "connections": [[fromLabel, toLabel], ...], "hasOwnSubsystems"}}, built lazily from
    node_index.json's "_systems" list
    (scripts/build_node_index.py). A system entry whose source file didn't parse as a
    mapping (or predates the richer index) has no "system" key and is skipped -- callers
    see it as simply unresolved, same as a system name that was never catalogued at all.

    Keyed by both the system's declared name AND its file's basename (without extension),
    matching how corpus 'subSystems:' references are sometimes written -- ros_plot.py's
    link_subsystems() does the same dual lookup for its own (unrelated) purpose. The
    declared name always wins a collision: it is registered first and basename aliases
    use setdefault, so a basename never shadows a real system name."""
    if not _system_index_cache["loaded"]:
        _system_index_cache["loaded"] = True
        try:
            with open(_NODE_INDEX_PATH, encoding="utf-8") as f:
                raw_systems = json.load(f).get("_systems") or []
            by_name = {}
            for entry in raw_systems:
                name = entry.get("system")
                if not name:
                    continue
                record = {
                    "file": entry.get("file"),
                    "nodes": entry.get("nodes") or {},
                    # the referenced system's OWN connections. Nothing in linting or emission
                    # reads them -- checkIfInterfaceInSystem looks only at `interfaces:` -- but
                    # the studio's subsystem views cannot draw the inside of a subsystem
                    # without them. Absent on an index built before they were recorded.
                    "connections": entry.get("connections") or [],
                    "hasOwnSubsystems": bool(entry.get("hasOwnSubsystems")),
                }
                by_name.setdefault(name, record)  # first file wins, same policy as nodes
                base = os.path.splitext(os.path.basename(entry.get("file") or ""))[0]
                if base:
                    by_name.setdefault(base, record)
            _system_index_cache["value"] = by_name
        except (IOError, OSError, ValueError):
            _system_index_cache["value"] = None
    return _system_index_cache["value"]


_bni_cache = {"loaded": False, "value": None}
_local_system_cache = {}
_local_nodes_cache = {}


def _build_node_index_module():
    """scripts/build_node_index.py, loaded BY PATH. It does `from rosmodel_lint import ...`, so
    a top-level import here would be a cycle, and by-name would need scripts/ already on
    sys.path. Returns None if it cannot be loaded; every caller degrades to "unresolved"."""
    if not _bni_cache["loaded"]:
        _bni_cache["loaded"] = True
        try:
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "build_node_index.py")
            spec = importlib.util.spec_from_file_location("_rosmodel_build_node_index", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _bni_cache["value"] = module
        except Exception:
            _bni_cache["value"] = None
    return _bni_cache["value"]


def load_local_nodes(base_dir):
    """'<package>.<node>' for every node a .ros2 file NEXT TO the linted file declares -- a
    project's own extracted models, staged beside the .rossystem that uses them. Same walk the
    catalogue is built with (build_node_index.extract_ros2_nodes)."""
    key = os.path.abspath(base_dir or ".")
    if key not in _local_nodes_cache:
        found = set()
        bni = _build_node_index_module()
        if bni is not None and HAVE_YAML:
            for path in sorted(glob.glob(os.path.join(key, "*.ros2"))):
                try:
                    found |= {"%s.%s" % (pkg, node)
                              for pkg, _art, node, _ifaces in bni.extract_ros2_nodes(bni.compose_yaml(path))}
                except Exception:
                    continue
        _local_nodes_cache[key] = found
    return _local_nodes_cache[key]


def load_local_system(base_dir, ref):
    """A `subSystems:` reference resolved against a .rossystem sitting NEXT TO the file being
    linted, in the same record shape load_system_index() returns (plus "local": True).

    Consulted only when the vendored catalogue has never heard of the name -- i.e. an ordinary
    project-local system. Without it, every connections: endpoint such a subsystem provides
    raises RM050 ("not a declared interface of system X"), which is this plugin's blind spot and
    not a defect in the model: the real validator's scoping walks the Xtext resource set, not
    assets/node_index.json. RM091 still fires, because "resolved by sitting next to you" is
    weaker evidence than "catalogued", and the author should know which one they got.

    The walk itself is build_node_index.extract_rossystem_system, so the code that BUILT the
    catalogue and the code that reads a local file cannot disagree about what a subsystem
    exposes (both read the referenced file's OWN interfaces: block, per checkIfInterfaceInSystem
    -- RosSystemValidator.xtend:87-109)."""
    key = (os.path.abspath(base_dir or "."), ref)
    if key in _local_system_cache:
        return _local_system_cache[key]
    _local_system_cache[key] = None
    bni = _build_node_index_module()
    if bni is None or not HAVE_YAML:
        return None
    for cand in (ref, os.path.splitext(os.path.basename(ref))[0]):
        path = os.path.join(base_dir or ".", cand + ".rossystem")
        if not os.path.isfile(path):
            continue
        try:
            root = bni.compose_yaml(path)
            if root is None:
                break
            # 4-tuple since connections were added to the index. The `except` below is broad
            # enough to swallow a TypeError from getting this wrong, and did: every RM050 on a
            # project-local subsystem came back, silently, because a resolution failure and a
            # crash are indistinguishable to the caller.
            _name, nodes, nested, conns = bni.extract_rossystem_system(root)
        except Exception:
            break
        if nodes is None:
            break
        _local_system_cache[key] = {"file": os.path.basename(path), "nodes": nodes,
                                    "connections": conns or [],
                                    "hasOwnSubsystems": bool(nested), "local": True}
        break
    return _local_system_cache[key]


class Finding(object):
    __slots__ = ("file", "line", "severity", "rule", "message", "hint")

    def __init__(self, file, line, severity, rule, message, hint=""):
        self.file = file
        self.line = line
        self.severity = severity
        self.rule = rule
        self.message = message
        self.hint = hint

    def as_dict(self):
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "hint": self.hint,
        }

    def sort_key(self):
        return (self.file, self.line, SEVERITY_ORDER.get(self.severity, 9), self.rule)


# --------------------------------------------------------------------------------------
# Grammar-derived vocabularies (see docs/grammar-subset.md)
# --------------------------------------------------------------------------------------

# Xtext common.Terminals ID; note it may not start with a digit.
RE_ID = re.compile(r"^\^?[A-Za-z_][A-Za-z_0-9]*$")

# Basics.xtext:  terminal ROS_CONVENTION_A: ( ('/' ID) | (ID '/') )* ;
RE_ROS_CONVENTION_A = re.compile(
    r"^(?:/[A-Za-z_][A-Za-z_0-9]*|[A-Za-z_][A-Za-z_0-9]*/)*$"
)

# Node blocks, in the emission profile's canonical order (rule 24 = grammar declaration order).
ROS2_NODE_BLOCKS = [
    "publishers",
    "subscribers",
    "serviceservers",
    "serviceclients",
    "actionservers",
    "actionclients",
    "parameters",
]

# Ros2.xtext AmentPackage members, in grammar-fixed order (rule 21).
AMENT_PACKAGE_KEYS = ["fromGitRepo", "artifacts", "dependencies"]

# grammar-subset.md sec 5.2: present in the token file but UNREACHABLE from AmentPackage.
ROS2_FORBIDDEN_SPEC_BLOCKS = ["msgs", "srvs", "actions"]

# Added to the grammar by commit 3d9e5ebd (2025-10-16). These were EXCLUDED while our oracle was
# the 2024-08-01 JAR, which predates them and cannot lex them. That pin was lifted 2026-07-21 when
# we rebuilt the language server from ipa-esa/RosTooling@esa/main (3.1.0-SNAPSHOT) -- verified by
# A/B on oracle case 04: legacy JAR REJECTED ("mismatched input 'lease_duration'"), current
# JAR ACCEPTED. They are now legal to emit; RM031 survives only as an INFO, because a *consumer*
# on an older toolchain build still cannot parse them.
QOS_NEWER = ["lease_duration", "liveliness", "lifespan", "deadline"]

# Sub-fields accepted by a current toolchain. Formerly QOS_PINNED (five); the four above were
# folded in when the pin was lifted.
QOS_PINNED = ["profile", "history", "depth", "reliability", "durability"] + QOS_NEWER

# emission-profile rule 33: JAR-supported but Corpus B suppresses them.
QOS_DISCOURAGED = ["profile", "history", "depth"]

QOS_ENUMS = {
    # `('liveliness:' Liveliness=('automatic'|'manual'))?` -- a keyword alternation like the
    # four below it, not an EString. It sat outside this table AND behind the QOS_NEWER branch
    # below, so `liveliness: banana` reached the server, which answers "no viable alternative
    # at input 'banana'" and rejects the file.
    "liveliness": ["automatic", "manual"],
    "profile": ["default_qos", "services_qos", "sensor_qos", "parameter_qos"],
    "history": ["keep_last", "keep_all"],
    "reliability": ["best_effort", "reliable"],
    "durability": ["transient_local", "volatile"],
}

# grammar-subset.md sec 5.3: reachable members of the ParameterType alternation.
PARAM_TYPES_SCALAR = ["Integer", "String", "Double", "Boolean", "Base64"]
PARAM_TYPES_CONTAINER = ["Array", "List", "Struct"]
PARAM_TYPES_UNREACHABLE = ["Any", "ParameterAny", "Date", "ParameterDate"]

# RosSystem.xtext top-level blocks, in the emission profile's order (rule 25).
ROSSYSTEM_TOP_KEYS = [
    "fromFile",
    "subSystems",
    "processes",
    "nodes",
    "parameters",
    "connections",
]

# RosSystem.xtext:60-75 -- fixed member order for a RosNode (rule 22).
ROSSYSTEM_NODE_KEYS = ["from", "namespace", "interfaces", "parameters"]

# checkPortPatterns (S4): the only three legal from->to pairings.
ARROW_FROM_TO = {"pub": "sub", "ss": "sc", "as": "ac"}
ARROW_ALL = ["pub", "sub", "ss", "sc", "as", "ac"]
ARROW_KIND_ORDER = ["pub", "sub", "ss", "sc", "as", "ac"]

INT32_MIN = -2147483648
INT32_MAX = 2147483647

# Tab stop used only to recover structure from tab-indented files (see Linter.compose).
TAB_STOP = 8

# ---- .ros vocabularies (Ros.xtext / Basics.xtext) ------------------------------------
#
# Ros.xtext:11-14  PackageSet returns PackageSet: {PackageSet} package+=Package_Impl* ;
# so a .ros file may declare SEVERAL top-level packages. _deps/common_msgs.ros declares nine
# and the oracle accepts it. This is the one structural difference from .ros2.

# Ros.xtext:25-46 -- Package_Impl members. Order is a fixed prefix (fromGitRepo, dependencies)
# followed by a free repeated alternation of the three spec blocks.
ROS_PACKAGE_PREFIX_KEYS = ["fromGitRepo", "dependencies"]
ROS_SPEC_BLOCKS = ["msgs", "srvs", "actions"]

# Ros.xtext:81-104 -- which body keywords each block's spec rule admits, in grammar order.
ROS_SPEC_BODIES = {
    "msgs": ["message"],
    "srvs": ["request", "response"],
    "actions": ["goal", "result", "feedback"],
}
ROS_ALL_BODIES = {b for bs in ROS_SPEC_BODIES.values() for b in bs}

# Basics.xtext:210-213 AbstractType -- the scalar alternatives, verbatim and complete.
ROS_SCALAR_TYPES = [
    "bool", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64",
    "float32", "float64", "string", "byte", "char", "time", "duration", "Header",
]

# The fourteen *Array rules. NOTE: time, duration and Header have NO array rule -- 'time[]'
# is a parse error. Do not derive this list from ROS_SCALAR_TYPES.
ROS_ARRAY_TYPES = [
    "bool[]", "int8[]", "uint8[]", "int16[]", "uint16[]", "int32[]", "uint32[]",
    "int64[]", "uint64[]", "float32[]", "float64[]", "string[]", "byte[]", "char[]",
]
ROS_PRIMITIVE_TYPES = set(ROS_SCALAR_TYPES) | set(ROS_ARRAY_TYPES)

# Basics.xtext:377 -- KEYWORD, a legal Data (field name) alternative.
ROS_FIELD_NAME_KEYWORDS = {
    "goal", "message", "result", "feedback", "name", "value", "service", "type",
    "action", "duration", "time",
}

# Basics.xtext:206-208  terminal MESSAGE_ASIGMENT: ((ID|STRING)'='(ID|STRING|INT|'-'INT));
# One token, so no whitespace may surround the '='.
_MA_ATOM = r"""(?:[A-Za-z_][A-Za-z_0-9]*|"[^"]*"|'[^']*')"""
RE_MESSAGE_ASIGMENT = re.compile(
    r"^" + _MA_ATOM + r"=(?:" + _MA_ATOM + r"|-?[0-9]+)$"
)

# A fixed- or bounded-size array: the grammar hard-codes the literal '[]' only.
RE_BOUNDED_ARRAY = re.compile(r"^(.*)\[\s*[0-9]+\s*\]$|^(.*)<=[0-9]+$")

# Characters that force quoting because Xtext ID admits none of them.
RE_NEEDS_QUOTING = re.compile(r"[./:\-\s]|^$")


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------

def is_ros_names(text):
    """RosNames: ROS_CONVENTION_A | ID | 'node'  (Basics.xtext:394-396). No STRING alternative."""
    if text == "node":
        return True
    if RE_ID.match(text):
        return True
    return bool(text) and bool(RE_ROS_CONVENTION_A.match(text))


def ros_split_comment(line):
    """Return (code, had_comment). Xtext hides SL_COMMENT ('//' and '#' are both configured in
    Basics.xtext via Terminals), but '#' inside a quoted EString is content, not a comment."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#":
            return line[:i], True
    return line, False


def ros_tokenise(text):
    """Split a .ros field line into grammar tokens: quoted strings stay whole, and a trailing
    '[]' binds to the preceding token even when a space separates them (Xtext hides whitespace,
    so `'pkg/msg/T' [] name` is a legal ArraySpecRef -- 4 corpus files write it that way)."""
    toks, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "\"'":
            j = text.find(ch, i + 1)
            j = n if j < 0 else j + 1
            toks.append(text[i:j])
            i = j
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in "\"'":
            j += 1
        if j < n and j > i and text[j] in "\"'":
            # A MESSAGE_ASIGMENT's STRING atom may abut '=' with no space, e.g.
            # 'NAME="VALUE"' (Basics.xtext:206-208 is one terminal, no internal whitespace).
            # The quote here is glued to the bare run just scanned, not a new token's start;
            # a token beginning WITH a quote is still handled by the branch above.
            quote = text[j]
            close = text.find(quote, j + 1)
            j = n if close < 0 else close + 1
        toks.append(text[i:j])
        i = j
    merged = []
    for tok in toks:
        if tok == "[]" and merged:
            merged[-1] += "[]"
        else:
            merged.append(tok)
    return merged


def parse_ros_indent(lines):
    """Indentation-stack parse of a .ros file, mirroring AbstractIndentationTokenSource.

    Returns a list of nodes: {"line": 1-based, "indent": col, "depth": 0-based nesting level,
    "text": code with comments stripped, "raw": original}. Depth is derived from an indent
    STACK, never from `indent // 2` -- the corpus uses 3/4/5/6/7/9-space steps and the real
    lexer accepts every one of them. Blank and comment-only lines carry no tokens and so
    synthesise no BEGIN/END; they are skipped.
    """
    nodes, stack = [], [0]
    for idx, raw in enumerate(lines, start=1):
        code, _ = ros_split_comment(raw)
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" 	"))
        if indent > stack[-1]:
            stack.append(indent)
        else:
            while len(stack) > 1 and indent < stack[-1]:
                stack.pop()
            # An indent that matches no open level is a dedent to an unaligned column. The
            # Xtext lexer resolves it to the nearest enclosing level; do the same rather than
            # inventing a diagnostic the real parser does not produce.
            if indent > stack[-1]:
                stack.append(indent)
        nodes.append({
            "line": idx,
            "indent": indent,
            "depth": len(stack) - 1,
            "text": code.strip(),
            "raw": raw,
        })
    return nodes


def collect_ros_local_specs(path):
    """Return the '<pkg>/<msg|srv|action>/<Name>' qualified names a .ros file DEFINES.

    check_type_catalogue() only has assets/type_index.json (a small hand-curated catalogue of
    COMMON message packages) to check a quoted type reference against, so a self-consistent,
    genuinely project-local package -- one whose own srv/action fields reference sibling
    messages from the SAME extraction, not the vendored catalogue -- reads as "does not exist"
    the moment that package happens to have ANY unrelated type already in the catalogue (the
    package-known-but-type-missing branch is an ERROR, not the lenient "unknown package, assume
    project-local" WARNING). Building this registry across every file generated by one
    extraction (see run_lint() below) and checking it first fixes that false positive without
    touching the vendored-catalogue heuristic itself.

    Best-effort and silent: a file this cannot parse cleanly contributes no names here, and
    still gets its real diagnostics from the normal Linter.run() pass over the same file.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except (IOError, OSError):
        return set()
    text = raw[3:].decode("utf-8", errors="replace") if raw.startswith(b"\xef\xbb\xbf") \
        else raw.decode("utf-8", errors="replace")
    nodes = parse_ros_indent(text.splitlines())
    specs = set()
    pkgs, cur = [], None
    for node in nodes:
        if node["depth"] == 0:
            cur = {"head": node, "body": []}
            pkgs.append(cur)
        elif cur is not None:
            cur["body"].append(node)
    for pkg in pkgs:
        head_text = pkg["head"]["text"]
        if not head_text.endswith(":"):
            continue
        pkg_name = head_text[:-1].strip().strip("\"'")
        body = pkg["body"]
        if not pkg_name or not body:
            continue
        base = min(n["depth"] for n in body)
        for node in body:
            if node["depth"] != base:
                continue
            block_text = node["text"]
            key = block_text.split(":", 1)[0].strip()
            if key not in ROS_SPEC_BLOCKS or not block_text.endswith(":"):
                continue
            kind = {"msgs": "msg", "srvs": "srv", "actions": "action"}[key]
            idx, depth = body.index(node), node["depth"]
            inner = []
            for later in body[idx + 1:]:
                if later["depth"] <= depth:
                    break
                inner.append(later)
            if not inner:
                continue
            spec_depth = min(n["depth"] for n in inner)
            for spec_node in inner:
                if spec_node["depth"] != spec_depth:
                    continue
                name = spec_node["text"]
                name = name[:-1] if name.endswith(":") else name
                if " " in name.strip():
                    continue
                bare = name.strip().strip("\"'")
                if bare:
                    specs.add(pkg_name + "/" + kind + "/" + bare)
    return specs


def strip_literal_quotes(text):
    """Remove one layer of literal quote characters from a plain YAML scalar fragment."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1], True
    return text, False


def uppercase_positions(name):
    return [i for i, ch in enumerate(name) if ch.isupper()]


# --------------------------------------------------------------------------------------
# YAML node accessors -- we compose rather than safe_load so we keep line numbers,
# quote style, and duplicate keys.
# --------------------------------------------------------------------------------------

def node_line(node):
    return node.start_mark.line + 1 if node is not None else 0


SUBSYSTEMS_KEY_RE = re.compile(r"^(?P<indent>[ \t]*)subSystems:[ \t]*(?:#.*)?$")
# One bare (optionally quoted) EString, optional trailing comment, no ':' -- i.e. NOT a key
# line, which is how a `subSystems:` block ends.
SUBSYSTEMS_ENTRY_RE = re.compile(r"""^(?:"[^"]*"|'[^']*'|[^\s:#][^:#]*?)[ \t]*(?:#.*)?$""")


def normalise_bare_subsystems(text):
    """Rewrite a MULTI-entry bare `subSystems:` block as a YAML block sequence, in memory.

    `components+=SubSystem*` (RosSystem.xtext) is a repetition, not a list production: each
    entry is one bare (optionally quoted) EString on its own indented line, and the real
    grammar accepts nothing else. Settled 2026-08-14 against the 3.1.0 language server, the
    experiment RM093's own hint asked for:

        subSystems:          subSystems:
          "turtlebot"          - "turtlebot"
          "extra"              - "extra"
        -> ACCEPTED          -> REJECTED, line 4:
           0 errors             mismatched input '-' expecting RULE_END

    One entry composes as a plain scalar and is valid YAML. TWO are two consecutive scalars,
    which PyYAML cannot compose at all ("expected <block end>, but found '<scalar>'") -- so
    the only form the toolchain accepts was the one form every YAML-based reader in this
    plugin rejected outright, with an RM008 ERROR, on a file the oracle accepts clean.

    Converting to `- item` HERE, line for line, lets composition succeed while every
    diagnostic still points at the author's own line. Nothing on disk changes and the
    emitter still writes the bare form, because that is what the grammar takes.

    Returns (text, synthesised_lines): the 1-based line numbers this function rewrote.
    check_subsystems needs them to tell OUR dashes (a legal source file) from the author's
    own (RM093 -- a real syntax error the oracle rejects).
    """
    lines = text.splitlines(True)
    out = list(lines)
    synth = set()
    i = 0
    while i < len(lines):
        m = SUBSYSTEMS_KEY_RE.match(lines[i].rstrip("\r\n"))
        if not m:
            i += 1
            continue
        key_indent = len(m.group("indent").expandtabs(TAB_STOP))
        entries = []
        entry_indent = None
        j = i + 1
        while j < len(lines):
            raw = lines[j].rstrip("\r\n")
            body = raw.strip()
            if not body or body.startswith("#"):
                j += 1
                continue
            stripped = raw.lstrip(" \t")
            indent = len(raw[:len(raw) - len(stripped)].expandtabs(TAB_STOP))
            if indent <= key_indent:
                break
            if entry_indent is None:
                entry_indent = indent
            elif indent != entry_indent:
                # Siblings of a repetition sit at ONE depth. A deeper or shallower line is a real
                # indentation error -- the server answers "mismatched input '' expecting RULE_END"
                # -- and emitting both at key_indent would flatten them into a valid-looking flat
                # sequence, turning a rejected file into a silent pass. Leave the block alone so
                # composition fails and RM008 reports it, exactly as before this function existed.
                entries = []
                break
            if body[0] in "-[{&*?|>%@`":
                # A '- item' sequence or a bracket list: composable as-is, and the author's own
                # shape is what RM093 has to see. Rewriting a dash line would nest it
                # ('- - "x"') and silently destroy the reference.
                entries = []
                break
            if not SUBSYSTEMS_ENTRY_RE.match(body):
                # A `key:` line ends the block -- keep whatever entries we already collected.
                # Reaching this indented means the file's own indentation is irregular, which
                # is common in the corpus and no reason to give up on the entries above it.
                break
            entries.append(j)
            j += 1
        if len(entries) > 1:
            for k in entries:
                raw = lines[k]
                ending = raw[len(raw.rstrip("\r\n")):]
                out[k] = " " * key_indent + "- " + raw.strip() + ending
                synth.add(k + 1)
        i = max(j, i + 1)
    return "".join(out), synth


def is_mapping(node):
    return HAVE_YAML and isinstance(node, yaml.MappingNode)


def is_sequence(node):
    return HAVE_YAML and isinstance(node, yaml.SequenceNode)


def is_scalar(node):
    return HAVE_YAML and isinstance(node, yaml.ScalarNode)


def mapping_items(node):
    """[(key_node, value_node), ...] preserving duplicates and order."""
    return list(node.value) if is_mapping(node) else []


def mapping_keys(node):
    return [k.value for k, _ in mapping_items(node) if is_scalar(k)]


def mapping_get(node, key):
    for k, v in mapping_items(node):
        if is_scalar(k) and k.value == key:
            return v
    return None


def mapping_get_pair(node, key):
    for k, v in mapping_items(node):
        if is_scalar(k) and k.value == key:
            return k, v
    return None, None


def scalar_style(node):
    """None for a plain scalar, '\"' or \"'\" for a quoted one."""
    return getattr(node, "style", None) if is_scalar(node) else None


def is_quoted(node):
    return scalar_style(node) in ("'", '"')


# --------------------------------------------------------------------------------------
# The linter
# --------------------------------------------------------------------------------------

# What counts as an extractor item still open. The scripts write `# FLAG`; the other two
# are what real runs reworded them into. Matching all three means RM097's count survives a
# rename, which is the whole point -- see the check itself for why.
OPEN_MARKER_RE = re.compile(r"^#\s*(FLAG|OPEN|UNRESOLVED)\b")


class Linter(object):

    def __init__(self, path, use_catalogue=True, local_specs=None):
        self.path = path
        self.findings = []
        self.kind = None          # "ros" | "ros2" | "rossystem"
        self.raw = b""
        self.text = ""
        self.lines = []
        self.root = None
        self.had_leading_tabs = False
        self.synth_subsystem_lines = set()   # lines normalise_bare_subsystems() rewrote
        self.use_catalogue = use_catalogue
        self.needed_type_files = set()
        self.needed_node_files = set()
        # '<pkg>/<msg|srv|action>/<Name>' names defined anywhere in this same extraction batch
        # (see collect_ros_local_specs / run_lint) -- checked before the vendored catalogue.
        self.local_specs = local_specs if local_specs is not None else set()

    # -- finding emission ---------------------------------------------------------------

    def add(self, line, severity, rule, message, hint=""):
        self.findings.append(Finding(self.path, line, severity, rule, message, hint))

    def error(self, line, rule, message, hint=""):
        self.add(line, ERROR, rule, message, hint)

    def warn(self, line, rule, message, hint=""):
        self.add(line, WARNING, rule, message, hint)

    def info(self, line, rule, message, hint=""):
        self.add(line, INFO, rule, message, hint)

    # -- entry point --------------------------------------------------------------------

    def run(self):
        ext = os.path.splitext(self.path)[1].lower()
        if ext == ".ros2":
            self.kind = "ros2"
        elif ext == ".rossystem":
            self.kind = "rossystem"
        elif ext == ".ros":
            self.kind = "ros"
        else:
            self.error(0, "RM000",
                       "Unrecognised extension '%s'; expected .ros, .ros2 or .rossystem." % ext,
                       "Rename the file or pass a model file.")
            return self.findings

        try:
            with open(self.path, "rb") as handle:
                self.raw = handle.read()
        except (IOError, OSError) as exc:
            self.error(0, "RM000", "Cannot read file: %s" % exc, "Check the path and permissions.")
            return self.findings

        self.check_bytes_and_layout()

        # .ros is not YAML-shaped: `float32 x` nested under an un-colonned `Pose` is folded by
        # PyYAML into one multi-line plain scalar, so composing it would silently discard every
        # field. It gets its own indentation parser and needs no PyYAML at all.
        if self.kind == "ros":
            self.check_ros()
            self._emit_catalogue_summary()
            return self.findings

        if not HAVE_YAML:
            self.info(0, "RM008",
                      "PyYAML not available (%s); structural checks skipped." % YAML_IMPORT_ERROR,
                      "Install PyYAML ('pip install PyYAML') to enable structural checks. "
                      "Layout checks above are complete and unaffected.")
            return self.findings

        if not self.compose():
            return self.findings

        if self.kind == "ros2":
            self.check_ros2()
        else:
            self.check_rossystem()

        self._emit_catalogue_summary()
        return self.findings

    def _emit_catalogue_summary(self):
        """One consolidated INFO per catalogue, listing which vendored files this model's
        resolved references need -- e.g. for collect_deps.py to stage into an oracle case
        dir. Never fires when catalogue checks are off or nothing resolved."""
        if not self.use_catalogue:
            return
        if self.needed_type_files:
            self.info(0, "RM083",
                      "%d catalogue type file(s) needed to fully resolve this model."
                      % len(self.needed_type_files),
                      "assets/roscommonobjects/%s -- copy into an oracle case dir with "
                      "scripts/collect_deps.py, or resolve manually."
                      % ", assets/roscommonobjects/".join(sorted(self.needed_type_files)))
        if self.needed_node_files:
            self.info(0, "RM087",
                      "%d catalogue node/system file(s) needed to fully resolve this model."
                      % len(self.needed_node_files),
                      "assets/rosmodelscatalog/%s -- copy into an oracle case dir with "
                      "scripts/collect_deps.py, or resolve manually."
                      % ", assets/rosmodelscatalog/".join(sorted(self.needed_node_files)))

    def _check_catalogue_disclosure(self, line, file_rel, kind, ref, rule_id):
        """RM088/RM089: a reference resolved against a vendored catalogue, but the source
        line doesn't name which file it resolved to. Reading the .rossystem/.ros2 shouldn't
        require running the linter or searching assets/ to find out which real file backs a
        given from:/type: reference -- the answer belongs inline, as a comment."""
        if not (1 <= line <= len(self.lines)):
            return
        basename = file_rel.rsplit("/", 1)[-1]
        src_line = self.lines[line - 1]
        if basename in src_line or file_rel in src_line:
            return
        self.warn(line, rule_id,
                  "%s reference '%s' resolves to a vendored catalogue file, but this line "
                  "does not say which one." % (kind, ref),
                  "Add a trailing comment naming the exact file this resolves to, e.g. "
                  "'# %s', so a reader can see which real file backs this reference without "
                  "running the linter or searching assets/ themselves." % file_rel)

    # -- byte and layout checks ---------------------------------------------------------

    def check_bytes_and_layout(self):
        raw = self.raw

        if raw.startswith(b"\xef\xbb\xbf"):
            self.warn(1, "RM002B", "File begins with a UTF-8 BOM.",
                      "Strip the BOM; the Xtext lexer sees it as content on line 1.")
            raw = raw[3:]

        try:
            self.text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.error(0, "RM008", "File is not valid UTF-8: %s" % exc,
                       "Re-encode the file as UTF-8.")
            self.text = raw.decode("utf-8", errors="replace")

        # Line endings. emission-profile rule 3 states MUST be LF -- but the same document
        # states that working-tree CRLF is a core.autocrlf=true checkout artefact that MUST NOT
        # be treated as evidence (all 305 stored blobs are pure LF). Reporting every corpus file
        # as an ERROR would therefore be reporting a git configuration, not a model defect.
        # Severity is deliberately WARNING. See scripts/README.md, "Deviations from the specs".
        if b"\r\n" in raw:
            count = raw.count(b"\r\n")
            self.warn(1, "RM002",
                      "File uses CRLF line endings (%d lines)." % count,
                      "Emit LF only (emission-profile rule 3). If this file came from a "
                      "core.autocrlf=true checkout, the CRLF is a checkout artefact, not a "
                      "defect in the stored blob.")

        self.lines = self.text.splitlines()

        if self.text and not self.text.endswith("\n"):
            self.warn(max(len(self.lines), 1), "RM005",
                      "File does not end with a newline.",
                      "Append exactly one trailing LF (emission-profile rule 4); it also lets "
                      "the indentation lexer flush pending END tokens cleanly.")
        elif self.text.endswith("\n\n"):
            self.warn(len(self.lines), "RM005",
                      "File ends with more than one newline.",
                      "Emit exactly one trailing LF (emission-profile rule 4).")

        indent_widths_seen = set()

        for idx, line in enumerate(self.lines, start=1):
            stripped = line.lstrip(" \t")
            leading = line[:len(line) - len(stripped)]

            if "\t" in leading:
                self.had_leading_tabs = True
                self.warn(idx, "RM001",
                          "Tab character in leading whitespace.",
                          "Indent with spaces only (emission-profile rule 1). NOT a toolchain "
                          "error: verified 2026-07-21 against the real language server, which "
                          "ACCEPTS tab-indented files with 0 diagnostics (a spaces/tabs pair "
                          "differing in nothing else both came back clean). The real consequence "
                          "is downstream -- YAML forbids tabs in indentation, so rossdl's "
                          "yaml.safe_load cannot read the file. Emit spaces; do not retrofit "
                          "existing corpus files on the strength of this finding.")
            elif "\t" in line:
                self.warn(idx, "RM008T",
                          "Tab character inside the line (not in indentation).",
                          "Replace with spaces; tabs inside model text are never required.")

            if stripped and not stripped.startswith("#"):
                width = len(leading)
                if width:
                    indent_widths_seen.add(width)
                    if width % 2 != 0:
                        self.warn(idx, "RM003",
                                  "Indent width %d is not a multiple of 2." % width,
                                  "Use exactly 2 spaces per nesting level "
                                  "(emission-profile rule 2).")

            if line != line.rstrip():
                self.warn(idx, "RM004", "Trailing whitespace.",
                          "Strip trailing whitespace (emission-profile rule 5).")

            colon = self.structural_colon(line)
            if colon is not None and colon > 0 and line[colon - 1] in " \t":
                self.warn(idx, "RM007",
                          "Space before the ':' of a key.",
                          "Write 'name:' not 'name :' (emission-profile rule 6). Legal but "
                          "defective; the only corpus instances are the malformed "
                          "turtlesim_system.rossystem lines.")

        self.check_blank_lines_in_blocks()

    @staticmethod
    def structural_colon(line):
        """Index of the first ':' outside quotes that ends a key, else None."""
        quote = None
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
                continue
            if ch == "#":
                return None
            if ch == ":":
                if i + 1 >= len(line) or line[i + 1] in " \t":
                    return i
                return None
        return None

    def check_blank_lines_in_blocks(self):
        """emission-profile rule 5: no blank lines inside an indented block."""
        def indent_of(s):
            return len(s) - len(s.lstrip(" \t"))

        for idx in range(1, len(self.lines) - 1):
            if self.lines[idx].strip():
                continue
            prev = next((self.lines[j] for j in range(idx - 1, -1, -1)
                         if self.lines[j].strip()), None)
            nxt = next((self.lines[j] for j in range(idx + 1, len(self.lines))
                        if self.lines[j].strip()), None)
            if prev is None or nxt is None:
                continue
            if indent_of(prev) > 0 and indent_of(nxt) > 0:
                self.warn(idx + 1, "RM006",
                          "Blank line inside an indented block.",
                          "Remove it (emission-profile rule 5). Interaction with "
                          "AbstractIndentationTokenSource is unverified, so this is treated as "
                          "a risk rather than a proven failure.")

        # A full-line comment at COLUMN 0 inside an indented block is a hard parse error. Settled
        # 2026-08-14 against the 3.1.0 server: the same file with that comment indented, or with
        # the text moved to the end of the previous line, is ACCEPTED 0E/0W; dedented to column 0
        # it is REJECTED with "missing EOF at ''". AbstractIndentationTokenSource emits the END
        # tokens for every open block when it sees a line at column 0, and the model ends there --
        # everything after it is unreachable input.
        #
        # YAML does not care (a comment is not a node), so composition succeeds and every other
        # check in this file passes on a file the toolchain cannot load. Nothing else catches it.
        for idx, line in enumerate(self.lines, start=1):
            stripped = line.strip()
            if not stripped.startswith("#") or line[:1] in (" ", "	"):
                continue
            if idx == 1 or not any(l.strip() for l in self.lines[:idx - 1]):
                continue                      # the file header block, which is legal and common
            nxt = next((l for l in self.lines[idx:] if l.strip()), None)
            if nxt is None or nxt.lstrip().startswith("#"):
                continue                      # trailing block: RM087 territory, not this
            if indent_of(nxt) > 0:
                self.error(idx, "RM094",
                           "Comment at column 0 inside an indented block.",
                           "Indent it to the block it annotates, or move the text to the end of "
                           "the previous line. A line at column 0 makes "
                           "AbstractIndentationTokenSource close every open block, and the "
                           "server then reports 'missing EOF' -- the model ends at this comment "
                           "and the %d line(s) after it are never read. Verified 2026-08-14: "
                           "indented, the same file is ACCEPTED with 0 errors."
                           % (len(self.lines) - idx))

        # How much of the extractor's work is still outstanding.
        #
        # The scripts leave a `# FLAG` comment wherever they refused to guess. Counting
        # those alone does not work: a reader who resolves some and rewrites the rest in
        # their own words drives the count to zero, and the file then looks untouched by
        # the extractor rather than finished. That happened in two of three trial runs,
        # both acting in good faith.
        #
        # So the scripts also stamp `# EXTRACTOR-FLAGS: N` -- a fact about what was found,
        # which nobody editing the model has a reason to alter. Reporting the stamp
        # alongside the live count keeps the original number visible however the comments
        # are reworded, which is the property that was lost. It does not make the count
        # impossible to falsify; it makes it durable.
        stamp = None
        for line in self.lines[:60]:
            if line.startswith("# EXTRACTOR-FLAGS:"):
                digits = line.split(":", 1)[1].strip()
                if digits.isdigit():
                    stamp = int(digits)
                break
        open_flags = [idx for idx, line in enumerate(self.lines, start=1)
                      if OPEN_MARKER_RE.match(line.lstrip())]

        if open_flags:
            found = ("; the extractor originally found %d" % stamp) if stamp is not None else ""
            self.warn(open_flags[0], "RM097",
                      "%d item(s) the extractor could not read are still open%s "
                      "(first at line %d)." % (len(open_flags), found, open_flags[0]),
                      "Each one is a name, type or parameter the extractor could not read, "
                      "carrying its file:line and the source expression. Usually it is NOT in "
                      "this model; a runtime-built name with a literal type is kept instead, "
                      "with {...} placeholders for its runtime parts -- not a real ROS name "
                      "either (it gets its own RM097 warning). Resolve it under "
                      "SKILL.md 8e -- trace it, infer it from a citable basis, or ask -- and "
                      "delete the comment ONLY once the thing it describes is actually in "
                      "the model. If it stays unresolved, leave the marker: rewording it "
                      "away makes a model with known holes look finished. "
                      "'# DROPPED'/'# NOTE'/'# CAUTION' are the scripts' disclosures of "
                      "things the DSL cannot express, not a worklist: leave them.")
        elif stamp:
            self.info(1, "RM097",
                      "All %d item(s) the extractor flagged are closed in this file." % stamp,
                      "Nothing is outstanding here. This is recorded so a reader can tell a "
                      "finished model from one the extractor never had anything to say "
                      "about -- the two look identical once the comments are gone.")

    # -- YAML composition ---------------------------------------------------------------

    def compose(self):
        """Compose the YAML node graph. Returns True on success."""
        source = self.text
        if "\t" in source:
            # Graceful degradation: RM001 already reported the tabs as a WARNING. Expand them so
            # the structural checks can still run instead of dying on an opaque YAML error.
            #
            # Expansion must use real tab-stop semantics (advance to the next multiple of 8),
            # not a flat substitution. A naive '\t' -> '  ' turns two tabs into 4 columns, which
            # is SHALLOWER than a parent indented with 6 spaces, inverting the nesting and
            # producing a spurious "expected <block end>" error. Tab stops preserve the author's
            # intended relative depth in the mixed-indentation files.
            fixed = []
            for line in source.splitlines():
                stripped = line.lstrip(" \t")
                leading = line[:len(line) - len(stripped)]
                col = 0
                for ch in leading:
                    col = ((col // TAB_STOP) + 1) * TAB_STOP if ch == "\t" else col + 1
                # Trailing tabs after a scalar are also rejected by YAML ("found character '\t'
                # that cannot start any token"); RM004/RM008T already reported them.
                fixed.append(" " * col + stripped.rstrip())
            source = "\n".join(fixed) + "\n"

        # The one multi-entry `subSystems:` form the grammar accepts is not composable YAML;
        # see normalise_bare_subsystems. Line-for-line, so the marks below stay the author's.
        source, self.synth_subsystem_lines = normalise_bare_subsystems(source)

        try:
            self.root = yaml.compose(io.StringIO(source))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = mark.line + 1 if mark is not None else 0
            problem = getattr(exc, "problem", None) or str(exc)
            self.error(line, "RM008",
                       "YAML parse error: %s" % problem,
                       "The RosTooling DSLs are YAML-shaped; a file rossdl cannot yaml.safe_load "
                       "cannot be consumed downstream even if Xtext accepts it.")
            return False

        if self.root is None:
            self.error(0, "RM008", "File is empty or contains no document.",
                       "A model file must declare exactly one root element.")
            return False

        if not is_mapping(self.root):
            self.error(node_line(self.root), "RM008",
                       "Root of the document is not a mapping.",
                       "The root must be '<name>:' followed by an indented block.")
            return False

        items = mapping_items(self.root)
        if len(items) != 1:
            self.error(node_line(self.root), "RM008",
                       "Root mapping has %d entries; exactly 1 is required." % len(items),
                       "A .%s file declares exactly one root element." % self.kind)
            if not items:
                return False

        self.check_duplicate_keys(self.root, "document root")
        return True

    def check_duplicate_keys(self, node, where):
        seen = {}
        for k, _ in mapping_items(node):
            if not is_scalar(k):
                continue
            if k.value in seen:
                self.error(node_line(k), "RM009",
                           "Duplicate key '%s' in %s (first seen on line %d)."
                           % (k.value, where, seen[k.value]),
                           "YAML silently keeps the last occurrence, so the earlier entry is "
                           "lost without warning. Names must be unique.")
            else:
                seen[k.value] = node_line(k)

    # -- shared name / quoting checks ---------------------------------------------------

    def check_ros_names_position(self, node, what):
        """A RosNames position: MUST be bare, MUST be expressible as ROS_CONVENTION_A | ID."""
        if not is_scalar(node):
            return
        name = node.value
        line = node_line(node)

        if is_quoted(node):
            self.error(line, "RM021",
                       "%s '%s' is quoted." % (what, name),
                       "RosNames (Basics.xtext:394) is ROS_CONVENTION_A | ID | 'node' and has no "
                       "STRING alternative, so quoting it is a parse error. Emit it bare "
                       "(emission-profile rule 7).")
            return

        if not is_ros_names(name):
            self.error(line, "RM016",
                       "%s '%s' is not expressible as RosNames." % (what, name),
                       "RosNames admits only ID ([A-Za-z_][A-Za-z_0-9]*) or ROS_CONVENTION_A "
                       "(('/'ID)|(ID'/'))*. Characters such as '.', '-' or an embedded '/' "
                       "cannot appear here and cannot be rescued by quoting (rule 7).")
        elif "/" in name:
            self.warn(line, "RM014",
                       "%s '%s' contains '/'." % (what, name),
                       "Grammatically legal via ROS_CONVENTION_A, but rossdl's launch generation "
                       "keys remappings on bare node names "
                       "(rossdl_cmake/__init__.py get_system_nodes / get_system_remappings), so a "
                       "slash here breaks downstream generation.")

    def check_estring_quoting(self, node, what, require_quotes=True):
        """An EString position: quoting is required iff the value is not a bare ID."""
        if not is_scalar(node):
            return
        name = node.value
        line = node_line(node)

        if name == "":
            self.error(line, "RM015",
                       "Empty %s name." % what,
                       "Never emit an empty name (emission-profile rule 20); it is an extraction "
                       "defect that also breaks alphabetical ordering and cross-referencing.")
            return

        if not is_quoted(node):
            if RE_NEEDS_QUOTING.search(name) or not RE_ID.match(name):
                self.error(line, "RM020",
                           "%s '%s' contains a character illegal in an Xtext ID but is not "
                           "quoted." % (what, name),
                           "EString is STRING | ID (Basics.xtext:391); ID admits no '.', '/', "
                           "':' or '-'. Quote it (emission-profile rule 10).")
            elif require_quotes:
                self.warn(line, "RM022",
                          "%s '%s' is unquoted." % (what, name),
                          "Quote every interface and parameter name unconditionally "
                          "(emission-profile rule 11), even where a bare ID would parse.")

    def check_name_conventions(self, node, what, rule, severity):
        """Mirrors checkNameConventionsNode / Artifact / Package (R1/R2/R3)."""
        if not is_scalar(node):
            return
        name = node.value
        positions = uppercase_positions(name)
        if not positions:
            return
        chars = ", ".join("'%s' at index %d" % (name[i], i) for i in positions)
        message = ("%s '%s' contains %d uppercase character(s): %s."
                   % (what, name, len(positions), chars))
        if severity == ERROR:
            hint = ("checkNameConventionsPackage raises error(): 'Capital letters are not "
                    "allowed'. Package names MUST be [a-z0-9_] only (emission-profile rule 8). "
                    "Note the validator loop does not short-circuit, so the real toolchain emits "
                    "one marker per uppercase character.")
        elif severity == INFO:
            hint = ("House style only -- NO validator checks this. A rossystem::RosNode label is a "
                    "different metaclass from the ros::Node that checkNameConventionsNode targets, "
                    "and RosSystemValidator.xtend declares no name-convention @Check. Lowercase "
                    "when authoring fresh; never mangle a transcribed name to silence this "
                    "(emission-profile rule 9).")
        else:
            hint = ("The validator raises warning(): 'Capital letters are not recommended'. "
                    "Lowercase where the upstream source permits, but do not mangle a real name "
                    "such as gazebo_sensor_B1_controller (emission-profile rule 9).")
        self.add(node_line(node), severity, rule, message, hint)

    def check_parameter_name_conventions(self, node):
        """Mirrors checkNameConventionsParameters (R4): the dot-suffix carve-out."""
        if not is_scalar(node):
            return
        name = node.value
        offenders = [i for i, ch in enumerate(name)
                     if ch.isupper() and "." not in name[i:]]
        if offenders:
            chars = ", ".join("'%s' at index %d" % (name[i], i) for i in offenders)
            self.warn(node_line(node), "RM013",
                      "Parameter name '%s' has uppercase in its final dot-segment: %s."
                      % (name, chars),
                      "checkNameConventionsParameters forgives an uppercase character at index i "
                      "only if name[i:] still contains a '.'. So 'Foo.bar' is clean, 'foo.Bar' "
                      "warns. Severity is WARNING despite the validator's 'has to follow' wording "
                      "-- trust the warning() call, not the prose.")

    def check_ordering(self, keys_present, canonical, line, rule, what, hint):
        ordered = [k for k in keys_present if k in canonical]
        expected = sorted(ordered, key=canonical.index)
        if ordered != expected:
            self.warn(line, rule,
                      "%s order is %s; canonical order is %s."
                      % (what, " -> ".join(ordered), " -> ".join(expected)),
                      hint)

    def check_alphabetical(self, node, line, what):
        names = [k.value for k, _ in mapping_items(node) if is_scalar(k)]
        if len(names) > 1 and names != sorted(names):
            self.warn(line, "RM040",
                      "Entries in %s are not alphabetically sorted." % what,
                      "Sort entries within a block by byte order (emission-profile rule 26); "
                      "Corpus B is 51/51 sorted once the empty-name defect is excluded.")

    # ----------------------------------------------------------------------------------
    # .ros   (Ros.xtext PackageSet / Package_Impl / TopicSpec / ServiceSpec / ActionSpec)
    # ----------------------------------------------------------------------------------
    #
    # Severity policy for this branch, because it is easy to get wrong:
    #
    #   ERROR   only where the real toolchain fails to PARSE or to LINK. Every ERROR below was
    #           confirmed against ask_oracle.py, not inferred. RosValidator.xtend declares no
    #           @Check at all on TopicSpec / MessagePart / AbstractType, so apart from
    #           checkNameConventionsPackage there is no validator to mirror here -- the
    #           constraints are the grammar's own.
    #   WARNING house style (emission-profile) and constructs with no corpus support.
    #   INFO    things the caller must be told about but that are perfectly legal.
    #
    # The corpus uses IRREGULAR indentation in message bodies (turtlesim.ros steps 4/6/7/9 and
    # has trailing spaces on most field lines) and the real parser accepts all of it. Structure
    # is therefore derived from an indent STACK, and nothing here rejects a file on column
    # values. RM003 (indent not a multiple of 2) still fires as a WARNING from the shared layout
    # pass, which is correct: it is style advice, not a claim the file will not parse.

    def check_ros(self):
        nodes = parse_ros_indent(self.lines)
        if not nodes:
            self.error(0, "RM070", "File declares no package.",
                       "A .ros file is a PackageSet (Ros.xtext:11-14): one or more "
                       "'<package_name>:' declarations at column 0, each with an indented body.")
            return

        # The RosTooling-NadiaHG fork serialises .ros in a BRACE syntax --
        # `PackageSet{ Package geometry_msgs{ Specs { TopicSpec Accel{ message { ... }}}}` --
        # which the indentation grammar in this repo's RosTooling clone cannot parse at all.
        # Say so once instead of emitting a wall of RM071s about the braces.
        if "{" in nodes[0]["text"] and not nodes[0]["text"].endswith(":"):
            self.error(nodes[0]["line"], "RM079",
                       "File uses the brace serialisation, not the indentation syntax.",
                       "Lines such as 'PackageSet{ Package std_msgs{ spec{' belong to the "
                       "RosTooling-NadiaHG fork's concrete syntax. The grammar this linter "
                       "mirrors (RosTooling HEAD, Ros.xtext:11-46) is indentation-based and "
                       "cannot parse it. Findings below are unreliable for this file. Which "
                       "fork is canonical is STATUS.md open question N2.")

        if nodes[0]["indent"] != 0:
            self.error(nodes[0]["line"], "RM070",
                       "First declaration is indented; a .ros file must open at column 0.",
                       "PackageSet holds Package_Impl entries whose name sits at column 0 "
                       "(Ros.xtext:11-14).")

        # Group into top-level packages. Unlike .ros2, SEVERAL are legal here --
        # package+=Package_Impl* -- and tests/oracle/cases/_deps/common_msgs.ros declares nine.
        pkgs, cur = [], None
        for node in nodes:
            if node["depth"] == 0:
                cur = {"head": node, "body": []}
                pkgs.append(cur)
            elif cur is not None:
                cur["body"].append(node)

        seen_pkg = {}
        for pkg in pkgs:
            name = self.check_ros_package_head(pkg["head"])
            if name is not None:
                if name in seen_pkg:
                    self.error(pkg["head"]["line"], "RM009",
                               "Duplicate package '%s' (first seen on line %d)."
                               % (name, seen_pkg[name]),
                               "Two Package_Impl entries with the same name make every "
                               "'<name>/msg/<Type>' qualified name ambiguous (RosQNP.xtend).")
                else:
                    seen_pkg[name] = pkg["head"]["line"]
            self.check_ros_package_body(pkg["head"], pkg["body"])

    def check_ros_package_head(self, head):
        text = head["text"]
        if not text.endswith(":"):
            self.error(head["line"], "RM070",
                       "Package declaration '%s' has no trailing ':'." % text,
                       "Package_Impl is name=RosNames':' (Ros.xtext:25-27). Without the colon "
                       "the line lexes as a spec name in a position that admits none.")
            return None
        name = text[:-1].strip()
        if not name:
            self.error(head["line"], "RM015", "Empty package name.",
                       "Never emit an empty name (emission-profile rule 20).")
            return None
        if name[0] in "\"'":
            self.error(head["line"], "RM021",
                       "Package name %s is quoted." % name,
                       "RosNames (Basics.xtext:394) has no STRING alternative, so quoting a "
                       "package name is a parse error (emission-profile rule 7).")
            name = name.strip("\"'")
        elif not is_ros_names(name):
            self.error(head["line"], "RM016",
                       "Package name '%s' is not expressible as RosNames." % name,
                       "RosNames admits only ID ([A-Za-z_][A-Za-z_0-9]*) or ROS_CONVENTION_A; "
                       "'.', '-' and ':' cannot appear and cannot be rescued by quoting.")
        positions = uppercase_positions(name)
        if positions:
            chars = ", ".join("'%s' at index %d" % (name[i], i) for i in positions)
            self.error(head["line"], "RM010",
                       "Package name '%s' contains %d uppercase character(s): %s."
                       % (name, len(positions), chars),
                       "checkNameConventionsPackage raises error(): 'Capital letters are not "
                       "allowed' (RosValidator.xtend:44-48). It is the same @Check that fires "
                       "on a .ros2 package name, confirmed at ERROR severity by the oracle. "
                       "The loop does not short-circuit, so the toolchain emits one marker per "
                       "uppercase character.")
        return name

    def check_ros_package_body(self, head, body):
        pkg_name = head["text"].rstrip(":")
        if not body:
            self.warn(head["line"], "RM071",
                      "Package '%s' has an empty body." % pkg_name,
                      "Every member of Package_Impl is optional, so this parses -- but a "
                      "package declaring no msgs:, srvs: or actions: defines nothing, and no "
                      "'type:' reference can ever resolve into it.")
            return

        base = min(n["depth"] for n in body)
        blocks_present = []
        for node in body:
            if node["depth"] != base:
                continue
            text = node["text"]
            key = text.split(":", 1)[0].strip()
            if not text.endswith(":") and ":" not in text:
                self.error(node["line"], "RM071",
                           "Unknown member '%s' at package level." % text,
                           "Package_Impl admits only fromGitRepo:, dependencies:, msgs:, "
                           "srvs:, actions: (Ros.xtext:25-46). A bare word here is most often "
                           "a spec name that is missing its enclosing 'msgs:' block.")
                continue
            if key in ROS_SPEC_BLOCKS:
                blocks_present.append(key)
                self.check_ros_specs(key, node, body)
            elif key == "fromGitRepo":
                value = text.split(":", 1)[1].strip()
                if value and value[0] not in "\"'":
                    self.error(node["line"], "RM020",
                               "fromGitRepo value %s is not quoted." % value,
                               "It is an EString containing '/' and ':'; quote it with double "
                               "quotes (emission-profile rule 14).")
            elif key == "dependencies":
                self.warn(node["line"], "RM044",
                          "'dependencies:' has zero corpus support.",
                          "It is an inline bracketed list nested inside an indented block -- a "
                          "shape the corpus never exercises against the indentation lexer "
                          "(emission-profile sec 3). Avoid.")
            else:
                self.error(node["line"], "RM071",
                           "Unknown member '%s:' at package level." % key,
                           "Package_Impl admits only fromGitRepo:, dependencies:, msgs:, "
                           "srvs:, actions: (Ros.xtext:25-46).")

        for b in ROS_SPEC_BLOCKS:
            if blocks_present.count(b) > 1:
                self.warn(head["line"], "RM072",
                          "Block '%s:' appears %d times in package '%s'."
                          % (b, blocks_present.count(b), pkg_name),
                          "Ros.xtext:30-45 is a repeated alternation "
                          "( msgs: | srvs: | actions: )*, so repetition PARSES and the specs "
                          "accumulate. It is still a defect in generated output -- merge them "
                          "into a single block.")

    def check_ros_specs(self, block, block_node, body):
        """Check every spec directly under one msgs: / srvs: / actions: block."""
        idx = body.index(block_node)
        depth = block_node["depth"]
        inner = []
        for node in body[idx + 1:]:
            if node["depth"] <= depth:
                break
            inner.append(node)

        if not inner:
            self.warn(block_node["line"], "RM071",
                      "Block '%s:' is empty." % block,
                      "An empty block parses (spec+=TopicSpec* permits zero) but declares "
                      "nothing. Drop it or fill it.")
            return

        spec_depth = min(n["depth"] for n in inner)
        spec_names, seen = [], {}
        kind = {"msgs": "msg", "srvs": "srv", "actions": "action"}[block]
        for pos, node in enumerate(inner):
            if node["depth"] != spec_depth:
                continue
            name = node["text"]
            if name.endswith(":"):
                self.error(node["line"], "RM073",
                           "Spec name '%s' has a trailing ':'." % name,
                           "TopicSpec/ServiceSpec/ActionSpec are "
                           "name=(EString|'Header'|'String') with NO ':' (Ros.xtext:81-104). "
                           "The spec name is the one named element in this language that "
                           "carries no colon; its BEGIN is synthesised from the indent alone.")
                name = name.rstrip(":")
            if " " in name.strip():
                self.error(node["line"], "RM073",
                           "Spec name line '%s' holds more than one token." % node["text"],
                           "A spec name is a single EString (Ros.xtext:81-104). Field lines "
                           "belong one level further in, under the body keyword.")
                continue
            bare = name.strip("\"'")
            if not bare:
                self.error(node["line"], "RM015", "Empty spec name.",
                           "Never emit an empty name (emission-profile rule 20).")
                continue
            spec_names.append(bare)
            if bare in seen:
                self.error(node["line"], "RM009",
                           "Duplicate spec '%s' in '%s:' (first seen on line %d)."
                           % (bare, block, seen[bare]),
                           "RosQNP.xtend derives the qualified name '<pkg>/%s/<Spec>', so a "
                           "duplicate makes every reference to it ambiguous." % kind)
            else:
                seen[bare] = node["line"]
            self.check_ros_spec_body(block, node, inner[pos + 1:])

        if len(spec_names) > 1 and spec_names != sorted(spec_names):
            self.warn(block_node["line"], "RM040",
                      "Specs in '%s:' are not alphabetically sorted." % block,
                      "Sort entries within a block by byte order (emission-profile rule 26).")

    def check_ros_spec_body(self, block, spec_node, rest):
        """Check the body keywords and the field lines of one spec."""
        depth = spec_node["depth"]
        own = []
        for node in rest:
            if node["depth"] <= depth:
                break
            own.append(node)

        legal = ROS_SPEC_BODIES[block]

        if not own:
            self.error(spec_node["line"], "RM073",
                       "Spec '%s' has no body keyword." % spec_node["text"],
                       "Ros.xtext:81-104 makes the keyword mandatory: a TopicSpec needs "
                       "'message', a ServiceSpec needs 'request' and 'response', an ActionSpec "
                       "needs 'goal', 'result' and 'feedback'. Only the indented field block "
                       "after each keyword is optional.")
            return

        body_depth = min(n["depth"] for n in own)
        found = []
        for pos, node in enumerate(own):
            if node["depth"] != body_depth:
                continue
            kw = node["text"].strip()
            if kw not in legal:
                if kw in ROS_ALL_BODIES:
                    self.error(node["line"], "RM073",
                               "Body keyword '%s' is not legal under '%s:'." % (kw, block),
                               "'%s:' specs admit only %s (Ros.xtext:81-104); '%s' belongs to "
                               "a different spec kind." % (block, " / ".join(legal), kw))
                else:
                    self.error(node["line"], "RM073",
                               "'%s' is not a body keyword." % kw,
                               "Expected one of %s at this level. If it was meant as a field "
                               "it is indented one level too little; if it was meant as a spec "
                               "name, one level too much." % " / ".join(legal))
                continue
            found.append(kw)
            self.check_ros_fields(node, own[pos + 1:])

        for kw in legal:
            if kw not in found:
                self.error(spec_node["line"], "RM073",
                           "Spec '%s' has no '%s' section." % (spec_node["text"], kw),
                           "Ros.xtext:81-104 makes the keyword itself mandatory -- only its "
                           "indented body is optional. Emit a bare '%s' line when the section "
                           "has no fields." % kw)

    def check_ros_fields(self, body_node, rest):
        depth = body_node["depth"]
        fields = []
        for node in rest:
            if node["depth"] <= depth:
                break
            fields.append(node)

        if not fields:
            self.info(body_node["line"], "RM080",
                      "'%s' has no fields." % body_node["text"],
                      "Legal -- '(BEGIN message=MessageDefinition END)?' is optional, and a "
                      "bodiless 'response' is normal. But if the SOURCE defined fields, this "
                      "is silent data loss: emit them, and report any you could not "
                      "(SKILL.md rule 8b).")
            return

        for node in fields:
            self.check_ros_field_line(node)

    def check_ros_field_line(self, node):
        toks = ros_tokenise(node["text"])
        line = node["line"]

        if len(toks) > 2:
            # MessagePart+=MessagePart* has NO line separator, so several fields on one line
            # PARSE. RosTooling's own basic_msgs/common_msgs.ros writes 'time stamp string id'
            # and the oracle accepts it. House style only -- never an error.
            self.warn(line, "RM078",
                      "%d tokens on one field line (%d field(s))."
                      % (len(toks), len(toks) // 2),
                      "Legal: MessageDefinition is MessagePart+=MessagePart* with no line "
                      "separator (Ros.xtext:107-109). Emit one field per line anyway "
                      "(emission-profile rule 2 readability); do not rewrite a source file "
                      "that does this.")

        if len(toks) % 2 != 0:
            self.error(line, "RM077",
                       "Field line '%s' has an odd number of tokens (%d)."
                       % (node["text"], len(toks)),
                       "Every MessagePart is exactly two tokens, a Type then a Data "
                       "(Basics.xtext:201-204). An odd count means a type with no name -- or a "
                       "constant written with spaces around '=': 'uint8 FAN_OFF = 0' lexes as "
                       "three tokens, MESSAGE_ASIGMENT requires 'uint8 FAN_OFF=0'.")

        for i in range(0, len(toks) - 1, 2):
            self.check_ros_field_type(toks[i], line)
            self.check_ros_field_name(toks[i + 1], line)

    def check_ros_field_type(self, tok, line):
        if tok in ROS_PRIMITIVE_TYPES:
            return

        if "=" in tok and not RE_MESSAGE_ASIGMENT.match(tok):
            # 'uint8 FAN_OFF = 0' tokenises to four tokens, so the pair walker lands on '=' in
            # a Type position. Say what is actually wrong rather than "unknown type '='".
            self.error(line, "RM077",
                       "Stray '=' in a field line (token '%s')." % tok,
                       "MESSAGE_ASIGMENT is a single terminal "
                       "((ID|STRING)'='(ID|STRING|INT|'-'INT), Basics.xtext:206-208), so a "
                       "constant may carry NO whitespace around its '='. Write "
                       "'uint8 FAN_OFF=0', not 'uint8 FAN_OFF = 0'. The oracle rejects the "
                       "spaced form with \"mismatched input '=' expecting RULE_END\".")
            return

        if RE_BOUNDED_ARRAY.match(tok):
            stem = tok.split("[")[0].split("<=")[0]
            self.error(line, "RM075",
                       "Fixed- or bounded-size array '%s'." % tok,
                       "The grammar hard-codes the literal '[]' (the fourteen *Array rules at "
                       "Basics.xtext:296-362, and ArraySpecRef at 373-375); ROS IDL's "
                       "'float32[3]' and 'string<=10' have no production at all. Verified "
                       "against the oracle, which reports 'Invalid token %s[' -- a LEXER "
                       "error, so the whole file fails to parse. Emit the unbounded form and "
                       "tell the caller the bound was dropped." % stem)
            return

        if tok.endswith("[]") and tok[:-2] in ("time", "duration", "Header"):
            self.error(line, "RM074",
                       "'%s' has no array form." % tok,
                       "AbstractType (Basics.xtext:212) lists exactly fourteen *Array rules, "
                       "and timeArray / durationArray / HeaderArray are not among them. Only "
                       "these may take '[]': %s."
                       % ", ".join(t[:-2] for t in ROS_ARRAY_TYPES))
            return

        base = tok[:-2] if tok.endswith("[]") else tok
        quoted = len(base) >= 2 and base[0] == base[-1] and base[0] in "\"'"

        if quoted:
            # SpecBaseRef = [TopicSpec|EString]. RosQNP.xtend gives every spec the qualified
            # name '<pkg>/msg/<Name>' (or /srv/, /action/), so nothing else can ever link.
            inner = base[1:-1]
            if not re.match(
                    r"^[A-Za-z_][A-Za-z_0-9]*/(msg|srv|action)/[A-Za-z_][A-Za-z_0-9]*$", inner):
                self.warn(line, "RM076",
                          "Spec reference '%s' is not '<package>/msg/<Type>'." % inner,
                          "RosQNP.xtend names every TopicSpec pkg_name + '/msg/' + spec_name "
                          "(and '/srv/', '/action/'), so only that shape resolves. Severity is "
                          "WARNING here because linking is cross-file and this linter sees one "
                          "file at a time -- but the oracle reports the failure as an ERROR "
                          "('Couldn't resolve reference to TopicSpec'), so treat it as one.")
            else:
                self.check_type_catalogue(inner, line)
            return

        if RE_MESSAGE_ASIGMENT.match(tok):
            self.error(line, "RM077",
                       "Constant '%s' appears in a Type position." % tok,
                       "MESSAGE_ASIGMENT is the Data half of a MessagePart, never the Type "
                       "half (Basics.xtext:201-208). Write '<type> <NAME>=<value>', for "
                       "example 'uint8 FAN_OFF=0'.")
            return

        # An unquoted, non-primitive token. It PARSES (EString admits a bare ID) but can never
        # LINK: every spec's qualified name contains '/', which no bare Xtext ID can express.
        # Verified -- the oracle rejects a bare same-package 'AllTypes' with
        # "Couldn't resolve reference to TopicSpec 'AllTypes'".
        self.error(line, "RM074",
                   "Unknown type '%s'." % tok,
                   "It is neither a primitive nor a quoted spec reference. A bare name can "
                   "NEVER resolve to a spec: RosQNP.xtend qualifies every spec as "
                   "'<pkg>/msg/<Name>', which contains '/' and so is not expressible as a bare "
                   "Xtext ID. This holds even for a type defined in the SAME package -- there "
                   "is no short form; write '\"<package>/msg/%s\"'. Legal primitives are: %s "
                   "(plus the fourteen '[]' forms)." % (tok, ", ".join(ROS_SCALAR_TYPES)))

    def check_ros_field_name(self, tok, line):
        if tok in ROS_FIELD_NAME_KEYWORDS or RE_MESSAGE_ASIGMENT.match(tok):
            return
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            return
        if not RE_ID.match(tok):
            self.error(line, "RM077",
                       "Field name '%s' is not a legal Data token." % tok,
                       "Data is (KEYWORD | MESSAGE_ASIGMENT | EString) "
                       "(Basics.xtext:201-204). An unquoted name must be an Xtext ID "
                       "([A-Za-z_][A-Za-z_0-9]*); '.', '-' and '/' need quoting. If this was "
                       "meant as a constant, remove the spaces around '=' so it lexes as one "
                       "MESSAGE_ASIGMENT token.")

    # ----------------------------------------------------------------------------------
    # Type catalogue (assets/type_index.json) -- shared by .ros field types and .ros2
    # interface/parameter type: refs. RM081-083.
    # ----------------------------------------------------------------------------------

    def check_type_catalogue(self, ref, line):
        """ref is an already shape-valid, unquoted 'pkg/(msg|srv|action)/Type' string."""
        if ref in self.local_specs:
            # Defined by a sibling file in this same extraction batch (or this file itself) --
            # a real, resolvable spec, not something only the vendored catalogue could confirm.
            return
        if not self.use_catalogue:
            return
        index = load_type_index()
        if index is None:
            return

        entry = index.get(ref)
        if entry is not None:
            self.needed_type_files.add(entry["file"])
            self._check_catalogue_disclosure(
                line, "assets/roscommonobjects/%s" % entry["file"], "Type", ref, "RM089")
            return

        pkg = ref.split("/", 1)[0]
        pkg_types = [k for k in index if k.split("/", 1)[0] == pkg]
        if not pkg_types:
            self.warn(line, "RM081",
                      "Type reference '%s' is not in the vendored type catalogue." % ref,
                      "assets/type_index.json (built from assets/roscommonobjects/) has no "
                      "package '%s' at all. A genuinely project-local message package is "
                      "legitimate here -- emit a companion .ros defining it. If '%s' was meant "
                      "to be a standard package, this is likely a typo or a package this "
                      "catalogue does not cover." % (pkg, pkg))
            return

        suggestion = difflib.get_close_matches(ref, pkg_types, n=1)
        hint = ("Package '%s' IS in the catalogue, but '%s' does not match any of its "
                "indexed types -- the package is known-complete here, so this is a real "
                "defect, not a plausibly-missing project-local type." % (pkg, ref))
        if suggestion:
            hint += " Did you mean '%s' (%s)?" % (suggestion[0], index[suggestion[0]]["file"])
        self.error(line, "RM082", "Type reference '%s' does not exist." % ref, hint)

    # ----------------------------------------------------------------------------------
    # .ros2
    # ----------------------------------------------------------------------------------

    def check_ros2(self):
        pkg_key, pkg_val = mapping_items(self.root)[0]

        self.check_ros_names_position(pkg_key, "Package name")
        self.check_name_conventions(pkg_key, "Package name", "RM010", ERROR)

        if not is_mapping(pkg_val):
            if pkg_val is not None and is_scalar(pkg_val) and pkg_val.value == "":
                self.error(node_line(pkg_key), "RM008",
                           "Package '%s' has an empty body." % pkg_key.value,
                           "An AmentPackage needs at least one of fromGitRepo:, artifacts:, "
                           "dependencies:.")
            return

        self.check_duplicate_keys(pkg_val, "package '%s'" % pkg_key.value)

        present = []
        for key, val in mapping_items(pkg_val):
            if not is_scalar(key):
                continue
            name, line = key.value, node_line(key)
            present.append(name)

            if name in ROS2_FORBIDDEN_SPEC_BLOCKS:
                self.error(line, "RM030",
                           "'%s:' block is not reachable in a .ros2 file." % name,
                           "Ros2.xtext makes AmentPackage the entry rule, and AmentPackage admits "
                           "only fromGitRepo:/artifacts:/dependencies:. The token is present in "
                           "InternalRos2Parser.tokens because Ros2 inherits Ros, but no grammar "
                           "path reaches it (grammar-subset.md sec 5.2). Message specs belong in "
                           "a .ros file.")
            elif name == "artifacts":
                self.check_ros2_artifacts(val)
            elif name == "dependencies":
                self.warn(line, "RM044",
                          "'dependencies:' has zero corpus support.",
                          "0 of 253 corpus .ros2 files use it, and it is an inline bracketed list "
                          "inside an indented block -- a shape the corpus never exercises against "
                          "the indentation lexer (emission-profile sec 3). Avoid.")
            elif name == "fromGitRepo":
                if is_scalar(val) and not is_quoted(val):
                    self.error(node_line(val), "RM020",
                               "fromGitRepo value '%s' is not quoted." % val.value,
                               "It is an EString containing '/' and ':'; quote it with double "
                               "quotes (emission-profile rule 14).")
            elif name not in AMENT_PACKAGE_KEYS:
                self.error(line, "RM037",
                           "Unknown key '%s:' at package level." % name,
                           "AmentPackage admits only fromGitRepo:, artifacts:, dependencies: "
                           "(Ros2.xtext:13-24).")

        self.check_ordering(present, AMENT_PACKAGE_KEYS, node_line(pkg_key), "RM039",
                            "AmentPackage member",
                            "Ros2.xtext:13-24 is a fixed sequence, not an alternation, so this "
                            "order is mandatory (emission-profile rule 21).")

    def check_ros2_artifacts(self, node):
        if not is_mapping(node):
            return
        self.check_duplicate_keys(node, "artifacts block")
        for art_key, art_val in mapping_items(node):
            if not is_scalar(art_key):
                continue
            self.check_ros_names_position(art_key, "Artifact name")
            self.check_name_conventions(art_key, "Artifact name", "RM011", WARNING)
            if is_mapping(art_val):
                self.check_ros2_artifact_body(art_key, art_val)

    def check_ros2_artifact_body(self, art_key, node):
        self.check_duplicate_keys(node, "artifact '%s'" % art_key.value)

        blocks_present = []
        for key, val in mapping_items(node):
            if not is_scalar(key):
                continue
            name, line = key.value, node_line(key)

            if name == "node":
                self.check_ros_names_position(val, "Node name")
                self.check_name_conventions(val, "Node name", "RM012", WARNING)
                continue

            if name in ROS2_FORBIDDEN_SPEC_BLOCKS:
                self.error(line, "RM030",
                           "'%s:' block is not reachable in a .ros2 file." % name,
                           "See grammar-subset.md sec 5.2. Message specs belong in a .ros file.")
                continue

            if name not in ROS2_NODE_BLOCKS:
                self.error(line, "RM036",
                           "Unknown block '%s:' inside artifact '%s'." % (name, art_key.value),
                           "A Node admits only: %s (Ros.xtext:121-159)."
                           % ", ".join(ROS2_NODE_BLOCKS))
                continue

            blocks_present.append(name)
            if is_mapping(val):
                self.check_duplicate_keys(val, "'%s:' block" % name)
                self.check_alphabetical(val, line, "'%s:'" % name)
                for entry_key, entry_val in mapping_items(val):
                    if not is_scalar(entry_key):
                        continue
                    if name == "parameters":
                        self.check_ros2_parameter(entry_key, entry_val)
                    else:
                        self.check_ros2_interface(name, entry_key, entry_val)

        if "node" not in mapping_keys(node):
            self.error(node_line(art_key), "RM038",
                       "Artifact '%s' has no 'node:' declaration." % art_key.value,
                       "Ros.xtext:122 -- a Node begins with 'node:' <RosNames>.")

        self.check_ordering(blocks_present, ROS2_NODE_BLOCKS, node_line(art_key), "RM039",
                            "Node block",
                            "The grammar permits any order, so convention decides. Canonical "
                            "order is the grammar's declaration order (emission-profile rule 24): "
                            "%s." % " -> ".join(ROS2_NODE_BLOCKS))

    def check_placeholder_name(self, name, line):
        """An interface name still holding the extractor's {...} placeholders (a name built at
        runtime). Syntactically valid, so a warning -- but it is not the real ROS name."""
        if "{" in name or "}" in name:
            self.warn(line, "RM097",
                      "Interface name '%s' is not resolved -- it still holds a {...} "
                      "placeholder from runtime-built code." % name,
                      "Each {...} stands for a value computed at runtime (a parameter, f-string "
                      "or variable), so no real ROS name looks like this and nothing will "
                      "connect to it. Resolve it under SKILL.md 8e, or rename it by hand.")

    def check_ros2_interface(self, block, key, val):
        self.check_estring_quoting(key, "Interface", require_quotes=True)
        if is_scalar(key):
            self.check_placeholder_name(str(key.value), node_line(key))
        if not is_mapping(val):
            self.error(node_line(key), "RM038",
                       "Interface '%s' has no body." % key.value,
                       "Every interface requires at least 'type:' (Ros2.xtext:49-112).")
            return

        self.check_duplicate_keys(val, "interface '%s'" % key.value)
        keys = mapping_keys(val)

        if "type" not in keys:
            self.error(node_line(key), "RM038",
                       "Interface '%s' is missing the mandatory 'type:'." % key.value,
                       "'type:' is not optional in Publisher/Subscriber/ServiceServer/"
                       "ServiceClient/ActionServer/ActionClient (Ros2.xtext:49-112).")
        else:
            type_node = mapping_get(val, "type")
            if is_scalar(type_node):
                if not is_quoted(type_node):
                    self.error(node_line(type_node), "RM020",
                               "Message type reference '%s' is not quoted." % type_node.value,
                               "A type reference such as 'std_msgs/msg/String' contains '/', "
                               "which an Xtext ID cannot hold (emission-profile rule 13).")
                elif re.match(r"^[A-Za-z_][A-Za-z_0-9]*/(msg|srv|action)/[A-Za-z_][A-Za-z_0-9]*$",
                              type_node.value):
                    self.check_type_catalogue(type_node.value, node_line(type_node))

        for extra in keys:
            if extra not in ("type", "ns", "qos"):
                self.error(node_line(mapping_get_pair(val, extra)[0]), "RM036",
                           "Unknown key '%s:' in interface '%s'." % (extra, key.value),
                           "An interface admits only type:, ns:, qos: in that order "
                           "(Ros2.xtext:49-112).")

        if "ns" in keys:
            self.warn(node_line(mapping_get_pair(val, "ns")[0]), "RM044",
                      "'ns:' has zero corpus support.",
                      "0 occurrences across all 253 corpus .ros2 files "
                      "(emission-profile sec 3). Avoid.")

        self.check_ordering([k for k in keys if k in ("type", "ns", "qos")],
                            ["type", "ns", "qos"], node_line(key), "RM039",
                            "Interface member",
                            "Ros2.xtext:49-112 fixes the order type: -> ns: -> qos: "
                            "(emission-profile rule 23).")

        if "qos" in keys:
            self.check_qos(mapping_get(val, "qos"))

    def check_ros2_parameter(self, key, val):
        self.check_estring_quoting(key, "Parameter", require_quotes=True)
        self.check_parameter_name_conventions(key)

        if not is_mapping(val):
            self.error(node_line(key), "RM038",
                       "Parameter '%s' has no body." % key.value,
                       "A parameter requires at least 'type:' (Ros2.xtext:114-124).")
            return

        self.check_duplicate_keys(val, "parameter '%s'" % key.value)
        keys = mapping_keys(val)

        # 'default:' is legal here even though it is not a member of the Parameter rule: it
        # belongs to ParameterType (Basics.xtext:75-109, e.g. ParameterIntegerType ::= 'Integer'
        # ('default:' default=ParameterInteger)?). Since 'type:' type=ParameterType has no
        # BEGIN/END around it, the default lands as a YAML sibling of 'type:', immediately after
        # it and before ns:/value:/qos:. None of the three spec documents mention this key.
        param_keys = ["type", "default", "ns", "value", "qos"]

        for extra in keys:
            if extra not in param_keys:
                self.error(node_line(mapping_get_pair(val, extra)[0]), "RM036",
                           "Unknown key '%s:' in parameter '%s'." % (extra, key.value),
                           "A Parameter admits only type:, ns:, value:, qos: "
                           "(Ros2.xtext:114-124), plus default: from ParameterType "
                           "(Basics.xtext:75-109).")

        self.check_ordering([k for k in keys if k in param_keys],
                            param_keys, node_line(key), "RM039",
                            "Parameter member",
                            "Ros2.xtext:114-124 fixes the order type: -> ns: -> value: -> qos:, "
                            "and ParameterType's optional default: binds directly to type: "
                            "(emission-profile rule 23).")

        type_node = mapping_get(val, "type")
        declared = None
        if type_node is None:
            self.error(node_line(key), "RM038",
                       "Parameter '%s' is missing the mandatory 'type:'." % key.value,
                       "'type:' is not optional (Ros2.xtext:114-124).")
        elif is_scalar(type_node):
            declared = self.check_parameter_type(type_node, key)

        if "value" in keys:
            self.check_parameter_value(declared, mapping_get(val, "value"), key)

        if "qos" in keys:
            self.check_qos(mapping_get(val, "qos"))

    def check_parameter_type(self, node, key):
        raw = node.value.strip()
        line = node_line(node)

        if is_quoted(node):
            self.error(line, "RM041",
                       "Parameter type '%s' is quoted." % raw,
                       "Parameter types are grammar keywords (Basics.xtext:51-110); quoting one "
                       "is a parse error. Corpus: 3575 bare, 0 quoted "
                       "(emission-profile rule 16).")
            return None

        base = re.split(r"[\[\s]", raw, 1)[0]

        if base in PARAM_TYPES_UNREACHABLE:
            self.error(line, "RM041",
                       "Parameter type '%s' is unreachable." % base,
                       "ParameterAnyType and ParameterDateType are defined in Basics.xtext but "
                       "are not members of the ParameterType alternation -- the Date branch is "
                       "commented out (grammar-subset.md sec 5.3). Never emit.")
            return None

        if base not in PARAM_TYPES_SCALAR + PARAM_TYPES_CONTAINER:
            self.error(line, "RM041",
                       "Unknown parameter type '%s'." % raw,
                       "Emittable types are exactly: %s and the bracketed forms %s."
                       % (", ".join(PARAM_TYPES_SCALAR),
                          ", ".join(t + "[...]" for t in PARAM_TYPES_CONTAINER)))
            return None

        if base in PARAM_TYPES_CONTAINER and "[" not in raw:
            self.error(line, "RM041",
                       "Container parameter type '%s' has no bracketed element type." % raw,
                       "Basics.xtext requires the bracketed form, e.g. Array[String]. The older "
                       "'Array:' + indent form was replaced by commit 4b9f44ad, before the pinned "
                       "JAR was built.")

        if base == "Struct":
            self.warn(line, "RM045",
                      "Struct parameter type is discouraged.",
                      "The ParameterStruct value rule mixes brackets with BEGIN/END indentation "
                      "tokens, is fragile under the indentation lexer, and no corpus file "
                      "exercises it (grammar-subset.md sec 5.4). CheckParameterValue also "
                      "recurses using instance fields as loop counters "
                      "(validator-rules.md sec 3.3).")
        elif base in ("List", "Base64"):
            self.warn(line, "RM045",
                      "%s parameter type has near-zero corpus support." % base,
                      "RosValidator emits info-level format hints for List/Struct/Base64, "
                      "implying they are under-exercised even by the tool authors "
                      "(emission-profile sec 3). Prefer scalar types or Array.")

        if base == "Array" and re.search(r"Array\s+\[", raw):
            self.info(line, "RM046",
                      "'Array [' has a space before the bracket.",
                      "Corpus B uses 'Array[String]' 6/6. Whitespace is hidden so both parse; "
                      "cosmetic only (emission-profile rule 17).")

        return base

    def check_parameter_value(self, declared, node, key):
        if node is None or not is_scalar(node):
            return
        raw = node.value
        line = node_line(node)

        if raw in ("True", "False", "TRUE", "FALSE"):
            self.error(line, "RM042",
                       "Boolean value '%s' is not lowercase." % raw,
                       "terminal BOOLEAN is 'true'|'false' only (Basics.xtext:173). Uppercase "
                       "does NOT raise an error -- it falls through the ParameterValue "
                       "alternation and is captured by ParameterString, so a type: Boolean "
                       "parameter silently receives a STRING. No validator detects this "
                       "(emission-profile rule 18).")

        if declared == "Double" and re.match(r"^[+-]?[0-9]+$", raw) and not is_quoted(node):
            self.error(line, "RM043",
                       "Double parameter '%s' has integer literal value '%s'."
                       % (key.value, raw),
                       "terminal DOUBLE requires a '.' or an exponent; a bare '%s' matches DECINT "
                       "and silently becomes a ParameterInteger. Emit '%s.0' "
                       "(emission-profile rule 19)." % (raw, raw))

        if declared == "Boolean" and raw not in ("true", "false") and not is_quoted(node):
            if raw not in ("True", "False", "TRUE", "FALSE"):
                self.warn(line, "RM042",
                          "Boolean parameter '%s' has non-boolean value '%s'."
                          % (key.value, raw),
                          "Expected 'true' or 'false' (Basics.xtext:173).")

        if (is_quoted(node) and re.match(r"^\s*[\[{].*[\]}]\s*$", raw, re.S)
                and (declared is None or declared.startswith("Array")
                     or declared.startswith("List"))):
            self.warn(line, "RM095",
                      "Parameter '%s' value is a quoted string shaped like a list/struct."
                      % key.value,
                      "A quoted \"[...]\" is a ParameterString; a .rossystem that references "
                      "this parameter and copies the string form will be rejected by "
                      "CheckParameterValue with 'Expect a list of elements' (validator-rules.md "
                      "sec 2.6) -- confirmed a transcribed .ros2 with this defect does not itself "
                      "error, so this is WARNING here, not ERROR. Report it as a source defect "
                      "(SKILL.md sec 8) and emit a real list in any .rossystem that uses it.")

    def check_qos(self, node):
        if not is_mapping(node):
            return
        self.check_duplicate_keys(node, "qos block")

        for key, val in mapping_items(node):
            if not is_scalar(key):
                continue
            name, line = key.value, node_line(key)

            if name in QOS_NEWER:
                self.info(line, "RM031",
                          "QoS field '%s:' requires a toolchain built after 2025-10-16." % name,
                          "LEGAL, not an error. Added by commit 3d9e5ebd (2025-10-16) and verified "
                          "ACCEPTED against our current oracle on 2026-07-21. Flagged only because "
                          "a consumer running an older build -- such as the LS JAR shipped in "
                          "vscode-RosTooling/resources/, built 2024-08-01 -- cannot LEX this "
                          "keyword and will report a syntax error on the whole publisher. "
                          "0 corpus files use it, so portability is untested in practice.")
                if name in ("lease_duration", "lifespan", "deadline"):
                    self.check_qos_duration(val, name)
                elif name in QOS_ENUMS and is_scalar(val):
                    # RM031 says the FIELD needs a recent toolchain; it says nothing about the
                    # value, and `continue`ing here skipped the enum check entirely.
                    if val.value not in QOS_ENUMS[name]:
                        self.error(node_line(val), "RM033",
                                   "QoS %s value '%s' is not permitted." % (name, val.value),
                                   "Allowed values: %s (Ros2.xtext). The server answers 'no "
                                   "viable alternative at input' and rejects the file."
                                   % ", ".join(QOS_ENUMS[name]))
                    elif is_quoted(val):
                        self.error(node_line(val), "RM033",
                                   "QoS %s value '%s' is quoted." % (name, val.value),
                                   "These are grammar keywords, not EStrings; quoting one is a "
                                   "parse error.")
                continue

            if name not in QOS_PINNED:
                self.error(line, "RM032",
                           "Unknown QoS field '%s:'." % name,
                           "The emittable QoS vocabulary is exactly: %s."
                           % ", ".join(QOS_PINNED))
                continue

            if name in QOS_DISCOURAGED:
                self.warn(line, "RM034",
                          "QoS field '%s:' is JAR-supported but discouraged." % name,
                          "Corpus B deliberately comments these out (57 blocks of "
                          "'# profile:' / '# history: UNKNOWN' / '# depth:'), most plausibly "
                          "because the extractor could not determine a trustworthy value. "
                          "Do not INVENT a value for these (emission-profile rule 33). "
                          "EXPECTED AND CORRECT when the value came from the input: this rule "
                          "warns against fabrication, not against transcription. Never drop a "
                          "concrete source value to get a clean run (SKILL.md, Validation).")

            if name == "depth":
                # Integer0 is `terminal DECINT: '0' | ('1'..'9' DIGIT*) | ('-''0'..'9' DIGIT*)`
                # -- the minus sign is IN the terminal, and the server accepts `depth: -5`
                # (verified 2026-08-14, ACCEPTED 0E/0W). The old regex rejected it while citing
                # the very grammar line that permits it. A negative queue depth is still almost
                # certainly a mistake, so it is reported -- as the WARNING it is, not as a
                # parse error it is not.
                text = val.value.strip() if is_scalar(val) else ""
                if is_scalar(val) and not re.match(r"^-?[0-9]+$", text):
                    self.error(node_line(val), "RM033",
                               "QoS depth '%s' is not an integer." % val.value,
                               "'depth:' Depth=Integer0 (Ros2.xtext:35), and Integer0 is the "
                               "DECINT terminal: an optional '-' followed by digits.")
                elif text.startswith("-"):
                    self.warn(node_line(val), "RM033",
                              "QoS depth '%s' is negative." % val.value,
                              "Legal -- DECINT admits a leading '-' and the server accepts it "
                              "(ACCEPTED 0E/0W, 2026-08-14). It is reported because a negative "
                              "queue depth has no meaning at runtime: rclcpp/rclpy take a "
                              "size_t, so this is a modelling mistake the toolchain will not "
                              "catch for you.")
            elif name in QOS_ENUMS and is_scalar(val):
                allowed = QOS_ENUMS[name]
                if val.value not in allowed:
                    self.error(node_line(val), "RM033",
                               "QoS %s value '%s' is not permitted." % (name, val.value),
                               "Allowed values: %s (Ros2.xtext:32-37)." % ", ".join(allowed))
                elif is_quoted(val):
                    self.error(node_line(val), "RM033",
                               "QoS %s value '%s' is quoted." % (name, val.value),
                               "These are grammar keywords, not EStrings; quoting one is a "
                               "parse error.")

    def check_qos_duration(self, node, field):
        """Mirrors CheckDuration (R14). Secondary to RM031 -- see README, 'Deviations'."""
        if not is_scalar(node):
            return
        raw = node.value
        line = node_line(node)

        if raw == "infinite" and not is_quoted(node):
            return

        if not is_quoted(node):
            self.error(line, "RM035",
                       "QoS %s value '%s' is neither quoted nor the keyword 'infinite'."
                       % (field, raw),
                       "The assignment is (EString | 'infinite') and EString is STRING | ID; "
                       "a bare digit sequence is neither, so the value must be quoted: "
                       "%s: \"1000000\" (emission-profile rule 34)." % field)
            return

        if not re.match(r"^[+-]?[0-9]+$", raw):
            self.error(line, "RM035",
                       "QoS %s value '%s' is not an integer string." % (field, raw),
                       "CheckDuration calls Integer.parseInt, which rejects decimal points, "
                       "underscores, exponents and whitespace (it does not trim).")
            return

        value = int(raw)
        if value < INT32_MIN or value > INT32_MAX:
            self.error(line, "RM035",
                       "QoS %s value '%s' overflows signed 32-bit." % (field, raw),
                       "Integer.parseInt caps at %d. The value is in NANOSECONDS, so the maximum "
                       "expressible duration is about 2.147 seconds -- any human-plausible "
                       "deadline such as \"5000000000\" (5 s) throws NumberFormatException. "
                       "This is the single most likely accidental violation. CONFIRMED against "
                       "the real validator 2026-07-21 (oracle case 14): deadline \"5000000000\" "
                       "-> ERROR, while \"1000\" and 'infinite' pass. Note this rule became MORE "
                       "relevant when the QoS pin was lifted -- these fields are only emittable "
                       "at all since then." % INT32_MAX)

    # ----------------------------------------------------------------------------------
    # .rossystem
    # ----------------------------------------------------------------------------------

    def check_rossystem(self):
        sys_key, sys_val = mapping_items(self.root)[0]
        self.check_estring_quoting(sys_key, "System", require_quotes=False)

        if not is_mapping(sys_val):
            return

        self.check_duplicate_keys(sys_val, "system '%s'" % sys_key.value)

        present = []
        interfaces = {}       # local name -> [{"kind","line","node","target"}, ...]
        node_names = []
        local_from = {}
        subsystems_val = None

        for key, val in mapping_items(sys_val):
            if not is_scalar(key):
                continue
            name, line = key.value, node_line(key)
            present.append(name)

            if name == "fromFile":
                self.check_from_file(val)
            elif name == "nodes":
                node_names, local_from = self.check_rossystem_nodes(val, interfaces)
            elif name == "subSystems":
                subsystems_val = val
                self.info(line, "RM061",
                          "'subSystems:' present -- the reuse checks RM090-RM093 apply "
                          "(bare-per-line, flat nesting, no label reachable two ways).",
                          "Emit this whenever the caller asked to reuse a composition -- do not "
                          "treat this INFO as a reason to leave it out (SKILL.md sec 8d). "
                          "checkIfInterfaceInSystem recurses exactly one level and casts "
                          "unconditionally, so a nested subsystem throws ClassCastException "
                          "(validator-rules.md sec 3.5). Keep nesting flat.")
            elif name == "processes":
                self.warn(line, "RM063",
                          "'processes:' has near-zero corpus support (1 file).",
                          "The single example, image_system_example.rossystem, is itself "
                          "malformed -- its sibling nodes: block is indented 5 spaces "
                          "(emission-profile sec 3). Avoid.")
            elif name == "connections":
                pass  # handled after nodes, below
            elif name != "parameters":
                self.error(line, "RM037",
                           "Unknown key '%s:' at system level." % name,
                           "RosSystem admits only: %s (RosSystem.xtext:13-45)."
                           % ", ".join(ROSSYSTEM_TOP_KEYS))

        if "fromFile" not in present:
            self.warn(node_line(sys_key), "RM053",
                      "'fromFile:' is absent.",
                      "Omitting it is corpus-normal (32/52 files) but trips a live bug: "
                      "fromFileHelper dereferences system.fromFile.empty and "
                      "system.fromFile.toString with no null guard, and the field defaults to "
                      "null (SystemImpl.java:70). Always emit a quoted fromFile containing '/' "
                      "(validator-rules.md sec 2.2).")

        if subsystems_val is not None:
            self.check_subsystems(subsystems_val, interfaces, node_names, local_from)

        # processes: must reference declared nodes (S1)
        proc_node = mapping_get(sys_val, "processes")
        if is_mapping(proc_node):
            self.check_processes(proc_node, node_names)

        # connections: needs the fully collected interface table (S3, S4)
        conn_node = mapping_get(sys_val, "connections")
        if conn_node is not None:
            self.check_connections(conn_node, interfaces, sys_key.value)

        self.check_ordering(present, ROSSYSTEM_TOP_KEYS, node_line(sys_key), "RM059",
                            "System block",
                            "RosSystem.xtext:13-45 is an alternation, so convention decides. "
                            "Observed corpus order is %s (emission-profile rule 25)."
                            % " -> ".join(ROSSYSTEM_TOP_KEYS))

    def check_subsystems(self, node, interfaces, local_node_names, local_from):
        """Resolve each subSystems: entry against assets/node_index.json's system table and
        merge its nodes' interfaces into this file's connection-membership table, mirroring
        checkIfInterfaceInSystem's one-level walk into SubSystem.system.components. Also
        flags the two ways a subsystem reference goes wrong for THIS project's purposes:
        RM090 (a node reachable both directly and through a subSystems: entry -- a genuine
        label collision, not just redundant modelling) and RM091 (the reference itself is
        unresolved, nests another subSystems: block, or resolves but exposes zero
        interfaces -- see build_node_index.py's extract_rossystem_system and
        snappy-dreaming-harbor.md point 3 for why the last case is real and common).

        'components+=SubSystem*' is a repeated rule, not a bracket list -- the corpus form is
        one bare (optionally quoted) EString per indented line, which a single-entry file
        composes as a plain scalar, not a sequence. Tolerate scalar/sequence/mapping shapes
        the same way ros_plot.py's model reader does, since nothing here depends on which
        shape produced the reference."""
        refs = []  # (name_string, line)
        if is_scalar(node) and node.value.strip():
            refs.append((node.value.strip(), node_line(node)))
        elif is_sequence(node):
            if getattr(node, "flow_style", False):
                self.error(node_line(node), "RM093",
                           "'subSystems:' uses the bracket-list form '[...]'.",
                           "'components+=SubSystem*' (RosSystem.xtext) is a repetition, not a "
                           "list production -- unlike 'nodes: [...]' inside a process, there is "
                           "no bracket form here. Emit one bare (optionally quoted) name per "
                           "indented line instead, e.g.:\n  subSystems:\n    \"turtlebot\"")
            elif not {node_line(i) for i in node.value} <= self.synth_subsystem_lines:
                # ANY dashed entry, not just two or more: `subSystems:` / `  - "turtlebot"` is one
                # entry and the server rejects it with the same "mismatched input '-'". Guarding
                # on len > 1 let the single-entry form -- the one a hand-editor is most likely to
                # write, copying the shape of every other list in the file -- through untouched.
                # Not our own normalisation (normalise_bare_subsystems) -- the author really
                # did write dashes, and the real parser will not take them.
                self.error(node_line(node), "RM093",
                           "'subSystems:' has %d %s, written as a '- item' block sequence."
                           % (len(node.value),
                              "entry" if len(node.value) == 1 else "entries"),
                           "The real grammar rejects this: 'mismatched input '-' expecting "
                           "RULE_END'. 'components+=SubSystem*' is a repetition, not a list "
                           "production, so each entry is one bare (optionally quoted) name on "
                           "its own indented line -- no dash, no brackets:\n"
                           "  subSystems:\n    \"turtlebot\"\n    \"extra\"\n"
                           "Settled 2026-08-14 against the 3.1.0 language server, which ACCEPTS "
                           "that bare two-entry form with 0 errors and REJECTS the dash form "
                           "above (this rule's hint used to call the question open; it is not). "
                           "Note the bare form is not loadable by yaml.safe_load -- this linter "
                           "normalises it internally, but a consumer such as rossdl cannot read "
                           "a multi-entry subSystems: model at all. That is an upstream conflict "
                           "between the grammar and the YAML shape, not something a model author "
                           "can write their way out of.")
            for item in node.value:
                if is_scalar(item) and item.value.strip():
                    refs.append((item.value.strip(), node_line(item)))
        elif is_mapping(node):
            for k, _v in mapping_items(node):
                if is_scalar(k):
                    refs.append((k.value.strip(), node_line(k)))

        if not refs or not self.use_catalogue:
            return
        index = load_system_index()
        if index is None:
            return

        for ref, line in refs:
            entry = index.get(ref)
            if entry is None:
                # a project-local target sitting next to this file resolves too. It is weaker
                # evidence than the catalogue, so RM091 still fires -- but its interfaces are
                # merged below, because otherwise every endpoint it provides raises RM050 and
                # the plugin rejects a layout the real validator accepts.
                entry = load_local_system(os.path.dirname(os.path.abspath(self.path)), ref)
                if entry is None:
                    self.warn(line, "RM091",
                              "subSystems: '%s' does not resolve against "
                              "assets/node_index.json's indexed systems, and no '%s.rossystem' "
                              "sits next to this file." % (ref, ref),
                              "Either it is a genuine project-local system kept elsewhere (fine, "
                              "but then this plugin cannot verify what it exposes) or the name "
                              "is wrong -- check the target file's own top-level key, not its "
                              "filename.")
                    continue
                self.warn(line, "RM091",
                          "subSystems: '%s' is not catalogued; resolved against the sibling "
                          "file '%s' instead." % (ref, entry["file"]),
                          "Its %d node(s) are treated as reachable, so connections: endpoints "
                          "they declare are accepted. Nothing outside this directory can "
                          "resolve the reference, though -- run /update-ros-catalog if the "
                          "target belongs in assets/rosmodelscatalog/."
                          % len(entry["nodes"]))

            # Same root as needed_node_files (assets/rosmodelscatalog/) -- a resolved
            # subSystems: target is itself a vendored file collect_deps.py needs to stage,
            # same as a resolved from:'s .ros2. Recorded even when the two branches below
            # flag the reference as risky or useless: staging it is still correct, and a
            # future catalogue sync could add the interfaces this version lacks. A sibling
            # file is NOT recorded: it is not under assets/ and collect_deps stages it by a
            # different path (its own local-file pass).
            if not entry.get("local"):
                self.needed_node_files.add(entry["file"])

            if entry["hasOwnSubsystems"]:
                self.warn(line, "RM091",
                          "subSystems: '%s' itself declares a subSystems: block." % ref,
                          "checkIfInterfaceInSystem casts every subsystem component "
                          "unconditionally to RosNode one level down (validator-rules.md "
                          "sec 3.5) -- referencing a system that is itself built from "
                          "subSystems: is two levels of nesting and throws "
                          "ClassCastException in the real validator. Keep nesting flat.")
                continue

            if not any(info["interfaces"] for info in entry["nodes"].values()):
                self.warn(line, "RM091",
                          "subSystems: '%s' resolves (%s) but declares zero 'interfaces:' on "
                          "any of its %d node(s)." % (ref, entry["file"], len(entry["nodes"])),
                          "A subsystem's connectable interfaces are exactly what its OWN "
                          "'interfaces:' block declares -- never derived from the .ros2 'from:' "
                          "it points at (checkIfInterfaceInSystem, "
                          "RosSystemValidator.xtend:87-109). This subSystems: reference is "
                          "grammatically valid but functionally inert here: nothing in it can be "
                          "a connections: endpoint. If you need to wire one of its nodes, "
                          "declare that node directly under this file's own nodes: instead.")
                # Still fall through to the collision checks below -- an interface-less
                # subsystem node can still collide on LABEL or from: with a local
                # declaration, which is a real defect independent of whether the
                # subsystem exposes anything to connect to.

            # Iterate every node, not just interface-exposing ones: RM090/RM092 are
            # name/from: collisions, not connectivity checks, so a subsystem node with
            # no interfaces: still needs to be checked -- it can still collide with a
            # local nodes: entry (bug found in review: this used to iterate only
            # interface-exposing nodes and silently missed that exact case for
            # catalogued systems like turtlebot_gazebo/cartographer that mix exposed
            # and interface-less nodes).
            for node_label, node_info in entry["nodes"].items():
                owner = "%s (via subSystems: '%s')" % (node_label, ref)

                if node_label in local_node_names:
                    self.warn(line, "RM090",
                              "Node label '%s' is declared directly under this file's nodes: "
                              "AND is reachable through subSystems: '%s'." % (node_label, ref),
                              "Demoted from ERROR 2026-08-14: the real 3.1.0 language server "
                              "ACCEPTS this, 0 errors, 0 warnings (a system reusing catalogued "
                              "'turtlebot' while also declaring 'turtlebot_node' locally). The "
                              "two nodes live in different Xtext resources and the server "
                              "resolves the name silently, so the old hint's claim -- 'two "
                              "distinct RosNode objects answer to the same name in this file's "
                              "scope' -- is not what happens. It is still worth saying: which "
                              "of the two a connections: endpoint binds to is unspecified, and "
                              "a reader cannot tell from this file alone. Declare it once if "
                              "you can -- drop the local entry and let the subsystem provide "
                              "it, or drop the reference if the local declaration must differ.")
                elif node_info["from"] and node_info["from"] in local_from.values():
                    local_label = next(k for k, v in local_from.items()
                                        if v == node_info["from"])
                    self.warn(line, "RM092",
                              "This file's node '%s' and node '%s' reachable via subSystems: "
                              "'%s' both resolve 'from:' to '%s'."
                              % (local_label, node_label, ref, node_info["from"]),
                              "Different labels pointing at the same real node is sometimes "
                              "legitimate (e.g. distinguishing roles), but if this is the same "
                              "logical node modelled twice rather than a genuine second "
                              "instance, keep only one -- prefer the subsystem's declaration "
                              "over re-declaring it locally.")

                for iface_name, kind in node_info["interfaces"].items():
                    interfaces.setdefault(iface_name, []).append(
                        {"kind": kind, "line": line, "node": owner, "target": None})

    def check_from_file(self, node):
        if not is_scalar(node):
            return
        raw = node.value
        line = node_line(node)

        if raw == "":
            self.error(line, "RM052",
                       "'fromFile:' is an empty string.",
                       "fromFileHelper then skips its info branch and evaluates "
                       "\"\".contains(\"/\") = false, producing an unconditional ERROR. "
                       "Never emit fromFile: \"\" (validator-rules.md sec 2.2).")
            return

        if "/" not in raw:
            self.error(line, "RM052",
                       "'fromFile:' value '%s' does not contain '/'." % raw,
                       "fromFileHelper raises error('Path not valid...'). Expected format: "
                       "\"NameOfThePackage/Path/to/ExecutableLaunchFile.launch.py\".")

        if "TODO" in raw:
            self.info(line, "RM066",
                      "'fromFile:' is the synthesised placeholder '%s'." % raw,
                      "fromFile: is mandatory in practice only to avoid the fromFileHelper NPE, "
                      "and 32/52 corpus models supply no launch file at all. The TODO sentinel is "
                      "the correct value when none is known (rossystem-syntax.md sec 2) -- it is "
                      "reported so the placeholder stays visible and is not mistaken for a "
                      "verified path. Replace it with the real launch file before use.")
        elif not os.path.abspath(self.path).startswith(os.path.abspath(_ASSETS_DIR) + os.sep):
            src_line = self.lines[line - 1] if 1 <= line <= len(self.lines) else ""
            if "# caller-supplied" not in src_line and "# on disk:" not in src_line:
                self.warn(line, "RM096",
                          "'fromFile:' is a specific path with no provenance comment.",
                          "Say '# caller-supplied' or '# on disk: <path>' on this line, or use "
                          "the TODO sentinel (SKILL.md rule 5). A confident-looking path with no "
                          "stated provenance is indistinguishable from a verified one -- that is "
                          "exactly what rule 5 warns fabrication is dangerous for.")

        if not is_quoted(node):
            self.error(line, "RM020",
                       "'fromFile:' value '%s' is not quoted." % raw,
                       "It contains '/' and '.', neither of which an Xtext ID admits "
                       "(emission-profile rule 14: double quotes for string data).")

    def check_rossystem_nodes(self, node, interfaces):
        if not is_mapping(node):
            return [], {}
        self.check_duplicate_keys(node, "nodes block")
        # No RM040 here on purpose: emission-profile rule 26's evidence base is .ros2 interface
        # and parameter blocks. Corpus B's own MT.rossystem does not sort its nodes: block, and
        # Corpus B is the weighted-decisive authority, so sorting it is not a house rule.

        names = []
        local_from = {}   # node label -> raw 'from:' string (for RM090/RM092 vs subSystems:)
        for key, val in mapping_items(node):
            if not is_scalar(key):
                continue
            names.append(key.value)
            if is_mapping(val):
                from_val = mapping_get(val, "from")
                if is_scalar(from_val):
                    local_from[key.value] = from_val.value
            self.check_estring_quoting(key, "Node", require_quotes=True)
            # RM067, not RM012: this is a rossystem::RosNode LABEL, a different metaclass from the
            # ros::Node that checkNameConventionsNode targets. RosSystemValidator.xtend has no
            # name-convention @Check at all (verified 2026-07-21: its only @Check methods are
            # checkIfNodeInSystem, fromFileHelper, checkIfInterfaceInSystem, checkPortPatterns,
            # MatchPortMsgs, CheckParameter, BinaryHelp, ArrayHelp, ListHelp, StructHelp). Reporting
            # it as RM012 conflated a house-style preference with a validator-derived rule and
            # produced WARNINGs on corpus-correct names such as
            # static_transform_publisher_vS7Rn4YQfVmDReBi. Kept as INFO for style visibility only;
            # emission-profile rule 9 says never mangle an upstream name to satisfy it.
            self.check_name_conventions(key, "System node label", "RM067", INFO)

            if "/" in key.value:
                self.warn(node_line(key), "RM062",
                          "System node name '%s' contains '/'." % key.value,
                          "rossdl's launch generation keys its remapping dictionary on node "
                          "names (rossdl_cmake/__init__.py get_system_remappings), and derives "
                          "the node class from from:.split('.')[1]; a slash here breaks that.")

            if is_mapping(val):
                self.check_rossystem_node_body(key, val, interfaces)
        return names, local_from

    def check_rossystem_node_body(self, node_key, node, interfaces):
        self.check_duplicate_keys(node, "node '%s'" % node_key.value)
        keys = mapping_keys(node)

        for extra in keys:
            if extra not in ROSSYSTEM_NODE_KEYS:
                self.error(node_line(mapping_get_pair(node, extra)[0]), "RM036",
                           "Unknown key '%s:' in node '%s'." % (extra, node_key.value),
                           "A RosNode admits only: %s (RosSystem.xtext:60-75)."
                           % ", ".join(ROSSYSTEM_NODE_KEYS))

        resolved = None
        if "from" not in keys:
            self.error(node_line(node_key), "RM038",
                       "Node '%s' is missing the mandatory 'from:'." % node_key.value,
                       "'from:' from=[ros::Node|EString] is not optional "
                       "(RosSystem.xtext:63).")
        else:
            resolved = self.check_from_reference(mapping_get(node, "from"), node_key)

        if "namespace" in keys:
            self.warn(node_line(mapping_get_pair(node, "namespace")[0]), "RM044",
                      "'namespace:' has zero corpus support.",
                      "0 of 52 corpus .rossystem files use it (emission-profile sec 3). Avoid.")

        self.check_ordering([k for k in keys if k in ROSSYSTEM_NODE_KEYS],
                            ROSSYSTEM_NODE_KEYS, node_line(node_key), "RM039",
                            "Node member",
                            "RosSystem.xtext:60-75 fixes the order %s "
                            "(emission-profile rule 22)." % " -> ".join(ROSSYSTEM_NODE_KEYS))

        iface_node = mapping_get(node, "interfaces")
        if iface_node is not None:
            self.check_interfaces(iface_node, node_key, interfaces, resolved)

        param_node = mapping_get(node, "parameters")
        if param_node is not None:
            self.check_rossystem_parameters(param_node, node_key)

    def check_from_reference(self, node, node_key):
        """Returns the resolved node-catalogue entry ({'file','artifact','interfaces'}) when
        'from:' matches a real assets/node_index.json entry, else None. The return value lets
        check_interfaces/parse_arrow validate arrow targets against THIS SPECIFIC node's real
        interface set, not just any node in the catalogue."""
        if not is_scalar(node):
            return None
        raw = node.value
        line = node_line(node)

        if not is_quoted(node):
            self.error(line, "RM020",
                       "'from:' value '%s' is not quoted." % raw,
                       "It is <packageName>.<nodeName>; the '.' cannot appear in an Xtext ID "
                       "(emission-profile rule 12). Corpus: 296/296 quoted.")

        if raw.count(".") != 1:
            self.warn(line, "RM057",
                      "'from:' value '%s' is not of the form <package>.<node>." % raw,
                      "Segment 2 is the target artifact's 'node:' value, NOT the artifact name -- "
                      "RosQNP.xtend returns pkg.name + '.' + node_name for a ros::Node, reading "
                      "the Package from obj.eContainer.eContainer, so the artifact level is "
                      "skipped (emission-profile rule 29). Note this is the OPPOSITE level from "
                      "an arrow target, which is <artifact>::<interface> (rule 30). rossdl also "
                      "does node['from'].strip('\"').split('.')[1] to derive the node class "
                      "(rossdl_cmake/__init__.py get_system_nodes), so anything other than "
                      "exactly one '.' breaks launch generation. The package part is resolved "
                      "through the target model's declared name, NEVER through the .ros2 "
                      "filename -- 76/253 files differ.")
            return None

        return self.check_node_catalogue(raw, line)

    def check_node_catalogue(self, ref, line):
        """ref is 'package.node'. RM084/085; returns the resolved catalogue entry or None."""
        if not self.use_catalogue:
            return None
        index = load_node_index()
        if index is None:
            return None

        entry = index.get(ref)
        if entry is not None:
            self.needed_node_files.add(entry["file"])
            self._check_catalogue_disclosure(
                line, "assets/rosmodelscatalog/%s" % entry["file"], "Node", ref, "RM088")
            return entry

        pkg = ref.split(".", 1)[0]
        pkg_nodes = [k for k in index if k.split(".", 1)[0] == pkg]
        if not pkg_nodes and ref in load_local_nodes(os.path.dirname(os.path.abspath(self.path))):
            # A project-local node defined by a sibling .ros2 -- the case the hint below calls
            # legitimate, now confirmed rather than assumed. An unknown node still warns.
            return None
        if not pkg_nodes:
            self.warn(line, "RM084",
                      "'from:' reference '%s' is not in the vendored node catalogue." % ref,
                      "assets/node_index.json (built from assets/rosmodelscatalog/) has no "
                      "package '%s' at all. A genuinely project-local node is legitimate here. "
                      "If '%s' was meant to be a standard package (e.g. a Nav2/TurtleBot3 "
                      "node), this is likely a typo or a package this catalogue does not "
                      "cover -- note TurtleBot 2/Kobuki is NOT in this catalogue at all, only "
                      "TurtleBot 3." % (pkg, pkg))
            return None

        suggestion = difflib.get_close_matches(ref, pkg_nodes, n=1)
        hint = ("Package '%s' IS in the catalogue, but '%s' does not match any of its "
                "indexed nodes -- the package is known-complete here, so this is a real "
                "defect, not a plausibly-missing project-local node." % (pkg, ref))
        if suggestion:
            hint += " Did you mean '%s' (%s)?" % (suggestion[0], index[suggestion[0]]["file"])
        self.error(line, "RM085", "Node reference '%s' does not exist." % ref, hint)
        return None

    def check_interfaces(self, node, node_key, interfaces, resolved=None):
        if not is_sequence(node):
            self.error(node_line(node), "RM055",
                       "'interfaces:' is not a list.",
                       "Each interface is a list item: - \"name\": pub-> \"artifact::iface\" "
                       "(RosSystem.xtext:77).")
            return

        local_seen = {}
        kinds_in_order = []

        for item in node.value:
            if not is_mapping(item):
                continue
            for key, val in mapping_items(item):
                if not is_scalar(key) or not is_scalar(val):
                    continue
                name, line = key.value, node_line(key)
                self.check_estring_quoting(key, "Interface", require_quotes=True)
                self.check_placeholder_name(next((x for x in (str(name), str(val.value)) if "{" in x or "}" in x), ""), line)

                if name in local_seen:
                    self.error(line, "RM058",
                               "Duplicate interface local name '%s' in node '%s' "
                               "(first seen on line %d)."
                               % (name, node_key.value, local_seen[name]),
                               "RosSystemConnection resolves [RosInterface|EString] BY NAME, so "
                               "duplicates break cross-referencing. Corpus B disambiguates by "
                               "kind prefix: sub_clock, sub_bond "
                               "(emission-profile rule 28).")
                else:
                    local_seen[name] = line

                kind, target = self.parse_arrow(val, name, line, resolved)
                if kind is None:
                    continue
                kinds_in_order.append(kind)

                # Reuse of the SAME local name in a DIFFERENT node is normal and correct:
                # Corpus B declares 'clock' in nearly every node. Rule 28 scopes uniqueness to
                # within a node. We only record the collision so that a connection which
                # actually references an ambiguous name can be reported (RM065).
                interfaces.setdefault(name, []).append(
                    {"kind": kind, "line": line, "node": node_key.value, "target": target})

        ordered = [k for k in kinds_in_order if k in ARROW_KIND_ORDER]
        if ordered != sorted(ordered, key=ARROW_KIND_ORDER.index):
            self.warn(node_line(node), "RM039",
                      "Interfaces in node '%s' are not grouped by kind in canonical order."
                      % node_key.value,
                      "Group by kind %s, alphabetical within each kind "
                      "(emission-profile rule 27)." % " -> ".join(ARROW_KIND_ORDER))

    def parse_arrow(self, val, name, line, resolved=None):
        raw = val.value.strip()

        if "->" not in raw:
            self.error(node_line(val), "RM055",
                       "Interface '%s' has no '->' arrow." % name,
                       "An InterfaceReference is one of pub->, sub->, ss->, sc->, as->, ac-> "
                       "(RosSystem.xtext:81-112).")
            return None, None

        prefix, target = raw.split("->", 1)
        prefix = prefix.strip()
        target = target.strip()

        if prefix not in ARROW_ALL:
            self.error(node_line(val), "RM055",
                       "Interface '%s' has unknown arrow prefix '%s->'." % (name, prefix),
                       "Valid prefixes are exactly: %s (RosSystem.xtext:81-112)."
                       % ", ".join(a + "->" for a in ARROW_ALL))
            return None, None

        stripped, was_quoted = strip_literal_quotes(target)

        if not was_quoted:
            self.error(node_line(val), "RM020",
                       "Arrow target '%s' of interface '%s' is not quoted." % (target, name),
                       "The target is <artifactName>::<interfaceName>; '::' cannot appear in "
                       "an Xtext ID, so quoting is structurally forced -- 622/622 corpus "
                       "occurrences are quoted (emission-profile rule 12).")

        if "::" not in stripped:
            self.warn(node_line(val), "RM056",
                      "Arrow target '%s' of interface '%s' is not <artifact>::<interface>."
                      % (stripped, name),
                      "The prefix is the target .ros2's ARTIFACT name (the key under artifacts:), "
                      "not its node: value -- RosQNP.xtend returns art.name + '::' + "
                      "interface.name from obj.eContainer.eContainer as Artifact, so the package "
                      "level is skipped (emission-profile rule 30). This is the OPPOSITE level "
                      "from 'from:', which is <package>.<node> (rule 29). Separately, rossdl "
                      "skips any interface whose target lacks '::' when building remappings "
                      "(rossdl_cmake/__init__.py), so the interface is silently dropped from "
                      "launch generation.")
        elif resolved is not None and self.use_catalogue:
            art, _, iface = stripped.partition("::")
            # Only check when the arrow's artifact matches the SAME node 'from:' resolved to --
            # RosSystemScopeProvider is stock/unverified, so a target naming a different
            # artifact might legitimately resolve elsewhere; skip rather than false-positive.
            if art == resolved["artifact"] and iface not in resolved["interfaces"]:
                candidates = list(resolved["interfaces"])
                suggestion = difflib.get_close_matches(iface, candidates, n=1)
                hint = ("'%s' resolved via 'from:' to %s, but '%s' is not among its indexed "
                        "interfaces (%s)." % (resolved["artifact"], resolved["file"], iface,
                                              ", ".join(sorted(candidates)) or "none"))
                if suggestion:
                    hint += " Did you mean '%s'?" % suggestion[0]
                self.error(node_line(val), "RM086",
                           "Arrow target '%s' of interface '%s' does not exist."
                           % (stripped, name), hint)

        return prefix, stripped

    def check_rossystem_parameters(self, node, node_key):
        if not is_sequence(node):
            return
        for item in node.value:
            if not is_mapping(item):
                continue
            items = mapping_items(item)
            if not items:
                continue
            key, val = items[0]
            if not is_scalar(key):
                continue
            self.check_estring_quoting(key, "Parameter", require_quotes=True)
            self.check_parameter_name_conventions(key)

            if is_scalar(val):
                if not is_quoted(val):
                    self.error(node_line(val), "RM020",
                               "Parameter reference '%s' is not quoted." % val.value,
                               "It is <artifactName>::<parameterName>; '::' cannot appear in "
                               "an Xtext ID (emission-profile rule 12).")
                if "::" not in val.value:
                    self.warn(node_line(val), "RM056",
                              "Parameter reference '%s' is not <artifact>::<parameter>."
                              % val.value,
                              "rossdl does parameter.values()[0].split('::') unguarded "
                              "(get_system_parameters), so a missing '::' raises ValueError "
                              "during launch generation.")

            value_node = mapping_get(item, "value")
            if value_node is None:
                self.error(node_line(key), "RM038",
                           "Parameter '%s' has no 'value:'." % key.value,
                           "RosParameter requires 'value:' value=ParameterValue "
                           "(RosSystem.xtext:114-119).")
            elif is_scalar(value_node) and value_node.value in ("True", "False", "TRUE", "FALSE"):
                self.error(node_line(value_node), "RM042",
                           "Boolean value '%s' is not lowercase." % value_node.value,
                           "terminal BOOLEAN is 'true'|'false' only (Basics.xtext:173). "
                           "Uppercase silently becomes a ParameterString "
                           "(emission-profile rule 18).")
            elif (is_scalar(value_node) and is_quoted(value_node)
                  and re.match(r"^\s*[\[{].*[\]}]\s*$", value_node.value, re.S)):
                self.error(node_line(value_node), "RM095",
                           "Parameter '%s' value is a quoted string shaped like a list/struct."
                           % key.value,
                           "A quoted \"[...]\" is a ParameterString; CheckParameterValue rejects "
                           "it against an Array/List parameter with 'Expect a list of elements' "
                           "(validator-rules.md sec 2.6). Emit value: [\"a\", \"b\"] and report "
                           "the conversion (SKILL.md sec 8) if the source .ros2 carried the "
                           "string form -- that is a source defect, not a style to preserve.")

    def check_processes(self, node, declared_nodes):
        for key, val in mapping_items(node):
            if not is_scalar(key) or not is_mapping(val):
                continue
            nodes_ref = mapping_get(val, "nodes")
            if not is_sequence(nodes_ref):
                continue
            for entry in nodes_ref.value:
                if not is_scalar(entry):
                    continue
                if entry.value not in declared_nodes:
                    self.error(node_line(entry), "RM054",
                               "Process '%s' references node '%s', which is not a component of "
                               "the system." % (key.value, entry.value),
                               "checkIfNodeInSystem raises error('The node ... is not part of "
                               "the system ...'). Declared nodes are: %s."
                               % (", ".join(sorted(declared_nodes)) or "(none)"))

    def check_connections(self, node, interfaces, system_name):
        if not is_sequence(node):
            self.error(node_line(node), "RM060",
                       "'connections:' is not a list.",
                       "Each connection is a list item: - [from_iface, to_iface] "
                       "(RosSystem.xtext:126-127).")
            return

        for item in node.value:
            line = node_line(item)

            if not is_sequence(item):
                self.error(line, "RM060",
                           "Connection is not a two-element list.",
                           "Emit only the RosSystemConnection form - [from_iface, to_iface] "
                           "referencing named RosInterface labels. The RosConnection branch "
                           "(referencing ros::Publisher directly) triggers a ClassCastException "
                           "in all three @Check methods, which cast unconditionally to "
                           "RosSystemConnectionImpl (validator-rules.md sec 3.4).")
                continue

            if len(item.value) != 2:
                self.error(line, "RM060",
                           "Connection has %d elements; exactly 2 are required."
                           % len(item.value),
                           "RosSystemConnection is '-' '[' from ',' to ']' "
                           "(RosSystem.xtext:126-127).")
                continue

            from_node, to_node = item.value
            if not (is_scalar(from_node) and is_scalar(to_node)):
                continue

            from_name = from_node.value
            to_name = to_node.value

            from_iface = self.resolve_endpoint(interfaces, from_name, from_node, "from")
            to_iface = self.resolve_endpoint(interfaces, to_name, to_node, "to")

            valid_list = ", ".join(sorted(interfaces)) or "(none declared)"

            if from_iface is None:
                self.error(node_line(from_node), "RM050",
                           "Connection 'from' endpoint '%s' is not a declared interface of "
                           "system '%s'." % (from_name, system_name),
                           "checkIfInterfaceInSystem raises error('The interface ... is not part "
                           "of the system ...'). Declared interfaces: %s." % valid_list)
                # The validator's 'to' check sits in the else branch, so a bad 'from' masks it.
                continue

            if to_iface is None:
                self.error(node_line(to_node), "RM050",
                           "Connection 'to' endpoint '%s' is not a declared interface of "
                           "system '%s'." % (to_name, system_name),
                           "checkIfInterfaceInSystem raises error(...). Declared interfaces: %s. "
                           "Note the real validator nests this check inside the 'from' else "
                           "branch, so it surfaces only once 'from' resolves." % valid_list)
                continue

            self.check_connection_direction(from_iface, to_iface, from_name, to_name,
                                            node_line(from_node))

    def resolve_endpoint(self, interfaces, name, node, role):
        """Resolve a connection endpoint name to its interface record, or None."""
        candidates = interfaces.get(name)
        if not candidates:
            return None
        if len(candidates) > 1:
            owners = ", ".join(sorted(set(c["node"] for c in candidates)))
            self.warn(node_line(node), "RM065",
                      "Connection '%s' endpoint '%s' is declared in %d nodes (%s)."
                      % (role, name, len(candidates), owners),
                      "RosSystemConnection resolves [RosInterface|EString] by name, so an "
                      "endpoint name owned by several nodes is ambiguous at link time. "
                      "Declaring the same local name in different nodes is fine on its own "
                      "(Corpus B does it throughout) -- it only becomes a problem once a "
                      "connection references it. Disambiguate by kind prefix, as Corpus B does "
                      "with sub_clock / sub_bond (emission-profile rule 28).")
        return candidates[0]

    def check_connection_direction(self, from_iface, to_iface, from_name, to_name, line):
        """Mirrors checkPortPatterns (S4)."""
        from_kind = from_iface["kind"]
        to_kind = to_iface["kind"]

        if from_kind not in ARROW_FROM_TO:
            expected_from = ", ".join(k + "->" for k in ARROW_FROM_TO)
            self.error(line, "RM051",
                       "Connection 'from' endpoint '%s' is a '%s->' interface; 'from' must be "
                       "the server/publisher side." % (from_name, from_kind),
                       "checkPortPatterns accepts only %s in the 'from' position "
                       "(RosPublisherReference, RosServiceServerReference, "
                       "RosActionServerReference). Reverse the connection." % expected_from)
            return

        expected_to = ARROW_FROM_TO[from_kind]
        if to_kind != expected_to:
            self.error(line, "RM051",
                       "Connection %s -> %s pairs '%s->' with '%s->'; expected '%s->'."
                       % (from_name, to_name, from_kind, to_kind, expected_to),
                       "The only legal pairings are pub->/sub->, ss->/sc->, as->/ac-> "
                       "(checkPortPatterns, emission-profile rule 31).")
            return

        if from_iface["target"] and to_iface["target"]:
            from_type = from_iface["target"].split("::", 1)[-1]
            to_type = to_iface["target"].split("::", 1)[-1]
            if from_type != to_type:
                self.info(line, "RM064",
                          "Connection %s -> %s links interfaces named '%s' and '%s'."
                          % (from_name, to_name, from_type, to_type),
                          "Not an error by itself. MatchPortMsgs (S5) requires both endpoints to "
                          "resolve to the SAME type object (Xtend '!==' is identity, not name "
                          "equality), which needs cross-file linking and cannot be checked "
                          "statically. Emit byte-identical type: strings on both ends.")


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def format_text(findings, files_checked, max_per_rule=10):
    """Render findings. Layout rules fire once per offending line, so a tab-indented file can
    produce hundreds of identical RM001s; cap each rule per file so the output stays readable."""
    out = []
    by_file = {}
    for finding in findings:
        by_file.setdefault(finding.file, []).append(finding)

    for path in files_checked:
        items = sorted(by_file.get(path, []), key=lambda f: f.sort_key())
        if not items:
            out.append("%s: OK" % path)
            continue
        out.append("%s" % path)

        shown_per_rule = {}
        suppressed = {}
        for finding in items:
            count = shown_per_rule.get(finding.rule, 0)
            if max_per_rule and count >= max_per_rule:
                suppressed[finding.rule] = suppressed.get(finding.rule, 0) + 1
                continue
            shown_per_rule[finding.rule] = count + 1
            out.append("  %s:%d  %-7s %s  %s"
                       % (os.path.basename(path), finding.line, finding.severity,
                          finding.rule, finding.message))
            if finding.hint:
                out.append("      hint: %s" % finding.hint)

        for rule in sorted(suppressed):
            out.append("  ... and %d more %s finding(s) in this file "
                       "(use --max-per-rule 0 to show all)." % (suppressed[rule], rule))
        out.append("")
    return "\n".join(out)


def run_hook():
    """PostToolUse hook mode, wired from hooks/hooks.json.

    Reads the hook event JSON on stdin, takes the written path from tool_input.file_path,
    lints it, and prints a blocking decision on stdout when an ERROR survives. Anything else
    (including a malformed event, an unreadable path, or a non-model extension) exits 0 in
    silence -- a linter that breaks the agent's turn on its own bad day is worse than no linter.

    PostToolUse fires AFTER the write succeeds, so blocking cannot prevent the bad file from
    reaching disk. It forces a corrective edit instead: the invariant bought is "no invalid model
    survives the turn", not "no invalid model is ever written". See README.md.
    """
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    path = (event.get("tool_input") or {}).get("file_path")
    if not path or os.path.splitext(path)[1] not in (".ros", ".ros2", ".rossystem"):
        return 0

    use_catalogue = os.environ.get("ROSMODEL_NO_CATALOGUE", "").strip() not in ("1", "true")
    try:
        findings = Linter(path, use_catalogue=use_catalogue).run()
    except Exception as exc:                                    # never break the turn
        print("rosmodel_lint: internal error on %s: %s" % (path, exc), file=sys.stderr)
        return 0

    errors = [f for f in findings if f.severity == ERROR]
    if not errors:
        return 0

    lines = ["%s: %d error(s) from rosmodel_lint.py -- fix and rewrite the file." % (path, len(errors))]
    for f in errors[:20]:
        lines.append("  line %s  %s  %s" % (f.line, f.rule, f.message))
        if f.hint:
            lines.append("      %s" % f.hint)
    if len(errors) > 20:
        lines.append("  ... and %d more." % (len(errors) - 20))

    print(json.dumps({"decision": "block", "reason": "\n".join(lines)}))
    return 0


def main(argv=None):
    if argv is None and "--hook" in sys.argv[1:]:
        return run_hook()

    parser = argparse.ArgumentParser(
        prog="rosmodel_lint.py",
        description="Static linter for RosTooling .ros, .ros2 and .rossystem model files.")
    parser.add_argument("--hook", action="store_true",
                        help="PostToolUse hook mode: read the event JSON on stdin, lint "
                             "tool_input.file_path, and emit a blocking decision on ERROR.")
    parser.add_argument("files", nargs="*",
                        help="Model files to check (globs are expanded).")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of text.")
    parser.add_argument("--min-severity", choices=[ERROR, WARNING, INFO], default=INFO,
                        help="Suppress findings less severe than this (default: INFO).")
    parser.add_argument("--quiet", action="store_true",
                        help="Print only the summary line (text mode only).")
    parser.add_argument("--max-per-rule", type=int, default=10, metavar="N",
                        help="In text output, show at most N findings per rule per file "
                             "(0 = unlimited, default 10). JSON output is never capped.")
    parser.add_argument("--no-catalogue", action="store_true",
                        help="Disable RM081-092 (assets/type_index.json + "
                             "assets/node_index.json lookups). Use for workspaces whose "
                             "message/node references are heavily project-local, where the "
                             "catalogue-absence WARNINGs would be pure noise.")
    args = parser.parse_args(argv)

    if args.hook:
        return run_hook()
    if not args.files:
        parser.error("the following arguments are required: files")

    paths = []
    for pattern in args.files:
        expanded = glob.glob(pattern, recursive=True)
        # A recursive glob such as '**/*.ros' also matches DIRECTORIES whose name ends in the
        # extension -- material/code has a plugin directory literally called
        # 'de.fraunhofer.ipa.ros'. Reading one yields a bare RM000 permission error that says
        # nothing useful, so drop directories before they reach the linter.
        expanded = [e for e in expanded if not os.path.isdir(e)]
        paths.extend(expanded if expanded else [pattern])

    threshold = SEVERITY_ORDER[args.min_severity]
    all_findings = []
    for path in paths:
        all_findings.extend(Linter(path, use_catalogue=not args.no_catalogue).run())

    shown = [f for f in all_findings if SEVERITY_ORDER.get(f.severity, 9) <= threshold]

    errors = sum(1 for f in shown if f.severity == ERROR)
    warnings = sum(1 for f in shown if f.severity == WARNING)
    infos = sum(1 for f in shown if f.severity == INFO)

    if args.json:
        payload = {
            "files_checked": paths,
            "pyyaml_available": HAVE_YAML,
            "summary": {"files": len(paths), "errors": errors,
                        "warnings": warnings, "infos": infos},
            "findings": [f.as_dict() for f in sorted(shown, key=lambda f: f.sort_key())],
        }
        print(json.dumps(payload, indent=2))
    else:
        if not args.quiet:
            print(format_text(shown, paths, max_per_rule=args.max_per_rule))
        print("%d file(s): %d error(s), %d warning(s), %d info(s)"
              % (len(paths), errors, warnings, infos))

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
