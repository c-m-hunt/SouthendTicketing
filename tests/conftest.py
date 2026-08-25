import json
import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = pathlib.Path(__file__).resolve().parent / "data"

# Point the app at a throwaway database before it is imported.
_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmpdir, "test.db")


@pytest.fixture(scope="session")
def spec_payload():
    return json.loads((DATA / "specification_criteria.json").read_text())


@pytest.fixture(scope="session")
def detail_payload():
    return json.loads((DATA / "detail.json").read_text())


@pytest.fixture(scope="session")
def fixtures_html():
    return (DATA / "fixtures_page.html").read_text()


@pytest.fixture
def flask_app():
    from app import app as flask_app
    from app import db

    flask_app.config.update(TESTING=True)
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app
        db.session.remove()


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


class FakeClient:
    """Stands in for KtcktsClient, serving the recorded payloads."""

    def __init__(self, fixtures, availability):
        self._fixtures = fixtures
        self._availability = availability
        self.calls = 0

    def fetch_fixtures(self):
        return self._fixtures

    def fetch_availability(self, product_id, include_seats=True):
        self.calls += 1
        return json.loads(json.dumps(self._availability, default=str))
