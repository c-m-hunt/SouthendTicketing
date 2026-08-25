"""Database schema for Southend ticket availability.

The ktckts data source exposes considerably more than the old ASP.NET site:
a full hierarchical stadium map, per-block open/total counts, per-seat status
grids and per-category pricing. The schema below splits that into a stable
stadium catalogue (Segment) and time-series observations (Snapshot,
SegmentSnapshot), with prices aggregated per fixture.
"""

import datetime as dt

from app import db


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Fixture(db.Model):
    """A match with tickets on sale."""

    __tablename__ = "fixture"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    slug = db.Column(db.String(255))
    url = db.Column(db.String(512))

    title = db.Column(db.String(255))
    opponent = db.Column(db.String(128))
    home_crest = db.Column(db.String(512))
    away_crest = db.Column(db.String(512))

    venue = db.Column(db.String(128))
    competition = db.Column(db.String(128))
    kickoff = db.Column(db.DateTime, index=True)
    is_home = db.Column(db.Boolean, default=True, nullable=False)

    # False once every segment reports isAvailable == false (sold out or
    # withdrawn); lets the UI grey a fixture out without a fresh fetch.
    on_sale = db.Column(db.Boolean, default=True, nullable=False)

    first_seen = db.Column(db.DateTime, default=utcnow)
    last_refreshed = db.Column(db.DateTime)

    snapshots = db.relationship(
        "Snapshot",
        backref="fixture",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Snapshot.captured_at",
    )
    prices = db.relationship(
        "FixturePrice", backref="fixture", lazy="dynamic", cascade="all, delete-orphan"
    )

    @property
    def kickoff_utc(self):
        if self.kickoff is None:
            return None
        return self.kickoff.replace(tzinfo=dt.timezone.utc)

    @property
    def is_past(self):
        if self.kickoff is None:
            return False
        return self.kickoff < utcnow()

    def latest_snapshot(self):
        return self.snapshots.order_by(Snapshot.captured_at.desc()).first()

    def __repr__(self):
        return f"<Fixture {self.code} {self.title}>"


class Segment(db.Model):
    """A stand, tier or block in the stadium map.

    ktckts segment ids are stable across fixtures (verified: all 63 ids and
    capacities are identical between every fixture), so this is a stadium-wide
    catalogue rather than per-fixture rows.
    """

    __tablename__ = "segment"

    id = db.Column(db.String(64), primary_key=True)
    parent_id = db.Column(db.String(64), db.ForeignKey("segment.id"), index=True)
    code = db.Column(db.String(32), index=True)
    name = db.Column(db.String(128))
    depth = db.Column(db.Integer, default=0)
    kind = db.Column(db.String(32))  # Virtual | Seats | Space
    capacity = db.Column(db.Integer, default=0)
    sort_order = db.Column(db.Integer, default=0)

    children = db.relationship(
        "Segment",
        backref=db.backref("parent", remote_side=[id]),
        lazy="select",
        order_by="Segment.sort_order",
    )

    @property
    def is_leaf(self):
        return self.kind != "Virtual"

    def __repr__(self):
        return f"<Segment {self.code} {self.name}>"


class FixturePrice(db.Model):
    """One ticket price for a fixture, aggregated across the ground.

    ktckts prices every area separately, but the same figures repeat across
    all of them, so prices are collapsed to one row per type at ingest.
    ``amount_pence`` and ``max_amount_pence`` are equal when every area
    charges the same, which is the usual case; a difference surfaces as a
    range rather than letting one area stand in for the rest.
    """

    __tablename__ = "fixture_price"
    __table_args__ = (db.UniqueConstraint("fixture_id", "name", name="uq_fixture_price"),)

    id = db.Column(db.Integer, primary_key=True)
    fixture_id = db.Column(db.Integer, db.ForeignKey("fixture.id"), nullable=False, index=True)

    name = db.Column(db.String(64), nullable=False)  # Adult, Senior (63+), ...
    amount_pence = db.Column(db.Integer)
    max_amount_pence = db.Column(db.Integer)
    restriction = db.Column(db.String(255))  # noneSelectableReason
    # Areas offering this type, named only when it is not sold ground-wide.
    areas = db.Column(db.String(512))
    category_count = db.Column(db.Integer, default=0)
    sort_order = db.Column(db.Integer, default=0)

    @property
    def amount(self):
        if self.amount_pence is None:
            return None
        return self.amount_pence / 100.0

    @property
    def max_amount(self):
        if self.max_amount_pence is None:
            return None
        return self.max_amount_pence / 100.0

    @property
    def varies(self):
        return self.amount_pence != self.max_amount_pence

    def __repr__(self):
        return f"<FixturePrice {self.name} {self.amount_pence}p>"


class Snapshot(db.Model):
    """Stadium-wide availability for one fixture at one moment.

    Capacity is the inventory loaded for this match. Blocks carrying no
    inventory are simply absent from it, so "sold" is the plain difference
    between capacity and what is still buyable — whether a seat is held by a
    season-ticket holder or was bought last night, it is gone either way.
    """

    __tablename__ = "snapshot"
    __table_args__ = (db.Index("ix_snapshot_fixture_time", "fixture_id", "captured_at"),)

    id = db.Column(db.Integer, primary_key=True)
    fixture_id = db.Column(db.Integer, db.ForeignKey("fixture.id"), nullable=False)
    captured_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    capacity = db.Column(db.Integer, default=0)  # seats loaded for this match
    available = db.Column(db.Integer, default=0)
    sold = db.Column(db.Integer, default=0)  # capacity - available
    unused_blocks = db.Column(db.Integer, default=0)  # blocks with no inventory

    segment_snapshots = db.relationship(
        "SegmentSnapshot", backref="snapshot", lazy="select", cascade="all, delete-orphan"
    )

    @property
    def percent_sold(self):
        if not self.capacity:
            return 0.0
        return round(self.sold / self.capacity * 100, 1)

    def __repr__(self):
        return f"<Snapshot fixture={self.fixture_id} sold={self.sold}>"


class SegmentSnapshot(db.Model):
    """Per-block counts within a snapshot.

    Only blocks with real capacity are stored; virtual parents are recomputed
    on read so the table stays roughly 30 rows per snapshot rather than 63.
    """

    __tablename__ = "segment_snapshot"
    __table_args__ = (db.Index("ix_segsnap_snapshot_segment", "snapshot_id", "segment_id"),)

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey("snapshot.id"), nullable=False)
    segment_id = db.Column(db.String(64), db.ForeignKey("segment.id"), nullable=False)

    open_count = db.Column(db.Integer, default=0)
    total_count = db.Column(db.Integer, default=0)
    # The upstream isAvailable flag: whether the block is buyable right now.
    # It does not distinguish sold out from not loaded, so it is display-only.
    is_on_sale = db.Column(db.Boolean, default=False)

    segment = db.relationship("Segment")

    @property
    def sold(self):
        return max(0, (self.total_count or 0) - (self.open_count or 0))

    @property
    def is_sold_out(self):
        return self.total_count > 0 and self.open_count == 0

    def __repr__(self):
        return f"<SegmentSnapshot {self.segment_id} {self.open_count}/{self.total_count}>"
