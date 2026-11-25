#!/usr/bin/env python3
"""
extract_docker.py -- scan a directory tree for Docker Compose files and Dockerfiles and emit
what they LITERALLY say about a ROS 2 deployment, as deterministic JSON.

WHY THIS EXISTS: a .rossystem names node instances and how they are wired; the deployment half
of that story -- which image runs which `ros2 run` / `ros2 launch`, with which remaps, namespace
and parameter files, on which network/IPC mode, from which Dockerfile and apt packages -- lives
in compose.yaml and Dockerfile, not in the ROS sources. Reading it by hand (or by LLM) is slow
and drifts; this script reads it once, mechanically, and an agent or a human decides what to
turn into a model. It proposes nothing and edits nothing.

Literal-only discipline (same as extract_rossystem.py): files are read as TEXT. Compose files go
through yaml.safe_load; Dockerfiles are tokenised by hand. Nothing is executed, docker is never
invoked, no environment or .env file is consulted. A value that depends on interpolation
(`${TAG}`, `${TAG:-latest}`, `$HOME`) is kept verbatim and named in that entry's `unresolved`
list, with its default when it has one. A command that is not recognisably `ros2 run` or
`ros2 launch` (possibly inside `bash -c "source ... && ros2 ..."`) yields `ros: null` plus an
`unresolved` note -- never a guess. Parameter values from `-p k:=v` are kept as the literal text.

Output is deterministic: paths are relative to <root-dir> with `/`, sorted; keys are sorted; no
timestamps. Skipped directories: .git, build, install, log, node_modules.

Usage:
    python3 extract_docker.py <root-dir> [-o OUT.json]

    Without -o the JSON goes to stdout. The scanned tree is only ever read.

Exit status: 0 on success (warnings, e.g. an unparseable compose file, do not change it);
2 on bad arguments or a <root-dir> that is not a directory.

Schema (version 1):
  {"version": 1,
   "compose_files": [{"path", "include", "services": [{"name", "image", "build", "command",
       "entrypoint", "environment", "env_file", "network_mode", "ipc", "volumes", "depends_on",
       "profiles", "ros", "dockerfile_ref", "unresolved"}]}],
   "dockerfiles": [{"path", "stages", "ros_distro", "apt", "env", "args", "workdir",
       "entrypoint", "cmd", "copies", "unresolved"}],
   "warnings": [...]}
"""

import argparse
import fnmatch
import json
import os
import posixpath
import re
import shlex
import sys

import yaml

SCHEMA_VERSION = 1
SKIP_DIRS = {".git", "build", "install", "log", "node_modules"}
COMPOSE_NAMES = {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
COMPOSE_PATTERNS = ("compose.*.yaml", "compose.*.yml", "docker-compose.*.yaml",
                    "docker-compose.*.yml")
SHELLS = {"bash", "sh", "zsh", "dash", "ash"}
SEPARATORS = {"&&", "||", ";", "|", "&"}

_VAR_RE = re.compile(
    r"(?<!\$)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+=])([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------

def is_compose_name(name):
    return name in COMPOSE_NAMES or any(fnmatch.fnmatchcase(name, p) for p in COMPOSE_PATTERNS)


def is_dockerfile_name(name):
    return name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile")


def var_notes(text):
    """Return ['${TAG:-latest} (default: latest)', ...] for every interpolation in `text`."""
    notes = []
    if not isinstance(text, str):
        return notes
    for m in _VAR_RE.finditer(text):
        raw = m.group(0)
        if m.group(1) and m.group(2) in ("-", ":-"):
            notes.append("%s (default: %s)" % (raw, m.group(3)))
        else:
            notes.append(raw)
    return notes


def split_words(text):
    """shlex.split with a whitespace fallback for unbalanced quotes."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def tokenize_shell(script):
    """Tokenise a shell script so that `&&`, `||`, `;`, `|` come out as their own tokens."""
    try:
        lex = shlex.shlex(script, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return script.split()


def split_segments(tokens):
    segs, cur = [], []
    for t in tokens:
        if t in SEPARATORS or (t and all(c in "&|;" for c in t)):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


# ---------------------------------------------------------------------------------------------
# command / ROS parsing
# ---------------------------------------------------------------------------------------------

def parse_command(value):
    """Compose command/entrypoint -> list of strings (string form is split with shlex)."""
    if value is None:
        return None
    if isinstance(value, str):
        return split_words(value)
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def new_ros_args():
    return {"namespace": None, "node_name": None, "remaps": [], "params_files": [], "params": {}}


_ROS_ARG_VALUE_FLAGS = {"-e", "--enclave", "--log-level", "--log-config-file"}


def parse_ros_args(tokens):
    """Parse the tokens AFTER `--ros-args` (up to a bare `--`)."""
    out = new_ros_args()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if t == "--":
            break
        if t in ("-r", "--remap") and nxt is not None:
            lhs, sep, rhs = nxt.partition(":=")
            if sep:
                if lhs == "__ns" or lhs.endswith(":__ns"):
                    out["namespace"] = rhs
                elif lhs == "__node" or lhs.endswith(":__node"):
                    out["node_name"] = rhs
                else:
                    out["remaps"].append([lhs, rhs])
            i += 2
        elif t in ("-p", "--param") and nxt is not None:
            k, sep, v = nxt.partition(":=")
            if sep:
                out["params"][k] = v
            i += 2
        elif t == "--params-file" and nxt is not None:
            out["params_files"].append(nxt)
            i += 2
        elif t in _ROS_ARG_VALUE_FLAGS and nxt is not None:
            i += 2
        elif t == "--ros-args":
            i += 1
        else:
            i += 1
    return out


def ros_from_segment(seg):
    """Given a token segment, find a `ros2 run|launch` invocation. Returns dict or None."""
    idx = 0
    while idx < len(seg) and (seg[idx] in ("exec", "sudo", "env", "time", "nohup")
                              or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", seg[idx])):
        idx += 1
    if idx >= len(seg) or posixpath.basename(seg[idx]) != "ros2":
        return None
    if idx + 1 >= len(seg):
        return None
    sub = seg[idx + 1]
    rest = seg[idx + 2:]
    if sub == "run":
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 2 if rest[j] == "--prefix" else 1
        if j + 1 >= len(rest):
            return None
        pkg, exe, tail = rest[j], rest[j + 1], rest[j + 2:]
        args = new_ros_args()
        if "--ros-args" in tail:
            args = parse_ros_args(tail[tail.index("--ros-args") + 1:])
        return {"package": pkg, "executable": exe, "launch_file": None, "ros_args": args}
    if sub == "launch":
        pos = [t for t in rest if not t.startswith("-") and ":=" not in t]
        if not pos:
            return None
        if len(pos) == 1:
            return {"package": None, "executable": None, "launch_file": pos[0],
                    "ros_args": new_ros_args()}
        return {"package": pos[0], "executable": None, "launch_file": pos[1],
                "ros_args": new_ros_args()}
    return None


def detect_ros(argv):
    """Find `ros2 run`/`ros2 launch` in an argv list, directly or inside `bash -c "..."`."""
    if not argv:
        return None
    segments = []
    if posixpath.basename(argv[0]) in SHELLS:
        for n, t in enumerate(argv[1:], start=1):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", t) and n + 1 < len(argv):
                segments = split_segments(tokenize_shell(argv[n + 1]))
                break
    if not segments:
        segments = split_segments(argv)
    for seg in segments:
        found = ros_from_segment(seg)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------------------------
# apt parsing
# ---------------------------------------------------------------------------------------------

_APT_OPTS_WITH_VALUE = {"-o", "-t", "-c"}


def parse_apt(run_text):
    """Return the sorted set of packages named by apt-get/apt `install` in a RUN body."""
    text = run_text.strip()
    if text.startswith("["):
        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                text = " ".join(str(a) for a in arr)
        except ValueError:
            pass
    pkgs = set()
    for seg in split_segments(tokenize_shell(text)):
        idx = None
        for n, t in enumerate(seg):
            if posixpath.basename(t) in ("apt-get", "apt"):
                idx = n
                break
        if idx is None:
            continue
        j = idx + 1
        while j < len(seg) and seg[j].startswith("-"):
            j += 2 if seg[j] in _APT_OPTS_WITH_VALUE else 1
        if j >= len(seg) or seg[j] != "install":
            continue
        j += 1
        while j < len(seg):
            t = seg[j]
            if t.startswith("-"):
                j += 2 if t in _APT_OPTS_WITH_VALUE else 1
                continue
            if t and all(c in "<>()" for c in t):
                break
            name = t.split("=", 1)[0].split("/", 1)[0] if not t.startswith(("./", "/")) else t
            if name:
                pkgs.add(name)
            j += 1
    return sorted(pkgs)


# ---------------------------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------------------------

def logical_lines(text):
    """Join `\\` continuations (dropping comment lines inside one) and skip heredoc bodies."""
    out, cur, heredoc = [], None, None
    for raw in text.splitlines():
        line = raw.rstrip()
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        if cur is None:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cur = ""
        elif line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        cur += line
        out.append(cur.strip())
        m = re.search(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", cur)
        if m and cur.split(None, 1)[0].upper() in ("RUN", "COPY", "ADD"):
            heredoc = m.group(1)
        cur = None
    if cur is not None:
        out.append(cur.strip())
    return out


def _json_list(rest):
    rest = rest.strip()
    if rest.startswith("["):
        try:
            v = json.loads(rest)
            if isinstance(v, list):
                return [str(x) for x in v]
        except ValueError:
            pass
    return None


def parse_env(rest):
    """ENV body -> list of (key, value); handles `K=V K2="V 2"` and `K V words`."""
    rest = rest.strip()
    if not rest:
        return []
    first = rest.split(None, 1)[0]
    if "=" in first:
        pairs = []
        for tok in split_words(rest):
            k, sep, v = tok.partition("=")
            if sep:
                pairs.append((k, v))
        return pairs
    parts = rest.split(None, 1)
    val = parts[1].strip() if len(parts) > 1 else ""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return [(parts[0], val)]


def distro_from_image(image):
    m = re.search(r"(?:^|/)ros:([a-z]+)(?:[-@:].*)?$", image)
    return m.group(1) if m else None


def parse_dockerfile(text, rel_path):
    stages, apt, env, args, copies, unresolved = [], set(), {}, {}, [], []
    workdir = entrypoint = cmd = None
    base_distro = None
    for line in logical_lines(text):
        parts = line.split(None, 1)
        instr = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""
        if instr == "FROM":
            toks = [t for t in split_words(rest) if not t.startswith("--")]
            if not toks:
                continue
            name = toks[2] if len(toks) >= 3 and toks[1].lower() == "as" else None
            stages.append({"name": name, "base_image": toks[0]})
            if base_distro is None:
                base_distro = distro_from_image(toks[0])
            for note in var_notes(toks[0]):
                unresolved.append("base image uses " + note)
        elif instr == "ARG":
            for tok in split_words(rest):
                k, sep, v = tok.partition("=")
                args[k] = v if sep else None
        elif instr == "ENV":
            for k, v in parse_env(rest):
                env[k] = v
        elif instr == "WORKDIR":
            workdir = rest.strip()
            for note in var_notes(workdir):
                unresolved.append("workdir uses " + note)
        elif instr == "ENTRYPOINT":
            entrypoint = _json_list(rest) or ["/bin/sh", "-c", rest.strip()]
        elif instr == "CMD":
            cmd = _json_list(rest) or ["/bin/sh", "-c", rest.strip()]
        elif instr in ("COPY", "ADD"):
            frm = None
            jl = _json_list(rest)
            toks = jl if jl is not None else split_words(rest)
            plain = []
            for t in toks:
                if t.startswith("--from="):
                    frm = t[len("--from="):]
                elif t.startswith("--"):
                    continue
                else:
                    plain.append(t)
            if len(plain) >= 2:
                for src in plain[:-1]:
                    copies.append({"src": src, "dest": plain[-1], "from": frm})
        elif instr == "RUN":
            for pkg in parse_apt(rest):
                apt.add(pkg)
    for pkg in sorted(apt):
        for note in var_notes(pkg):
            unresolved.append("apt package %s uses %s" % (pkg, note))
    distro = base_distro
    if distro is None:
        distro = env.get("ROS_DISTRO") or args.get("ROS_DISTRO")
    return {
        "path": rel_path,
        "stages": stages,
        "ros_distro": distro,
        "apt": sorted(apt),
        "env": env,
        "args": args,
        "workdir": workdir,
        "entrypoint": entrypoint,
        "cmd": cmd,
        "copies": copies,
        "unresolved": sorted(set(unresolved)),
    }


# ---------------------------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------------------------

def _scalar(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def parse_environment(value):
    if isinstance(value, dict):
        return {str(k): _scalar(v) for k, v in value.items()}
    out = {}
    if isinstance(value, list):
        for item in value:
            k, sep, v = str(item).partition("=")
            out[k] = v if sep else None
    return out


def parse_env_file(value):
    if isinstance(value, str):
        return [value]
    out = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "path" in item:
                out.append(str(item["path"]))
            elif not isinstance(item, (dict, list)):
                out.append(str(item))
    return out


def parse_volumes(value):
    out = []
    if not isinstance(value, list):
        return out
    for v in value:
        if isinstance(v, dict):
            s = ":".join(str(x) for x in (v.get("source"), v.get("target")) if x is not None)
            if v.get("read_only"):
                s += ":ro"
            out.append(s)
        else:
            out.append(str(v))
    return out


def parse_build(value):
    if value is None:
        return None
    if isinstance(value, str):
        return {"context": value, "dockerfile": None, "args": {}}
    if isinstance(value, dict):
        args = value.get("args")
        return {"context": _scalar(value.get("context")),
                "dockerfile": _scalar(value.get("dockerfile")),
                "args": parse_environment(args) if args is not None else {}}
    return None


def parse_include(value):
    out = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            p = item.get("path")
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, list):
                out.extend(str(x) for x in p)
    return out


def build_service(name, svc, compose_dir, dockerfile_paths):
    if not isinstance(svc, dict):
        svc = {}
    unresolved = []

    def note(field, text):
        for n in var_notes(text):
            unresolved.append("%s uses %s" % (field, n))

    image = _scalar(svc.get("image"))
    note("image", image)
    build = parse_build(svc.get("build"))
    command = parse_command(svc.get("command"))
    entrypoint = parse_command(svc.get("entrypoint"))
    environment = parse_environment(svc.get("environment"))
    env_file = parse_env_file(svc.get("env_file"))
    volumes = parse_volumes(svc.get("volumes"))

    if build:
        note("build.context", build["context"])
        note("build.dockerfile", build["dockerfile"])
    for field, argv in (("command", command), ("entrypoint", entrypoint)):
        for tok in argv or []:
            note(field, tok)
    for k in sorted(environment):
        note("environment.%s" % k, environment[k])
    for v in volumes:
        note("volume", v)
    for f in env_file:
        note("env_file", f)

    deps = svc.get("depends_on")
    if isinstance(deps, dict):
        depends_on = sorted(str(k) for k in deps)
    elif isinstance(deps, list):
        depends_on = sorted(str(d) for d in deps)
    else:
        depends_on = []
    profiles = svc.get("profiles")
    profiles = [str(p) for p in profiles] if isinstance(profiles, list) else []

    ros = detect_ros(command) if command else None
    if ros is None and entrypoint:
        ros = detect_ros(entrypoint + (command or []))
    if ros is None and (command or entrypoint):
        unresolved.append("command is not a recognised ros2 run/launch")

    ref = None
    if build and build["context"] is not None and "$" not in (build["context"] or ""):
        ctx = posixpath.normpath(posixpath.join(compose_dir, build["context"]))
        df = build["dockerfile"] or "Dockerfile"
        if "$" not in df:
            cand = posixpath.normpath(posixpath.join(ctx, df))
            if cand in dockerfile_paths:
                ref = cand
            else:
                unresolved.append("dockerfile %s not among scanned files" % cand)

    return {
        "name": name,
        "image": image,
        "build": build,
        "command": command,
        "entrypoint": entrypoint,
        "environment": environment,
        "env_file": env_file,
        "network_mode": _scalar(svc.get("network_mode")),
        "ipc": _scalar(svc.get("ipc")),
        "volumes": volumes,
        "depends_on": depends_on,
        "profiles": profiles,
        "ros": ros,
        "dockerfile_ref": ref,
        "unresolved": sorted(set(unresolved)),
    }


# ---------------------------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------------------------

def find_files(root):
    compose, dockerfiles = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            if is_compose_name(fn):
                compose.append(rel)
            if is_dockerfile_name(fn):
                dockerfiles.append(rel)
    return sorted(compose), sorted(dockerfiles)


def read_text(root, rel):
    with open(os.path.join(root, *rel.split("/")), "r", encoding="utf-8") as fh:
        return fh.read()


def scan(root):
    compose_paths, docker_paths = find_files(root)
    warnings, compose_out, docker_out = [], [], []

    for rel in docker_paths:
        try:
            docker_out.append(parse_dockerfile(read_text(root, rel), rel))
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append("%s: cannot read Dockerfile: %s" % (rel, exc))

    docker_set = {d["path"] for d in docker_out}
    seen = {}
    for rel in compose_paths:
        try:
            data = yaml.safe_load(read_text(root, rel))
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append("%s: cannot read compose file: %s" % (rel, exc))
            continue
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = " (line %d)" % (mark.line + 1) if mark else ""
            warnings.append("%s: cannot parse YAML%s: %s" % (
                rel, where, getattr(exc, "problem", None) or "invalid YAML"))
            continue
        if not isinstance(data, dict):
            warnings.append("%s: top level is not a mapping; skipped" % rel)
            continue
        cdir = posixpath.dirname(rel) or "."
        services = data.get("services")
        services = services if isinstance(services, dict) else {}
        svc_out = [build_service(str(n), services[n], cdir, docker_set)
                   for n in sorted(services, key=str)]
        for s in svc_out:
            seen.setdefault(s["name"], []).append(rel)
        compose_out.append({"path": rel, "include": parse_include(data.get("include")),
                            "services": svc_out})

    for name in sorted(seen):
        if len(seen[name]) > 1:
            warnings.append("duplicate service name '%s' in %s" % (name, ", ".join(seen[name])))

    return {"version": SCHEMA_VERSION, "compose_files": compose_out, "dockerfiles": docker_out,
            "warnings": sorted(warnings)}


def render(result):
    return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scan compose files and Dockerfiles under a directory; emit JSON.")
    ap.add_argument("root", help="directory to scan")
    ap.add_argument("-o", "--out", help="write JSON here instead of stdout")
    ns = ap.parse_args(argv)  # argparse exits with status 2 on bad arguments
    if not os.path.isdir(ns.root):
        print("extract_docker.py: not a directory: %s" % ns.root, file=sys.stderr)
        return 2
    text = render(scan(ns.root))
    if ns.out:
        with open(ns.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
