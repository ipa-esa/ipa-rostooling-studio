#!/usr/bin/env python3
"""Pytest suite for lifecycle node extraction, active_in states, and scoped rossystem connections."""

import json
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


def test_linter_accepts_a_valid_lifecycle_node(tmp_path):
    # Lifecycle mistakes (active_in on a plain node, an unknown state, a quoted 'true') are the
    # language server's to report; see tests/fixtures/server_coverage/RM03*-*lifecycle*.
    good = tmp_path / "good_lifecycle.ros2"
    good.write_text("""good_pkg:
  artifacts:
    good_node:
      node: good_node
      lifecycle: true
      publishers:
        'chatter':
          type: 'std_msgs/msg/String'
          active_in: [Active]
""")
    linter = rosmodel_lint.Linter(str(good))
    linter.run()
    errors = [f for f in linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not errors, errors


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



def test_linter_subsystem_connection(tmp_path):
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


def test_plugin_bases_slots_and_attachments_lint_and_plot(tmp_path):
    """Phase 4 plugin syntax: bases declare class/header/loader, plugins name a base with
    'implements: <package>.<Base>', slots own their parameter, and a system attaches plugins
    per slot under 'slots:'."""
    import ros_plot

    plug_ros2 = tmp_path / "planner_plugins.ros2"
    plug_ros2.write_text("""planner_plugins:
  pluginbases:
    GlobalPlanner:
      class: "nav2_core::GlobalPlanner"
      header: "nav2_core/global_planner.hpp"
      loader: pluginlib
    LeafBase:
      class: "custom_core::LeafBase"
      header: "custom_core/leaf_base.hpp"
      loader: pluginlib
  artifacts:
    straight_line_lib:
      plugins:
        StraightLine:
          implements: planner_plugins.GlobalPlanner
          publishers:
            debug_plan:
              type: "std_msgs/msg/String"
        Smooth:
          implements: planner_plugins.GlobalPlanner
    leaf_lib:
      plugins:
        DockLeaf:
          implements: planner_plugins.LeafBase
""")
    p_linter = rosmodel_lint.Linter(str(plug_ros2))
    p_linter.run()
    p_errors = [f for f in p_linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not p_errors, f"Node-less plugin library had lint errors: {p_errors}"

    host_ros2 = tmp_path / "planner_host.ros2"
    host_ros2.write_text("""planner_host:
  artifacts:
    planner_server:
      node: planner_server
      pluginslots:
        global_planner:
          base: planner_plugins.GlobalPlanner
          parameter: planner_plugin
          default: planner_plugins.StraightLine
        leaves:
          base: planner_plugins.LeafBase
          parameter: leaf_libraries
          format: library
          multiple: true
""")
    h_linter = rosmodel_lint.Linter(str(host_ros2))
    h_linter.run()
    h_errors = [f for f in h_linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not h_errors, f"Host pluginslot model had lint errors: {h_errors}"

    sys_file = tmp_path / "plug_app.rossystem"
    sys_file.write_text("""plug_app:
  fromFile: "plug_bringup/launch/app.launch.py"  # caller-supplied
  nodes:
    "host":
      from: "planner_host.planner_server"  # project-local
      slots:
        global_planner: [planner_plugins.Smooth]
        leaves: [planner_plugins.DockLeaf]
""")
    s_linter = rosmodel_lint.Linter(str(sys_file))
    s_linter.run()
    s_errors = [f for f in s_linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not s_errors, f"System with slot attachments had lint errors: {s_errors}"

    model = ros_plot.extract_model(str(sys_file))
    assert model["nodes"][0]["slots"] == [
        {"slot": "global_planner", "plugins": ["planner_plugins.Smooth"]},
        {"slot": "leaves", "plugins": ["planner_plugins.DockLeaf"]},
    ]
    assert model["nodes"][0]["plugins"] == ["planner_plugins.Smooth", "planner_plugins.DockLeaf"]
    html = ros_plot.render_html([model])
    assert "planner_plugins.Smooth" in html


PHASE4_CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "plugins_phase4")


def test_linter_never_rejects_plugin_syntax_the_server_accepts():
    """Every file in fixtures/plugins_phase4 was run through the real language server
    (tests/oracle/ask_oracle.py); expected.json records whether it reported errors. Judging the
    plugin syntax is the server's job; the linter must not raise an error on a file it accepts."""
    with open(os.path.join(PHASE4_CASES, "expected.json")) as fh:
        expected = json.load(fh)
    false_alarms = []
    for case, files in sorted(expected["verdicts"].items()):
        for name, rejected in files.items():
            if rejected:
                continue
            linter = rosmodel_lint.Linter(os.path.join(PHASE4_CASES, case, name))
            linter.run()
            errors = [(f.rule, f.message) for f in linter.findings if f.severity == rosmodel_lint.ERROR]
            if errors:
                false_alarms.append((case, name, errors))
    assert not false_alarms, false_alarms


def test_phase2_extension_blocks_lint_and_plot(tmp_path):
    import ros_plot

    ext_ros2 = tmp_path / "ext_node.ros2"
    ext_ros2.write_text("""ext_node:
  artifacts:
    app_bin:
      node: app_node
      publishers:
        heartbeat:
          type: "std_msgs/msg/String"
      parameters:
        rate_hz:
          type: Integer
          default: 10
      @audit:
        channel: "main"
        out_port: pub->heartbeat
        rate_param: param->rate_hz
    ext_lib:
      plugins:
        CustomExt:
          implements: ext_node.ExtBase
          @audit:
            channel: "plugin"
  pluginbases:
    ExtBase:
      class: "ext_node::ExtBase"
      header: "ext_node/ext_base.hpp"
      loader: pluginlib
""")
    r2_linter = rosmodel_lint.Linter(str(ext_ros2))
    r2_linter.run()
    r2_errors = [f for f in r2_linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not r2_errors, f"Valid .ros2 with @ blocks had lint errors: {r2_errors}"

    ext_sys = tmp_path / "ext_sys.rossystem"
    ext_sys.write_text("""ext_sys:
  fromFile: "ext_bringup/launch/app.launch.py"  # caller-supplied
  nodes:
    "app":
      from: "ext_node.app_node"  # project-local
      interfaces:
        - "heartbeat": pub-> "app_bin::heartbeat"
          @iface_ext:
            qos_tag: "fast"
      @audit:
        channel: "sys_override"
        out_port: pub->heartbeat
  @sys_meta:
    revision: 2
""")
    s_linter = rosmodel_lint.Linter(str(ext_sys))
    s_linter.run()
    s_errors = [f for f in s_linter.findings if f.severity == rosmodel_lint.ERROR]
    assert not s_errors, f"Valid .rossystem with @ blocks had lint errors: {s_errors}"

    model = ros_plot.extract_model(str(ext_sys))
    assert any(e["name"] == "@sys_meta" for e in model["extensions"]), model["extensions"]
    assert any(e["name"] == "@audit" for e in model["nodes"][0]["extensions"]), model["nodes"][0]["extensions"]
    html = ros_plot.render_html([model])
    assert "@audit" in html
