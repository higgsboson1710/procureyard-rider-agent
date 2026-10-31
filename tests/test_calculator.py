"""
Unit tests for app.calculator policy logic.
"""

import pytest
from datetime import datetime, date
from decimal import Decimal
from app.calculator import (
    calculate_trip_fare,
    calculate_cancellation_penalty,
    is_within_7_days,
    evaluate_daily_incentive,
    calculate_rider_day_summary,
    to_ist_date
)


def test_completed_trip_fare_calculations():
    # Base case: 2.0 km or less, 1.0 surge -> exactly base fare 25
    assert calculate_trip_fare(distance_km=1.5, surge_multiplier=1.0, status="completed") == 25
    assert calculate_trip_fare(distance_km=2.0, surge_multiplier=1.0, status="completed") == 25

    # 3.0 km -> 25 + (1.0 * 6) = 31
    assert calculate_trip_fare(distance_km=3.0, surge_multiplier=1.0, status="completed") == 31

    # Scenario 18: 7.4 km, surge 1.0 -> 25 + (5.4 * 6) = 25 + 32.4 = 57.4 -> rounds to 57
    assert calculate_trip_fare(distance_km=7.4, surge_multiplier=1.0, status="completed") == 57

    # Surge rounding test (0.5 rounds UP):
    # e.g., base 25, distance 2 km (fare 25), surge 1.1 -> 25 * 1.1 = 27.5 -> 28
    assert calculate_trip_fare(distance_km=2.0, surge_multiplier=1.1, status="completed") == 28

    # 25 * 1.02 = 25.5 -> 26
    assert calculate_trip_fare(distance_km=2.0, surge_multiplier=1.02, status="completed") == 26


def test_cancellation_penalties():
    # Trip fare for cancelled trips is 0
    assert calculate_trip_fare(distance_km=5.0, surge_multiplier=1.5, status="cancelled_by_rider") == 0
    assert calculate_trip_fare(distance_km=5.0, surge_multiplier=1.5, status="cancelled_by_customer") == 0

    # Cancelled by rider -> -10 penalty
    assert calculate_cancellation_penalty(status="cancelled_by_rider") == -10
    
    # Cancelled by customer -> 0 penalty
    assert calculate_cancellation_penalty(status="cancelled_by_customer") == 0


def test_daily_incentive():
    assert evaluate_daily_incentive(0) == 0
    assert evaluate_daily_incentive(11) == 0
    assert evaluate_daily_incentive(12) == 150
    assert evaluate_daily_incentive(15) == 150


def test_7_day_dispute_window():
    received_at = datetime.fromisoformat("2026-09-23T09:00:00+05:30")
    
    # Same day
    assert is_within_7_days(date(2026, 9, 23), received_at) is True
    # 7 days before: 23 - 7 = 16 Sep (within window)
    assert is_within_7_days(date(2026, 9, 16), received_at) is True
    # 8 days before: 15 Sep (outside window)
    assert is_within_7_days(date(2026, 9, 15), received_at) is False
    # Scenario 12 check: 13 Sep relative to 23 Sep (10 days prior) -> outside window
    assert is_within_7_days(date(2026, 9, 13), received_at) is False


def test_day_summary_scenario_1_surge_difference():
    # Scenario 1: Rider R003, Sep 20: T926334 was paid 49 instead of 74 -> difference is 25
    trips = [
        {"trip_id": "T926334", "distance_km": 6.0, "surge_multiplier": 1.5, "status": "completed"}
    ]
    # (25 + 4*6) * 1.5 = 49 * 1.5 = 73.5 -> rounds to 74
    payout_lines = [
        {"line_type": "trip", "trip_id": "T926334", "amount": 49}
    ]
    summary = calculate_rider_day_summary(trips, payout_lines)
    assert summary["trip_discrepancies"]["T926334"]["expected"] == 74
    assert summary["trip_discrepancies"]["T926334"]["actual"] == 49
    assert summary["trip_discrepancies"]["T926334"]["difference"] == 25
    assert summary["total_underpayment"] == 25


def test_day_summary_scenario_9_double_penalty():
    # Scenario 9: 1 rider cancellation, but penalized twice (-10 each = -20)
    trips = [
        {"trip_id": "T1", "distance_km": 0.0, "surge_multiplier": 1.0, "status": "cancelled_by_rider"}
    ]
    payout_lines = [
        {"line_type": "cancellation_penalty", "trip_id": "T1", "amount": -10},
        {"line_type": "cancellation_penalty", "trip_id": "T1", "amount": -10}
    ]
    summary = calculate_rider_day_summary(trips, payout_lines)
    assert summary["expected_penalties"] == -10
    assert summary["actual_penalties"] == -20
    assert summary["penalty_difference"] == 10
    assert summary["total_underpayment"] == 10
