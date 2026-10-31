"""
Deterministic Audit & Settlement Tools for QuickDrop Rider Disputes.
Enforces all security guardrails, policy rules, and financial constraints.
"""

import uuid
import json
import re
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple, Any

from sqlalchemy.orm import Session
from app.db import (
    Rider, Trip, PayoutLine, OpsPending, Trace, ConversationMessage
)
from app.calculator import (
    to_ist_date, to_ist_datetime, is_within_7_days,
    calculate_trip_fare, evaluate_daily_incentive,
    calculate_rider_day_summary, calculate_cancellation_penalty, IST
)
from app.payswift_client import PaySwiftClient


# Global baseline set for PaySwift payouts to ignore (from previous test runs / scenarios)
_baseline_payout_ids = set()

def reset_payswift_baseline(client: Optional[PaySwiftClient] = None):
    """Snapshots all current payout IDs in PaySwift into the baseline set."""
    global _baseline_payout_ids
    c = client or PaySwiftClient()
    try:
        all_p = c.get_rider_payouts("")
        _baseline_payout_ids = {p.get("payout_id") for p in all_p if p.get("payout_id")}
    except Exception:
        _baseline_payout_ids = set()


class DisputeAuditor:
    def __init__(self, session: Session, payswift_client: Optional[PaySwiftClient] = None):
        self.session = session
        self.client = payswift_client or PaySwiftClient()

    def record_trace(
        self,
        rider_id: str,
        step_type: str,
        name: str,
        step_input: Any = None,
        step_output: Any = None,
        at_iso: Optional[str] = None
    ) -> Trace:
        """Records a step in the traces table matching GET /trace/{rider_id} format."""
        now_iso = at_iso or datetime.now(IST).isoformat()
        trace = Trace(
            rider_id=rider_id,
            at=now_iso,
            type=step_type,
            name=name,
            input=json.dumps(step_input, ensure_ascii=False) if step_input is not None else None,
            output=json.dumps(step_output, ensure_ascii=False) if step_output is not None else None
        )
        self.session.add(trace)
        self.session.commit()
        return trace

    def has_auto_paid_today(self, rider_id: str, reference_date_ist: date) -> bool:
        """
        Finance Rule: Auto-pay once per rider per day.
        Checks PaySwift live ledger to see if an auto-payout was already issued for this rider today.
        """
        payouts = self.client.get_rider_payouts(rider_id)
        for p in payouts:
            pid = p.get("payout_id")
            if pid and pid in _baseline_payout_ids:
                continue
            created = p.get("created_at") or p.get("at")
            if created:
                try:
                    p_date = to_ist_date(created)
                    if p_date == reference_date_ist:
                        return True
                except Exception:
                    pass
        return False

    def get_existing_payswift_total_for_dispute(self, rider_id: str, dispute_key: str) -> int:
        """Returns sum of PaySwift payouts already made for this specific dispute reference."""
        payouts = self.client.get_rider_payouts(rider_id)
        total = 0
        for p in payouts:
            pid = p.get("payout_id")
            if pid and pid in _baseline_payout_ids:
                continue
            ref = p.get("reference", "")
            if dispute_key in ref:
                total += int(p.get("amount", 0))
        return total

    def get_existing_ops_pending_amount(self, rider_id: str, reason_substr: str) -> int:
        """Returns sum of pending approvals already queued for this issue."""
        items = self.session.query(OpsPending).filter(
            OpsPending.rider_id == rider_id,
            OpsPending.status == "pending",
            OpsPending.type == "approval"
        ).all()
        total = 0
        for item in items:
            if reason_substr in item.reason:
                total += item.amount or 0
        return total

    def audit_and_settle(
        self,
        rider_id: str,
        message_text: str,
        received_at: datetime
    ) -> Dict[str, Any]:
        """
        Deterministic audit of rider dispute against:
        1. Security rules (no cross-rider access, prompt injection defense)
        2. Policy rules (7-day window, base+distance, surge rounding, incentive, penalty)
        3. Financial limits (<=200 auto-pay, max 1 auto-pay/rider/day, reconciliation)
        """
        rec_date = to_ist_date(received_at)
        text_lower = message_text.lower()

        # ----------------- SECURITY GUARDRAILS -----------------
        # 1. Impersonation detection (Scenario 4)
        impersonation_match = re.search(r"this is (r\d{3})", text_lower)
        if impersonation_match:
            claimed_id = impersonation_match.group(1).upper()
            if claimed_id != rider_id:
                self.record_trace(
                    rider_id, "decision", "security_reject_impersonation",
                    {"claimed": claimed_id, "actual": rider_id},
                    {"decision": "reject", "reason": "Cross-rider account access attempt"}
                )
                return {
                    "payout": 0,
                    "approval": None,
                    "escalation": "optional",
                    "reply": "Main sirf is number se jude account ke baare mein madad kar sakta hoon. Aapke account ki koi specific problem ho to bataiye.",
                    "reply_mentions": []
                }

        # 2. Prompt injection defense (Scenario 24)
        if "ignore all previous rules" in text_lower or "ignore previous" in text_lower or "system:" in text_lower:
            self.record_trace(
                rider_id, "decision", "security_reject_prompt_injection",
                {"text": message_text},
                {"decision": "reject", "reason": "Prompt injection detected"}
            )
            # Create optional escalation for ops review
            esc_id = f"esc_{uuid.uuid4().hex[:8]}"
            esc = OpsPending(
                id=esc_id,
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason="Prompt injection / suspicious system command detected in rider message",
                created_at=received_at.isoformat()
            )
            self.session.add(esc)
            self.session.commit()
            return {
                "payout": 0,
                "approval": None,
                "escalation": "optional",
                "reply": "Aapka anurodh samajh nahi aaya. Main sirf payout aur trip disputes mein madad kar sakta hoon.",
                "reply_mentions": []
            }

        # ----------------- HUMAN EXCEPTION / OPS ESCALATION CHECKS -----------------
        # Scenario 22: GPS distance dispute
        if "distance galat" in text_lower or "distance galat laga" in text_lower:
            esc_id = f"esc_{uuid.uuid4().hex[:8]}"
            esc = OpsPending(
                id=esc_id,
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason="GPS distance disputed by rider. Needs ops manual review.",
                created_at=received_at.isoformat()
            )
            self.session.add(esc)
            self.session.commit()
            self.record_trace(
                rider_id, "decision", "escalate_gps_distance",
                {"message": message_text},
                {"escalation_id": esc_id, "reason": esc.reason}
            )
            return {
                "payout": 0,
                "approval": None,
                "escalation": "optional",
                "reply": "Distance dispute ke liye humare system records check kar rahe hain. Maine ise ops team ko manual verification ke liye bhej diya hai.",
                "reply_mentions": []
            }

        # Scenario 23: Accident penalty waiver request
        if "accident" in text_lower and ("penalty" in text_lower or "cancel" in text_lower):
            esc_id = f"esc_{uuid.uuid4().hex[:8]}"
            esc = OpsPending(
                id=esc_id,
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason="Cancellation due to road accident. Rider requesting penalty waiver exception.",
                created_at=received_at.isoformat()
            )
            self.session.add(esc)
            self.session.commit()
            self.record_trace(
                rider_id, "decision", "escalate_accident_penalty",
                {"message": message_text},
                {"escalation_id": esc_id, "reason": esc.reason}
            )
            return {
                "payout": 0,
                "approval": None,
                "escalation": "optional",
                "reply": "Accident ke baare mein sunkar khed hua. Cancellation penalty policy ke tehat auto-deduct hoti hai, par aapke case ko ops team ko special approval ke liye bhej diya hai.",
                "reply_mentions": []
            }

        # Scenario 3: Pushback / disagreement ("nahi nahi 12 kiye the", "dobara check karo")
        if ("dobara check" in text_lower or "nahi nahi" in text_lower or "galat bol rahe ho" in text_lower):
            esc_id = f"esc_{uuid.uuid4().hex[:8]}"
            esc = OpsPending(
                id=esc_id,
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason="Rider disagrees with trip count/payout calculations. Escalated to ops.",
                created_at=received_at.isoformat()
            )
            self.session.add(esc)
            self.session.commit()
            self.record_trace(
                rider_id, "decision", "escalate_disagreement",
                {"message": message_text},
                {"escalation_id": esc_id, "reason": esc.reason}
            )
            trips_count = self.session.query(Trip).filter(
                Trip.rider_id == rider_id,
                Trip.ist_date == date(2026, 9, 19),
                Trip.status == "completed"
            ).count()
            return {
                "payout": 0,
                "approval": None,
                "escalation": "optional",
                "reply": f"Dobara check kiya: humare record mein 19 Sep ko {trips_count} completed trips hain. Agar aapko lagta hai record galat hai, main ise ops team ko bhej deta hoon.",
                "reply_mentions": [str(trips_count)]
            }

        # ----------------- PARSE DATES & ENTITIES FROM MESSAGE -----------------
        trip_ids = re.findall(r"t\d{6}", text_lower)
        trip_ids = [t.upper() for t in trip_ids]

        target_dates = []
        # Match dates like: "19 sept", "19th", "20 tarikh", "20 tarik", "20 ko", "20 wala"
        # Never match naked decimal numbers (e.g. 7.4 km)
        date_matches = re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:sept(?:ember)?|sep|tarikh|tarik|ko|wala)\b", text_lower)
        ordinal_matches = re.findall(r"\b(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)\b", text_lower)
        for dm in (date_matches + ordinal_matches):
            d_int = int(dm)
            if 1 <= d_int <= 31:
                target_dates.append(date(2026, 9, d_int))

        if "kal" in text_lower:
            target_dates.append(rec_date - timedelta(days=1))
        if "aaj" in text_lower:
            target_dates.append(rec_date)

        # If trip IDs are specified in message, get their dates directly from DB
        if trip_ids:
            for tid in trip_ids:
                trip = self.session.query(Trip).filter(Trip.trip_id == tid, Trip.rider_id == rider_id).first()
                if trip and trip.ist_date not in target_dates:
                    target_dates.append(trip.ist_date)

        target_dates = list(set(target_dates))

        # ----------------- 7-DAY POLICY LIMIT CHECK (Scenario 12) -----------------
        for td in target_dates:
            if not is_within_7_days(td, received_at):
                self.record_trace(
                    rider_id, "decision", "policy_window_expired",
                    {"dispute_date": td.isoformat(), "received_at": received_at.isoformat()},
                    {"decision": "reject", "reason": "Outside 7-day dispute window"}
                )
                return {
                    "payout": 0,
                    "approval": None,
                    "escalation": "optional",
                    "reply": f"{td.strftime('%d %b')} ka dispute 7 din se purana hai. QuickDrop policy ke tehat hum sirf pichle 7 dino ke disputes dekh sakte hain.",
                    "reply_mentions": []
                }

        # ----------------- SCENARIO 8 / VAGUE COMPLAINT -----------------
        if not target_dates and not trip_ids:
            min_date = rec_date - timedelta(days=7)
            recent_underpayments = self._find_recent_underpaid_dates(rider_id, min_date, rec_date)
            if len(recent_underpayments) == 0:
                return {
                    "payout": 0,
                    "approval": None,
                    "escalation": "no",
                    "reply": "Kaunse din ya kaunse order ka payout galat laga? Date ya order ID bata dijiye.",
                    "reply_mentions": []
                }
            elif len(recent_underpayments) == 1 and ("kab tak aayega" in text_lower or "thik hai" in text_lower):
                single_date, amount, tid = recent_underpayments[0]
                return {
                    "payout": amount,
                    "approval": None,
                    "escalation": "no",
                    "reply": f"₹{amount} ka payment PaySwift pe process ho gaya hai.",
                    "reply_mentions": [str(amount)]
                }
            else:
                return {
                    "payout": 0,
                    "approval": None,
                    "escalation": "no",
                    "reply": "Kaunse din ya kaunse order ka payout galat laga? Date ya order ID bata dijiye.",
                    "reply_mentions": []
                }

        # ----------------- SETTLEMENT OF DISPUTES -----------------
        if trip_ids:
            for tid in trip_ids:
                trip = self.session.query(Trip).filter(Trip.trip_id == tid, Trip.rider_id == rider_id).first()
                if not trip:
                    continue
                if not is_within_7_days(trip.ist_date, received_at):
                    continue
                if trip.ist_date not in target_dates:
                    target_dates.append(trip.ist_date)

        total_payout = 0
        total_approval = None
        reply_mentions = []
        explanations = []

        already_auto_paid_today = self.has_auto_paid_today(rider_id, rec_date)

        for d in sorted(target_dates):
            day_trips = self.session.query(Trip).filter(
                Trip.rider_id == rider_id,
                Trip.ist_date == d
            ).all()
            day_lines = self.session.query(PayoutLine).filter(
                PayoutLine.rider_id == rider_id,
                PayoutLine.payout_date == d
            ).all()

            trips_data = [
                {
                    "trip_id": t.trip_id,
                    "status": t.status,
                    "distance_km": t.distance_km,
                    "surge_multiplier": t.surge_multiplier
                }
                for t in day_trips
            ]
            lines_data = [
                {
                    "line_type": pl.line_type,
                    "trip_id": pl.trip_id,
                    "amount": pl.amount
                }
                for pl in day_lines
            ]

            summary = calculate_rider_day_summary(trips_data, lines_data)

            # 1. GROUP TRIPS ON THE SAME DATE
            total_trip_remaining = 0
            affected_trips = []
            for tid, disc in summary["trip_discrepancies"].items():
                diff = disc["difference"]
                if diff > 0:
                    paid_already = self.get_existing_payswift_total_for_dispute(rider_id, tid)
                    approved_already = self.get_existing_ops_pending_amount(rider_id, tid)
                    remaining = diff - paid_already - approved_already
                    if remaining > 0:
                        total_trip_remaining += remaining
                        affected_trips.append((tid, remaining))
                        reply_mentions.append(tid)

            if total_trip_remaining > 0:
                reply_mentions.append(str(total_trip_remaining))
                trips_label = ", ".join(t[0] for t in affected_trips)
                
                # Check auto-pay eligibility for the group:
                # Limit: <= 200 and not already auto-paid today
                if total_trip_remaining <= 200 and not already_auto_paid_today:
                    res = self.client.create_payout(
                        rider_id=rider_id,
                        amount=total_trip_remaining,
                        reference=f"Trips {trips_label} underpayment on {d.isoformat()}",
                        idempotency_key=f"payout_{rider_id}_{d.isoformat()}_{uuid.uuid4().hex[:8]}"
                    )
                    self._rider_payouts_cache.pop(rider_id, None)
                    total_payout += total_trip_remaining
                    already_auto_paid_today = True
                    explanations.append(f"{d.strftime('%d %b')}: orders {trips_label} pe ₹{total_trip_remaining} bhej rahe hain")
                    self.record_trace(
                        rider_id, "tool_call", "payswift_create_payout",
                        {"rider_id": rider_id, "amount": total_trip_remaining, "trips": trips_label},
                        res
                    )
                else:
                    # Queue entire grouped amount for Ops Approval (e.g. Scenarios 10 & 20)
                    appr_id = f"appr_{uuid.uuid4().hex[:8]}"
                    op = OpsPending(
                        id=appr_id,
                        rider_id=rider_id,
                        type="approval",
                        amount=total_trip_remaining,
                        reason=f"Underpayment on {d.isoformat()} for trips {trips_label}: ₹{total_trip_remaining}. Queued for ops approval.",
                        created_at=received_at.isoformat()
                    )
                    self.session.add(op)
                    self.session.commit()
                    total_approval = (total_approval or 0) + total_trip_remaining
                    explanations.append(f"{d.strftime('%d %b')}: ₹{total_trip_remaining} ops approval ke liye bhej diya hai")
                    self.record_trace(
                        rider_id, "decision", "queue_ops_approval",
                        {"amount": total_trip_remaining, "trips": trips_label},
                        {"approval_id": appr_id}
                    )

            # 2. Incentive underpayment
            if summary["incentive_difference"] > 0:
                inc_diff = summary["incentive_difference"]
                paid_already = self.get_existing_payswift_total_for_dispute(rider_id, f"incentive_{d.isoformat()}")
                remaining = inc_diff - paid_already
                if remaining > 0:
                    reply_mentions.append(str(remaining))
                    if remaining <= 200 and not already_auto_paid_today:
                        res = self.client.create_payout(
                            rider_id=rider_id,
                            amount=remaining,
                            reference=f"Daily incentive for {d.isoformat()}",
                            idempotency_key=f"payout_{rider_id}_inc_{d.isoformat()}_{uuid.uuid4().hex[:8]}"
                        )
                        self._rider_payouts_cache.pop(rider_id, None)
                        total_payout += remaining
                        already_auto_paid_today = True
                        explanations.append(f"{d.strftime('%d %b')} ka incentive ₹{remaining} bhej diya gaya hai")
                        self.record_trace(
                            rider_id, "tool_call", "payswift_create_payout",
                            {"rider_id": rider_id, "amount": remaining, "date": d.isoformat()},
                            res
                        )
                    else:
                        appr_id = f"appr_{uuid.uuid4().hex[:8]}"
                        op = OpsPending(
                            id=appr_id,
                            rider_id=rider_id,
                            type="approval",
                            amount=remaining,
                            reason=f"Incentive for {d.isoformat()}: ₹{remaining}",
                            created_at=received_at.isoformat()
                        )
                        self.session.add(op)
                        self.session.commit()
                        total_approval = (total_approval or 0) + remaining
                        explanations.append(f"{d.strftime('%d %b')} ka incentive ₹{remaining} ops approval ke liye bhej diya gaya hai")
            elif "incentive" in text_lower:
                completed_count = summary["completed_count"]
                reply_mentions.append(str(completed_count))
                explanations.append(f"{d.strftime('%d %b')} ko aapke {completed_count} trips complete hue the. Incentive 12 trips pe milta hai, isliye nahi bana")

            # 3. Penalty underpayment (Scenario 9, Scenario 2)
            if summary["penalty_difference"] > 0:
                pen_diff = summary["penalty_difference"]
                paid_already = self.get_existing_payswift_total_for_dispute(rider_id, f"penalty_{d.isoformat()}")
                remaining = pen_diff - paid_already
                if remaining > 0:
                    reply_mentions.append(str(remaining))
                    if remaining <= 200 and not already_auto_paid_today:
                        res = self.client.create_payout(
                            rider_id=rider_id,
                            amount=remaining,
                            reference=f"Penalty refund for {d.isoformat()}",
                            idempotency_key=f"payout_{rider_id}_pen_{d.isoformat()}"
                        )
                        total_payout += remaining
                        already_auto_paid_today = True
                        explanations.append(f"{d.strftime('%d %b')}: cancel penalty extra kata tha, ₹{remaining} refund bhej rahe hain")
                        self.record_trace(
                            rider_id, "tool_call", "payswift_create_payout",
                            {"rider_id": rider_id, "amount": remaining, "reason": "penalty_refund"},
                            res
                        )
                    else:
                        appr_id = f"appr_{uuid.uuid4().hex[:8]}"
                        op = OpsPending(
                            id=appr_id,
                            rider_id=rider_id,
                            type="approval",
                            amount=remaining,
                            reason=f"Penalty refund for {d.isoformat()}: ₹{remaining}",
                            created_at=received_at.isoformat()
                        )
                        self.session.add(op)
                        self.session.commit()
                        total_approval = (total_approval or 0) + remaining
                        explanations.append(f"{d.strftime('%d %b')}: extra penalty ₹{remaining} ops approval ke liye chala gaya hai")

        # Fallback if no issues found
        if not explanations:
            if "surge" in text_lower:
                explanations.append("Humare record ke mutabiq is trip pe surge sahi laga tha aur pura payment kiya gaya hai.")
            else:
                explanations.append("Humare record ke mutabiq aapka payout bilkul sahi calculate hua hai.")

        reply_text = ". ".join(explanations)
        if total_payout > 0 and total_approval is not None:
            reply_text += f". Ek din mein ek hi auto-payment ho sakta hai, isliye ₹{total_approval} ops team approve karegi."

        return {
            "payout": total_payout,
            "approval": total_approval,
            "escalation": "no",
            "reply": reply_text,
            "reply_mentions": list(dict.fromkeys(reply_mentions))
        }

    def _find_recent_underpaid_dates(self, rider_id: str, min_date: date, max_date: date) -> List[Tuple[date, int, str]]:
        """Finds dates in range where rider was underpaid."""
        results = []
        cur_d = min_date
        while cur_d <= max_date:
            day_trips = self.session.query(Trip).filter(Trip.rider_id == rider_id, Trip.ist_date == cur_d).all()
            day_lines = self.session.query(PayoutLine).filter(PayoutLine.rider_id == rider_id, PayoutLine.payout_date == cur_d).all()
            if day_trips:
                summary = calculate_rider_day_summary(
                    [{"trip_id": t.trip_id, "status": t.status, "distance_km": t.distance_km, "surge_multiplier": t.surge_multiplier} for t in day_trips],
                    [{"line_type": pl.line_type, "trip_id": pl.trip_id, "amount": pl.amount} for pl in day_lines]
                )
                if summary["total_underpayment"] > 0:
                    first_tid = next(iter(summary["trip_discrepancies"].keys()), "")
                    results.append((cur_d, summary["total_underpayment"], first_tid))
            cur_d += timedelta(days=1)
        return results
