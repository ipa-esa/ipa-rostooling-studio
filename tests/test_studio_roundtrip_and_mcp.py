#!/usr/bin/env python3
"""Pytest suite for Track 2: 8 CoreSense roles, round-trip reconciliation, shared catalogue sync, and Stage 5 MCP coupling."""

import json
from pathlib import Path
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from studio_mcp import StudioMcpBridge, resolve_mcp_command  # noqa: E402
from studio_store import CORESENSE_ROLES, StudioError, StudioStore  # noqa: E402


def test_coresense_roles_and_legacy_migration(tmp_path):
    store = StudioStore(tmp_path / "storage", ROOT)
    env = store.create_project({"name": "role_migration_probe", "mode": "starter"})
    pid = env["projectId"]

    assert CORESENSE_ROLES == (
        "Sensing",
        "Perception",
        "World Modeling",
        "Planning",
        "Control",
        "Actuation",
        "Communication",
        "Unassigned",
    )
    assert env["project"]["studio"]["modules"]["publisher"]["role"] == "Sensing"
    assert env["project"]["studio"]["modules"]["subscriber"]["role"] == "Planning"

    # Legacy roles ('Afferent', 'Efferent', etc.) normalize seamlessly on save
    proj = env["project"]
    proj["studio"]["modules"]["publisher"]["role"] = "Afferent"
    proj["studio"]["modules"]["subscriber"]["role"] = "Efferent"
    updated = store.save_project(pid, {"expectedRevision": env["revision"], "project": proj})
    assert updated["project"]["studio"]["modules"]["publisher"]["role"] == "Sensing"
    assert updated["project"]["studio"]["modules"]["subscriber"]["role"] == "Actuation"


def test_materialize_and_reconcile_file_roundtrip(tmp_path):
    store = StudioStore(tmp_path / "storage", ROOT)
    env = store.create_project({"name": "roundtrip_demo", "mode": "starter"})
    pid = env["projectId"]

    proj = env["project"]
    proj["studio"]["brief"]["purpose"] = "Keep intent intact across VS Code Micro edits"
    proj["studio"]["behavior"].append(
        {
            "id": "beh_1",
            "action": "Publish heartbeat",
            "condition": "Always",
            "recovery": "Restart publisher",
            "moduleId": "publisher",
            "criterionId": "",
            "failures": [],
        }
    )
    env = store.save_project(pid, {"expectedRevision": env["revision"], "project": proj})

    ws_models = tmp_path / "ros2_ws" / "models"
    mat = store.materialize_models(pid, {"targetDir": str(ws_models)})
    assert mat["modelState"] == "clean"
    sys_path = Path(mat["primarySystemFile"])
    assert sys_path.is_file()
    assert store.find_project_for_file(str(sys_path))["projectId"] == pid

    # Simulate Micro Editor saving a .layout.json with updated coordinates & CoreSense role
    layout_dir = ws_models / ".rostooling" / "layout"
    layout_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / (sys_path.name + ".layout.json")).write_text(
        json.dumps(
            {
                "version": 1,
                "modelFile": sys_path.name,
                "modelType": "rossystem",
                "nodes": [
                    {"id": "publisher", "label": "publisher", "x": 310, "y": 420, "role": "Perception"},
                    {"id": "subscriber", "label": "subscriber", "x": 680, "y": 420, "role": "Control"},
                ],
            }
        ),
        encoding="utf-8",
    )

    reconciled = store.reconcile_file(pid, {"filePath": str(sys_path)})
    assert reconciled["modelState"] == "clean"
    assert reconciled["project"]["studio"]["brief"]["purpose"] == "Keep intent intact across VS Code Micro edits"
    assert len(reconciled["project"]["studio"]["behavior"]) == 1

    pub = next(n for n in reconciled["project"]["nodes"] if n["label"] == "publisher")
    sub = next(n for n in reconciled["project"]["nodes"] if n["label"] == "subscriber")
    assert (pub["x"], pub["y"]) == (310, 420)
    assert (sub["x"], sub["y"]) == (680, 420)
    assert reconciled["project"]["studio"]["modules"][pub["id"]]["role"] == "Perception"
    assert reconciled["project"]["studio"]["modules"][sub["id"]]["role"] == "Control"


def test_mcp_bridge_stdio_jsonrpc_and_domain_shield(tmp_path, monkeypatch):
    fake_mcp = tmp_path / "fake_mcp_server.py"
    fake_mcp.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, sys
            from pathlib import Path

            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                msg = json.loads(line)
                method = msg.get("method")
                req_id = msg.get("id")
                if method == "initialize":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-mcp", "version": "1.0"}}}
                    sys.stdout.write(json.dumps(resp) + "\\n")
                    sys.stdout.flush()
                elif method == "tools/call":
                    params = msg.get("params") or {}
                    tool = params.get("name")
                    args = params.get("arguments") or {}
                    if tool == "validate_ros_model":
                        payload = {"file": Path(args["model_path"]).name, "isValid": True, "errorCount": 0, "warningCount": 0, "diagnostics": []}
                    elif tool == "generate_ros2_wrappers":
                        ws = Path(args["workspace_dir"])
                        if os.environ.get("FAKE_TAMPER_DOMAIN") == "1":
                            for p in ws.rglob("*Algorithm.hpp"):
                                p.write_text("// TAMPERED", encoding="utf-8")
                        payload = {"status": "success", "package": "studio_demo", "workspaceDir": str(ws), "writtenFiles": ["CMakeLists.txt"], "skippedFiles": ["include/studio_demo/PublisherAlgorithm.hpp"]}
                    elif tool == "generate_rossystem_code":
                        payload = {"status": "success", "workspaceDir": args["workspace_dir"], "writtenFiles": ["launch/roundtrip.launch.py"], "skippedFiles": []}
                    elif tool == "build_ros2_package":
                        payload = {"command": "colcon build --symlink-install", "exitCode": 0, "success": True, "stdout": "Summary: 1 package finished", "stderr": ""}
                    else:
                        payload = {"error": f"unknown tool {tool}"}
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
                    sys.stdout.write(json.dumps(resp) + "\\n")
                    sys.stdout.flush()
            """
        ),
        encoding="utf-8",
    )
    fake_mcp.chmod(0o755)
    monkeypatch.setenv("ROSTOOLING_MCP_BIN", str(fake_mcp))

    store = StudioStore(tmp_path / "storage", ROOT)
    env = store.create_project({"name": "mcp_demo", "mode": "starter"})
    pid = env["projectId"]

    ws_root = tmp_path / "ws"
    alg_file = ws_root / "src-gen" / "studio_demo" / "include" / "studio_demo" / "PublisherAlgorithm.hpp"
    alg_file.parent.mkdir(parents=True, exist_ok=True)
    alg_file.write_text("// USER HANDWRITTEN DOMAIN LOGIC\n", encoding="utf-8")

    bridge = StudioMcpBridge(store, ROOT)
    try:
        st = bridge.status()
        assert st["available"] is True

        val = bridge.validate_models(pid, {"workspaceDir": str(ws_root)})
        assert val["isValid"] is True

        wrap = bridge.generate_wrappers(pid, {"workspaceDir": str(ws_root), "language": "cpp", "rosDistro": "humble"})
        assert wrap["ok"] is True
        assert str(alg_file.resolve()) in wrap["protectedDomainFiles"]
        assert alg_file.read_text(encoding="utf-8") == "// USER HANDWRITTEN DOMAIN LOGIC\n"

        sys_gen = bridge.generate_system(pid, {"workspaceDir": str(ws_root)})
        assert sys_gen["ok"] is True

        bld = bridge.build_workspace(pid, {"workspaceDir": str(ws_root), "packageName": "studio_demo"})
        assert bld["build"]["success"] is True
    finally:
        bridge.close()

    # Verify Domain Logic Protection Shield catches any illegal mutation
    monkeypatch.setenv("FAKE_TAMPER_DOMAIN", "1")
    bridge2 = StudioMcpBridge(store, ROOT)
    try:
        with pytest.raises(StudioError) as exc_info:
            bridge2.generate_wrappers(pid, {"workspaceDir": str(ws_root)})
        assert exc_info.value.code == "domain_shield_violation"
    finally:
        bridge2.close()


def test_real_rostooling_mcp_discovery():
    cmd = resolve_mcp_command(ROOT)
    assert cmd is not None and any("rostooling-mcp" in part for part in cmd)
