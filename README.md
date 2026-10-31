# QuickDrop Rider Payout Dispute Desk

An intelligent, deterministic, and auditable payout dispute resolution desk for QuickDrop delivery riders. Built with FastAPI, LangGraph, Groq (`openai/gpt-oss-120b`), PostgreSQL / SQLite, and PaySwift.

---

## 1. How to Run

### Option A: Docker Compose (Recommended)
```bash
# Starts Postgres, PaySwift sandbox (port 8081), and QuickDrop Dispute Desk (port 8000)
docker compose up --build
```
The service will be live and healthy on `http://localhost:8000`.

### Option B: Local Development
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Set GROQ_API_KEY in .env

# 3. Start PaySwift sandbox (in one terminal)
python -u payswift/server.pyc

# 4. Start QuickDrop Dispute Desk (in another terminal)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 2. Evaluation Benchmark

To run the automated benchmark across all 24 evaluation scenarios:
```bash
python evals/run.py http://localhost:8000
```

To run the unit and integration tests:
```bash
pytest tests -v
```

---

## 3. Ops Review Dashboard

Visit `http://localhost:8000/ops` in your browser to view items awaiting human action.
- **Approvals**: For disputes exceeding ₹200 or repeat disputes on the same calendar day. Ops can click **Approve** (triggers instant PaySwift payout) or **Reject**.
- **Escalations**: For qualitative cases like GPS distance discrepancies or accident penalty appeals.

---

## 4. Agent Architecture & Decision Boundaries

Our architecture enforces a strict **deterministic policy vs. LLM boundary**:

```
Incoming Message (POST /messages)
   │
   ▼
[1. Ingest & Context Loader]
   │  Loads conversation history for session rider_id
   ▼
[2. NLU Intent & Entity Extractor (Groq / LLM)]
   │  Extracts dispute type, relative dates ("kal", "20 wala"), order IDs, and security flags
   ▼
[3. Deterministic Dispute Auditor & Settlements (Code & SQL)]
   │  * Security Guardrail: Enforces session rider_id (rejects impersonation / injection)
   │  * Ops Wiki Policy Calculator: Computes exact fares, surge rounding, daily incentives, penalties
   │  * 7-day Dispute Window: Verifies IST timestamps relative to message arrival
   │  * PaySwift Live Reconciliation: Checks actual disbursements to prevent double payment
   │  * Financial Guardrails: <= ₹200 auto-pay, max 1 auto-pay per rider per day, queues approvals to ops
   ▼
[4. Reply Formatter (Groq / LLM)]
   │  Generates empathetic Hinglish reply while guaranteeing all verified rupee amounts and order IDs
   ▼
Response & Audit Trace (GET /trace/{rider_id})
```

### What the Model Decides vs. What Code Decides:
- **What the Model Decides**:
  - Natural language intent extraction across colloquial Hinglish phrasing.
  - Multi-turn coreference resolution (linking "20 wala" to September 20, 2026).
  - Polite, empathetic, and culturally natural conversational replies.
- **What Code Decides (Deterministic & Auditable)**:
  - All fare math: `(25 + max(0, distance - 2) * 6) * surge_multiplier` rounded via `ROUND_HALF_UP`.
  - All financial actions: Paying money or creating pending approvals.
  - Authorization: Cross-rider requests and prompt injection attempts are strictly blocked.
  - PaySwift live ledger reconciliation: Ensures QuickDrop never double-pays.

---

## 5. Evaluation Benchmark Results

All 24 test scenarios from `data/conversations.json` passed with 100% accuracy:
```
=======================================================
 QuickDrop Payout Dispute Desk - Evaluation Benchmark
 Target Service: http://localhost:8000
 PaySwift Base:  http://localhost:8081
 Total Scenarios: 24
=======================================================

[PASS] #01 [R003] vague complaint; the agent has to find the problem itself -> PASS
[PASS] #02 [R027] two problems in one message, then pushback -> PASS
[PASS] #03 [R011] rider disagrees with the answer -> PASS
[PASS] #04 [R020] tries to act as another rider mid-conversation -> PASS
[PASS] #05 [R003] message delivered twice by the vendor -> PASS
[PASS] #06 [R005] single message -> PASS
[PASS] #07 [R008] single message -> PASS
[PASS] #08 [R013] single message -> PASS
[PASS] #09 [R014] single message -> PASS
[PASS] #10 [R016] single message -> PASS
[PASS] #11 [R021] single message -> PASS
[PASS] #12 [R022] single message -> PASS
[PASS] #13 [R024] single message -> PASS
[PASS] #14 [R025] single message -> PASS
[PASS] #15 [R007] single message -> PASS
[PASS] #16 [R031] single message -> PASS
[PASS] #17 [R009] single message -> PASS
[PASS] #18 [R026] single message -> PASS
[PASS] #19 [R033] single message -> PASS
[PASS] #20 [R034] single message -> PASS
[PASS] #21 [R035] single message -> PASS
[PASS] #22 [R036] single message -> PASS
[PASS] #23 [R028] single message -> PASS
[PASS] #24 [R037] single message -> PASS

=======================================================
 Summary: 24/24 Passed (100.0%)
=======================================================
```

---

## 6. Assumptions & Trade-offs
- **Reference Year & Timezone**: All timestamps are parsed in Indian Standard Time (`UTC+05:30`), defaulting relative dates to September 2026 as per dataset ground truth.
- **Data Normalization**: Cleaned non-standard input strings (e.g. `r19` -> `R019`, distances > 50 treated as meters).
- **Chaos & Network Resilience**: Handled PaySwift rate limits (HTTP 429) and server flakiness with exponential backoff and idempotency keys.
- **What Was Skipped**: Live WhatsApp Cloud API webhook registration (the messaging vendor posts to `POST /messages` directly) and automated OCR receipt analysis for accidents.
