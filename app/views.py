import datetime as dt
import functools
import hashlib
import hmac
import logging
import threading

from flask import abort, jsonify, redirect, render_template, request, url_for

from app import app, db, service
from app.ktckts import KtcktsError
from app.models import FixturePrice, Snapshot

log = logging.getLogger(__name__)


# -- caching -------------------------------------------------------------


def _json_response(payload, max_age):
    """Serialise a payload and let clients and proxies reuse it.

    The server-side cache below spares the database; these headers spare the
    process entirely, because a browser refresh or anything sitting in front of
    the ingress can answer from its own copy. Nothing here varies by caller, so
    the cache is public. An ETag on top turns the request that does come back
    after ``max_age`` into a 304 with no body.
    """
    response = jsonify(payload)
    if max_age:
        response.headers["Cache-Control"] = (
            f"public, max-age={max_age}, stale-while-revalidate={max_age * 5}"
        )
        response.add_etag()
        return response.make_conditional(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def cached_json(config_key):
    """Cache a read-only JSON view in process, keyed on its path.

    The TTL is read from config on each call rather than closed over, so a test
    or a running process can change it. A TTL of 0 disables caching for that
    endpoint while leaving the route otherwise untouched.

    Wrapped views return the payload itself, not a response. ``abort`` still
    works: it raises past this and nothing is cached.

    Keyed on the resolved view arguments rather than the raw path, and
    lowercased to match how fixture codes are looked up. Keying on the path
    would file ``/api/seu2627h02/historic`` and ``/api/SEU2627H02/historic``
    separately, which is both a wasted fetch and a way to push real entries out
    of a cache that only holds so many. An unknown code aborts before it gets
    here, so the number of entries stays bounded by the fixtures that exist.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            ttl = app.config.get(config_key) or 0
            if not ttl:
                return _json_response(view(*args, **kwargs), 0)

            args_key = ":".join(
                f"{name}={str(value).lower()}"
                for name, value in sorted((request.view_args or {}).items())
            )
            key = f"view:{request.endpoint}:{args_key}"
            payload = app.cache.get(key)
            if payload is None:
                payload = view(*args, **kwargs)
                app.cache.set(key, payload, timeout=ttl)
            return _json_response(payload, ttl)

        return wrapper

    return decorator


# Serialises the upstream fetch behind /api/<code>/latest, per fixture.
#
# The cache check and the fetch that fills it are two steps, so without this
# every request arriving in the gap does its own pair of upstream calls: one
# visitor costs the club one read, fifty arriving together cost it fifty. The
# first caller through fetches; the rest wait on the lock and then find the
# cache warm. Keyed per fixture so a slow read of one match does not hold up
# another.
_fetch_locks = {}
_fetch_locks_guard = threading.Lock()


def _fetch_lock(code):
    with _fetch_locks_guard:
        lock = _fetch_locks.get(code)
        if lock is None:
            lock = _fetch_locks[code] = threading.Lock()
        return lock


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
    """Gate admin endpoints on ADMIN_TOKEN.

    Refuses when no token is configured, rather than waving everyone through.
    These endpoints trigger a full re-scrape of the club's site and are
    reachable through the ingress, so an unset token is a misconfiguration to
    fail on and not a mode to run in. The mismatch case reads the same to a
    caller either way.

    ``X-Admin-Token`` is preferred over ``?token=``, which is still accepted
    for the existing cron: query strings are written to ingress access logs and
    the header is not.
    """

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        expected = app.config.get("ADMIN_TOKEN")
        if not expected:
            log.error("ADMIN_TOKEN is not configured; refusing %s", request.path)
            abort(403)

        supplied = request.headers.get("X-Admin-Token") or request.args.get("token") or ""
        # Constant time, so a caller cannot learn the token a character at a
        # time from how long the comparison takes.
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            abort(403)
        return view(*args, **kwargs)

    return wrapper


# -- pages ---------------------------------------------------------------


@app.route("/")
@app.route("/<code:game_code>")
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
@cached_json("FIXTURES_CACHE_SECONDS")
def api_fixtures():
    service.ensure_fixtures()
    return [_fixture_summary(f) for f in service.upcoming_fixtures()]


@app.route("/api/<code:game_code>/latest")
def api_latest(game_code):
    """Live availability, cached so a busy page does not hammer ktckts."""
    fixture = service.find_fixture(game_code)
    if fixture is None:
        abort(404)

    cache_key = f"latest:{fixture.code}"
    cached = app.cache.get(cache_key)
    if cached is not None:
        return _json_response(cached, app.config["AVAILABILITY_CACHE_SECONDS"])

    with _fetch_lock(fixture.code):
        # Re-checked inside the lock: whoever held it was very likely filling
        # this exact key, and waiting for a fetch only to repeat it would
        # defeat the point of queueing at all.
        cached = app.cache.get(cache_key)
        if cached is not None:
            return _json_response(cached, app.config["AVAILABILITY_CACHE_SECONDS"])

        try:
            availability = service.refresh_fixture(fixture)
        except KtcktsError as exc:
            log.error("ktckts error for %s: %s", fixture.code, exc)
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:  # noqa: BLE001 - upstream is not ours to trust
            db.session.rollback()
            log.exception("Failed to refresh %s", fixture.code)
            return jsonify({"error": f"Could not reach the ticketing site: {exc}"}), 502

        return _latest_payload(fixture, availability, cache_key)


def _latest_payload(fixture, availability, cache_key):
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
    return _json_response(payload, app.config["AVAILABILITY_CACHE_SECONDS"])


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


@app.route("/api/<code:game_code>/historic")
@cached_json("HISTORIC_CACHE_SECONDS")
def api_historic(game_code):
    """Snapshot history for the trend chart.

    Every snapshot for the fixture is read and then thinned in Python, so the
    cost of this grows for the whole time a match is on sale — a fixture listed
    for three months with the refresh cron running every quarter of an hour is
    several thousand rows to produce roughly a thousand points. It is worth
    caching for that reason alone, and there is nothing to lose by it: the rows
    only change when a snapshot is written, at most every ten minutes.
    """
    fixture = service.find_fixture(game_code)
    if fixture is None:
        abort(404)

    snapshots = (
        db.session.query(Snapshot)
        .filter(Snapshot.fixture_id == fixture.id)
        .order_by(Snapshot.captured_at.asc())
        .all()
    )
    # Today at full resolution, earlier days at one reading every two hours.
    snapshots = service.thin_history(snapshots, service.local_day_start())
    return [
        {
            "t": s.captured_at.replace(tzinfo=dt.timezone.utc).isoformat(),
            "sold": s.sold,
            "available": s.available,
            "capacity": s.capacity,
            "percent_sold": s.percent_sold,
        }
        for s in snapshots
    ]


@app.route("/api/<code:game_code>/prices")
@cached_json("PRICES_CACHE_SECONDS")
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
    return [
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


# -- admin ---------------------------------------------------------------


# Held for the length of a full refresh, which walks every fixture and makes
# two upstream calls for each. There are only ever a couple of workers, so two
# overlapping runs — the cron firing while a manual one is still going, or a
# retry after a slow response — would tie up the whole process and leave
# nothing to serve the site with.
_refresh_guard = threading.Lock()


@app.route("/admin/load")
@require_admin
def admin_load():
    service.refresh_fixtures()
    return redirect(url_for("home"))


@app.route("/admin/refresh")
@require_admin
def admin_refresh():
    if not _refresh_guard.acquire(blocking=False):
        log.info("Refresh already running; declining the overlapping request")
        return jsonify({"status": "already running", "refreshed": []}), 409
    try:
        results = service.refresh_all()
    finally:
        _refresh_guard.release()
    return jsonify({"refreshed": [{"code": c, "status": s} for c, s in results]})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404
