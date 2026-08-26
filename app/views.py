import datetime as dt
import functools
import hashlib
import logging

from flask import abort, jsonify, redirect, render_template, request, url_for

from app import app, db, service
from app.ktckts import KtcktsError
from app.models import FixturePrice, Snapshot

log = logging.getLogger(__name__)


def _serialise_tree(nodes):
    return [
        {
            "code": n["code"],
            "name": n["name"],
            "kind": n["kind"],
            "depth": n["depth"],
            "open": n["open_count"],
            "total": n["total_count"],
            "sold": n["sold"],
            "in_use": n["in_use"],
            "sold_out": n["sold_out"],
            "has_seats": n.get("has_seats", False),
            "away": n.get("away", False),
            "state": n["state"],
            "buyable": n["is_on_sale"],
            "children": _serialise_tree(n["children"]),
        }
        for n in nodes
    ]


def _fixture_summary(fixture):
    latest = fixture.latest_snapshot()
    return {
        "code": fixture.code,
        "title": fixture.title,
        "opponent": fixture.opponent,
        "kickoff": fixture.kickoff.isoformat() if fixture.kickoff else None,
        "venue": fixture.venue,
        "competition": fixture.competition,
        "crest": fixture.away_crest,
        "url": fixture.url,
        "on_sale": fixture.on_sale,
        "sold": latest.sold if latest else None,
        "available": latest.available if latest else None,
        "capacity": latest.capacity if latest else None,
        "percent_sold": latest.percent_sold if latest else None,
    }


def require_admin(view):
    """Gate admin endpoints on ADMIN_TOKEN when one is configured."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        expected = app.config.get("ADMIN_TOKEN")
        if expected:
            supplied = request.args.get("token") or request.headers.get("X-Admin-Token")
            if supplied != expected:
                abort(403)
        return view(*args, **kwargs)

    return wrapper


# -- pages ---------------------------------------------------------------


@app.route("/")
@app.route("/<game_code>")
def home(game_code=""):
    service.ensure_fixtures()
    fixtures = service.upcoming_fixtures()
    seasons = service.season_fixtures()

    fixture = service.find_fixture(game_code) if game_code else None
    if game_code and fixture is None:
        abort(404)
    if fixture is None:
        fixture = fixtures[0] if fixtures else (seasons[0] if seasons else None)

    return render_template("index.html", fixture=fixture, fixtures=fixtures, season_fixtures=seasons)


# -- api -----------------------------------------------------------------


@app.route("/api/fixtures")
def api_fixtures():
    service.ensure_fixtures()
    return jsonify([_fixture_summary(f) for f in service.upcoming_fixtures()])


@app.route("/api/<game_code>/latest")
def api_latest(game_code):
    """Live availability, cached so a busy page does not hammer ktckts."""
    fixture = service.find_fixture(game_code)
    if fixture is None:
        abort(404)

    cache_key = f"latest:{fixture.code}"
    cached = app.cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        availability = service.refresh_fixture(fixture)
    except KtcktsError as exc:
        log.error("ktckts error for %s: %s", fixture.code, exc)
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:  # noqa: BLE001 - upstream is not ours to trust
        db.session.rollback()
        log.exception("Failed to refresh %s", fixture.code)
        return jsonify({"error": f"Could not reach the ticketing site: {exc}"}), 502

    totals = availability["totals"]
    tree = service.build_segment_tree(availability["segments"])

    payload = {
        "code": fixture.code,
        "title": fixture.title,
        "kickoff": fixture.kickoff.isoformat() if fixture.kickoff else None,
        "venue": fixture.venue,
        "competition": fixture.competition,
        "url": fixture.url,
        "on_sale": fixture.on_sale,
        "totals": {
            "capacity": totals["capacity"],
            "available": totals["available"],
            "sold": totals["sold"],
            "percent_sold": totals["percent_sold"],
            "unused_blocks": totals["unused_blocks"],
        },
        "seats": {
            "open": totals.get("seat_open"),
            "total": totals.get("seat_total"),
            "taken": totals.get("seat_taken"),
            "verified": totals.get("seat_verified"),
            # Non-zero simply means tickets moved between the two upstream
            # calls; it is not an error.
            "drift": totals.get("seat_drift"),
        },
        "stands": _serialise_tree(tree),
        "retrieved_at": dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"),
    }

    app.cache.set(cache_key, payload, timeout=app.config["AVAILABILITY_CACHE_SECONDS"])
    return jsonify(payload)


@app.route("/map.svg")
def venue_map():
    """The stadium plan, prepared for inlining and coloured by the client."""
    try:
        markup, _codes = service.venue_map()
    except LookupError:
        abort(404)
    except Exception as exc:  # noqa: BLE001 - the page degrades to the block list
        log.exception("Venue map unavailable")
        return jsonify({"error": str(exc)}), 502

    response = app.response_class(markup, mimetype="image/svg+xml")
    # The map changes about as often as the stadium does, but an ETag keeps a
    # stale copy from outliving a fix: revalidation costs a 304, and a day of
    # hard caching is a long time to serve the wrong picture.
    response.set_etag(hashlib.sha256(markup.encode("utf-8")).hexdigest()[:32])
    response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    return response.make_conditional(request)


@app.route("/api/<game_code>/historic")
def api_historic(game_code):
    """Snapshot history for the trend chart."""
    fixture = service.find_fixture(game_code)
    if fixture is None:
        abort(404)

    snapshots = (
        db.session.query(Snapshot)
        .filter(Snapshot.fixture_id == fixture.id)
        .order_by(Snapshot.captured_at.asc())
        .all()
    )
    return jsonify(
        [
            {
                "t": s.captured_at.replace(tzinfo=dt.timezone.utc).isoformat(),
                "sold": s.sold,
                "available": s.available,
                "capacity": s.capacity,
                "percent_sold": s.percent_sold,
            }
            for s in snapshots
        ]
    )


@app.route("/api/<game_code>/prices")
def api_prices(game_code):
    fixture = service.find_fixture(game_code)
    if fixture is None:
        abort(404)
    prices = (
        db.session.query(FixturePrice)
        .filter(FixturePrice.fixture_id == fixture.id)
        .order_by(FixturePrice.sort_order)
        .all()
    )
    return jsonify(
        [
            {
                "type": price.name,
                "amount": price.amount,
                "max_amount": price.max_amount,
                "varies": price.varies,
                "restriction": price.restriction or None,
                "areas": price.areas,
            }
            for price in prices
        ]
    )


# -- admin ---------------------------------------------------------------


@app.route("/admin/load")
@require_admin
def admin_load():
    service.refresh_fixtures()
    return redirect(url_for("home"))


@app.route("/admin/refresh")
@require_admin
def admin_refresh():
    results = service.refresh_all()
    return jsonify({"refreshed": [{"code": c, "status": s} for c, s in results]})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404
