"""Ingest layer: maps ktckts responses onto the database.

Kept separate from views so refreshing can also be driven from a cron job or
shell without going through HTTP.
"""

import datetime as dt
import logging
import threading

from sqlalchemy import select

from app import app, db
from app import stadium_map
from app.ktckts import KtcktsClient
from app.models import (
    Fixture,
    FixturePrice,
    Segment,
    SegmentSnapshot,
    Snapshot,
    utcnow,
)

log = logging.getLogger(__name__)

_client = None
_fixtures_refreshed_at = None
_refresh_lock = threading.Lock()
_refresh_in_flight = False

# The venue map describes the ground, not the match: the bytes are identical
# for every fixture, so it is fetched once per process rather than per view.
_map_lock = threading.Lock()
_map_markup = None
_map_codes = ()


def client():
    global _client
    if _client is None:
        _client = KtcktsClient(
            app.config["KTCKTS_BASE_URL"],
            fixtures_path=app.config["KTCKTS_FIXTURES_PATH"],
            timeout=app.config["KTCKTS_TIMEOUT"],
            connect_timeout=app.config["KTCKTS_CONNECT_TIMEOUT"],
        )
    return _client


# -- fixtures ------------------------------------------------------------


def refresh_fixtures():
    """Sync the fixture list. Returns (added, updated)."""
    fixtures = client().fetch_fixtures()
    added = updated = 0

    for data in fixtures:
        fixture = db.session.scalar(
            select(Fixture).where(Fixture.product_id == data["product_id"])
        )
        if fixture is None:
            fixture = Fixture(product_id=data["product_id"], first_seen=utcnow())
            db.session.add(fixture)
            added += 1
        else:
            updated += 1

        fixture.code = data["code"]
        fixture.slug = data["slug"]
        fixture.url = data["url"]
        fixture.title = data["title"]
        fixture.opponent = data["opponent"]
        fixture.home_crest = data["home_crest"]
        fixture.away_crest = data["away_crest"]
        fixture.venue = data["venue"]
        fixture.competition = data["competition"]
        fixture.kickoff = data["kickoff"]
        fixture.is_home = data["is_home"]

    db.session.commit()
    global _fixtures_refreshed_at
    _fixtures_refreshed_at = utcnow()
    log.info("Fixture refresh: %s added, %s updated", added, updated)
    return added, updated


def fixtures_are_stale():
    """True when the fixture list has not been re-scraped recently.

    Tracked in memory rather than from a column: fixture rows only change when
    the club adds a match, so any timestamp on them would read as stale
    forever. Under multiple workers each simply refreshes on its own hour.
    """
    if _fixtures_refreshed_at is None:
        return True
    age = (utcnow() - _fixtures_refreshed_at).total_seconds()
    return age > app.config["FIXTURE_REFRESH_SECONDS"]


def _refresh_fixtures_in_background():
    """Re-scrape the fixture list without holding up a page render."""
    global _refresh_in_flight

    with _refresh_lock:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True

    def run():
        global _refresh_in_flight, _fixtures_refreshed_at
        try:
            with app.app_context():
                refresh_fixtures()
        except Exception:  # noqa: BLE001 - the stale list still renders
            # Stamp the attempt anyway, so a persistently broken upstream is
            # retried on the normal interval instead of on every request.
            _fixtures_refreshed_at = utcnow()
            log.exception("Background fixture refresh failed")
        finally:
            with _refresh_lock:
                _refresh_in_flight = False

    threading.Thread(target=run, name="fixture-refresh", daemon=True).start()


def ensure_fixtures():
    """Make sure fixtures exist, refreshing stale ones out of band.

    Only an empty table blocks the request: there is nothing to render without
    it. Once some fixtures are known, a stale list is refreshed in the
    background so a slow or unreachable upstream cannot stall the page.
    """
    if db.session.scalar(select(db.func.count(Fixture.id))) == 0:
        try:
            return refresh_fixtures()
        except Exception:  # noqa: BLE001 - render the empty state instead
            log.exception("Initial fixture load failed")
        return 0, 0

    if fixtures_are_stale():
        _refresh_fixtures_in_background()
    return 0, 0


# -- segments and prices -------------------------------------------------


def sync_segments(segments):
    """Upsert the stadium catalogue.

    Parents are written before children so the self-referential foreign key
    is always satisfiable.
    """
    known = {s.id: s for s in db.session.scalars(select(Segment)).all()}
    for data in sorted(segments, key=lambda s: s["depth"]):
        segment = known.get(data["id"])
        if segment is None:
            segment = Segment(id=data["id"])
            db.session.add(segment)
            known[segment.id] = segment
        segment.parent_id = data["parent_id"]
        segment.code = data["code"]
        segment.name = data["name"]
        segment.depth = data["depth"]
        segment.kind = data["kind"]
        segment.capacity = data["total_count"]
        segment.sort_order = data["sort_order"]
    db.session.flush()


def sync_prices(fixture, prices):
    """Replace this fixture's aggregated price rows."""
    existing = {
        p.name: p
        for p in db.session.scalars(
            select(FixturePrice).where(FixturePrice.fixture_id == fixture.id)
        ).all()
    }
    seen = set()

    for data in prices:
        seen.add(data["name"])
        price = existing.get(data["name"])
        if price is None:
            price = FixturePrice(fixture_id=fixture.id, name=data["name"])
            db.session.add(price)
        price.amount_pence = data["amount_pence"]
        price.max_amount_pence = data["max_amount_pence"]
        price.restriction = data["restriction"]
        price.areas = data["areas"]
        price.category_count = data["category_count"]
        price.sort_order = data["sort_order"]

    for name, price in existing.items():
        if name not in seen:
            db.session.delete(price)


# -- snapshots -----------------------------------------------------------


def _should_snapshot(fixture):
    latest = fixture.latest_snapshot()
    if latest is None:
        return True
    age = (utcnow() - latest.captured_at).total_seconds()
    return age >= app.config["SNAPSHOT_MIN_INTERVAL_SECONDS"]


def refresh_fixture(fixture, force_snapshot=False):
    """Fetch live availability for one fixture and persist it.

    Returns the availability payload with a ``snapshot`` key holding the row
    that was written, or the most recent one if the interval guard skipped it.
    """
    availability = client().fetch_availability(fixture.product_id)
    segments = availability["segments"]
    totals = availability["totals"]

    sync_segments(segments)
    sync_prices(fixture, availability["prices"])

    fixture.last_refreshed = utcnow()
    fixture.on_sale = any(s["is_on_sale"] for s in segments)

    snapshot = None
    if force_snapshot or _should_snapshot(fixture):
        snapshot = Snapshot(
            fixture_id=fixture.id,
            captured_at=utcnow(),
            capacity=totals["capacity"],
            available=totals["available"],
            sold=totals["sold"],
            unused_blocks=totals["unused_blocks"],
        )
        db.session.add(snapshot)
        db.session.flush()

        # Store leaves only; virtual parents are recomputed on read.
        parent_ids = {s["parent_id"] for s in segments if s["parent_id"]}
        for data in segments:
            if data["id"] in parent_ids or data["total_count"] <= 0:
                continue
            db.session.add(
                SegmentSnapshot(
                    snapshot_id=snapshot.id,
                    segment_id=data["id"],
                    open_count=data["open_count"],
                    total_count=data["total_count"],
                    is_on_sale=data["is_on_sale"],
                )
            )

    db.session.commit()

    availability["snapshot"] = snapshot or fixture.latest_snapshot()
    return availability


def refresh_all(force_snapshot=True):
    """Refresh every upcoming fixture. Intended for cron."""
    refresh_fixtures()
    results = []
    for fixture in upcoming_fixtures():
        try:
            refresh_fixture(fixture, force_snapshot=force_snapshot)
            results.append((fixture.code, "ok"))
        except Exception as exc:  # noqa: BLE001 - one bad fixture must not stop the run
            db.session.rollback()
            log.exception("Refresh failed for %s", fixture.code)
            results.append((fixture.code, f"error: {exc}"))
    return results


def venue_map():
    """Return the prepared stadium SVG, fetching it at most once.

    Any upcoming fixture will do as the productId; the response does not vary
    by match.
    """
    global _map_markup, _map_codes

    if _map_markup is not None:
        return _map_markup, _map_codes

    with _map_lock:
        if _map_markup is not None:
            return _map_markup, _map_codes

        fixture = db.session.scalar(select(Fixture).order_by(Fixture.kickoff.asc()))
        if fixture is None:
            raise LookupError("No fixture available to fetch the venue map with")

        markup, codes = stadium_map.prepare(client().fetch_map(fixture.product_id))
        _map_markup, _map_codes = markup, tuple(codes)
        log.info("Venue map loaded: %s blocks", len(codes))
        return _map_markup, _map_codes


# -- queries -------------------------------------------------------------


def upcoming_fixtures():
    cutoff = utcnow() - dt.timedelta(hours=4)  # keep a match visible while it's on
    return list(
        db.session.scalars(
            select(Fixture)
            .where(Fixture.kickoff >= cutoff)
            .order_by(Fixture.kickoff.asc())
        )
    )


def find_fixture(code):
    if not code:
        return None
    return db.session.scalar(select(Fixture).where(Fixture.code == code.upper()))


def build_segment_tree(segments):
    """Nest flat segment dicts into a tree, recomputing parent roll-ups.

    Parent open/total counts come straight from the API, but recomputing them
    from children keeps the tree self-consistent if a child is filtered out.
    """
    by_id = {}
    roots = []
    for data in sorted(segments, key=lambda s: (s["depth"], s["sort_order"])):
        node = dict(data, children=[])
        by_id[node["id"]] = node
        parent = by_id.get(node["parent_id"])
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)

    def prune(node):
        node["children"] = [prune(c) for c in node["children"]]
        node["sold"] = max(0, node["total_count"] - node["open_count"])
        # A block with inventory and nothing left is sold out; one with no
        # inventory was never part of this match.
        node["in_use"] = node["total_count"] > 0
        node["sold_out"] = node["in_use"] and node["open_count"] == 0
        node["state"] = stadium_map.classify(node)
        return node

    return [prune(r) for r in roots]
