#!/usr/bin/env python3
"""extract_docker.py: golden output, determinism, read-only scan, command and apt parsers."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts  # noqa: F401 - scripts/__init__.py puts scripts/ on sys.path
import extract_docker as ed

SCRIPT = ROOT / "scripts" / "extract_docker.py"
FIXTURE = ROOT / "tests" / "fixtures" / "docker"
GOLDEN = FIXTURE / "golden" / "expected.json"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def snapshot(root):
    snap = {}
    for dp, dn, fn in os.walk(root):
        for name in dn + fn:
            p = Path(dp) / name
            st = p.stat()
            digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            snap[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size, digest)
    return snap


# ---- golden / determinism / read-only ----------------------------------------------------------

def test_golden(tmp_path):
    out = tmp_path / "out.json"
    r = run(FIXTURE, "-o", out)
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == GOLDEN.read_bytes()


def test_stdout_matches_file(tmp_path):
    out = tmp_path / "out.json"
    run(FIXTURE, "-o", out)
    assert run(FIXTURE).stdout.encode() == out.read_bytes()


def test_deterministic_bytes(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    run(FIXTURE, "-o", a)
    run(FIXTURE, "-o", b)
    assert a.read_bytes() == b.read_bytes()


def test_scan_is_read_only(tmp_path):
    before = snapshot(FIXTURE)
    assert run(FIXTURE).returncode == 0
    assert run(FIXTURE, "-o", tmp_path / "o.json").returncode == 0
    assert snapshot(FIXTURE) == before


def test_schema_and_skips():
    d = json.loads(GOLDEN.read_text())
    assert d["version"] == 1
    assert set(d) == {"version", "compose_files", "dockerfiles", "warnings"}
    paths = [c["path"] for c in d["compose_files"]] + [x["path"] for x in d["dockerfiles"]]
    assert paths == sorted(paths[:len(d["compose_files"])]) + sorted(paths[len(d["compose_files"]):])
    assert not any(p.startswith(("build/", "node_modules/")) for p in paths)
    assert not any("\\" in p for p in paths)
    assert any("cannot parse YAML" in w and w.startswith("broken/") for w in d["warnings"])
    assert any("duplicate service name 'talker'" in w for w in d["warnings"])


def test_fixture_facts():
    d = json.loads(GOLDEN.read_text())
    svc = {s["name"]: s for c in d["compose_files"] for s in c["services"]
           if c["path"] == "referenced/compose.yaml"}
    t = svc["talker"]["ros"]
    assert (t["package"], t["executable"]) == ("demo_nodes_cpp", "talker")
    assert t["ros_args"]["namespace"] == "/robot" and t["ros_args"]["node_name"] == "talker_a"
    assert t["ros_args"]["remaps"] == [["chatter", "/robot/chatter"]]
    assert t["ros_args"]["params_files"] == ["/config/talker.yaml"]
    assert svc["launcher"]["ros"]["launch_file"] == "x.launch.py"
    assert svc["launcher"]["dockerfile_ref"] == "referenced/docker/Dockerfile.ros"
    assert svc["talker"]["environment"]["ROS_DOMAIN_ID"] == "7"
    tag = [s for c in d["compose_files"] if c["path"].startswith("strings/")
           for s in c["services"] if s["name"] == "talker"][0]
    assert tag["image"] == "myrepo/ros-demo:${TAG:-latest}"
    assert "image uses ${TAG:-latest} (default: latest)" in tag["unresolved"]
    df = d["dockerfiles"][0]
    assert df["ros_distro"] == "jazzy" and [s["name"] for s in df["stages"]] == ["base", "runtime"]


def test_bad_args_exit_2(tmp_path):
    assert run().returncode == 2
    assert run(tmp_path / "missing").returncode == 2
    assert run(tmp_path, "--bogus").returncode == 2


def test_empty_root(tmp_path):
    d = ed.scan(str(tmp_path))
    assert d == {"version": 1, "compose_files": [], "dockerfiles": [], "warnings": []}


# ---- command parser ----------------------------------------------------------------------------

def test_string_command_split():
    assert ed.parse_command("ros2 run a b --ros-args -p 'x:=hello world'") == [
        "ros2", "run", "a", "b", "--ros-args", "-p", "x:=hello world"]
    assert ed.parse_command(["a", 1]) == ["a", "1"]
    assert ed.parse_command(None) is None
    assert ed.parse_command("echo 'unbalanced") == ["echo", "'unbalanced"]


def test_ros_run_full():
    r = ed.detect_ros(ed.parse_command(
        "ros2 run --prefix 'gdb -ex run --args' pkg exe --ros-args --log-level debug "
        "-r a:=b --remap node:c:=d -r __ns:=/ns -r __node:=n -p k:=1 --param s:=x "
        "--params-file p1.yaml --params-file p2.yaml -- user_arg"))
    assert (r["package"], r["executable"]) == ("pkg", "exe")
    a = r["ros_args"]
    assert a["namespace"] == "/ns" and a["node_name"] == "n"
    assert a["remaps"] == [["a", "b"], ["node:c", "d"]]
    assert a["params"] == {"k": "1", "s": "x"}
    assert a["params_files"] == ["p1.yaml", "p2.yaml"]


def test_ros_prefixed_node_remaps():
    r = ed.detect_ros(["ros2", "run", "p", "e", "--ros-args", "-r", "foo:__node:=bar",
                       "-r", "foo:__ns:=/z"])
    assert r["ros_args"]["node_name"] == "bar" and r["ros_args"]["namespace"] == "/z"


def test_ros_launch_forms():
    r = ed.detect_ros(["ros2", "launch", "-d", "pkg", "f.launch.py", "a:=b"])
    assert (r["package"], r["launch_file"], r["executable"]) == ("pkg", "f.launch.py", None)
    r = ed.detect_ros(["ros2", "launch", "/abs/f.launch.py"])
    assert r["package"] is None and r["launch_file"] == "/abs/f.launch.py"


@pytest.mark.parametrize("argv", [
    ["bash", "-c", "source /opt/ros/jazzy/setup.bash && ros2 run p e"],
    ["/bin/bash", "-lc", "cd /ws; . install/setup.sh; exec ros2 run p e"],
    ["sh", "-c", "ros2 run p e | tee log"],
    ["bash", "-c", "ROS_LOG_DIR=/x sudo ros2 run p e"],
    ["bash", "-c", "echo 'x && y' && ros2 run p e"],
])
def test_ros_in_wrapper(argv):
    r = ed.detect_ros(argv)
    assert (r["package"], r["executable"]) == ("p", "e")


@pytest.mark.parametrize("argv", [
    [], ["rviz2"], ["bash", "-c", "echo ros2 run p e"], ["ros2"], ["ros2", "run", "p"],
    ["ros2", "topic", "list"], ["bash", "-c", "sleep 1"], ["echo", "ros2", "run", "p", "e"],
])
def test_not_ros(argv):
    assert ed.detect_ros(argv) is None


def test_variables_in_ros_command_are_verbatim():
    r = ed.detect_ros(["ros2", "run", "${PKG:-demo}", "node"])
    assert r["package"] == "${PKG:-demo}"
    assert ed.var_notes("a ${PKG:-demo} $HOME ${X} $$escaped") == [
        "${PKG:-demo} (default: demo)", "$HOME", "${X}"]


# ---- apt parser --------------------------------------------------------------------------------

@pytest.mark.parametrize("run_text,expected", [
    ("apt-get install -y curl git", ["curl", "git"]),
    ("apt install --no-install-recommends -y b a", ["a", "b"]),
    ("apt-get -y --no-install-recommends install pkg", ["pkg"]),
    ("apt-get update && apt-get install -y a && rm -rf /var/lib/apt/lists/*", ["a"]),
    ("apt-get install -yqq -o Dpkg::Options::=--force-confnew a", ["a"]),
    ("apt-get install -t jammy-backports a", ["a"]),
    ("apt-get install git=1:2.43.0-1 curl/jammy libfoo:amd64", ["curl", "git", "libfoo:amd64"]),
    ("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y a; echo done", ["a"]),
    ("apt-get install -y a a b", ["a", "b"]),
    ("apt-get remove -y a", []),
    ("apt-get update", []),
    ("apt-get install -y ros-${ROS_DISTRO}-foo", ["ros-${ROS_DISTRO}-foo"]),
    ('apt-get install -y "a" \'b\'', ["a", "b"]),
    ('["apt-get", "install", "-y", "a"]', ["a"]),
    ("apt-get install -y a > /dev/null 2>&1 && echo ok", ["a"]),
    ("apt-get install -y $(cat pkgs.txt)", ["$"]),
    ("pip install foo", []),
])
def test_apt(run_text, expected):
    got = ed.parse_apt(run_text)
    if expected == ["$"]:
        assert isinstance(got, list)  # command substitution: must not raise, nothing asserted
    else:
        assert got == expected


def test_dockerfile_continuations_and_heredoc():
    text = ("FROM ros:humble-ros-base\n"
            "RUN apt-get update \\\n  && apt-get install -y \\\n  a \\\n  # c\n  b\n"
            "RUN <<EOF\napt-get install -y hidden\nEOF\n"
            "ENV A=1 B=\"two words\"\nENV C three words\nARG X\nARG Y=2\n")
    d = ed.parse_dockerfile(text, "Dockerfile")
    assert d["apt"] == ["a", "b"]
    assert d["env"] == {"A": "1", "B": "two words", "C": "three words"}
    assert d["args"] == {"X": None, "Y": "2"}
    assert d["ros_distro"] == "humble"


def test_ros_distro_fallbacks():
    assert ed.parse_dockerfile("FROM ubuntu:24.04\nARG ROS_DISTRO=jazzy\n", "D")["ros_distro"] == "jazzy"
    assert ed.parse_dockerfile("FROM ubuntu:24.04\nENV ROS_DISTRO=kilted\n", "D")["ros_distro"] == "kilted"
    assert ed.parse_dockerfile("FROM ubuntu:24.04\n", "D")["ros_distro"] is None
    d = ed.parse_dockerfile("ARG ROS_DISTRO=jazzy\nFROM ros:${ROS_DISTRO}\n", "D")
    assert d["ros_distro"] == "jazzy"
    assert d["unresolved"] == ["base image uses ${ROS_DISTRO}"]
