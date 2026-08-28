import logging

from flask import Flask
from flask_caching import Cache
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.routing import BaseConverter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

log = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object("config")

app.cache = Cache(app, config={"CACHE_TYPE": "SimpleCache"})


class FixtureCodeConverter(BaseConverter):
    """Matches the shape of a ktckts fixture code and nothing else.

    ``/<game_code>`` sits at the root, so without this every scanner probing
    for ``/.env`` or ``/wp-login.php`` reaches the view and does four database
    queries before rendering a 404. Codes are alphanumeric (``SEU2627H03``,
    ``SEU2627HST``), with hyphens and underscores allowed for the package
    slugs season tickets are named from. Excluding dots and slashes is what
    turns the common junk paths away at routing, where they cost nothing.
    """

    regex = r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}"


app.url_map.converters["code"] = FixtureCodeConverter

db = SQLAlchemy(app)


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, _record):
    """Set the pragmas SQLite does not default to but this app needs.

    ``foreign_keys`` because segments are self-referential, so a dangling
    parent reference would otherwise be stored silently instead of failing
    loudly.

    ``journal_mode=WAL`` because reads and writes share this database: a page
    view refreshing availability writes segments, prices and a snapshot, and
    in the default rollback journal that blocks every reader for the duration.
    WAL lets them run concurrently. It is a persistent property of the file
    rather than of the connection, so it survives a restart — and an older
    build rolled back onto the same volume reads a WAL database quite happily,
    which is what keeps this safe to deploy against the existing history.

    ``busy_timeout`` because the writers that do still collide — a page view
    and the refresh cron landing together — should wait their turn rather than
    fail the request outright with "database is locked".
    """
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

from app import models, views  # noqa: E402,F401 - registers models and routes

with app.app_context():
    db.create_all()
    # Add kind column to existing databases that predate it.
    with db.engine.connect() as _conn:
        try:
            _conn.execute(
                db.text("ALTER TABLE fixture ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'match'")
            )
            _conn.commit()
        except Exception:
            pass  # Column already exists.

if not app.config.get("ADMIN_TOKEN"):
    # /admin/refresh re-scrapes the club's whole fixture list, and in the
    # cluster it is reachable through the ingress. Unset, the endpoints refuse
    # every caller rather than serving that to anyone who finds the URL, so
    # this is a warning about the cron being broken, not about exposure.
    log.warning("ADMIN_TOKEN is not set; /admin/* will refuse all requests")
