"""Client for the ktckts (Kaizen) ticketing platform.

Southend United's ticketing moved from a bespoke ASP.NET site to
southendunitedfc.ktckts.com. Nothing here is a documented public API, so the
shapes below were derived by inspecting live responses.

Two things make this awkward:

1. The JSON endpoints are POST-only and sit behind ASP.NET Core antiforgery.
   A GET of any page sets a ``csrfToken`` cookie and embeds a matching request
   token in a hidden input; the token must be echoed in an ``X-CSRF-TOKEN``
   header alongside the cookie. One token works for every product, so a single
   session is enough.

2. The fixture list is server-rendered HTML. Each card carries the productId
   in the id of its collapsible "Matchday Extras" panel, which saves a request
   per fixture compared with loading every event page.
"""

import base64
import datetime as dt
import logging
import re
from html import unescape

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Per-seat status bytes in detailedAvailability.statusBytes.
#
# Observed values are 0x01, 0x03, 0x07, 0x09, 0xF0 and 0xF8. Bit 0x08 behaves
# as a modifier rather than a status of its own (0x01 -> 0x09, 0xF0 -> 0xF8),
# so masking it off leaves three states. Checked against each inventory set's
# own openCount/totalCount over 300 sets spanning six fixtures: zero
# mismatches. Masking also means a new modifier bit degrades gracefully
# instead of landing in the unknown bucket.
SEAT_MODIFIER_MASK = ~0x08
SEAT_GAP = 0x07  # not part of this inventory set (padding in the grid)
SEAT_OPEN = 0xF0  # available to buy


def classify_seat(byte):
    """Return "gap", "open" or "taken" for one status byte."""
    base = byte & SEAT_MODIFIER_MASK
    if base == SEAT_GAP:
        return "gap"
    if base == SEAT_OPEN:
        return "open"
    return "taken"

_MONEY_RE = re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)")
_CSRF_RE = re.compile(r'<input name="csrfToken" type="hidden" value="([^"]+)"')
_PRODUCT_RE = re.compile(r"fixtureEnhance_(prdct_[0-9a-f-]+)")
_EVENT_URL_RE = re.compile(r"/event/([a-z0-9]+)/([a-z0-9-]+)")

# "Friday, 28 August 2026 19:45"
_DATE_FORMATS = ("%A, %d %B %Y %H:%M", "%A, %d %B %Y", "%d %B %Y %H:%M", "%d %B %Y")


class KtcktsError(RuntimeError):
    """Raised when the upstream ticketing site behaves unexpectedly."""


def parse_money(text):
    """Return pence from a rendered price like "£23.00", or None."""
    if not text:
        return None
    match = _MONEY_RE.search(text.replace(",", ""))
    if not match:
        return None
    return int(round(float(match.group(1)) * 100))


def parse_kickoff(text):
    """Parse the fixture card's date string into a naive datetime."""
    if not text:
        return None
    cleaned = " ".join(unescape(text).split())
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    log.warning("Could not parse kickoff date %r", text)
    return None


class KtcktsClient:
    """Fetches fixtures and availability, reusing one antiforgery token."""

    def __init__(
        self,
        base_url,
        fixtures_path="/brand/match-tickets",
        timeout=30,
        connect_timeout=5,
    ):
        self.base_url = base_url.rstrip("/")
        self.fixtures_path = fixtures_path
        # ktckts resolves to IPv6 addresses ahead of IPv4. urllib3 works
        # through them in order and, unlike curl, has no Happy Eyeballs
        # fallback, so on a host without IPv6 routing it stalls for the whole
        # timeout before reaching a working address. A short connect timeout
        # turns that into a fast failover; the read timeout stays generous.
        self.timeout = (connect_timeout, timeout)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._csrf_token = None

    # -- plumbing --------------------------------------------------------

    def _url(self, path):
        return self.base_url + path

    def _get_html(self, path):
        response = self.session.get(self._url(path), timeout=self.timeout)
        response.raise_for_status()
        # Every page embeds a token; cache the first one we see.
        match = _CSRF_RE.search(response.text)
        if match:
            self._csrf_token = match.group(1)
        return response.text

    def _ensure_token(self):
        if not self._csrf_token:
            self._get_html(self.fixtures_path)
        if not self._csrf_token:
            raise KtcktsError("Could not obtain a csrfToken from the ticketing site")
        return self._csrf_token

    def _post_json(self, path, product_id):
        token = self._ensure_token()
        response = self.session.post(
            self._url(path),
            params={"productId": product_id},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CSRF-TOKEN": token,
            },
            json={},
            timeout=self.timeout,
        )
        if response.status_code == 403:
            # Token expired mid-run; refresh once and retry.
            log.info("Antiforgery token rejected, refreshing")
            self._csrf_token = None
            token = self._ensure_token()
            response = self.session.post(
                self._url(path),
                params={"productId": product_id},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-CSRF-TOKEN": token,
                },
                json={},
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()

    # -- fixtures --------------------------------------------------------

    def fetch_fixtures(self):
        """Scrape the match-tickets brand page into fixture dicts."""
        html = self._get_html(self.fixtures_path)
        soup = BeautifulSoup(html, "html5lib")
        fixtures = []

        for article in soup.select("article.kaizen-fixture"):
            group = article.parent
            fixture = self._parse_fixture_card(article, group)
            if fixture:
                fixtures.append(fixture)

        if not fixtures:
            raise KtcktsError("No fixtures found; the fixture page markup may have changed")
        return fixtures

    def _parse_fixture_card(self, article, group):
        link = None
        for anchor in article.select("a[href]"):
            if "/event/" in anchor["href"]:
                link = anchor["href"]
                break
        if not link:
            return None

        url_match = _EVENT_URL_RE.search(link)
        if not url_match:
            return None
        code, slug = url_match.group(1), url_match.group(2)

        # The productId lives on the sibling collapse panel, not the card.
        product_id = None
        scope = group if group is not None else article
        product_match = _PRODUCT_RE.search(str(scope))
        if product_match:
            product_id = product_match.group(1)
        if not product_id:
            log.warning("No productId for fixture %s; skipping", code)
            return None

        def text_of(selector):
            node = article.select_one(selector)
            return node.get_text(strip=True) if node else None

        crests = [img.get("src") for img in article.select(".kaizen-fixture__crests img")]
        crest_alts = [img.get("alt", "") for img in article.select(".kaizen-fixture__crests img")]
        opponent = next(
            (alt for alt in crest_alts if alt and "Southend United" not in alt), None
        )

        title = text_of(".kaizen-fixture__title") or ""
        if not opponent and " v " in title:
            opponent = title.split(" v ", 1)[1].strip()

        sponsor = article.select_one(".kaizen-fixture__sponsor img")
        badge = (text_of(".kaizen-fixture__badge") or "HOME").strip().upper()

        return {
            "product_id": product_id,
            "code": code.upper(),
            "slug": slug,
            "url": link,
            "title": unescape(title),
            "opponent": unescape(opponent) if opponent else None,
            "home_crest": crests[0] if crests else None,
            "away_crest": crests[1] if len(crests) > 1 else None,
            "venue": text_of(".kaizen-fixture__meta .venue"),
            "competition": unescape(sponsor.get("alt")) if sponsor and sponsor.get("alt") else None,
            "kickoff": parse_kickoff(text_of(".kaizen-fixture__meta .date")),
            "is_home": badge != "AWAY",
        }

    # -- availability ----------------------------------------------------

    def fetch_map(self, product_id):
        """Return the venue map SVG.

        This is the club's own stadium plan, annotated with a ktckts namespace
        that names each block. It describes the venue rather than the match:
        the bytes are identical for every fixture, so callers should cache it.
        The nonce the page passes is not enforced, but it is sent anyway to
        stay close to what a browser does.
        """
        token = self._ensure_token()
        response = self.session.get(
            self._url("/api/product/map"),
            params={"productId": product_id},
            headers={"Accept": "image/svg+xml", "X-CSRF-TOKEN": token},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.text

    def fetch_specification_criteria(self, product_id):
        return self._post_json("/api/product/specificationCriteria", product_id)

    def fetch_detail(self, product_id):
        return self._post_json("/api/product/detail", product_id)

    def fetch_availability(self, product_id, include_seats=True):
        """Return normalised availability for one fixture.

        ``specificationCriteria`` alone gives every block's open/total, which
        is what the site displays. ``detail`` is fetched as well so the
        per-seat grid can independently confirm the decoding still holds.
        """
        spec = self.fetch_specification_criteria(product_id)
        segments = parse_segments(spec)
        prices = aggregate_prices(parse_prices(spec))

        seat_totals = None
        if include_seats:
            try:
                detail = self.fetch_detail(product_id)
                seat_totals = parse_seat_status(detail)
                with_seats = parse_seat_layouts(detail)
                for segment in segments:
                    segment["has_seats"] = segment["id"] in with_seats
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning("Seat-level detail unavailable for %s: %s", product_id, exc)

        totals = summarise(segments)
        if seat_totals:
            totals["seat_open"] = seat_totals["open"]
            totals["seat_total"] = seat_totals["total"]
            totals["seat_taken"] = seat_totals["taken"]
            totals["seat_verified"] = seat_totals["verified"]
            # Drift between the two endpoints is just tickets selling between
            # the two requests, so report it rather than treating it as fault.
            totals["seat_drift"] = seat_totals["open"] - totals["available"]

        return {"segments": segments, "prices": prices, "totals": totals}


# -- response parsing ----------------------------------------------------


def parse_segments(spec):
    """Flatten availableSegments into dicts, dropping the synthetic root.

    ktckts includes an unnamed DEFAULT segment carrying stadium-wide totals.
    It is the parent of the five stands even though it shares their depth of
    0, so keeping it would double every count. Dropping it means re-parenting
    the stands to nothing, otherwise they keep a foreign key to a row that is
    never stored.
    """
    raw_segments = spec.get("availableSegments") or []
    root_ids = {
        s.get("id") for s in raw_segments if (s.get("code") or "").strip() == "DEFAULT"
    }

    segments = []
    for order, raw in enumerate(raw_segments):
        code = (raw.get("code") or "").strip()
        if code == "DEFAULT":
            continue
        parent_id = raw.get("parentId")
        if parent_id in root_ids:
            parent_id = None
        segments.append(
            {
                "id": raw.get("id"),
                "parent_id": parent_id,
                "code": code,
                "name": (raw.get("name") or code or "").strip(),
                "depth": raw.get("depth") or 0,
                "kind": raw.get("type"),
                "open_count": raw.get("openCount") or 0,
                "total_count": raw.get("totalCount") or 0,
                "is_on_sale": bool(raw.get("isAvailable")),
                "is_selectable": bool(raw.get("isSelectable")),
                # Set from the seat layouts once detail has been fetched.
                "has_seats": False,
                "sort_order": order,
            }
        )
    return segments


def parse_prices(spec):
    """Flatten availablePrices.categories into per-category price rows."""
    prices = []
    categories = ((spec.get("availablePrices") or {}).get("categories")) or []
    for cat_order, category in enumerate(categories):
        category_id = category.get("id")
        if not category_id:
            continue
        for type_order, price_type in enumerate(category.get("priceTypes") or []):
            prices.append(
                {
                    "category_id": category_id,
                    "category_name": (category.get("name") or "").strip(),
                    "price_type_id": price_type.get("id"),
                    "name": (price_type.get("name") or "").strip(),
                    "amount_pence": parse_money(price_type.get("ticketValue")),
                    "full_amount_pence": parse_money(price_type.get("fullTicketValue")),
                    "max_selectable": price_type.get("maxSelectable"),
                    "restriction": (price_type.get("noneSelectableReason") or "").strip(),
                    "sort_order": cat_order * 100 + type_order,
                }
            )
    return prices


def aggregate_prices(prices):
    """Collapse per-category prices into one list per price type.

    ktckts prices every area separately, but in practice the same figures
    repeat across all of them: Adult is the same in all fifteen categories
    that offer it, and so on. Rendering a panel per category just repeats the
    same six numbers twenty times.

    Amounts are kept as a low/high pair rather than a single figure, so a
    genuine difference between areas shows as a range instead of one area
    silently winning.
    """
    total_categories = len({p["category_id"] for p in prices})
    by_name = {}

    for price in prices:
        entry = by_name.get(price["name"])
        if entry is None:
            entry = {
                "name": price["name"],
                "amount_pence": price["amount_pence"],
                "max_amount_pence": price["amount_pence"],
                "restriction": price["restriction"],
                "categories": [],
                "sort_order": price["sort_order"],
            }
            by_name[price["name"]] = entry

        amount = price["amount_pence"]
        if amount is not None:
            if entry["amount_pence"] is None:
                entry["amount_pence"] = entry["max_amount_pence"] = amount
            else:
                entry["amount_pence"] = min(entry["amount_pence"], amount)
                entry["max_amount_pence"] = max(entry["max_amount_pence"], amount)

        if price["category_name"] not in entry["categories"]:
            entry["categories"].append(price["category_name"])
        # Keep the first restriction seen; they are identical where they exist.
        if not entry["restriction"] and price["restriction"]:
            entry["restriction"] = price["restriction"]

    result = []
    for entry in by_name.values():
        count = len(entry["categories"])
        # Hospitality packages (Executive Box, 1906 Club, Captains Suite...)
        # appear at zero because they are priced through the separate
        # hospitality brand, not sold here. Listing them at "£0.00" alongside
        # real ticket prices reads as free, so they are left out. A genuinely
        # free type offered across the ground, like Carer, still shows.
        if count * 2 < total_categories and not entry["amount_pence"]:
            continue
        # A type offered by most areas is a general matchday price; one
        # confined to a corner of the ground is named so it still means
        # something once the per-area breakdown is gone.
        is_general = total_categories and count * 2 >= total_categories
        result.append(
            {
                "name": entry["name"],
                "amount_pence": entry["amount_pence"],
                "max_amount_pence": entry["max_amount_pence"],
                "restriction": entry["restriction"],
                "category_count": count,
                "areas": None if is_general else ", ".join(sorted(entry["categories"])),
                "sort_order": entry["sort_order"],
            }
        )

    # Dearest first, so the headline adult price leads.
    result.sort(key=lambda p: (-(p["amount_pence"] or 0), p["sort_order"]))
    for order, entry in enumerate(result):
        entry["sort_order"] = order
    return result


def parse_seat_status(detail):
    """Count seats from the base64 per-seat status grids.

    The result is checked against the same response's own openCount/totalCount
    rather than against a separately fetched payload: the two endpoints are
    distinct requests, so real sales landing between them would otherwise look
    like a decoding error.
    """
    availability = detail.get("detailedAvailability") or {}
    counts = {"open": 0, "taken": 0, "total": 0}
    mismatched_sets = 0

    for segment in availability.get("bySegment") or []:
        for inventory_set in segment.get("byInventorySet") or []:
            encoded = inventory_set.get("statusBytes")
            if not encoded:
                continue
            set_open = set_total = 0
            for byte in base64.b64decode(encoded):
                status = classify_seat(byte)
                if status == "gap":
                    continue
                set_total += 1
                if status == "open":
                    set_open += 1

            counts["open"] += set_open
            counts["total"] += set_total
            counts["taken"] += set_total - set_open

            if (
                inventory_set.get("openCount") != set_open
                or inventory_set.get("totalCount") != set_total
            ):
                mismatched_sets += 1

    counts["mismatched_sets"] = mismatched_sets
    counts["verified"] = mismatched_sets == 0
    counts["reported_open"] = availability.get("openCount")
    counts["reported_total"] = availability.get("totalCount")

    if mismatched_sets:
        log.warning(
            "Seat grid disagreed with reported counts in %s inventory set(s); "
            "the status byte encoding may have changed",
            mismatched_sets,
        )
    return counts


def parse_seat_layouts(detail):
    """Segment ids that have real seats drawn on the map.

    Separates two things that both report zero inventory: a block of actual
    seats that is simply not sold here (East A, the away end, the directors'
    box, press and broadcast) and a shape with no seating at all (the
    hospitality boxes and lounges).
    """
    details = (detail.get("segmentDetails") or {}).get("segmentDetails") or []
    with_seats = set()
    for segment in details:
        names = segment.get("seatNames") or []
        if any(name and name != "-" for name in names):
            with_seats.add(segment.get("id"))
    return with_seats


def summarise(segments):
    """Roll top-level stands up into stadium totals.

    Only root segments are summed; everything deeper is a child and would
    otherwise be counted twice. Roots are identified by parentage rather than
    by ``depth``, because upstream gives the DEFAULT row the same depth as the
    stands hanging off it.

    A block's ``totalCount`` is the inventory loaded for this fixture, which
    is what separates the two reasons a block shows nothing available:

    * ``totalCount == 0`` — the block holds no sellable inventory (East A,
      North ND and NE, West P, the hospitality boxes). These are constant
      across every fixture, so they are structurally not sold here rather
      than withheld for one match. They contribute nothing.
    * ``totalCount > 0`` with ``openCount == 0`` — every seat has gone, i.e.
      sold out (East B-D, South Upper F-J).

    ``isAvailable`` is false in both cases, so it cannot tell them apart and
    is kept only to describe whether a block is buyable right now.
    """
    capacity = 0
    available = 0

    for segment in segments:
        if segment["parent_id"] is not None:
            continue
        capacity += segment["total_count"]
        available += segment["open_count"]

    sold = max(0, capacity - available)

    # Leaves carrying no inventory: reported as a count of blocks, never as
    # seats, so they cannot distort the totals.
    parent_ids = {s["parent_id"] for s in segments if s["parent_id"]}
    unused_blocks = sum(
        1
        for s in segments
        if s["id"] not in parent_ids and s["total_count"] == 0
    )

    return {
        "capacity": capacity,
        "available": available,
        "sold": sold,
        "percent_sold": round(sold / capacity * 100, 1) if capacity else 0.0,
        "unused_blocks": unused_blocks,
    }
