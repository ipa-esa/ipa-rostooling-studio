#!/usr/bin/env python3
"""catalogue_query.py, add_provenance.py and ros_catalog_sources.py."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts  # noqa: F401,E402
import add_provenance  # noqa: E402
import catalogue_query  # noqa: E402
import ros_catalog_sources  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def vendored_catalogue_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSMODEL_CATALOGUE_DIR", str(tmp_path / "no_shared_catalogue"))


def test_query_finds_types_nodes_and_systems(capsys):
    assert catalogue_query.main(["type", "LaserScan"]) == 0
    assert "sensor_msgs/msg/LaserScan" in capsys.readouterr().out
    assert catalogue_query.main(["show", "amcl.amcl"]) == 0
    out = capsys.readouterr().out
    assert "assets/rosmodelscatalog/navigation/amcl.ros2" in out and "sub scan" in out
    assert catalogue_query.main(["system", "ur_robot"]) == 0
    assert "exposes no interfaces" in capsys.readouterr().out


def test_query_reports_a_miss(capsys):
    assert catalogue_query.main(["node", "no_such_thing_zzz"]) == 1
    assert catalogue_query.main(["show", "nav2_amcl.amcl"]) == 1
    assert "amcl.amcl" in capsys.readouterr().err


def test_provenance_comments_references_and_leaves_the_rest(tmp_path):
    (tmp_path / "rosnodes").mkdir()
    pkg = tmp_path / "rosnodes" / "mine.ros2"
    pkg.write_bytes(
        b"mine:\r\n  artifacts:\r\n    worker:\r\n      node: worker\r\n      publishers:\r\n"
        b"        'a':\r\n          type: 'sensor_msgs/msg/LaserScan'\r\n"
        b"        'b':\r\n          type: 'nowhere_msgs/msg/Thing'\r\n")
    system = tmp_path / "s.rossystem"
    system.write_text(
        's:\n  nodes:\n    "amcl":\n      from: "amcl.amcl"\n'
        '    "w":\n      from: "mine.worker"\n'
        '    "k":\n      from: "amcl.amcl" # hand written\n', encoding="utf-8")

    before = system.read_bytes()
    assert add_provenance.main([str(system), "--check"]) == 1
    assert system.read_bytes() == before
    assert add_provenance.main([str(system)]) == 0
    text = system.read_text()
    assert 'from: "amcl.amcl" # assets/rosmodelscatalog/navigation/amcl.ros2\n' in text
    assert 'from: "mine.worker" # project-local, see rosnodes/mine.ros2\n' in text
    assert 'from: "amcl.amcl" # hand written\n' in text
    assert add_provenance.main([str(system), "--check"]) == 0

    assert add_provenance.main([str(pkg)]) == 1  # one reference resolves nowhere
    raw = pkg.read_bytes()
    assert b"LaserScan' # assets/roscommonobjects/" in raw and raw.count(b"\r\n") == 9
    assert b"type: 'nowhere_msgs/msg/Thing'\r\n" in raw


AMCL = ROOT / "assets" / "rosmodelscatalog" / "navigation" / "amcl.ros2"


@pytest.mark.parametrize("edit, from_catalogue", [
    (lambda text: text, True),                                           # untouched copy
    (lambda text: text.replace("\n", "  # note\r\n"), True),              # comments, CRLF
    (lambda text: "# header\n\n" + text, True),
    (lambda text: text.replace("scan", "my_scan"), False),               # edited copy
    (lambda text: "amcl:\n  artifacts:\n    amcl:\n      node: amcl\n", False),  # own file
], ids=["copy", "comments-and-crlf", "header", "edited", "own"])
def test_provenance_tells_a_catalogue_copy_from_an_own_file(tmp_path, edit, from_catalogue):
    original = AMCL.read_text(encoding="utf-8")
    (tmp_path / "amcl.ros2").write_bytes(edit(original).encode("utf-8"))
    system = tmp_path / "s.rossystem"
    system.write_text('s:\n  nodes:\n    "amcl":\n      from: "amcl.amcl"\n', encoding="utf-8")
    assert add_provenance.main([str(system)]) == 0
    expected = ("# assets/rosmodelscatalog/navigation/amcl.ros2" if from_catalogue
                else "# project-local, see amcl.ros2")
    assert system.read_text(encoding="utf-8").rstrip().endswith(expected)


def test_a_hash_inside_quotes_is_content():
    assert add_provenance.without_comments('value: "a # b"  # c\n# d\n') == ['value: "a # b"']
    assert add_provenance.without_comments("x: 'it' # y") == ["x: 'it'"]


def test_catalog_sources_reports_only_new_repositories(tmp_path):
    existing = tmp_path / "popular.md"
    existing.write_text("| [nav2](https://github.com/ros-navigation/navigation2) |\n")
    listing = tmp_path / "list.md"
    listing.write_text(
        "- [Nav2](https://github.com/ros-navigation/navigation2.git) - navigation\n"
        "- [MoveIt 2](https://github.com/moveit/moveit2) - manipulation\n"
        "- https://github.com/sponsors/someone\n")
    html_page = tmp_path / "table.html"
    html_page.write_text('<a href="https://github.com/moveit/moveit2/issues">x</a>\n')
    result = ros_catalog_sources.collect([str(listing), str(html_page)], existing.read_text())
    assert (result["total"], result["known"]) == (2, 1)
    assert [r["name"] for r in result["new"]] == ["MoveIt 2"]
    assert len(result["new"][0]["sources"]) == 2
    broken = ros_catalog_sources.collect([str(tmp_path / "missing")], "")
    assert broken["sources"][0]["status"] == "failed"
