#!/usr/bin/env python3
"""Pytest suite for lifecycle node extraction, active_in states, and scoped rossystem connections."""

import os
from pathlib import Path
import tempfile
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extract_ros2_interfaces
import extract_rossystem
import rosmodel_lint


def test_cpp_lifecycle_extraction(tmp_path):
    pkg_dir = tmp_path / "cpp_lifecycle_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "package.xml").write_text("""<?xml version="1.0"?>
<package format="3">
  <name>cpp_lifecycle_pkg</name>
  <version>0.0.1</version>
  <description>Lifecycle test package</description>
  <maintainer email="test@test.com">test</maintainer>
  <license>Apache-2.0</license>
  <buildtool_depend>ament_cmake</buildtool_depend>
</package>
""")
    (pkg_dir / "CMakeLists.txt").write_text("""
cmake_minimum_required(VERSION 3.8)
project(cpp_lifecycle_pkg)
add_executable(managed_node_exec src/managed_node.cpp)
""")
    src_dir = pkg_dir / "src"
    src_dir.mkdir()
    (src_dir / "managed_node.cpp").write_text("""
#include <rclcpp_lifecycle/lifecycle_node.hpp>
#include <std_msgs/msg/string.hpp>

class ManagedNode : public rclcpp_lifecycle::LifecycleNode {
public:
  ManagedNode() : rclcpp_lifecycle::LifecycleNode("managed_node") {
    pub_ = create_publisher<std_msgs::msg::String>("lifecycle_chatter", 10);
  }
private:
  rclcpp_lifecycle::LifecyclePublisher<std_msgs::msg::String>::SharedPtr pub_;
};
""")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    resolver = extract_ros2_interfaces.TypeResolver([str(pkg_dir)])
    pkg = extract_ros2_interfaces.PackageDraft("cpp_lifecycle_pkg", str(pkg_dir), "ament_cmake")
    pkg.targets[str((src_dir / "managed_node.cpp").resolve())] = "managed_node_exec"

    parser = extract_ros2_interfaces._load_cpp_parser()
    assert parser is not None, "tree_sitter_cpp must be installed"
    extract_ros2_interfaces.extract_cpp(pkg, [str(src_dir / "managed_node.cpp")], parser, resolver, str(pkg_dir))
    extract_ros2_interfaces.resolve_fallback_artifacts(pkg)

    emitted = extract_ros2_interfaces.emit_package(pkg, resolver, str(pkg_dir), emit_qos=True)
    assert "lifecycle: true" in emitted
    assert "active_in: [Active]" in emitted
    assert "managed_node_exec:" in emitted
    assert "node: managed_node" in emitted

    # Verify that rosmodel_lint validates the emitted lifecycle model without errors
    model_file = out_dir / "cpp_lifecycle_pkg.ros2"
    model_file.write_text(emitted)
    linter = rosmodel_lint.Linter(str(model_file))
    linter.run()
    errors = [f for f in linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not errors, f"Emitted lifecycle model has lint errors: {errors}"


def test_python_lifecycle_extraction(tmp_path):
    pkg_dir = tmp_path / "py_lifecycle_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "package.xml").write_text("""<?xml version="1.0"?>
<package format="3">
  <name>py_lifecycle_pkg</name>
  <version>0.0.1</version>
  <description>Py Lifecycle test</description>
  <maintainer email="test@test.com">test</maintainer>
  <license>Apache-2.0</license>
</package>
""")
    src_file = pkg_dir / "py_lifecycle_node.py"
    src_file.write_text("""
import rclpy
from rclpy.lifecycle import Node as LifecycleNode
from std_msgs.msg import String

class PyManagedNode(LifecycleNode):
    def __init__(self):
        super().__init__('py_managed_node')
        self.pub = self.create_publisher(String, 'py_chatter', 10)
""")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    resolver = extract_ros2_interfaces.TypeResolver([str(pkg_dir)])
    pkg = extract_ros2_interfaces.PackageDraft("py_lifecycle_pkg", str(pkg_dir), "ament_python")

    extract_ros2_interfaces.extract_python(pkg, str(src_file), resolver, str(pkg_dir))
    extract_ros2_interfaces.resolve_fallback_artifacts(pkg)

    emitted = extract_ros2_interfaces.emit_package(pkg, resolver, str(pkg_dir), emit_qos=True)
    assert "lifecycle: true" in emitted
    assert "active_in: [Active]" in emitted
    assert "node: py_managed_node" in emitted

    model_file = out_dir / "py_lifecycle_pkg.ros2"
    model_file.write_text(emitted)
    linter = rosmodel_lint.Linter(str(model_file))
    linter.run()
    errors = [f for f in linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not errors, f"Emitted python lifecycle model has lint errors: {errors}"


def test_linter_lifecycle_validation(tmp_path):
    # 1. active_in on a non-lifecycle node is rejected (RM036)
    bad_ros2 = tmp_path / "bad_active_in.ros2"
    bad_ros2.write_text("""bad_pkg:
  artifacts:
    bad_node:
      node: bad_node
      publishers:
        'chatter':
          type: 'std_msgs/msg/String'
          active_in: [Active]
""")
    linter = rosmodel_lint.Linter(str(bad_ros2))
    linter.run()
    rm036 = [f for f in linter.findings if f.rule == "RM036" and "active_in" in f.message]
    assert rm036, "active_in on non-lifecycle node must trigger RM036"

    # 2. invalid lifecycle state is rejected (RM033)
    bad_state = tmp_path / "bad_state.ros2"
    bad_state.write_text("""bad_pkg:
  artifacts:
    bad_node:
      node: bad_node
      lifecycle: true
      publishers:
        'chatter':
          type: 'std_msgs/msg/String'
          active_in: [InvalidState]
""")
    linter = rosmodel_lint.Linter(str(bad_state))
    linter.run()
    rm033 = [f for f in linter.findings if f.rule == "RM033" and "InvalidState" in f.message]
    assert rm033, "invalid lifecycle state must trigger RM033"

    # 3. quoted lifecycle value is rejected (RM033)
    quoted_lc = tmp_path / "quoted_lc.ros2"
    quoted_lc.write_text("""bad_pkg:
  artifacts:
    bad_node:
      node: bad_node
      lifecycle: "true"
""")
    linter = rosmodel_lint.Linter(str(quoted_lc))
    linter.run()
    rm033 = [f for f in linter.findings if f.rule == "RM033" and "quoted" in f.message]
    assert rm033, "quoted lifecycle value must trigger RM033"


def test_scoped_connections_and_candidates():
    # Test connection_candidates in extract_rossystem
    class MockSysDraft:
        def __init__(self):
            self.nodes = [
                {
                    "label": "slam",
                    "interfaces": [
                        {"label": "map", "kind": "pub", "target": "slam::map", "type": "nav_msgs/msg/OccupancyGrid"}
                    ]
                },
                {
                    "label": "planner",
                    "interfaces": [
                        {"label": "map", "kind": "sub", "target": "planner::map", "type": "nav_msgs/msg/OccupancyGrid"}
                    ]
                },
                {
                    "label": "unique_pub",
                    "interfaces": [
                        {"label": "unique_out", "kind": "pub", "target": "u::out", "type": "std_msgs/msg/String"}
                    ]
                },
                {
                    "label": "unique_sub",
                    "interfaces": [
                        {"label": "unique_in", "kind": "sub", "target": "u::out", "type": "std_msgs/msg/String"}
                    ]
                }
            ]

    sys_draft = MockSysDraft()
    candidates = extract_rossystem.connection_candidates(sys_draft)
    assert len(candidates) == 2

    # Duplicate interface label 'map' must be qualified with node name
    map_cand = [c for c in candidates if c["topic"] == "map"][0]
    assert map_cand["from"] == "slam.map"
    assert map_cand["to"] == "planner.map"

    # Unique interface labels must stay unqualified
    unique_cand = [c for c in candidates if c["topic"] == "out"][0]
    assert unique_cand["from"] == "unique_out"
    assert unique_cand["to"] == "unique_in"


def test_linter_scoped_connections(tmp_path):
    # System with duplicate interface labels 'map'
    rossystem_file = tmp_path / "scoped_test.rossystem"
    rossystem_file.write_text("""scoped_test:
  fromFile: "scoped_test/launch/test.launch.py"  # caller-supplied
  nodes:
    "slam":
      from: "slam_pkg.slam_node"
      interfaces:
        - "map": pub-> "slam_art::map"
    "nav2":
      from: "nav2_pkg.nav2_node"
      interfaces:
        - "map": sub-> "nav2_art::map"
    "costmap":
      from: "costmap_pkg.costmap_node"
      interfaces:
        - "map": sub-> "costmap_art::map"
  connections:
    - ["slam.map", "nav2.map"]
    - ["slam.map", "costmap.map"]
""")

    linter = rosmodel_lint.Linter(str(rossystem_file))
    linter.run()
    errors = [f for f in linter.findings if f.severity == rosmodel_lint.ERROR]
    # Qualified connections must NOT produce RM050 (unknown endpoint)
    assert not errors, f"Qualified endpoints produced errors: {errors}"
    # Qualified connections must NOT produce RM065 (ambiguous endpoint)
    rm065 = [f for f in linter.findings if f.rule == "RM065"]
    assert not rm065, f"Qualified endpoints produced RM065: {rm065}"

    # Now verify that bare 'map' triggers RM065 warning
    ambiguous_file = tmp_path / "ambiguous_test.rossystem"
    ambiguous_file.write_text("""ambiguous_test:
  fromFile: "test/launch/test.launch.py"  # caller-supplied
  nodes:
    "slam":
      from: "slam_pkg.slam_node"
      interfaces:
        - "map": pub-> "slam_art::map"
    "nav2":
      from: "nav2_pkg.nav2_node"
      interfaces:
        - "map": sub-> "nav2_art::map"
  connections:
    - ["map", "map"]
""")
    linter_ambig = rosmodel_lint.Linter(str(ambiguous_file))
    linter_ambig.run()
    rm065_ambig = [f for f in linter_ambig.findings if f.rule == "RM065"]
    assert len(rm065_ambig) >= 1, "Unqualified duplicate endpoint must produce RM065"
    assert "slam.map" in rm065_ambig[0].hint


def test_linter_semicolon_connections(tmp_path):
    # System using various semicolon connection formats
    semicolon_sys = tmp_path / "semicolon_test.rossystem"
    semicolon_sys.write_text("""semicolon_test:
  fromFile: "test/launch/test.launch.py"  # caller-supplied
  nodes:
    "slam":
      from: "slam_pkg.slam_node"  # project-local
      interfaces:
        - "map": pub-> "slam_art::map"
        - "scan": sub-> "slam_art::scan"
    "nav2":
      from: "nav2_pkg.nav2_node"  # project-local
      interfaces:
        - "map": sub-> "nav2_art::map"
        - "scan": pub-> "nav2_art::scan"
  connections:
    - [slam.map; nav2.map]
    - ["nav2.scan"; "slam.scan"]
    - [slam.map; nav2.map];
    - [nav2.scan, slam.scan];
    - slam.map; nav2.map
    - nav2.scan; slam.scan;
""")

    linter = rosmodel_lint.Linter(str(semicolon_sys))
    linter.run()
    errors = [f for f in linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not errors, f"Semicolon connection formats produced errors: {errors}"

    # Also test error cases
    bad_sys = tmp_path / "bad_semicolon_test.rossystem"
    bad_sys.write_text("""bad_semicolon_test:
  fromFile: "test/launch/test.launch.py"  # caller-supplied
  nodes:
    "slam":
      from: "slam_pkg.slam_node"  # project-local
      interfaces:
        - "map": pub-> "slam_art::map"
    "nav2":
      from: "nav2_pkg.nav2_node"  # project-local
      interfaces:
        - "map": sub-> "nav2_art::map"
  connections:
    - [nav2.map; slam.map] # reverse direction
    - [slam.unknown; nav2.map] # nonexistent
    - [slam.map; nav2.map; extra] # 3 elements
""")

    linter_bad = rosmodel_lint.Linter(str(bad_sys))
    linter_bad.run()
    bad_errors = [f for f in linter_bad.findings if f.severity == rosmodel_lint.ERROR]
    assert len(bad_errors) == 3, f"Expected 3 errors, got: {bad_errors}"
    rules = [f.rule for f in bad_errors]
    assert "RM051" in rules, "Reverse direction must trigger RM051"
    assert "RM050" in rules, "Nonexistent endpoint must trigger RM050"
    assert "RM060" in rules, "3-element connection must trigger RM060"


def test_linter_subsystem_connection_and_rm053_silencing(tmp_path):
    # Sibling subsystem file
    subsys_file = tmp_path / "sub_cell.rossystem"
    subsys_file.write_text("""sub_cell:
  fromFile: "sub_cell/launch/sub.launch.py"  # caller-supplied
  nodes:
    "broadcaster":
      from: "broadcaster_pkg.broadcaster_node"  # project-local
      interfaces:
        - "/joint_states": pub-> "broadcaster::joint_states"
""")

    # Main system referencing subsystem with qualified connection and # generates launch comment
    gen_sys = tmp_path / "app_system.rossystem"
    gen_sys.write_text("""app_system: # generates launch
  subSystems:
    "sub_cell"
  nodes:
    "coordinator":
      from: "coord_pkg.coordinator"  # project-local
      interfaces:
        - "joint_states": sub-> "coord_node::joint_states"
  connections:
    - [broadcaster::/joint_states, coordinator::joint_states]
""")

    linter = rosmodel_lint.Linter(str(gen_sys))
    linter.run()
    errors = [f for f in linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not errors, f"Subsystem connection produced errors: {errors}"
    rm053_findings = [f for f in linter.findings if f.rule == "RM053"]
    assert not rm053_findings, "RM053 must stay quiet for launch generator system"

    # Verify RM053 still warns when no marker or launch file exists
    plain_sys = tmp_path / "plain_system.rossystem"
    plain_sys.write_text("""plain_system:
  nodes:
    "solo":
      from: "solo_pkg.solo"  # project-local
""")
    linter_plain = rosmodel_lint.Linter(str(plain_sys))
    linter_plain.run()
    plain_rm053 = [f for f in linter_plain.findings if f.rule == "RM053"]
    assert len(plain_rm053) == 1, "RM053 must fire when fromFile is absent and not a generator"
