# SouthendTicketing

Live ticket availability for Southend United home fixtures at Roots Hall.

The club moved ticketing from a bespoke ASP.NET site to Kaizen's **ktckts**
platform, so this app was reworked around the new source. It now reads the
full stadium map — every stand, tier and block — plus per-seat status grids
and per-category pricing.

## The data source

Fixtures come from the match-tickets brand page; availability comes from two
JSON endpoints on `southendunitedfc.ktckts.com`. Neither is a documented API,
so `app/ktckts.py` records what was worked out by inspecting live traffic:

| What | Where |
| --- | --- |
| Fixture list, kickoff times, `productId` | `GET /brand/match-tickets` (HTML) |
| Blocks, open/total counts, prices | `POST /api/product/specificationCriteria?productId=…` |
| Per-seat status grid | `POST /api/product/detail?productId=…` |

Two things make this awkward:

1. **Antiforgery.** The JSON endpoints are POST-only and sit behind ASP.NET
   Core antiforgery. Loading any page sets a `csrfToken` cookie and embeds a
   matching token in a hidden input; the token must be echoed back in an
   `X-CSRF-TOKEN` header. One token works for every fixture, so a single
   session covers a whole refresh.

2. **Seat status bytes.** `detail` returns each block as a base64 grid, one
   byte per seat. Observed values are `0x01`, `0x03`, `0x07`, `0x09`, `0xF0`
   and `0xF8`. Bit `0x08` behaves as a modifier rather than a status of its
   own (`0x01`→`0x09`, `0xF0`→`0xF8`), so masking it off leaves three states:
   `0x07` gap, `0xF0` available, anything else taken. This was checked against
   each inventory set's own reported counts across 300 sets spanning six
   fixtures, with zero mismatches, and is re-checked on every fetch.

### What "sold" means

`openCount` is what is buyable right now; `totalCount` is the sellable
inventory in that block. A block showing nothing available can mean two
different things, and `isAvailable` is false for both — it is `totalCount`
that tells them apart:

| Signal | Meaning | Example |
| --- | --- | --- |
| `totalCount > 0`, `openCount == 0` | **Sold out** | East B–D, South Upper F–J |
| `totalCount == 0` | No sellable inventory, ever | East A, North ND/NE, West P, hospitality boxes |

The zero-inventory set is identical across all eleven fixtures, so those
blocks are structurally not sold through this system (season tickets, press
and directors, and boxes sold via the separate hospitality brand) rather than
withheld for a particular match.

So **sold is simply `capacity - available`** over blocks that carry
inventory, and zero-inventory blocks are excluded entirely. Whether a seat is
held by a season-ticket holder or was bought last night, it is gone either
way — which is also how the original site counted.

### Away fixtures

Dropped. The old site scraped a separate away-tickets page that no longer
exists — ktckts lists only home fixtures — and its sold figure was guesswork
even then. `Fixture.is_home` is retained so away matches can be reintroduced
if the club starts selling them here.

## Running it

Dependencies are managed with [uv](https://docs.astral.sh/uv/); `uv run`
creates and syncs the environment on first use, so there is no separate
install step.

```
git clone https://github.com/Chr12t0pher/SouthendTicketing.git
cd SouthendTicketing
uv run python wsgi.py
```

Then visit <http://127.0.0.1:5000>. The database is created on first run and
fixtures are loaded on the first page view.

On macOS port 5000 is taken by AirPlay Receiver, so use
`PORT=5057 uv run python wsgi.py` (or disable AirPlay Receiver in System
Settings).

### Command line

```
uv run southend-tickets fixtures          # re-scrape the fixture list
uv run southend-tickets refresh           # snapshot every fixture (for cron)
uv run southend-tickets show SEU2627H03   # print current availability
```

A refresh every 10–15 minutes via cron gives a useful sales curve:

```
*/15 * * * * cd /opt/app && /usr/local/bin/uv run --locked southend-tickets refresh >> /var/log/southend.log 2>&1
```

### Managing dependencies

```
uv sync                    # match the environment to uv.lock
uv add <package>           # add a runtime dependency
uv add --dev <package>     # add a dev-only dependency
uv lock --upgrade          # refresh the lockfile
```

`uv.lock` is committed, so builds and CI resolve to identical versions.

### Configuration

All optional, all via environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///app.db` | Database location |
| `AVAILABILITY_CACHE_SECONDS` | `120` | How long a live read is cached |
| `SNAPSHOT_MIN_INTERVAL_SECONDS` | `600` | Minimum gap between stored snapshots |
| `FIXTURE_REFRESH_SECONDS` | `3600` | How often the fixture list is re-scraped |
| `ADMIN_TOKEN` | unset | If set, required for `/admin/*` |
| `KTCKTS_BASE_URL` | the club's site | Override for testing |

### Docker

```
docker build -t southend-tickets .
docker run -p 8080:8080 -v southend-data:/data southend-tickets
```

The image is a multi-stage uv build: dependencies resolve from `uv.lock` in a
builder, and only the resulting venv and source reach the runtime image. It
runs as a non-root user, and the database lives on a `/data` volume so it
survives redeploys — `DATABASE_URL` already points there.

## HTTP API

| Route | Returns |
| --- | --- |
| `GET /api/fixtures` | Upcoming fixtures with their latest totals |
| `GET /api/<code>/latest` | Live availability, including the full stand tree |
| `GET /api/<code>/historic` | Snapshot history for the trend chart |
| `GET /api/<code>/prices` | Prices grouped by category |
| `GET /healthz` | Liveness probe |

## Schema

| Table | Holds |
| --- | --- |
| `fixture` | One row per match, with its `productId` |
| `segment` | Stadium catalogue — stands, tiers, blocks. Stable across fixtures |
| `price_category` / `fixture_price` | Price categories and per-fixture amounts |
| `snapshot` | Capacity, sold and available at one moment |
| `segment_snapshot` | Per-block counts within a snapshot (leaf blocks only) |

Segment ids and capacities are identical across every fixture, so the map is
stored once rather than per match.

## Tests

```
uv run pytest
```

Tests run against payloads recorded from the live site (`tests/data/`), so a
change in the upstream response shape fails here rather than in production.

## Disclaimer

This code and the [site](http://southend.cstevens.me) associated with it are
not affiliated with or endorsed by Southend United Football Club.

## License

MIT © [Chris Stevens](http://cstevens.biz)
