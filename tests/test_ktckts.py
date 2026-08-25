"""Tests for parsing the ktckts responses.

These run against payloads recorded from the live site, so a change in the
upstream shape shows up here rather than in production.
"""

import base64
import datetime as dt

import pytest

from app import ktckts


# -- seat status bytes ---------------------------------------------------


@pytest.mark.parametrize(
    "byte, expected",
    [
        (0x07, "gap"),
        (0x0F, "gap"),  # gap + modifier bit
        (0xF0, "open"),
        (0xF8, "open"),  # open + modifier bit
        (0x01, "taken"),
        (0x03, "taken"),
        (0x09, "taken"),  # 0x01 + modifier bit
    ],
)
def test_classify_seat(byte, expected):
    assert ktckts.classify_seat(byte) == expected


def test_seat_grid_reconciles_with_reported_counts(detail_payload):
    """Every inventory set's decoded grid must match its own open/total.

    This is the check that proves the byte encoding is still understood; it
    compares within one response, so tickets selling mid-run cannot skew it.
    """
    result = ktckts.parse_seat_status(detail_payload)

    assert result["mismatched_sets"] == 0
    assert result["verified"] is True
    assert result["open"] == result["reported_open"]
    assert result["total"] == result["reported_total"]
    assert result["open"] + result["taken"] == result["total"]


def test_seat_status_flags_unknown_encoding(detail_payload):
    """A status byte that decodes wrongly must surface, not pass silently."""
    payload = {
        "detailedAvailability": {
            "openCount": 4,
            "totalCount": 4,
            "bySegment": [
                {
                    "byInventorySet": [
                        {
                            # Four "open" seats claimed, but the grid says taken.
                            "statusBytes": base64.b64encode(bytes([0x01] * 4)).decode(),
                            "openCount": 4,
                            "totalCount": 4,
                        }
                    ]
                }
            ],
        }
    }
    result = ktckts.parse_seat_status(payload)
    assert result["verified"] is False
    assert result["mismatched_sets"] == 1


# -- segments ------------------------------------------------------------


def test_parse_segments_drops_synthetic_root(spec_payload):
    segments = ktckts.parse_segments(spec_payload)
    assert segments, "expected segments"
    assert all(s["code"] != "DEFAULT" for s in segments), "DEFAULT would double totals"


def test_top_level_capacities_sum_to_stadium_total(spec_payload):
    """The stands must add up to the DEFAULT row the API reports."""
    default = next(
        s for s in spec_payload["availableSegments"] if s["code"] == "DEFAULT"
    )
    segments = ktckts.parse_segments(spec_payload)
    stands = [s for s in segments if s["depth"] == 0]

    assert sum(s["total_count"] for s in stands) == default["totalCount"]
    assert sum(s["open_count"] for s in stands) == default["openCount"]


def test_children_sum_to_their_parent(spec_payload):
    segments = ktckts.parse_segments(spec_payload)
    by_id = {s["id"]: s for s in segments}
    children = {}
    for segment in segments:
        if segment["parent_id"]:
            children.setdefault(segment["parent_id"], []).append(segment)

    for parent_id, kids in children.items():
        parent = by_id[parent_id]
        assert sum(k["total_count"] for k in kids) == parent["total_count"], parent["code"]
        assert sum(k["open_count"] for k in kids) == parent["open_count"], parent["code"]


# -- totals --------------------------------------------------------------


def test_sold_out_block_counts_as_sold():
    """A block with inventory and nothing left has sold out, not closed.

    East A-D and South Upper F-J report isAvailable=false with a full
    totalCount. Those seats are gone, and excluding them would understate
    sales by thousands.
    """
    segments = [
        {
            "id": "stand", "parent_id": None, "code": "ST", "name": "Stand", "depth": 0,
            "kind": "Virtual", "open_count": 40, "total_count": 300,
            "is_on_sale": True, "is_selectable": True, "sort_order": 0,
        },
        {
            "id": "open-block", "parent_id": "stand", "code": "A", "name": "A", "depth": 1,
            "kind": "Seats", "open_count": 40, "total_count": 100,
            "is_on_sale": True, "is_selectable": True, "sort_order": 1,
        },
        {
            # Full capacity, nothing available, flagged unavailable: sold out.
            "id": "sold-out", "parent_id": "stand", "code": "B", "name": "B", "depth": 1,
            "kind": "Seats", "open_count": 0, "total_count": 200,
            "is_on_sale": False, "is_selectable": False, "sort_order": 2,
        },
    ]
    totals = ktckts.summarise(segments)

    assert totals["capacity"] == 300
    assert totals["available"] == 40
    assert totals["sold"] == 260, "the 200 sold-out seats must count as sold"
    assert totals["percent_sold"] == 86.7
    assert totals["unused_blocks"] == 0


def test_blocks_with_no_inventory_are_ignored():
    """North ND/NE and the hospitality boxes are not part of the match."""
    segments = [
        {
            "id": "stand", "parent_id": None, "code": "ST", "name": "Stand", "depth": 0,
            "kind": "Virtual", "open_count": 40, "total_count": 100,
            "is_on_sale": True, "is_selectable": True, "sort_order": 0,
        },
        {
            "id": "live", "parent_id": "stand", "code": "A", "name": "A", "depth": 1,
            "kind": "Seats", "open_count": 40, "total_count": 100,
            "is_on_sale": True, "is_selectable": True, "sort_order": 1,
        },
        {
            "id": "unused", "parent_id": "stand", "code": "ND", "name": "ND", "depth": 1,
            "kind": "Seats", "open_count": 0, "total_count": 0,
            "is_on_sale": False, "is_selectable": False, "sort_order": 2,
        },
    ]
    totals = ktckts.summarise(segments)

    assert totals["capacity"] == 100, "an empty block adds no capacity"
    assert totals["sold"] == 60
    assert totals["unused_blocks"] == 1


def test_summarise_real_payload_is_internally_consistent(spec_payload):
    totals = ktckts.summarise(ktckts.parse_segments(spec_payload))
    assert totals["sold"] + totals["available"] == totals["capacity"]
    assert 0 <= totals["percent_sold"] <= 100
    assert totals["capacity"] > 0


def test_east_stand_sold_out_blocks_are_counted(spec_payload):
    """Regression: East A-D carry inventory with nothing available."""
    segments = ktckts.parse_segments(spec_payload)
    east = [s for s in segments if s["code"] in {"BLA", "BLB", "BLC", "BLD", "BLE"}]

    with_inventory = [s for s in east if s["total_count"] > 0]
    assert with_inventory, "East Stand should carry inventory"

    totals = ktckts.summarise(segments)
    # Every seat in those blocks that is not open must land in the sold total.
    assert totals["sold"] >= sum(
        s["total_count"] - s["open_count"] for s in with_inventory
    )


# -- prices --------------------------------------------------------------


def test_parse_prices(spec_payload):
    prices = ktckts.parse_prices(spec_payload)
    assert prices

    adult = next(
        p for p in prices
        if p["name"] == "Adult" and p["category_name"] == "The Climatec Group East Stand"
    )
    assert adult["amount_pence"] == 2300
    assert adult["price_type_id"].startswith("prtyp_")
    assert all(p["category_id"].startswith("prcat_") for p in prices)


def test_aggregate_prices_collapses_identical_categories(spec_payload):
    """The same six figures repeat across every area, so collapse them."""
    raw = ktckts.parse_prices(spec_payload)
    aggregated = ktckts.aggregate_prices(raw)

    assert len(raw) > len(aggregated)
    assert len({p["name"] for p in aggregated}) == len(aggregated), "one row per type"

    adult = next(p for p in aggregated if p["name"] == "Adult")
    assert adult["amount_pence"] == 2300
    assert adult["max_amount_pence"] == 2300
    assert adult["areas"] is None, "a ground-wide price needs no area note"
    assert adult["category_count"] > 1

    # Dearest first, so the headline adult price leads.
    amounts = [p["amount_pence"] for p in aggregated]
    assert amounts == sorted(amounts, reverse=True)
    assert aggregated[0]["name"] == "Adult"


def test_aggregate_prices_reports_a_range_when_areas_differ():
    """A genuine difference must show as a range, not one area winning."""
    raw = [
        {
            "category_id": "prcat_1", "category_name": "East", "price_type_id": "t1",
            "name": "Adult", "amount_pence": 2300, "full_amount_pence": 2300,
            "max_selectable": 12, "restriction": "", "sort_order": 0,
        },
        {
            "category_id": "prcat_2", "category_name": "West", "price_type_id": "t2",
            "name": "Adult", "amount_pence": 2600, "full_amount_pence": 2600,
            "max_selectable": 12, "restriction": "", "sort_order": 1,
        },
    ]
    adult = next(p for p in ktckts.aggregate_prices(raw) if p["name"] == "Adult")

    assert adult["amount_pence"] == 2300
    assert adult["max_amount_pence"] == 2600
    assert adult["category_count"] == 2


def test_aggregate_prices_drops_zero_priced_hospitality_placeholders(spec_payload):
    """Hospitality packages sit at zero because they sell elsewhere.

    Listing them beside real prices would read as free tickets.
    """
    aggregated = ktckts.aggregate_prices(ktckts.parse_prices(spec_payload))
    names = {p["name"] for p in aggregated}

    assert "Executive Box" not in names
    assert "1906 Club" not in names
    assert "Captains Suite" not in names
    # A genuinely free type offered across the ground survives.
    assert "Carer" in names
    assert names == {
        "Adult", "Senior (63+)", "Young Adult (17-22)",
        "Junior (9-16)", "Child (8 and under)", "Carer",
    }


def test_aggregate_prices_names_areas_for_area_specific_types():
    """A priced type confined to one area keeps that context."""
    raw = [
        {
            "category_id": f"prcat_{i}", "category_name": f"Area {i}", "price_type_id": f"t{i}",
            "name": "Adult", "amount_pence": 2300, "full_amount_pence": 2300,
            "max_selectable": 12, "restriction": "", "sort_order": i,
        }
        for i in range(4)
    ]
    raw.append({
        "category_id": "prcat_x", "category_name": "Directors Box", "price_type_id": "tx",
        "name": "Matchday Lunch", "amount_pence": 9500, "full_amount_pence": 9500,
        "max_selectable": 4, "restriction": "", "sort_order": 9,
    })
    aggregated = ktckts.aggregate_prices(raw)

    lunch = next(p for p in aggregated if p["name"] == "Matchday Lunch")
    assert lunch["areas"] == "Directors Box", "an area-specific price says where"
    assert next(p for p in aggregated if p["name"] == "Adult")["areas"] is None


@pytest.mark.parametrize(
    "text, expected",
    [("£23.00", 2300), ("£8.50", 850), ("£0.00", 0), ("£1,234.00", 123400), ("", None), (None, None)],
)
def test_parse_money(text, expected):
    assert ktckts.parse_money(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Friday, 28 August 2026 19:45", dt.datetime(2026, 8, 28, 19, 45)),
        ("Saturday, 5 September 2026 12:30", dt.datetime(2026, 9, 5, 12, 30)),
        ("not a date", None),
        ("", None),
    ],
)
def test_parse_kickoff(text, expected):
    assert ktckts.parse_kickoff(text) == expected


# -- fixture page scraping -----------------------------------------------


def test_fetch_fixtures_parses_the_listing(fixtures_html, monkeypatch):
    client = ktckts.KtcktsClient("https://example.test")
    monkeypatch.setattr(client, "_get_html", lambda path: fixtures_html)

    fixtures = client.fetch_fixtures()

    assert len(fixtures) == 11
    first = fixtures[0]
    assert first["code"] == "SEU2627H02"
    assert first["product_id"] == "prdct_019e4f0a-b219-4be7-abcf-4bb8d4e9e397"
    assert first["title"] == "Southend United v Kidderminster Harriers"
    assert first["opponent"] == "Kidderminster Harriers FC"
    assert first["venue"] == "Roots Hall Stadium"
    assert first["competition"] == "Enterprise National League"
    assert first["kickoff"] == dt.datetime(2026, 8, 28, 19, 45)
    assert first["is_home"] is True

    # Every fixture must carry the identifiers the availability calls need.
    assert all(f["product_id"].startswith("prdct_") for f in fixtures)
    assert len({f["product_id"] for f in fixtures}) == 11
    assert len({f["code"] for f in fixtures}) == 11


def test_fetch_fixtures_raises_when_markup_changes(monkeypatch):
    client = ktckts.KtcktsClient("https://example.test")
    monkeypatch.setattr(client, "_get_html", lambda path: "<html><body>nothing</body></html>")

    with pytest.raises(ktckts.KtcktsError):
        client.fetch_fixtures()


def test_summarise_uses_parentage_not_depth():
    """Upstream gives DEFAULT the same depth as its children.

    Totals must therefore be rolled up from real roots, or a stand sharing a
    depth with its parent would be counted twice.
    """
    segments = [
        {
            "id": "stand", "parent_id": None, "code": "ST", "name": "Stand",
            "depth": 0, "kind": "Virtual", "open_count": 10, "total_count": 100,
            "is_on_sale": True, "is_selectable": True, "sort_order": 0,
        },
        {
            # Same depth as its parent, exactly as ktckts reports the stands.
            "id": "block", "parent_id": "stand", "code": "A", "name": "A",
            "depth": 0, "kind": "Seats", "open_count": 10, "total_count": 100,
            "is_on_sale": True, "is_selectable": True, "sort_order": 1,
        },
    ]
    totals = ktckts.summarise(segments)

    assert totals["capacity"] == 100, "child must not be double counted"
    assert totals["available"] == 10
    assert totals["sold"] == 90


def test_parse_segments_reparents_stands_off_the_default_root(spec_payload):
    """Dropping DEFAULT must not leave children pointing at a missing row."""
    segments = ktckts.parse_segments(spec_payload)
    ids = {s["id"] for s in segments}

    assert all(
        s["parent_id"] is None or s["parent_id"] in ids for s in segments
    ), "no segment may reference a parent that was dropped"

    roots = [s for s in segments if s["parent_id"] is None]
    assert {r["code"] for r in roots} == {"EAS", "NTH", "NWW", "STH", "WES"}
