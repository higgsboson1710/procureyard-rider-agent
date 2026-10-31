"""
Unit tests for database initialization, schema, and CSV seeding.
"""

import pytest
import os
from pathlib import Path
from app.db import (
    init_db, seed_data_if_empty, get_db_session,
    Rider, Trip, PayoutLine, Trace, OpsPending, ConversationMessage
)


def test_db_seeding_and_counts(tmp_path):
    test_db_path = tmp_path / "test_quickdrop.db"
    db_url = f"sqlite:///{test_db_path}"
    
    init_db(db_url)
    
    # Run seeding from data/ directory
    repo_data_dir = Path(__file__).resolve().parent.parent / "data"
    seed_data_if_empty(base_data_dir=str(repo_data_dir))
    
    session = get_db_session()
    try:
        rider_count = session.query(Rider).count()
        trip_count = session.query(Trip).count()
        payout_count = session.query(PayoutLine).count()
        
        # Verify counts match unique entries in CSVs
        assert rider_count == 40
        assert trip_count == 3304
        assert payout_count == 3222

        # Verify idempotency: calling seed_data_if_empty again shouldn't duplicate
        seed_data_if_empty(base_data_dir=str(repo_data_dir))
        assert session.query(Rider).count() == 40
        assert session.query(Trip).count() == 3304

        # Verify conversation messages unique message_id constraint
        first_trip = session.query(Trip).first()
        msg1 = ConversationMessage(
            message_id="wamid.TEST1",
            rider_id="R001",
            text="hello",
            received_at=first_trip.started_at
        )
        session.add(msg1)
        session.commit()

        # Adding same message_id should fail
        msg2 = ConversationMessage(
            message_id="wamid.TEST1",
            rider_id="R001",
            text="hello again",
            received_at=first_trip.started_at
        )
        session.add(msg2)
        with pytest.raises(Exception):
            session.commit()
        session.rollback()

    finally:
        session.close()
