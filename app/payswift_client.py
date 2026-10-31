"""
Client for PaySwift Payouts API.
Handles:
- POST /v1/payouts with Idempotency-Key
- GET /v1/payouts?rider_id={rider_id}
- Retries with backoff on 429 (rate limit) or 5xx server errors
- Idempotency / 409 Conflict handling
"""

import os
import time
import uuid
import logging
from typing import List, Dict, Optional, Any
import requests

logger = logging.getLogger(__name__)

class PaySwiftClient:
    def __init__(self, base_url: Optional[str] = None):
        raw_url = base_url or os.environ.get("PAYSWIFT_BASE_URL", "http://localhost:8081")
        self.base_url = raw_url.rstrip("/")

    def get_rider_payouts(self, rider_id: str, max_retries: int = 3) -> List[Dict[str, Any]]:
        """
        Retrieves all payouts recorded in PaySwift for this rider.
        GET /v1/payouts?rider_id={rider_id}
        """
        url = f"{self.base_url}/v1/payouts"
        params = {"rider_id": rider_id}

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(url, params=params, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        return data
                    return data.get("data", data.get("payouts", []))
                elif resp.status_code in (429, 500, 502, 503, 504):
                    logger.warning("PaySwift GET returned status %d. Retry %d/%d", resp.status_code, attempt, max_retries)
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                else:
                    logger.error("PaySwift GET error %d: %s", resp.status_code, resp.text)
                    return []
            except requests.RequestException as e:
                logger.warning("PaySwift GET connection failed (%s). Retry %d/%d", str(e), attempt, max_retries)
                time.sleep(0.5 * (2 ** (attempt - 1)))
        return []

    def create_payout(
        self,
        rider_id: str,
        amount: int,
        reference: str = "",
        idempotency_key: Optional[str] = None,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """
        Issues a payout to the rider via PaySwift.
        POST /v1/payouts
        Body: { "rider_id": "...", "amount": 120, "reference": "..." }
        Header: Idempotency-Key
        """
        if amount <= 0:
            return {"status": "error", "message": "Payout amount must be positive"}

        url = f"{self.base_url}/v1/payouts"
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key or str(uuid.uuid4())
        }
        payload = {
            "rider_id": rider_id,
            "amount": int(amount),
            "reference": reference or f"Dispute resolution for {rider_id}"
        }

        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=8)
                if resp.status_code in (200, 201):
                    return resp.json()
                elif resp.status_code == 409:
                    # Idempotent replay: already processed
                    logger.info("PaySwift returned 409 Conflict - payment already processed with idempotency key")
                    try:
                        return resp.json()
                    except Exception:
                        return {"status": "already_processed", "amount": amount}
                elif resp.status_code in (429, 500, 502, 503, 504):
                    logger.warning("PaySwift POST returned %d. Retry %d/%d", resp.status_code, attempt, max_retries)
                    time.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                else:
                    logger.error("PaySwift POST failed with %d: %s", resp.status_code, resp.text)
                    return {"status": "error", "code": resp.status_code, "detail": resp.text}
            except requests.RequestException as e:
                logger.warning("PaySwift POST connection error (%s). Retry %d/%d", str(e), attempt, max_retries)
                time.sleep(0.5 * (2 ** (attempt - 1)))

        return {"status": "error", "message": "PaySwift unreachable after retries"}

