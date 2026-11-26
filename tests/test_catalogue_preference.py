#!/usr/bin/env python3
"""extract_rossystem.py and ros_studio.py read catalogue models from the shared catalogue repos
first and fall back to the vendored snapshot, like the linter and collect_deps.py."""

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts  # noqa: F401,E402
import extract_rossystem  # noqa: E402
import ros_studio  # noqa: E402
import rosmodel_lint as L  # noqa: E402
import pytest  # noqa: E402

VENDORED = ROOT / "assets" / "rosmodelscatalog"
AMCL = "navigation/amcl.ros2"
TURTLEBOT = "robots/turtlebot3/robot/turtlebot.rossystem"


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """A shared catalogue whose amcl and turtlebot models differ from the vendored copies."""
    repo = tmp_path / "catalogue_repos" / "RosModelsCatalog"
    for rel in (AMCL, TURTLEBOT):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(VENDORED / rel, repo / rel)
    amcl = repo / AMCL
    amcl.write_text(amcl.read_text(encoding="utf-8").replace('"scan":', '"shared_scan":', 1),
                    encoding="utf-8")
    monkeypatch.setenv("ROSMODEL_CATALOGUE_DIR", str(tmp_path / "catalogue_repos"))
    return repo


def test_catalogue_model_path_prefers_the_shared_repos(shared, monkeypatch, tmp_path):
    assert L.catalogue_model_path(AMCL) == str(shared / AMCL)
    assert L.catalogue_model_path("navigation/no_such.ros2") is None
    monkeypatch.setenv("ROSMODEL_CATALOGUE_DIR", str(tmp_path / "absent"))
    assert L.catalogue_model_path(AMCL) == str(VENDORED / AMCL)


def test_extract_rossystem_reads_the_shared_model(shared):
    parsed, source, _note = extract_rossystem.ModelIndex(None).resolve_package("amcl", "amcl")
    assert source == "catalogue:" + AMCL and parsed.path == str(shared / AMCL)
    names = {name for _kind, name in parsed.artifacts["amcl"]["ifaces"]}
    assert "shared_scan" in names and "scan" not in names


def test_studio_subsystem_view_reads_the_shared_model(shared, monkeypatch):
    seen = []
    real = ros_studio.ros_plot.extract_model
    monkeypatch.setattr(ros_studio.ros_plot, "extract_model",
                        lambda path, **kw: seen.append(path) or real(path, **kw))
    cat_file, nodes, _path, graph = ros_studio.resolve_subsystem("turtlebot", str(shared))
    assert cat_file == TURTLEBOT and nodes and graph is not None
    assert seen == [str(shared / TURTLEBOT)]
