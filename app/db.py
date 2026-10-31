"""
Database configuration, SQLAlchemy models, and CSV seeding for QuickDrop.
"""

import os
import csv
import json
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Date, DateTime,
    Text, ForeignKey, Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from app.calculator import to_ist_date, to_ist_datetime

Base = declarative_base()

# ----------------- Models -----------------

def normalize_rider_id(rider_id: str) -> str:
    """Normalizes rider ID to standard R001-R040 format (handling R7 -> R007, r19 -> R019)."""
    r = rider_id.strip().upper()
    if r.startswith("R") and r[1:].isdigit():
        return f"R{int(r[1:]):03d}"
    return r


class Rider(Base):
    __tablename__ = "riders"
    rider_id = Column(String(50), primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    city = Column(String(100), nullable=False)
    joined_on = Column(Date, nullable=False)


class Trip(Base):
    __tablename__ = "trips"
    trip_id = Column(String(50), primary_key=True, index=True)
    rider_id = Column(String(50), ForeignKey("riders.rider_id"), index=True, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ist_date = Column(Date, index=True, nullable=False)
    status = Column(String(50), nullable=False)  # completed, cancelled_by_customer, cancelled_by_rider
    distance_km = Column(Float, nullable=False)
    surge_multiplier = Column(Float, nullable=False, default=1.0)


class PayoutLine(Base):
    __tablename__ = "payout_lines"
    line_id = Column(String(50), primary_key=True, index=True)
    payout_date = Column(Date, index=True, nullable=False)
    rider_id = Column(String(50), ForeignKey("riders.rider_id"), index=True, nullable=False)
    line_type = Column(String(50), nullable=False)  # trip, daily_incentive, cancellation_penalty
    trip_id = Column(String(50), nullable=True)
    amount = Column(Integer, nullable=False)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(100), unique=True, index=True, nullable=False)  # wamid idempotency
    rider_id = Column(String(50), index=True, nullable=False)
    text = Column(Text, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    reply = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Trace(Base):
    """
    Matching GET /trace/{rider_id} format:
    [{"at": "<ISO time>", "type": "...", "name": "...", "input": {...}, "output": {...}}]
    """
    __tablename__ = "traces"
    id = Column(Integer, primary_key=True, autoincrement=True)
    rider_id = Column(String(50), index=True, nullable=False)
    at = Column(String(50), nullable=False)
    type = Column(String(50), nullable=False)  # message_in, tool_call, decision, reply, error
    name = Column(String(100), nullable=False)
    input = Column(Text, nullable=True)   # Stored as JSON string
    output = Column(Text, nullable=True)  # Stored as JSON string


class OpsPending(Base):
    """
    Matching GET /ops/pending format:
    [{"id": "...", "rider_id": "...", "type": "approval|escalation", "amount": 120, "reason": "...", "created_at": "..."}]
    """
    __tablename__ = "ops_pending"
    id = Column(String(100), primary_key=True)
    rider_id = Column(String(50), index=True, nullable=False)
    type = Column(String(50), nullable=False)  # approval, escalation
    amount = Column(Integer, nullable=True)    # whole rupees for approvals
    reason = Column(Text, nullable=False)
    status = Column(String(50), default="pending")  # pending, approved, rejected
    created_at = Column(String(50), nullable=False)
    extra_metadata = Column(Text, nullable=True)


# ----------------- DB Session & Init -----------------

_engine = None
_SessionFactory = None

def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "sqlite:///quickdrop.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def init_db(database_url: Optional[str] = None):
    global _engine, _SessionFactory
    url = database_url or get_database_url()
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    
    _engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    _SessionFactory = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
    return _engine


def get_db():
    if _SessionFactory is None:
        init_db()
    session = _SessionFactory()
    try:
        yield session
    finally:
        session.close()


def reseed_db():
    """Drops all tables and re-seeds cleanly."""
    global _engine
    if _engine is None:
        init_db()
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    seed_data_if_empty()



def get_db_session():
    if _SessionFactory is None:
        init_db()
    return _SessionFactory()


# ----------------- Seeding -----------------

def seed_data_if_empty(base_data_dir: Optional[str] = None):
    """Loads riders.csv, trips.csv, payout_lines.csv into database if tables are empty."""
    session = get_db_session()
    try:
        # Check if already seeded
        if session.query(Rider).first() is not None:
            return

        data_dir = Path(base_data_dir) if base_data_dir else Path(__file__).resolve().parent.parent / "data"

        # 1. Seed Riders
        riders_csv = data_dir / "riders.csv"
        if riders_csv.exists():
            with open(riders_csv, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                seen_riders = set()
                riders = []
                for row in reader:
                    rid = row["rider_id"]
                    if rid in seen_riders:
                        continue
                    seen_riders.add(rid)
                    riders.append(
                        Rider(
                            rider_id=rid,
                            name=row["name"],
                            city=row["city"],
                            joined_on=date.fromisoformat(row["joined_on"])
                        )
                    )
                session.bulk_save_objects(riders)
                session.commit()

        # 2. Seed Trips (deduplicate identical duplicate lines if any)
        trips_csv = data_dir / "trips.csv"
        if trips_csv.exists():
            with open(trips_csv, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                seen_trips = set()
                trips = []
                for row in reader:
                    tid = row["trip_id"]
                    if tid in seen_trips:
                        continue
                    seen_trips.add(tid)
                    started_dt = to_ist_datetime(row["started_at"])
                    ist_d = started_dt.date()
                    raw_dist = float(row["distance_km"])
                    # Some loggers record meters instead of km (e.g. 5000 -> 5.0)
                    dist_km = raw_dist / 1000.0 if raw_dist > 50.0 else raw_dist
                    trips.append(
                        Trip(
                            trip_id=tid,
                            rider_id=normalize_rider_id(row["rider_id"]),
                            started_at=started_dt,
                            ist_date=ist_d,
                            status=row["status"],
                            distance_km=dist_km,
                            surge_multiplier=float(row["surge_multiplier"])
                        )
                    )
                session.bulk_save_objects(trips)
                session.commit()

        # 3. Seed Payout Lines
        payouts_csv = data_dir / "payout_lines.csv"
        if payouts_csv.exists():
            with open(payouts_csv, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                seen_lines = set()
                lines = []
                for row in reader:
                    lid = row["line_id"]
                    if lid in seen_lines:
                        continue
                    seen_lines.add(lid)
                    lines.append(
                        PayoutLine(
                            line_id=lid,
                            payout_date=date.fromisoformat(row["payout_date"]),
                            rider_id=normalize_rider_id(row["rider_id"]),
                            line_type=row["line_type"],
                            trip_id=row["trip_id"] if row["trip_id"] else None,
                            amount=int(row["amount"])
                        )
                    )
                session.bulk_save_objects(lines)
                session.commit()

    finally:
        session.close()


def reseed_db():
    """Drops all tables and re-seeds cleanly."""
    global _engine
    if _engine is None:
        init_db()
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    seed_data_if_empty()

