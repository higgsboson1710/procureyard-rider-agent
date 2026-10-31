"""
FastAPI application for QuickDrop Rider Dispute Desk.
Implements:
- GET /health
- POST /messages (with wamid idempotency)
- GET /trace/{rider_id}
- GET /ops/pending
- Ops actions: POST /ops/{action}/{id}
- Ops HTML Dashboard on GET /ops
"""

import json
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import (
    init_db, seed_data_if_empty, get_db,
    ConversationMessage, Trace, OpsPending, normalize_rider_id
)
from app.agent import DisputeAgent
from app.payswift_client import PaySwiftClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB and seed CSV datasets if empty
    init_db()
    seed_data_if_empty()
    yield


app = FastAPI(title="QuickDrop Rider Dispute Desk", lifespan=lifespan)
agent = DisputeAgent()


# ----------------- Request / Response Models -----------------

class MessageRequest(BaseModel):
    message_id: str
    rider_id: str
    text: str
    received_at: str


class MessageResponse(BaseModel):
    reply: str


# ----------------- Endpoints -----------------

@app.get("/health")
def health_check():
    """Health check endpoint answering 200 when ready."""
    return {"status": "ok"}


@app.post("/admin/reset")
def reset_system(db: Session = Depends(get_db)):
    """
    Resets the desk state to clean fresh state:
    - Clears conversation messages, traces, and ops_pending
    - Snapshots PaySwift existing payouts so each scenario runs isolated
    """
    from app.tools import reset_payswift_baseline
    db.query(ConversationMessage).delete()
    db.query(Trace).delete()
    db.query(OpsPending).delete()
    db.commit()
    reset_payswift_baseline()
    return {"status": "reset_complete"}


@app.post("/messages", response_model=MessageResponse)
def handle_message(req: MessageRequest, db: Session = Depends(get_db)):
    """
    Handles incoming rider message.
    Idempotent: If message_id was already processed, returns cached reply.
    """
    existing = db.query(ConversationMessage).filter(
        ConversationMessage.message_id == req.message_id
    ).first()

    if existing and existing.reply:
        # Idempotent response for duplicate delivery (Scenario 5)
        return MessageResponse(reply=existing.reply)

    try:
        reply_text = agent.handle_message(
            rider_id=req.rider_id,
            message_id=req.message_id,
            text=req.text,
            received_at=req.received_at
        )
        return MessageResponse(reply=reply_text)
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Fallback friendly response
        return MessageResponse(reply="Aapka message mil gaya hai. Ops team ise review kar rahi hai.")


@app.get("/trace/{rider_id}")
def get_trace(rider_id: str, db: Session = Depends(get_db)):
    """
    Returns ordered audit trace of all steps performed for this rider.
    Shape: [{"at": "...", "type": "...", "name": "...", "input": {...}, "output": {...}}]
    """
    norm_id = normalize_rider_id(rider_id)
    traces = db.query(Trace).filter(
        Trace.rider_id == norm_id
    ).order_by(Trace.id.asc()).all()

    results = []
    for t in traces:
        in_val = None
        if t.input:
            try:
                in_val = json.loads(t.input)
            except Exception:
                in_val = t.input
        
        out_val = None
        if t.output is not None:
            try:
                out_val = json.loads(t.output)
            except Exception:
                out_val = t.output

        results.append({
            "at": t.at,
            "type": t.type,
            "name": t.name,
            "input": in_val,
            "output": out_val
        })
    return results


@app.get("/ops/pending")
def get_ops_pending(db: Session = Depends(get_db)):
    """
    Returns list of items awaiting ops action.
    Shape: [{"id": "...", "rider_id": "...", "type": "approval|escalation", "amount": 120, "reason": "...", "created_at": "..."}]
    """
    items = db.query(OpsPending).filter(
        OpsPending.status == "pending"
    ).order_by(OpsPending.created_at.asc()).all()

    return [
        {
            "id": item.id,
            "rider_id": item.rider_id,
            "type": item.type,
            "amount": item.amount,
            "reason": item.reason,
            "created_at": item.created_at
        }
        for item in items
    ]


@app.post("/ops/approve/{item_id}")
def approve_pending_item(item_id: str, db: Session = Depends(get_db)):
    """Approves an ops approval item and triggers payout via PaySwift."""
    item = db.query(OpsPending).filter(OpsPending.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    item.status = "approved"
    if item.type == "approval" and item.amount and item.amount > 0:
        client = PaySwiftClient()
        client.create_payout(
            rider_id=item.rider_id,
            amount=item.amount,
            reference=f"Ops approved: {item.reason}",
            idempotency_key=f"ops_appr_{item.id}"
        )
    db.commit()
    return {"status": "approved", "item_id": item_id}


@app.post("/ops/reject/{item_id}")
def reject_pending_item(item_id: str, db: Session = Depends(get_db)):
    """Rejects an ops pending item."""
    item = db.query(OpsPending).filter(OpsPending.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    item.status = "rejected"
    db.commit()
    return {"status": "rejected", "item_id": item_id}


@app.get("/ops", response_class=HTMLResponse)
def ops_dashboard(db: Session = Depends(get_db)):
    """Simple Ops review web dashboard."""
    pending = db.query(OpsPending).order_by(OpsPending.created_at.desc()).all()
    rows = ""
    for item in pending:
        amt_str = f"₹{item.amount}" if item.amount else "N/A"
        badge_color = "#e2e8f0" if item.status != "pending" else ("#fed7aa" if item.type == "approval" else "#e9d5ff")
        action_btns = ""
        if item.status == "pending":
            action_btns = f"""
            <button onclick="actionItem('approve', '{item.id}')" style="background:#16a34a;color:white;border:none;padding:4px 8px;border-radius:4px;cursor:pointer;">Approve</button>
            <button onclick="actionItem('reject', '{item.id}')" style="background:#dc2626;color:white;border:none;padding:4px 8px;border-radius:4px;cursor:pointer;">Reject</button>
            """
        else:
            action_btns = f"<span>{item.status.upper()}</span>"

        rows += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd;">{item.id}</td>
            <td style="padding:8px;border:1px solid #ddd;"><b>{item.rider_id}</b></td>
            <td style="padding:8px;border:1px solid #ddd;"><span style="background:{badge_color};padding:2px 6px;border-radius:4px;">{item.type}</span></td>
            <td style="padding:8px;border:1px solid #ddd;">{amt_str}</td>
            <td style="padding:8px;border:1px solid #ddd;">{item.reason}</td>
            <td style="padding:8px;border:1px solid #ddd;">{item.created_at}</td>
            <td style="padding:8px;border:1px solid #ddd;">{action_btns}</td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>QuickDrop Ops Dispute Desk</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 30px; background: #f8fafc; color: #1e293b; }}
            h1 {{ color: #0f172a; }}
            table {{ border-collapse: collapse; width: 100%; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            th {{ background: #f1f5f9; text-align: left; padding: 10px; border: 1px solid #ddd; }}
        </style>
    </head>
    <body>
        <h1>QuickDrop Ops Review Desk</h1>
        <p>Review and act on dispute approvals and escalations.</p>
        <table>
            <thead>
                <tr>
                    <th>ID</th><th>Rider</th><th>Type</th><th>Amount</th><th>Reason</th><th>Created At</th><th>Actions</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        <script>
            async function actionItem(action, id) {{
                const res = await fetch('/ops/' + action + '/' + id, {{ method: 'POST' }});
                if (res.ok) {{ location.reload(); }}
                else {{ alert('Failed to ' + action); }}
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
