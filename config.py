import os

BASEDIR = os.path.abspath(os.path.dirname(__file__))

SQLALCHEMY_DATABASE_URI = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASEDIR, "app.db")
)
SQLALCHEMY_TRACK_MODIFICATIONS = False

# --- ktckts data source -------------------------------------------------
# Southend United moved ticketing to Kaizen's ktckts platform. Fixtures are
# listed on a "brand" page; per-fixture availability comes from two JSON
# endpoints that require an antiforgery token lifted from any page load.
KTCKTS_BASE_URL = os.environ.get("KTCKTS_BASE_URL", "https://southendunitedfc.ktckts.com")
KTCKTS_FIXTURES_PATH = os.environ.get("KTCKTS_FIXTURES_PATH", "/brand/match-tickets")
KTCKTS_SEASON_PATH = os.environ.get("KTCKTS_SEASON_PATH", "/brand/season")
KTCKTS_TIMEOUT = int(os.environ.get("KTCKTS_TIMEOUT", "30"))
# Kept short so a host without IPv6 routing fails over to IPv4 quickly.
KTCKTS_CONNECT_TIMEOUT = int(os.environ.get("KTCKTS_CONNECT_TIMEOUT", "5"))

# How long a fetched availability read is served from cache, and the minimum
# gap between persisted snapshots. Snapshots drive the historic chart, so
# writing one per page view would bloat the table for no extra resolution.
AVAILABILITY_CACHE_SECONDS = int(os.environ.get("AVAILABILITY_CACHE_SECONDS", "120"))
SNAPSHOT_MIN_INTERVAL_SECONDS = int(os.environ.get("SNAPSHOT_MIN_INTERVAL_SECONDS", "600"))

# The fixture list changes rarely; refresh it lazily rather than per request.
FIXTURE_REFRESH_SECONDS = int(os.environ.get("FIXTURE_REFRESH_SECONDS", "3600"))

# Guards the /admin/* endpoints when set.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")

# --- analytics ----------------------------------------------------------
# GA4 measurement ID for sufc-tickets.chris-hunt.net. It is a public
# identifier — it ships in the page source of every site that uses one — so it
# lives here rather than in a secret, which also keeps the deploy from needing
# an extra environment variable in the cluster manifest. Set it to an empty
# string to serve the site without analytics.
GA_MEASUREMENT_ID = os.environ.get("GA_MEASUREMENT_ID", "G-V6DXY889G9")
