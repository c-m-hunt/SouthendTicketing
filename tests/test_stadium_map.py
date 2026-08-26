"""Tests for preparing the ktckts venue SVG.

Run against the real map recorded from the live site, so a change in the
upstream drawing shows up here rather than as a blank stadium in production.
"""

import re
import xml.etree.ElementTree as ET

import pytest

from app import stadium_map as sm

KT = sm.KT
SVG = sm.SVG


@pytest.fixture(scope="module")
def prepared(request):
    svg = request.getfixturevalue("venue_map_svg")
    return sm.prepare(svg)


def test_every_block_is_addressable(prepared):
    markup, codes = prepared
    assert len(codes) >= 54

    tagged = set(re.findall(r'data-code="([^"]+)"', markup))
    assert {"BLA", "BLB", "BLND", "BLNE", "BLQ", "DIR"} <= tagged


def test_seat_guide_paths_are_removed(prepared, venue_map_svg):
    """ktckts grows seat rows between these; statically they are black bars."""
    markup, _ = prepared
    assert "seats-top" in venue_map_svg, "the source should have guides to remove"
    assert "seats-top" not in markup
    assert "seats-bottom" not in markup


def test_placeholder_fill_cannot_beat_the_stylesheet(prepared):
    """The file ships a purple fill by class and inline; both must go.

    Its own <style> is inlined after ours, so leaving the class on would let
    every block render purple.
    """
    markup, _ = prepared
    root = ET.fromstring(markup)

    shapes = [e for e in root.iter() if "seg-shape" in (e.get("class") or "")]
    assert shapes

    for shape in shapes:
        assert shape.get("class") == "seg-shape", "original fill classes must be dropped"
        assert "fill:" not in (shape.get("style") or "")


def test_block_labels_are_styleable(prepared):
    """Labels carry an inline white fill that vanishes on the pale hatch."""
    markup, _ = prepared
    root = ET.fromstring(markup)

    labels = [e for e in root.iter() if "seg-label" in (e.get("class") or "")]
    assert labels
    for label in labels:
        assert "fill:" not in (label.get("style") or "")


def test_shapes_belong_to_their_nearest_segment(prepared):
    """Segment groups nest; an inner block must not inherit the outer code."""
    markup, _ = prepared
    root = ET.fromstring(markup)
    parents = {c: p for p in root.iter() for c in p}

    for shape in [e for e in root.iter() if "seg-shape" in (e.get("class") or "")]:
        code = shape.get("data-code")
        node = parents.get(shape)
        while node is not None and node.attrib.get(KT + "type") != "segment":
            node = parents.get(node)
        assert node is not None, "every tagged shape sits inside a segment"
        assert node.attrib.get(KT + "code") == code


def test_hatch_pattern_is_defined(prepared):
    markup, _ = prepared
    root = ET.fromstring(markup)
    patterns = [e for e in root.iter(f"{SVG}pattern") if e.get("id") == sm.HATCH_ID]
    assert len(patterns) == 1
    assert [e for e in patterns[0].iter() if "seg-hatch__line" in (e.get("class") or "")]


def test_map_is_responsive(prepared):
    markup, _ = prepared
    root = ET.fromstring(markup)
    assert root.get("viewBox"), "the viewBox must survive so proportions hold"
    assert root.get("width") is None, "a fixed width would stop CSS sizing it"
    assert root.get("height") is None


def test_active_content_is_stripped():
    """The file is third-party markup going into our DOM."""
    hostile = """<svg xmlns="http://www.w3.org/2000/svg"
                      xmlns:ktckts="http://www.kaizenticketing.com" viewBox="0 0 10 10">
      <script>alert(1)</script>
      <g ktckts:type="segment" ktckts:code="BLA" onclick="alert(2)">
        <polygon points="0,0 1,1" ktckts:show-availability="true" style="fill:#800080"/>
      </g>
      <foreignObject><div>x</div></foreignObject>
      <image href="https://evil.test/x.png"/>
    </svg>"""
    markup, codes = sm.prepare(hostile)

    assert codes == ["BLA"]
    assert "<script" not in markup
    assert "foreignObject" not in markup
    assert "onclick" not in markup
    assert "evil.test" not in markup


def test_prepare_rejects_a_map_it_does_not_recognise():
    """A silent upstream change should fail loudly, not render an empty ground."""
    with pytest.raises(ValueError):
        sm.prepare('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')

    with pytest.raises(ValueError):
        sm.prepare("definitely not xml <<<")


@pytest.mark.parametrize(
    "segment, expected",
    [
        # East A: 493 real seats, none sellable — not the same as sold out.
        ({"total_count": 0, "open_count": 0, "has_seats": True}, "unsold"),
        # A hospitality box: no seating at all.
        ({"total_count": 0, "open_count": 0, "has_seats": False}, "empty"),
        ({"total_count": 295, "open_count": 0, "has_seats": True}, "soldout"),
        ({"total_count": 400, "open_count": 300, "has_seats": True}, "roomy"),
        ({"total_count": 400, "open_count": 40, "has_seats": True}, "tight"),
        # An unopened away block has no seat grid to read, but it is still
        # real seating rather than a box with no seats in it.
        ({"code": "BLND", "total_count": 0, "open_count": 0, "has_seats": False}, "unsold"),
        # Opened for a big away following: coloured like anywhere else.
        ({"code": "BLNF", "total_count": 402, "open_count": 325, "has_seats": True}, "roomy"),
    ],
)
def test_classify(segment, expected):
    assert sm.classify(segment) == expected


@pytest.mark.parametrize(
    "code, expected",
    [("BLND", True), ("BLNE", True), ("BLNF", True), ("blnd", True),
     ("BLNA", False), ("BLN", False), ("BLA", False), (None, False), ("", False)],
)
def test_is_away(code, expected):
    assert sm.is_away(code) is expected
