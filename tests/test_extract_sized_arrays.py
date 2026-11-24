#!/usr/bin/env python3
"""A .msg field with a fixed or bounded array size is written as T[N] / T[<=N] in the .ros model."""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts  # noqa: F401 - scripts/__init__.py puts scripts/ on sys.path
import extract_ros2_interfaces as ex


def emit(typ):
    flags = []
    return ex.emit_ros_type(typ, "demo_msgs", "demo.msg", 1, flags), flags


@pytest.mark.parametrize("typ,expected", [
    ("float64[9]", "float64[9]"),
    ("int32[<=5]", "int32[<=5]"),
    ("string[3]", "string[3]"),
    ("byte[<=16]", "byte[<=16]"),
    ("time[2]", "time[2]"),
    ("duration[<=3]", "duration[<=3]"),
    ("Header[2]", "Header[2]"),
    ("Base[3]", '"demo_msgs/msg/Base"[3]'),
    ("other_msgs/Base[<=10]", '"other_msgs/msg/Base"[<=10]'),
    ("int32[]", "int32[]"),
    ("Base[]", '"demo_msgs/msg/Base"[]'),
    ("float32", "float32"),
])
def test_sizes_and_the_unbounded_forms(typ, expected):
    text, flags = emit(typ)
    assert text == expected
    assert flags == []


@pytest.mark.parametrize("typ", ["time[]", "duration[]", "Header[]"])
def test_unbounded_arrays_of_time_types_still_have_no_form(typ):
    text, flags = emit(typ)
    assert text is None
    assert len(flags) == 1


@pytest.mark.parametrize("typ", ["string<=10", "string<=10[]", "int32[N]"])
def test_what_has_no_form_is_dropped_with_a_report(typ):
    text, flags = emit(typ)
    assert text is None
    assert len(flags) == 1
