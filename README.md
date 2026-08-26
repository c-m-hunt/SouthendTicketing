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
| `totalCount == 0` | No sellable inventory | East A, West P, hospitality boxes, unopened away blocks |

Outside the away end the zero-inventory set is identical across all eleven
fixtures, so those blocks are structurally not sold through this system
(season tickets, press and directors, and boxes sold via the separate
hospitality brand) rather than withheld for a particular match.

### The away end

`BLND`, `BLNE` and `BLNF` — ND, NE and NF at the far end of the North Bank —
are the away allocation, and how much of it the club opens depends on the
visiting support. So a zero-inventory away block means *closed for this
fixture*, not never sold here, and the two must not look alike.

`stadium_map.AWAY_BLOCKS` is the single source of that fact. Every block
carries an `away` flag through the API, which the page uses to outline those
blocks on the map, tag them AWAY, list them under their own heading, and
total them into an "away tickets sold" headline stat — including the ones
that are shut, since which of them is open is exactly what someone looking at
an away game wants to know. Those seats sell through the club's own system,
so they are already inside the overall sold figure; the tile pulls them back
out rather than adding to it. A closed away block is hatched
rather than greyed out: ktckts publishes a seat grid only for inventory on
sale, so `has_seats` is false for it, but the seats are plainly there.

So **sold is simply `capacity - available`** over blocks that carry
inventory, and zero-inventory blocks are excluded entirely. Whether a seat is
held by a season-ticket holder or was bought last night, it is gone either
way — which is also how the original site counted.

### The stadium map

`/api/product/map` returns the club's own Roots Hall plan as SVG, annotated
with a `ktckts` namespace that names each block:

```xml
<g ktckts:type="segment" ktckts:code="BLQ"> … <polygon ktckts:show-availability="true"/> </g>
```

Those codes match the segment codes exactly — 54 shapes, 54 blocks — so the
map is coloured by a direct lookup rather than any hand-maintained geometry.
It describes the venue rather than the match (byte-identical for every
fixture), so it is fetched once per process and served from `/map.svg`.

`app/stadium_map.py` prepares it: seat guide paths are dropped (ktckts grows
seat rows between them at render time, but statically they draw as black bars
across each block), the placeholder purple fill is stripped so the stylesheet
can colour blocks, block letters are made themeable, and anything active is
removed before it goes into the page.

### Away fixtures

Dropped. The old site scraped a separate away-tickets page that no longer
exists — ktckts lists only home fixtures — and its sold figure was guesswork
even then. `Fixture.is_home` is retained so away matches can be reintroduced
if the club starts selling them here.

### Analytics

Google Analytics 4, property **SUFC Ticket Stats** under the personal
`Chris Hunt` account (`396396`), web stream `sufc-tickets.chris-hunt.net`.

`GA_MEASUREMENT_ID` in `config.py` holds the measurement ID. It is a public
identifier — every site using GA ships one in its page source — so it is a
default in the repo rather than a secret, which keeps the deploy from needing
another environment variable in the cluster manifest. The tag renders only
when it is non-empty, so `GA_MEASUREMENT_ID=` turns analytics off for a local
run or a fork.

Nothing about consent is wired up: the tag sets GA's cookies as soon as the
page loads, which UK PECR expects visitors to have agreed to first. Options,
if that matters here, are a consent banner in front of the tag or cookieless
measurement (`client_storage: 'none'`), which costs returning-visitor
figures.

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

## Deployment

Live at **https://sufc-tickets.chris-hunt.net**, on a DigitalOcean Kubernetes
cluster. Pushing to `main` is the whole deploy:

```
push to main
  └─ .github/workflows/deploy.yml
       ├─ tests must pass
       ├─ build, push to ECR (sites/sufc-tickets)
       ├─ pull the image back and prove it boots
       └─ commit the new digest to c-m-hunt/server-setup
            └─ Spacelift applies it to the cluster
```

AWS access is by OIDC against a role scoped to this repo and branch, so no
long-lived keys are stored here. Two repository secrets are required:
`AWS_DEPLOY_ROLE_ARN` and `SERVER_REPO_DEPLOY_KEY`.

The smoke test is not redundant with the test job. Tests import from the source
tree, where every file obviously exists; the image is a different set of files
assembled by the Dockerfile, and a missing `COPY`, a dependency that only lives
in the dev group, or an unwritable `/data` all pass the suite and still produce
a container that dies at start-up. It runs before the digest bump, so a failure
leaves the cluster pointing at the last image known to boot.

### Cluster shape

The manifests live in `tf/modules/sites/sufc-tickets` in the server-setup repo.

Snapshots accumulate in SQLite on a ReadWriteOnce volume, which is what the
sales-over-time chart is drawn from. That forces **one replica** and the
`Recreate` strategy: a rolling update would need the new pod to attach the
volume while the old one still holds it, and would stall on Multi-Attach. A few
seconds of downtime per deploy buys history that survives one.

A CronJob calls `/admin/refresh` every 15 minutes so the chart keeps filling
when nobody is browsing. It goes over HTTP via the in-cluster Service rather
than mounting the volume itself, which would require it to land on the same
node. That endpoint is reachable through the ingress and triggers a full
re-scrape of the club's site, so `ADMIN_TOKEN` is always set.

## HTTP API

| Route | Returns |
| --- | --- |
| `GET /api/fixtures` | Upcoming fixtures with their latest totals |
| `GET /api/<code>/latest` | Live availability, including the full stand tree |
| `GET /api/<code>/historic` | Snapshot history for the trend chart |
| `GET /api/<code>/prices` | Prices grouped by category |
| `GET /map.svg` | Roots Hall plan, prepared for inlining |
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
