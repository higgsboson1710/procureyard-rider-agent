"""
Evaluation Runner for QuickDrop Rider Payout Dispute Desk.
Runs all scenarios from data/conversations.json against the service URL.
Usage:
    python evals/run.py http://localhost:8000
"""

import sys
import os
import json
import time
from pathlib import Path
from typing import Dict, Any, List
import requests

def get_payswift_payouts(payswift_url: str, rider_id: str) -> List[Dict[str, Any]]:
    """Fetches list of payouts from PaySwift sandbox."""
    try:
        r = requests.get(f"{payswift_url}/v1/payouts", params={"rider_id": rider_id}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else data.get("data", data.get("payouts", []))
    except Exception:
        pass
    return []

def run_evals(service_url: str, payswift_url: str = "http://localhost:8081") -> bool:
    service_url = service_url.rstrip("/")
    payswift_url = payswift_url.rstrip("/")

    # Check health
    try:
        h = requests.get(f"{service_url}/health", timeout=5)
        if h.status_code != 200:
            print(f"[ERROR] Service health check returned {h.status_code}")
            return False
    except Exception as e:
        print(f"[ERROR] Failed to reach service at {service_url}: {e}")
        return False

    # Locate conversations.json
    script_dir = Path(__file__).resolve().parent
    repo_dir = script_dir.parent
    conv_file = repo_dir / "data" / "conversations.json"

    if not conv_file.exists():
        print(f"[ERROR] conversations.json not found at {conv_file}")
        return False

    with open(conv_file, "r", encoding="utf-8") as f:
        scenarios = json.load(f)

    total_scenarios = len(scenarios)
    passed_count = 0
    results_table = []

    print(f"\n=======================================================")
    print(f" QuickDrop Payout Dispute Desk - Evaluation Benchmark")
    print(f" Target Service: {service_url}")
    print(f" PaySwift Base:  {payswift_url}")
    print(f" Total Scenarios: {total_scenarios}")
    print(f"=======================================================\n")

    for idx, s in enumerate(scenarios, 1):
        scen_name = s.get("scenario", f"Scenario {idx}")
        rider_id = s.get("rider_id")
        expected = s.get("expected", {})
        turns = s.get("turns", [])

        # 1. Reset desk state for clean scenario isolation
        try:
            requests.post(f"{service_url}/admin/reset", timeout=5)
        except Exception:
            pass

        # 2. Snapshot initial PaySwift payouts for this rider
        init_payouts = get_payswift_payouts(payswift_url, rider_id)
        init_payout_sum = sum(int(p.get("amount", 0)) for p in init_payouts)

        # 3. Deliver turns to the agent
        agent_replies = []
        delivery_ok = True

        for t in turns:
            if t.get("from") == "rider":
                payload = {
                    "message_id": t.get("message_id"),
                    "rider_id": rider_id,
                    "text": t.get("text"),
                    "received_at": t.get("received_at")
                }
                try:
                    resp = requests.post(f"{service_url}/messages", json=payload, timeout=12)
                    if resp.status_code == 200:
                        rep_text = resp.json().get("reply", "")
                        agent_replies.append(rep_text)
                    else:
                        delivery_ok = False
                except Exception as ex:
                    delivery_ok = False

        # 4. Check results
        final_payouts = get_payswift_payouts(payswift_url, rider_id)
        final_payout_sum = sum(int(p.get("amount", 0)) for p in final_payouts)
        actual_payout_delta = final_payout_sum - init_payout_sum

        # Check ops pending
        actual_approval = 0
        has_escalation = False
        try:
            ops_resp = requests.get(f"{service_url}/ops/pending", timeout=5)
            if ops_resp.status_code == 200:
                pending_items = ops_resp.json()
                for item in pending_items:
                    if item.get("rider_id") == rider_id:
                        if item.get("type") == "approval":
                            actual_approval += int(item.get("amount", 0))
                        elif item.get("type") == "escalation":
                            has_escalation = True
        except Exception:
            pass

        actual_approval_val = actual_approval if actual_approval > 0 else None

        # Check trace
        trace_ok = False
        try:
            tr_resp = requests.get(f"{service_url}/trace/{rider_id}", timeout=5)
            if tr_resp.status_code == 200:
                traces = tr_resp.json()
                if isinstance(traces, list) and len(traces) >= len(turns):
                    trace_ok = all("at" in t and "type" in t and "name" in t for t in traces)
        except Exception:
            pass

        # Evaluate against expected
        passed = False
        fail_reasons = []

        if not delivery_ok:
            fail_reasons.append("Message delivery failed")

        # Handle 'one_of' options
        if "one_of" in expected:
            matched_one_of = False
            for opt in expected["one_of"]:
                exp_p = opt.get("payout", 0)
                exp_a = opt.get("approval")
                if actual_payout_delta == exp_p and actual_approval_val == exp_a:
                    matched_one_of = True
                    break
            if not matched_one_of:
                fail_reasons.append(f"Payout/Approval mismatch (actual: payout={actual_payout_delta}, approval={actual_approval_val})")
        else:
            exp_p = expected.get("payout", 0)
            exp_a = expected.get("approval")
            if actual_payout_delta != exp_p:
                fail_reasons.append(f"Payout delta mismatch: expected {exp_p}, got {actual_payout_delta}")
            if actual_approval_val != exp_a:
                fail_reasons.append(f"Approval mismatch: expected {exp_a}, got {actual_approval_val}")

        # Check escalation
        exp_esc = expected.get("escalation", "no")
        if exp_esc == "no" and has_escalation:
            fail_reasons.append("Unexpected escalation queued")
        elif exp_esc == "required" and not has_escalation:
            fail_reasons.append("Required escalation missing")

        # Check reply mentions
        all_replies_combined = " ".join(agent_replies).lower()
        reply_mentions = expected.get("reply_mentions", [])
        for mention in reply_mentions:
            if str(mention).lower() not in all_replies_combined:
                fail_reasons.append(f"Reply missing required mention '{mention}'")

        if not trace_ok:
            fail_reasons.append("Trace incomplete or invalid")

        passed = (len(fail_reasons) == 0)
        if passed:
            passed_count += 1
            status_str = "PASS"
        else:
            status_str = f"FAIL ({'; '.join(fail_reasons)})"

        results_table.append((idx, scen_name, rider_id, status_str))
        print(f"[{status_str[:4]}] #{idx:02d} [{rider_id}] {scen_name[:40]:<40} -> {status_str}", flush=True)

    print(f"\n=======================================================")
    print(f" Summary: {passed_count}/{total_scenarios} Passed ({(passed_count/total_scenarios)*100:.1f}%)")
    print(f"=======================================================\n")

    return passed_count == total_scenarios

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    ps_target = os.environ.get("PAYSWIFT_BASE_URL", "http://localhost:8081")
    success = run_evals(target, ps_target)
    sys.exit(0 if success else 1)
