"""
Deterministic Policy Calculator for QuickDrop Rider Payouts.
Implements the exact rules from docs/policy.md:
- Completed trip fare: (25 + max(0, distance_km - 2) * 6) * surge_multiplier, rounded to nearest rupee (0.5 rounds up).
- Cancelled by rider: ₹0 trip fare, but incurs ₹10 penalty.
- Cancelled by customer: ₹0 trip fare, ₹0 penalty.
- Daily incentive: ₹150 for 12 or more completed trips in a single IST calendar day.
- 7-day dispute window: Disputed date must be within 7 calendar days before the message received_at date in IST.
"""

from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple, Any
import zoneinfo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def to_ist_datetime(dt_str_or_obj: Any) -> datetime:
    """Converts an ISO string or datetime object to an aware IST datetime."""
    if isinstance(dt_str_or_obj, str):
        clean_str = dt_str_or_obj.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
    else:
        dt = dt_str_or_obj
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)
    return dt


def to_ist_date(dt_str_or_obj: Any) -> date:
    """Returns the IST calendar date from an ISO timestamp or date object."""
    if isinstance(dt_str_or_obj, date) and not isinstance(dt_str_or_obj, datetime):
        return dt_str_or_obj
    dt = to_ist_datetime(dt_str_or_obj)
    return dt.date()


def calculate_trip_fare(distance_km: float, surge_multiplier: float, status: str) -> int:
    """
    Calculates expected trip fare according to policy:
    - completed: (25 + max(0, distance - 2) * 6) * surge
      rounded to nearest integer (ROUND_HALF_UP).
    - cancelled_by_rider: 0 (the ₹10 penalty is recorded separately as a penalty line)
    - cancelled_by_customer: 0
    """
    if status != "completed":
        return 0

    d_dist = Decimal(str(distance_km))
    extra_dist = max(Decimal("0"), d_dist - Decimal("2"))
    base_and_dist = Decimal("25") + (extra_dist * Decimal("6"))
    d_surge = Decimal(str(surge_multiplier))
    fare = base_and_dist * d_surge
    # Round 0.5 up to nearest whole rupee
    rounded_fare = fare.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(rounded_fare)


def calculate_cancellation_penalty(status: str) -> int:
    """
    Returns penalty in rupees (negative number as recorded in payout_lines):
    - cancelled_by_rider: -10
    - otherwise: 0
    """
    return -10 if status == "cancelled_by_rider" else 0


def is_within_7_days(dispute_date: date, received_at: datetime) -> bool:
    """
    Policy: 'We only look at disputes for the last 7 days.'
    Calendar days in IST: dispute_date must be between (received_date - 7 days) and received_date.
    """
    ref_date = to_ist_date(received_at)
    min_date = ref_date - timedelta(days=7)
    return min_date <= dispute_date <= ref_date


def evaluate_daily_incentive(completed_trips_count: int) -> int:
    """
    Policy: 'Daily incentive of ₹150 for 12 or more trips in a day.'
    """
    return 150 if completed_trips_count >= 12 else 0


def calculate_rider_day_summary(
    trips_on_day: List[Dict],
    payout_lines_on_day: List[Dict]
) -> Dict:
    """
    Computes expected vs actual payouts for a rider on a given day.
    trips_on_day: list of dicts with trip_id, status, distance_km, surge_multiplier
    payout_lines_on_day: list of dicts with line_type, trip_id, amount
    """
    expected_trip_fares = {}
    completed_count = 0
    rider_cancellations = 0

    for t in trips_on_day:
        tid = t["trip_id"]
        status = t["status"]
        fare = calculate_trip_fare(t["distance_km"], t["surge_multiplier"], status)
        expected_trip_fares[tid] = fare
        if status == "completed":
            completed_count += 1
        elif status == "cancelled_by_rider":
            rider_cancellations += 1

    expected_incentive = evaluate_daily_incentive(completed_count)

    # Actual amounts paid
    actual_trip_fares = {}
    actual_incentive = 0
    actual_penalties = 0

    for pl in payout_lines_on_day:
        l_type = pl["line_type"]
        amt = pl["amount"]
        tid = pl.get("trip_id")
        if l_type == "trip" and tid:
            actual_trip_fares[tid] = actual_trip_fares.get(tid, 0) + amt
        elif l_type == "daily_incentive":
            actual_incentive += amt
        elif l_type == "cancellation_penalty":
            actual_penalties += amt  # penalties are negative in payout_lines

    # Differences per trip
    trip_discrepancies = {}
    all_trip_ids = set(expected_trip_fares.keys()) | set(actual_trip_fares.keys())
    for tid in all_trip_ids:
        exp = expected_trip_fares.get(tid, 0)
        act = actual_trip_fares.get(tid, 0)
        diff = exp - act
        if diff != 0:
            trip_discrepancies[tid] = {
                "expected": exp,
                "actual": act,
                "difference": diff  # positive if rider was underpaid
            }

    # Incentive difference
    incentive_diff = expected_incentive - actual_incentive

    # Penalty difference: expected penalties are (-10 * rider_cancellations)
    expected_penalty_total = -10 * rider_cancellations
    # If actual_penalties is more negative than expected (e.g. -20 vs -10),
    # then penalty_diff = (-10) - (-20) = +10 (rider was over-penalized by 10)
    penalty_diff = expected_penalty_total - actual_penalties

    trip_underpaid = sum(td["difference"] for td in trip_discrepancies.values() if td["difference"] > 0)
    incentive_underpaid = max(0, incentive_diff)
    penalty_underpaid = max(0, penalty_diff)

    total_owed = trip_underpaid + incentive_underpaid + penalty_underpaid

    return {
        "completed_count": completed_count,
        "rider_cancellations": rider_cancellations,
        "expected_incentive": expected_incentive,
        "actual_incentive": actual_incentive,
        "incentive_difference": incentive_diff,
        "expected_penalties": expected_penalty_total,
        "actual_penalties": actual_penalties,
        "penalty_difference": penalty_diff,
        "trip_discrepancies": trip_discrepancies,
        "total_underpayment": total_owed
    }
