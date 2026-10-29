#!/usr/bin/env python3
"""Pytest suite for Phase 1 CLI packaging, shared catalogue discovery, and regression harnesses."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts  # noqa: F401 - verifies scripts/__init__.py sys.path bootstrap
import build_node_index
import build_type_index
import rosmodel_lint


def test_fallback_when_shared_catalogue_absent(tmp_path, monkeypatch):
    empty_cat = tmp_path / "absent_catalogue"
    monkeypatch.setenv("ROSMODEL_CATALOGUE_DIR", str(empty_cat))

    assert rosmodel_lint.resolve_shared_type_roots() == []
    assert rosmodel_lint.resolve_shared_node_roots() == []

    t_roots, t_source = build_type_index.resolve_catalog_roots()
    assert t_source == "assets/roscommonobjects/"
    assert len(t_roots) == 1 and os.path.samefile(t_roots[0], ROOT / "assets" / "roscommonobjects")

    n_roots, n_source = build_node_index.resolve_catalog_roots()
    assert n_source == "assets/rosmodelscatalog/"
    assert len(n_roots) == 1 and os.path.samefile(n_roots[0], ROOT / "assets" / "rosmodelscatalog")

    types = rosmodel_lint.load_type_index()
    nodes = rosmodel_lint.load_node_index()
    assert "std_msgs/msg/String" in types
    assert "amcl.amcl" in nodes


def test_shared_catalogue_overlay_and_custom_folders(tmp_path, monkeypatch):
    cat_root = tmp_path / "catalogue_repos"
    comm_dir = (
        cat_root
        / "RosCommonObjects"
        / "de.fraunhofer.ipa.ros.communication.objects"
        / "custom_bundle"
    )
    models_dir = cat_root / "RosModelsCatalog" / "custom_domain"
    extra_folder = tmp_path / "user_custom_models"
    comm_dir.mkdir(parents=True)
    models_dir.mkdir(parents=True)
    extra_folder.mkdir(parents=True)

    (comm_dir / "acme_msgs.ros").write_text(
        "acme_msgs:\n"
        "  msgs:\n"
        "    WidgetStatus\n"
        "      message\n"
        "        string label\n",
        encoding="utf-8",
    )

    (models_dir / "acme_driver.ros2").write_text(
        "acme_driver:\n"
        "  artifacts:\n"
        "    acme_driver_node:\n"
        "      node: acme_driver\n"
        "      publishers:\n"
        '        "status":\n'
        '          type: "acme_msgs/msg/WidgetStatus" # acme_msgs.ros\n',
        encoding="utf-8",
    )

    (extra_folder / "acme_subsys.rossystem").write_text(
        "acme_subsys:\n"
        "  nodes:\n"
        "    driver:\n"
        '      from: "acme_driver.acme_driver" # acme_driver.ros2\n'
        "      interfaces:\n"
        '        - "status_out": pub-> "acme_driver_node::status"\n',
        encoding="utf-8",
    )

    (cat_root / "catalogue_config.json").write_text(
        json.dumps({"customFolders": [{"name": "user_extra", "path": str(extra_folder)}]}),
        encoding="utf-8",
    )

    monkeypatch.setenv("ROSMODEL_CATALOGUE_DIR", str(cat_root))

    t_roots, _ = build_type_index.resolve_catalog_roots()
    n_roots, _ = build_node_index.resolve_catalog_roots()
    assert any("RosCommonObjects" in r for r in t_roots)
    assert any("RosModelsCatalog" in r for r in n_roots)
    assert str(extra_folder) in n_roots

    types = rosmodel_lint.load_type_index()
    nodes = rosmodel_lint.load_node_index()
    systems = rosmodel_lint.load_system_index()

    assert "acme_msgs/msg/WidgetStatus" in types
    assert "std_msgs/msg/String" in types  # fallback preserved
    assert "acme_driver.acme_driver" in nodes
    assert nodes["acme_driver.acme_driver"]["interfaces"] == {"status": "pub"}
    assert "acme_subsys" in systems
    assert (cat_root / ".catalogue_index_cache.json").is_file()

    # Lint models referencing the shared-catalogue-only type and node
    sample_ros2 = tmp_path / "check_acme_client.ros2"
    sample_ros2.write_text(
        "check_acme_client:\n"
        "  artifacts:\n"
        "    listener_node:\n"
        "      node: listener\n"
        "      subscribers:\n"
        '        "status":\n'
        '          type: "acme_msgs/msg/WidgetStatus" # acme_msgs.ros\n',
        encoding="utf-8",
    )
    ros2_findings = rosmodel_lint.Linter(str(sample_ros2), use_catalogue=True).run()
    assert [(f.rule, f.message) for f in ros2_findings if f.severity in ("ERROR", "WARNING")] == []

    sample_sys = tmp_path / "check_acme.rossystem"
    sample_sys.write_text(
        "check_acme:\n"
        '  fromFile: "check_acme/launch/check_acme.launch.py" # caller-supplied\n'
        "  nodes:\n"
        '    "drv":\n'
        '      from: "acme_driver.acme_driver" # acme_driver.ros2\n'
        "      interfaces:\n"
        '        - "status": pub-> "acme_driver_node::status"\n',
        encoding="utf-8",
    )
    findings = rosmodel_lint.Linter(str(sample_sys), use_catalogue=True).run()
    errors_or_warns = [(f.rule, f.message) for f in findings if f.severity in ("ERROR", "WARNING")]
    assert errors_or_warns == []


def test_ask_oracle_passes_catalogue_jvm_property(tmp_path, monkeypatch):
    import importlib.util

    cat_dir = tmp_path / "cat_override"
    cat_dir.mkdir()
    fake_jar = tmp_path / "rostooling_extension-2.2.1.jar"
    fake_jar.write_text("", encoding="utf-8")

    monkeypatch.setenv("ROSMODEL_CATALOGUE_DIR", str(cat_dir))

    spec = importlib.util.spec_from_file_location(
        "_ask_oracle_test", str(ROOT / "tests" / "oracle" / "ask_oracle.py")
    )
    ask_oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ask_oracle)

    captured = {}

    class DummyPipe:
        def readline(self):
            return b""

    class DummyProc:
        stdin = mock.MagicMock()
        stdout = DummyPipe()
        stderr = DummyPipe()

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return DummyProc()

    with mock.patch.object(ask_oracle.subprocess, "Popen", side_effect=fake_popen):
        ask_oracle.Server(tmp_path, jar=fake_jar)

    assert f"-Drostooling.catalogue.path={cat_dir.resolve()}" in captured["cmd"]


@pytest.mark.parametrize(
    "module_name",
    [
        "scripts.rosmodel_lint",
        "scripts.ros_studio",
        "scripts.ros_plot",
        "scripts.extract_ros2_interfaces",
        "scripts.extract_rossystem",
        "scripts.studio_server",
    ],
)
def test_cli_entrypoints_help(module_name):
    res = subprocess.run(
        [sys.executable, "-m", module_name, "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    assert "usage:" in res.stdout.lower()


@pytest.mark.parametrize(
    "script_args",
    [
        ["tests/docs_check.py"],
        ["tests/extract_golden.py"],
        ["tests/oracle_gate.py"],
        ["tests/studio_agent_checks.py"],
        ["tests/studio_roundtrip.py"],
    ],
)
def test_existing_regression_suite(script_args):
    res = subprocess.run(
        [sys.executable, *script_args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, f"{script_args} failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
