"""Tests for ingest and the HTTP routes, using recorded payloads."""

import datetime as dt
import threading
import time

import pytest

from conftest import FakeClient


FIXTURE_ROWS = [
    {
        "product_id": "prdct_test-1",
        "code": "SEUTESTH01",
        "slug": "southend-united-v-testers",
        "url": "https://example.test/event/seutesth01/southend-united-v-testers",
        "title": "Southend United v Testers",
        "opponent": "Testers FC",
        "home_crest": "https://example.test/home.png",
        "away_crest": "https://example.test/away.png",
        "venue": "Roots Hall Stadium",
        "competition": "Enterprise National League",
        "kickoff": dt.datetime.now() + dt.timedelta(days=7),
        "is_home": True,
    },
    {
        "product_id": "prdct_test-2",
        "code": "SEUTESTH02",
        "slug": "southend-united-v-past",
        "url": "https://example.test/event/seutesth02/southend-united-v-past",
        "title": "Southend United v Past",
        "opponent": "Past FC",
        "home_crest": None,
        "away_crest": None,
        "venue": "Roots Hall Stadium",
        "competition": "Enterprise National League",
        "kickoff": dt.datetime.now() - dt.timedelta(days=7),
        "is_home": True,
    },
]


@pytest.fixture
def seeded(flask_app, spec_payload, detail_payload, monkeypatch):
    """Install a fake ktckts client and load the recorded fixture data."""
    from app import ktckts, service

    availability = {
        "segments": ktckts.parse_segments(spec_payload),
        "prices": ktckts.aggregate_prices(ktckts.parse_prices(spec_payload)),
        "totals": ktckts.summarise(ktckts.parse_segments(spec_payload)),
    }
    seats = ktckts.parse_seat_status(detail_payload)
    availability["totals"].update(
        seat_open=seats["open"],
        seat_total=seats["total"],
        seat_taken=seats["taken"],
        seat_verified=seats["verified"],
        seat_drift=seats["open"] - availability["totals"]["available"],
    )

    fake = FakeClient(FIXTURE_ROWS, availability)
    monkeypatch.setattr(service, "client", lambda: fake)
    monkeypatch.setattr(service, "_fixtures_refreshed_at", None)
    monkeypatch.setattr(service, "_season_refreshed_at", None)
    flask_app.cache.clear()

    service.refresh_fixtures()
    return fake


# -- ingest --------------------------------------------------------------


def test_refresh_fixtures_is_idempotent(seeded, flask_app):
    from app import service
    from app.models import Fixture
    from app import db

    added, updated = service.refresh_fixtures()
    assert (added, updated) == (0, 2)
    assert db.session.query(Fixture).count() == 2


def test_upcoming_excludes_finished_matches(seeded):
    from app import service

    codes = [f.code for f in service.upcoming_fixtures()]
    assert codes == ["SEUTESTH01"]


def test_refresh_fixture_writes_a_snapshot(seeded):
    from app import db, service
    from app.models import Segment, SegmentSnapshot, Snapshot, FixturePrice

    fixture = service.find_fixture("SEUTESTH01")
    service.refresh_fixture(fixture, force_snapshot=True)

    snapshot = fixture.latest_snapshot()
    assert snapshot is not None
    assert snapshot.sold + snapshot.available == snapshot.capacity

    assert db.session.query(Segment).count() > 0
    assert db.session.query(SegmentSnapshot).count() > 0
    assert db.session.query(FixturePrice).count() > 0

    # Only leaf blocks are stored, so there must be fewer rows than segments.
    assert (
        db.session.query(SegmentSnapshot).count()
        < db.session.query(Segment).count()
    )
    assert db.session.query(Snapshot).count() == 1


def test_snapshot_interval_is_respected(seeded, flask_app):
    from app import db, service
    from app.models import Snapshot

    flask_app.config["SNAPSHOT_MIN_INTERVAL_SECONDS"] = 3600
    fixture = service.find_fixture("SEUTESTH01")

    service.refresh_fixture(fixture, force_snapshot=True)
    service.refresh_fixture(fixture)  # too soon; must not add a second row

    assert db.session.query(Snapshot).count() == 1


def test_segments_have_no_dangling_parents(seeded):
    from app import db, service
    from app.models import Segment

    fixture = service.find_fixture("SEUTESTH01")
    service.refresh_fixture(fixture, force_snapshot=True)

    ids = {s.id for s in db.session.query(Segment).all()}
    for segment in db.session.query(Segment).all():
        assert segment.parent_id is None or segment.parent_id in ids


def test_prices_are_replaced_not_duplicated(seeded):
    from app import db, service
    from app.models import FixturePrice

    fixture = service.find_fixture("SEUTESTH01")
    service.refresh_fixture(fixture, force_snapshot=True)
    first = db.session.query(FixturePrice).count()

    service.refresh_fixture(fixture, force_snapshot=True)
    assert db.session.query(FixturePrice).count() == first


# -- tree ----------------------------------------------------------------


def test_build_segment_tree_nests_and_has_single_roots(seeded, spec_payload):
    from app import ktckts, service

    segments = ktckts.parse_segments(spec_payload)
    tree = service.build_segment_tree(segments)

    assert {node["code"] for node in tree} == {"EAS", "NTH", "NWW", "STH", "WES"}

    south = next(n for n in tree if n["code"] == "STH")
    tiers = {c["code"] for c in south["children"]}
    assert tiers == {"STHLO", "STHUP"}, "South Stand should keep its two tiers"
    assert all(c["children"] for c in south["children"])

    # Every segment must appear exactly once in the tree.
    seen = []

    def walk(nodes):
        for node in nodes:
            seen.append(node["id"])
            walk(node["children"])

    walk(tree)
    assert len(seen) == len(segments)
    assert len(set(seen)) == len(seen)


def test_away_blocks_are_flagged_and_kept(seeded, spec_payload):
    """ND, NE and NF are the away end, and an unopened one still has to show.

    The club opens as much of it as the visiting support needs, so a block
    with no inventory is closed for this fixture rather than never sold here.
    """
    from app import ktckts, service

    tree = service.build_segment_tree(ktckts.parse_segments(spec_payload))
    north = next(n for n in tree if n["code"] == "NTH")
    blocks = {n["code"]: n for n in north["children"]}

    assert {c for c, n in blocks.items() if n["away"]} == {"BLND", "BLNE", "BLNF"}
    # Closed for this fixture: hatched, not greyed out as seatless.
    assert blocks["BLND"]["in_use"] is False
    assert blocks["BLND"]["state"] == "unsold"
    # Opened, and coloured by what is left like any other block.
    assert blocks["BLNF"]["in_use"] is True
    assert blocks["BLNF"]["state"] == "roomy"
    assert blocks["BLNA"]["away"] is False


def test_api_latest_carries_the_away_flag(seeded, client):
    data = client.get("/api/SEUTESTH01/latest").get_json()
    north = next(s for s in data["stands"] if s["code"] == "NTH")
    away = {b["code"] for b in north["children"] if b["away"]}
    assert away == {"BLND", "BLNE", "BLNF"}


# -- routes --------------------------------------------------------------


def test_home_renders(seeded, client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Southend United v Testers" in body
    assert "Availability by stand" in body


def test_home_redirects_to_first_upcoming(seeded, client):
    body = client.get("/").get_data(as_text=True)
    assert 'data-code="SEUTESTH01"' in body


def test_unknown_fixture_is_404(seeded, client):
    assert client.get("/NOPE").status_code == 404


def test_api_latest(seeded, client):
    response = client.get("/api/SEUTESTH01/latest")
    assert response.status_code == 200
    data = response.get_json()

    totals = data["totals"]
    assert totals["sold"] + totals["available"] == totals["capacity"]
    assert data["seats"]["verified"] is True
    assert len(data["stands"]) == 5
    assert data["stands"][0]["children"], "stands should carry their blocks"


def test_api_latest_is_cached(seeded, client):
    client.get("/api/SEUTESTH01/latest")
    calls_after_first = seeded.calls
    client.get("/api/SEUTESTH01/latest")
    assert seeded.calls == calls_after_first, "second call should hit the cache"


def test_api_latest_reports_upstream_failure(seeded, client, monkeypatch, flask_app):
    from app import service

    class Broken:
        def fetch_availability(self, *a, **k):
            raise RuntimeError("upstream down")

    flask_app.cache.clear()
    monkeypatch.setattr(service, "client", lambda: Broken())

    response = client.get("/api/SEUTESTH01/latest")
    assert response.status_code == 502
    assert "error" in response.get_json()


# -- history thinning -----------------------------------------------------


class _Row:
    """Stands in for a Snapshot row; thin_history only reads captured_at."""

    def __init__(self, captured_at, sold):
        self.captured_at = captured_at
        self.sold = sold


def test_thin_history_keeps_todays_readings_whole():
    import datetime as dt

    from app import service

    day_start = dt.datetime(2026, 8, 25, 23, 0)  # midnight BST
    rows = [_Row(day_start + dt.timedelta(minutes=10 * i), 100 + i) for i in range(12)]

    kept = service.thin_history(rows, day_start)
    assert kept == rows, "today must not be thinned"


def test_thin_history_keeps_the_last_reading_of_each_two_hour_bucket():
    """Totals are cumulative, so the bucket's final reading is its figure."""
    import datetime as dt

    from app import service

    day_start = dt.datetime(2026, 8, 25, 23, 0)
    # Four readings across two buckets, all before today.
    rows = [
        _Row(dt.datetime(2026, 8, 24, 10, 5), 10),
        _Row(dt.datetime(2026, 8, 24, 11, 55), 20),  # last of the 10:00-12:00 bucket
        _Row(dt.datetime(2026, 8, 24, 12, 5), 30),
        _Row(dt.datetime(2026, 8, 24, 13, 40), 40),  # last of the 12:00-14:00 bucket
    ]

    kept = service.thin_history(rows, day_start)
    assert [r.sold for r in kept] == [20, 40]


def test_thin_history_flushes_the_last_old_bucket_before_today():
    """A part-filled bucket running up to midnight must still be emitted."""
    import datetime as dt

    from app import service

    day_start = dt.datetime(2026, 8, 25, 23, 0)
    rows = [
        _Row(dt.datetime(2026, 8, 25, 22, 5), 1),
        _Row(dt.datetime(2026, 8, 25, 22, 30), 2),  # last before the boundary
        _Row(dt.datetime(2026, 8, 25, 23, 5), 3),   # first of today
    ]

    assert [r.sold for r in service.thin_history(rows, day_start)] == [2, 3]


def test_thin_history_buckets_are_aligned_to_the_clock():
    """Two readings an hour apart can still land in different buckets.

    Windows are fixed on the clock, not measured from the first reading, so
    21:00 closes one bucket and 22:30 opens the next.
    """
    import datetime as dt

    from app import service

    day_start = dt.datetime(2026, 8, 25, 23, 0)
    rows = [
        _Row(dt.datetime(2026, 8, 25, 21, 0), 1),   # 20:00-22:00 window
        _Row(dt.datetime(2026, 8, 25, 22, 30), 2),  # 22:00-00:00 window
    ]

    assert [r.sold for r in service.thin_history(rows, day_start)] == [1, 2]


def test_thin_history_handles_an_empty_series():
    import datetime as dt

    from app import service

    assert service.thin_history([], dt.datetime(2026, 8, 25, 23, 0)) == []


def test_local_day_start_follows_british_summer_time():
    """UTC midnight would eat the first hour of today for half the year."""
    import datetime as dt

    from app import service

    # 26 Aug is BST (UTC+1): local midnight is 23:00 UTC the day before.
    summer = service.local_day_start(dt.datetime(2026, 8, 26, 12, 0))
    assert summer == dt.datetime(2026, 8, 25, 23, 0)

    # 26 Jan is GMT: local midnight is UTC midnight.
    winter = service.local_day_start(dt.datetime(2026, 1, 26, 12, 0))
    assert winter == dt.datetime(2026, 1, 26, 0, 0)


def test_api_historic_thins_older_days(seeded, client, flask_app):
    """The endpoint returns one point per two hours for anything before today."""
    import datetime as dt

    from app import db, service
    from app.models import Snapshot

    fixture = service.find_fixture("SEUTESTH01")
    db.session.query(Snapshot).filter(Snapshot.fixture_id == fixture.id).delete()

    # Start on a bucket boundary so the arithmetic below is plain.
    base = (service.local_day_start() - dt.timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # 24 readings 30 minutes apart across 12 hours: 6 two-hour buckets.
    for i in range(24):
        db.session.add(
            Snapshot(
                fixture_id=fixture.id,
                captured_at=base + dt.timedelta(minutes=30 * i),
                capacity=1000,
                available=1000 - i,
                sold=i,
            )
        )
    db.session.commit()

    data = client.get("/api/SEUTESTH01/historic").get_json()
    assert len(data) == 6, "12 hours of old readings should thin to 6 points"
    # Each point is the last reading of its bucket: indexes 3, 7, 11, ...
    assert [d["sold"] for d in data] == [3, 7, 11, 15, 19, 23]


def test_api_historic(seeded, client):
    from app import service

    service.refresh_fixture(service.find_fixture("SEUTESTH01"), force_snapshot=True)
    data = client.get("/api/SEUTESTH01/historic").get_json()
    assert len(data) >= 1
    assert {"t", "sold", "available", "capacity", "percent_sold"} <= set(data[0])


def test_api_prices_is_a_single_flat_list(seeded, client):
    from app import service

    service.refresh_fixture(service.find_fixture("SEUTESTH01"), force_snapshot=True)
    data = client.get("/api/SEUTESTH01/prices").get_json()

    assert data
    assert len({p["type"] for p in data}) == len(data), "one row per ticket type"

    adult = next(p for p in data if p["type"] == "Adult")
    assert adult["amount"] == 23.0
    assert adult["varies"] is False
    assert data[0]["type"] == "Adult", "dearest first"


def test_api_fixtures(seeded, client):
    data = client.get("/api/fixtures").get_json()
    assert [f["code"] for f in data] == ["SEUTESTH01"]


def test_admin_requires_token_when_configured(seeded, client, flask_app):
    flask_app.config["ADMIN_TOKEN"] = "s3cret"
    try:
        assert client.get("/admin/load").status_code == 403
        assert client.get("/admin/load?token=s3cret").status_code == 302
    finally:
        flask_app.config["ADMIN_TOKEN"] = None


def test_analytics_tag_is_rendered_when_configured(seeded, client, flask_app, monkeypatch):
    monkeypatch.setitem(flask_app.config, "GA_MEASUREMENT_ID", "G-TESTID1234")
    body = client.get("/").get_data(as_text=True)
    assert "googletagmanager.com/gtag/js?id=G-TESTID1234" in body
    assert 'gtag(\'config\', "G-TESTID1234")' in body


def test_analytics_tag_can_be_turned_off(seeded, client, flask_app, monkeypatch):
    """An empty measurement ID serves the site without the third-party script."""
    monkeypatch.setitem(flask_app.config, "GA_MEASUREMENT_ID", "")
    body = client.get("/").get_data(as_text=True)
    assert "googletagmanager" not in body


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}


# -- fixture refresh scheduling -----------------------------------------


def test_empty_table_loads_fixtures_synchronously(flask_app, monkeypatch):
    """With nothing to show, the first request must wait for real data."""
    from app import db, service
    from app.models import Fixture

    fake = FakeClient(FIXTURE_ROWS, {})
    monkeypatch.setattr(service, "client", lambda: fake)
    monkeypatch.setattr(service, "_fixtures_refreshed_at", None)

    assert db.session.query(Fixture).count() == 0
    service.ensure_fixtures()
    assert db.session.query(Fixture).count() == 2


def test_stale_refresh_does_not_block_the_request(seeded, monkeypatch):
    """A slow upstream must not hold up a page that already has fixtures."""
    from app import service

    calls = []
    monkeypatch.setattr(service, "_fixtures_refreshed_at", None)
    monkeypatch.setattr(
        service, "_refresh_fixtures_in_background", lambda: calls.append(1)
    )

    service.ensure_fixtures()
    assert calls == [1], "stale list should refresh out of band"


def test_fresh_fixtures_are_not_refreshed_again(seeded, monkeypatch):
    """A list that is neither empty nor stale must not be re-scraped."""
    from app import db, service
    from app.models import Fixture, utcnow

    # Season tickets are counted separately, and ensure_fixtures() refreshes
    # whenever none are stored regardless of staleness. Without one here the
    # assertion below would be measuring that clause rather than staleness.
    db.session.add(
        Fixture(
            product_id="prdct_test-season",
            code="SEUTESTS01",
            kind="season",
            title="2026/27 Season Ticket",
        )
    )
    db.session.commit()

    calls = []
    monkeypatch.setattr(service, "_fixtures_refreshed_at", utcnow())
    monkeypatch.setattr(
        service, "_refresh_fixtures_in_background", lambda: calls.append(1)
    )

    service.ensure_fixtures()
    assert calls == []


def test_initial_load_failure_renders_empty_state(flask_app, client, monkeypatch):
    """An unreachable upstream on a cold database must not 500."""
    from app import service

    class Broken:
        def fetch_fixtures(self):
            raise RuntimeError("upstream down")

    monkeypatch.setattr(service, "client", lambda: Broken())
    monkeypatch.setattr(service, "_fixtures_refreshed_at", None)

    response = client.get("/")
    assert response.status_code == 200
    assert "No fixtures on sale" in response.get_data(as_text=True)


# -- venue map -----------------------------------------------------------


def test_map_route_serves_the_prepared_svg(seeded, client, venue_map_svg, monkeypatch):
    from app import service

    fake = type("C", (), {"fetch_map": lambda self, pid: venue_map_svg})()
    monkeypatch.setattr(service, "client", lambda: fake)
    monkeypatch.setattr(service, "_map_markup", None)
    monkeypatch.setattr(service, "_map_codes", ())

    response = client.get("/map.svg")
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert response.headers.get("ETag")

    body = response.get_data(as_text=True)
    assert 'data-code="BLA"' in body
    assert "seats-top" not in body


def test_map_is_fetched_once_per_process(seeded, client, venue_map_svg, monkeypatch):
    """It describes the venue, not the match, so it must not refetch."""
    from app import service

    calls = []

    class Counting:
        def fetch_map(self, product_id):
            calls.append(product_id)
            return venue_map_svg

    monkeypatch.setattr(service, "client", lambda: Counting())
    monkeypatch.setattr(service, "_map_markup", None)
    monkeypatch.setattr(service, "_map_codes", ())

    client.get("/map.svg")
    client.get("/map.svg")
    assert len(calls) == 1


def test_stands_carry_a_map_state(seeded, client):
    """The map colours blocks from this, so every block needs one."""
    response = client.get("/api/SEUTESTH01/latest")
    data = response.get_json()

    def walk(nodes):
        for node in nodes:
            assert node["state"] in {"roomy", "tight", "soldout", "unsold", "empty"}
            walk(node["children"])

    walk(data["stands"])


# -- load shedding -------------------------------------------------------


def test_failing_season_scrape_backs_off(flask_app, monkeypatch):
    """A broken season page must not mean a re-scrape on every page view.

    ``ensure_fixtures`` goes looking whenever no season fixtures are stored.
    Nothing is stored when the scrape fails, so without a stamp recording the
    attempt that condition stays true and each request starts another full
    read of the club's site.
    """
    from app import db, service
    from app.models import Fixture, utcnow

    db.session.add(
        Fixture(
            product_id="prdct_test-match",
            code="SEUTESTH09",
            kind="match",
            kickoff=utcnow() + dt.timedelta(days=7),
        )
    )
    db.session.commit()

    calls = {"fixtures": 0, "season": 0}

    def ok_fixtures():
        calls["fixtures"] += 1
        return 0, 1

    def broken_season():
        calls["season"] += 1
        raise RuntimeError("season page markup changed")

    monkeypatch.setattr(service, "refresh_fixtures", ok_fixtures)
    monkeypatch.setattr(service, "refresh_season_fixtures", broken_season)
    monkeypatch.setattr(service, "_fixtures_refreshed_at", utcnow())
    monkeypatch.setattr(service, "_season_refreshed_at", None)

    for _ in range(5):
        service.ensure_fixtures()
        for thread in threading.enumerate():
            if thread.name == "fixture-refresh":
                thread.join(5)

    assert calls["season"] == 1, "five page views should cost one attempt, not five"
    assert calls["fixtures"] == 1


def test_junk_paths_never_reach_the_database(client, monkeypatch):
    """Scanners probing the root must be turned away at routing."""
    from app import service

    calls = []
    monkeypatch.setattr(service, "ensure_fixtures", lambda: calls.append(1))

    for path in ("/.env", "/wp-login.php", "/robots.txt", "/a"):
        assert client.get(path).status_code == 404, path

    assert calls == [], "no junk path should have loaded fixtures"


def test_real_fixture_codes_still_route(seeded, client):
    """The converter must not be so tight that it turns away real codes."""
    assert client.get("/SEUTESTH01").status_code == 200
    # Season codes come from a package slug, which may carry a hyphen.
    assert client.get("/SEU2627-HST").status_code == 404, "unknown, but routed"
    assert client.get("/NOPE").status_code == 404


def test_concurrent_cache_misses_make_one_upstream_call(seeded, client, flask_app):
    """Fifty visitors arriving together must not mean fifty upstream reads."""
    from app import service

    flask_app.cache.clear()
    slow = seeded
    original = slow.fetch_availability

    def fetch_availability(product_id, include_seats=True):
        time.sleep(0.2)  # widen the window between the cache check and the fill
        return original(product_id, include_seats)

    slow.fetch_availability = fetch_availability
    before = slow.calls

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                flask_app.test_client().get("/api/SEUTESTH01/latest").status_code
            )
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert results == [200] * 8
    assert slow.calls - before == 1, "the fetch should happen once and be shared"


def test_admin_refuses_when_no_token_is_configured(seeded, client, flask_app):
    """An unset token is a misconfiguration, not an open door."""
    flask_app.config["ADMIN_TOKEN"] = None
    assert client.get("/admin/load").status_code == 403
    assert client.get("/admin/refresh").status_code == 403


def test_admin_accepts_the_token_as_a_header(seeded, client, flask_app):
    flask_app.config["ADMIN_TOKEN"] = "s3cret"
    try:
        response = client.get("/admin/load", headers={"X-Admin-Token": "s3cret"})
        assert response.status_code == 302
        assert client.get("/admin/load", headers={"X-Admin-Token": "wrong"}).status_code == 403
    finally:
        flask_app.config["ADMIN_TOKEN"] = None


# -- endpoint caching ----------------------------------------------------


def test_api_historic_is_cached(seeded, client, flask_app, monkeypatch):
    from app import service

    service.refresh_fixture(service.find_fixture("SEUTESTH01"), force_snapshot=True)
    flask_app.cache.clear()

    calls = []
    original = service.thin_history
    monkeypatch.setattr(
        service,
        "thin_history",
        lambda *a, **k: (calls.append(1), original(*a, **k))[1],
    )

    first = client.get("/api/SEUTESTH01/historic")
    second = client.get("/api/SEUTESTH01/historic")

    assert first.get_json() == second.get_json()
    assert len(calls) == 1, "the second read should come from the cache"


def test_api_prices_is_cached(seeded, client, flask_app):
    from app import service

    service.refresh_fixture(service.find_fixture("SEUTESTH01"), force_snapshot=True)
    flask_app.cache.clear()

    assert client.get("/api/SEUTESTH01/prices").get_json()
    # Emptying the table would show through immediately if this were uncached.
    from app import db
    from app.models import FixturePrice

    db.session.query(FixturePrice).delete()
    db.session.commit()

    assert client.get("/api/SEUTESTH01/prices").get_json(), "should still serve the cached list"


def test_cached_endpoints_can_be_turned_off(seeded, client, flask_app):
    """A TTL of zero serves live data and tells clients not to store it."""
    flask_app.config["PRICES_CACHE_SECONDS"] = 0
    try:
        flask_app.cache.clear()
        response = client.get("/api/SEUTESTH01/prices")
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        flask_app.config["PRICES_CACHE_SECONDS"] = 300


@pytest.mark.parametrize(
    "path",
    ["/api/fixtures", "/api/SEUTESTH01/latest", "/api/SEUTESTH01/historic",
     "/api/SEUTESTH01/prices"],
)
def test_json_endpoints_are_publicly_cacheable(seeded, client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "public" in response.headers["Cache-Control"]
    assert "max-age=" in response.headers["Cache-Control"]
    assert response.headers.get("ETag")


def test_repeat_request_revalidates_to_304(seeded, client):
    """A browser coming back with the ETag should get no body."""
    first = client.get("/api/SEUTESTH01/historic")
    again = client.get(
        "/api/SEUTESTH01/historic", headers={"If-None-Match": first.headers["ETag"]}
    )
    assert again.status_code == 304
    assert again.get_data() == b""


def test_unknown_fixture_is_not_cached(seeded, client):
    """A 404 must not be stored under the path of a fixture added later."""
    assert client.get("/api/SEUTESTZZ99/historic").status_code == 404
    assert client.get("/api/SEUTESTZZ99/prices").status_code == 404


# -- retention -----------------------------------------------------------


def test_prune_reports_before_it_deletes(seeded, flask_app):
    """The default run must not touch anything."""
    from app import db, service
    from app.models import SegmentSnapshot, Snapshot, utcnow

    service.refresh_fixture(service.find_fixture("SEUTESTH01"), force_snapshot=True)
    snapshot = db.session.query(Snapshot).first()
    snapshot.captured_at = utcnow() - dt.timedelta(days=400)
    db.session.commit()

    before = db.session.query(SegmentSnapshot).count()
    assert before > 0
    assert service.count_segment_snapshots(180) == before
    assert db.session.query(SegmentSnapshot).count() == before, "counting must not delete"

    deleted = service.prune_segment_snapshots(180)
    assert deleted == before
    assert db.session.query(SegmentSnapshot).count() == 0
    # The chart is drawn from these, so they must survive the prune.
    assert db.session.query(Snapshot).count() > 0


def test_prune_keeps_recent_rows(seeded):
    from app import db, service
    from app.models import SegmentSnapshot

    service.refresh_fixture(service.find_fixture("SEUTESTH01"), force_snapshot=True)
    before = db.session.query(SegmentSnapshot).count()

    assert service.prune_segment_snapshots(180) == 0
    assert db.session.query(SegmentSnapshot).count() == before


# -- sqlite pragmas ------------------------------------------------------


def test_database_uses_wal_and_waits_on_a_busy_writer(flask_app):
    """Concurrent readers and writers, and a lock contest that waits."""
    from app import db

    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_cache_key_ignores_the_case_of_the_code(seeded, client, flask_app, monkeypatch):
    """Case variants must share one entry, not each claim their own."""
    from app import service

    service.refresh_fixture(service.find_fixture("SEUTESTH01"), force_snapshot=True)
    flask_app.cache.clear()

    calls = []
    original = service.thin_history
    monkeypatch.setattr(
        service,
        "thin_history",
        lambda *a, **k: (calls.append(1), original(*a, **k))[1],
    )

    first = client.get("/api/SEUTESTH01/historic")
    second = client.get("/api/seutesth01/historic")

    assert first.get_json() == second.get_json()
    assert len(calls) == 1, "differing only in case should not mean a second read"
