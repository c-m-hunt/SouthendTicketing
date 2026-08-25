import logging

from flask import Flask
from flask_caching import Cache
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = Flask(__name__)
app.config.from_object("config")

app.cache = Cache(app, config={"CACHE_TYPE": "SimpleCache"})

db = SQLAlchemy(app)


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
    """SQLite ignores foreign keys unless asked.

    Segments are self-referential, so a dangling parent reference would
    otherwise be stored silently instead of failing loudly.
    """
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

from app import models, views  # noqa: E402,F401 - registers models and routes

with app.app_context():
    db.create_all()
