"""Prepare the ktckts venue SVG for inlining into the page.

The upstream file is the club's own stadium plan, exported from Inkscape and
annotated with a ``ktckts`` namespace. Three things have to happen before it
can be dropped into a page and coloured from live data:

* Blocks must be addressable, so each availability shape is tagged with the
  block code taken from its enclosing segment group.
* The shapes must be colourable from CSS, which means dropping the inline
  purple placeholder fill that would otherwise win over any stylesheet.
* It is third-party markup going into our DOM, so anything active is removed.

The seat guide paths go too: ktckts draws individual seats between them at
render time, but left alone they show up as black bars across each block.
"""

import logging
import re
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
KT_NS = "http://www.kaizenticketing.com"
SVG = f"{{{SVG_NS}}}"
KT = f"{{{KT_NS}}}"

# Guide paths for generating seat rows; decoration only in a static render.
GUIDE_TYPES = {"seats-top", "seats-bottom"}

# Nothing here should ever be active, but the file is not ours.
FORBIDDEN_TAGS = {"script", "foreignObject", "animate", "animateTransform", "set", "handler"}

SHAPE_TAGS = {"polygon", "path", "rect", "circle", "ellipse", "polyline"}
LABEL_TAGS = {"text", "tspan"}

_FILL_RE = re.compile(r"\s*fill\s*:[^;]*;?", re.I)


def _local(tag):
    return tag.split("}")[-1]


def _strip_fill(style):
    """Remove any fill declaration so a stylesheet can set the colour."""
    return _FILL_RE.sub("", style or "").strip().strip(";")


def prepare(svg_text):
    """Return (svg_markup, block_codes) ready to inline.

    Raises ValueError if the document is not the annotated venue map, so a
    silent upstream change surfaces instead of rendering a blank stadium.
    """
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("ktckts", KT_NS)

    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as exc:
        raise ValueError(f"Venue map is not valid XML: {exc}") from exc

    _remove(root, lambda e: _local(e.tag) in FORBIDDEN_TAGS)
    _remove(root, lambda e: e.attrib.get(KT + "type") in GUIDE_TYPES)

    codes = _tag_segments(root)
    if not codes:
        raise ValueError("Venue map contains no ktckts segment groups")

    _add_hatch_pattern(root)
    _scrub_attributes(root)
    _make_responsive(root)

    markup = ET.tostring(root, encoding="unicode")
    return markup, codes


def _remove(root, predicate):
    """Drop matching elements; ElementTree has no parent pointers."""
    for parent in root.iter():
        for child in [c for c in parent if predicate(c)]:
            parent.remove(child)


def _tag_segments(root):
    """Give every availability shape a data-code naming its block.

    Segment groups nest, so a shape is attributed to its *nearest* segment
    ancestor. Walking each group's whole subtree instead would tag an inner
    block twice and let the outer group's code win.
    """
    parents = {child: parent for parent in root.iter() for child in parent}

    def nearest_segment(element):
        node = parents.get(element)
        while node is not None:
            if node.attrib.get(KT + "type") == "segment":
                return node
            node = parents.get(node)
        return None

    codes = []
    for group in root.iter():
        if group.attrib.get(KT + "type") == "segment" and group.attrib.get(KT + "code"):
            code = group.attrib[KT + "code"]
            codes.append(code)
            group.set("data-code", code)

    for element in root.iter():
        marked = element.attrib.get(KT + "show-availability") == "true"
        if not marked and _local(element.tag) not in SHAPE_TAGS:
            continue

        group = nearest_segment(element)
        if group is None:
            continue
        code = group.attrib.get(KT + "code")
        if not code:
            continue

        # Segments that mark their own availability shapes are taken at their
        # word; the rest fall back to every drawable child.
        owns_marked = any(
            e.attrib.get(KT + "show-availability") == "true"
            for e in group.iter()
            if nearest_segment(e) is group
        )
        if owns_marked and not marked:
            continue

        element.set("data-code", code)
        # The original classes are dropped, not appended to: the file's own
        # stylesheet sets .fil4 to a placeholder purple and, being inlined
        # after ours, would otherwise win.
        element.set("class", "seg-shape")
        _set_style(element, _strip_fill(element.get("style")))

    _tag_labels(root, nearest_segment)
    return codes


def _tag_labels(root, nearest_segment):
    """Let block letters be recoloured along with their block.

    The letters carry an inline white fill, which suits a solid block but
    disappears against the pale hatch used for blocks that are not sold here.
    Stripping the fill hands the choice to the stylesheet, which can also then
    follow the light and dark themes.
    """
    for element in root.iter():
        if _local(element.tag) not in LABEL_TAGS:
            continue
        if nearest_segment(element) is None:
            continue
        element.set("class", ((element.get("class", "") + " seg-label").strip()))
        _set_style(element, _strip_fill(element.get("style")))


def _set_style(element, style):
    if style:
        element.set("style", style)
    else:
        element.attrib.pop("style", None)


# Distinguishes "real seats nobody can buy here" from "no seating at all".
# Flat grey for both would make East A read as though it did not exist.
HATCH_ID = "seg-hatch"


def _add_hatch_pattern(root):
    """Add a diagonal hatch used to fill blocks that are not sold here.

    Its parts are styled by class rather than hard-coded, so the page's own
    stylesheet can theme it. currentColor is no help inside a pattern, which
    resolves against where it is defined rather than where it is used.
    """
    defs = root.find(f"{SVG}defs")
    if defs is None:
        defs = ET.Element(f"{SVG}defs")
        root.insert(0, defs)

    pattern = ET.SubElement(defs, f"{SVG}pattern")
    pattern.set("id", HATCH_ID)
    pattern.set("patternUnits", "userSpaceOnUse")
    pattern.set("width", "90")
    pattern.set("height", "90")
    pattern.set("patternTransform", "rotate(45)")

    background = ET.SubElement(pattern, f"{SVG}rect")
    background.set("width", "90")
    background.set("height", "90")
    background.set("class", "seg-hatch__bg")

    stripe = ET.SubElement(pattern, f"{SVG}line")
    stripe.set("x1", "0")
    stripe.set("y1", "0")
    stripe.set("x2", "0")
    stripe.set("y2", "90")
    stripe.set("stroke-width", "34")
    stripe.set("class", "seg-hatch__line")


def _scrub_attributes(root):
    """Remove event handlers and remote references."""
    for element in root.iter():
        for name in list(element.attrib):
            local = _local(name).lower()
            if local.startswith("on"):
                del element.attrib[name]
                continue
            if local in ("href", "src") and not element.attrib[name].startswith("#"):
                del element.attrib[name]


def _make_responsive(root):
    """Let CSS size the map, keeping the viewBox to preserve proportions."""
    if not root.get("viewBox"):
        width, height = root.get("width", ""), root.get("height", "")
        digits = re.compile(r"[\d.]+")
        if digits.search(width) and digits.search(height):
            root.set(
                "viewBox",
                f"0 0 {digits.search(width).group()} {digits.search(height).group()}",
            )
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)
    root.set("preserveAspectRatio", "xMidYMid meet")
    root.set("class", "stadium-map__svg")


# The North Bank's ND, NE and NF are the away end. How much of it is opened
# depends on how many visiting supporters are expected, so a zero-inventory
# away block is unused *this week* rather than never sold here.
AWAY_BLOCKS = frozenset({"BLND", "BLNE", "BLNF"})


def is_away(code):
    """True for a block that belongs to the away allocation."""
    return (code or "").strip().upper() in AWAY_BLOCKS


def classify(segment):
    """Bucket a block for colouring.

    Zero inventory means two different things, and conflating them is what
    makes East A look as though it should be sold out: it holds 493 real
    seats that Southend does not sell here, which is not the same as a
    hospitality box with no seating at all.
    """
    total = segment.get("total_count") or 0
    open_count = segment.get("open_count") or 0

    if total == 0 and open_count == 0:
        # An unopened away block has no seat grid to read, because ktckts only
        # publishes one for inventory on sale. It is still real seating, so it
        # is hatched rather than greyed out like a box with no seats at all.
        if is_away(segment.get("code")):
            return "unsold"
        return "unsold" if segment.get("has_seats") else "empty"
    if total == 0:
        # Season tickets: total capacity unknown, but seats are available.
        return "roomy"
    if open_count == 0:
        return "soldout"
    return "roomy" if open_count / total > 0.35 else "tight"
