"""
LangGraph Agent for QuickDrop Rider Dispute Desk.
Orchestrates conversation memory, deterministic policy auditing, and response formatting.
"""

import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END
from sqlalchemy.orm import Session
from app.db import (
    Trace, ConversationMessage, get_db_session
)
from app.calculator import to_ist_datetime, IST
from app.tools import DisputeAuditor


class AgentState(TypedDict):
    rider_id: str
    message_id: str
    text: str
    received_at: str
    conversation_history: List[Dict[str, str]]
    audit_result: Optional[Dict[str, Any]]
    reply: str


class DisputeAgent:
    def __init__(self, session_factory=get_db_session):
        self.session_factory = session_factory
        self.workflow = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(AgentState)

        workflow.add_node("ingest_message", self.ingest_message_node)
        workflow.add_node("audit_and_decide", self.audit_and_decide_node)
        workflow.add_node("format_reply", self.format_reply_node)

        workflow.set_entry_point("ingest_message")
        workflow.add_edge("ingest_message", "audit_and_decide")
        workflow.add_edge("audit_and_decide", "format_reply")
        workflow.add_edge("format_reply", END)

        return workflow.compile()

    def ingest_message_node(self, state: AgentState) -> Dict[str, Any]:
        """Logs the incoming message to traces and retrieves past conversation history."""
        session: Session = self.session_factory()
        try:
            rider_id = state["rider_id"]
            message_id = state["message_id"]
            text = state["text"]
            received_at = state["received_at"]

            # Record message_in step in Trace
            trace = Trace(
                rider_id=rider_id,
                at=received_at,
                type="message_in",
                name="incoming_message",
                input=json.dumps({
                    "message_id": message_id,
                    "text": text,
                    "received_at": received_at
                }, ensure_ascii=False),
                output=None
            )
            session.add(trace)
            session.commit()

            # Retrieve previous messages for context
            prev_msgs = session.query(ConversationMessage).filter(
                ConversationMessage.rider_id == rider_id
            ).order_by(ConversationMessage.id.asc()).all()

            history = []
            for m in prev_msgs:
                history.append({"role": "rider", "content": m.text})
                if m.reply:
                    history.append({"role": "agent", "content": m.reply})

            return {"conversation_history": history}
        finally:
            session.close()

    def audit_and_decide_node(self, state: AgentState) -> Dict[str, Any]:
        """Runs the deterministic dispute auditor."""
        session: Session = self.session_factory()
        try:
            auditor = DisputeAuditor(session)
            rider_id = state["rider_id"]
            text = state["text"]
            received_dt = to_ist_datetime(state["received_at"])

            # Check if this message is a follow-up ("20 wala", "10 wala bhi de do na", etc.)
            combined_text = text
            if state.get("conversation_history"):
                last_turns = [h["content"] for h in state["conversation_history"][-3:]]
                combined_text = " ".join(last_turns) + " " + text

            audit_result = auditor.audit_and_settle(
                rider_id=rider_id,
                message_text=combined_text if len(text.split()) < 4 else text,
                received_at=received_dt
            )
            return {"audit_result": audit_result}
        finally:
            session.close()

    def format_reply_node(self, state: AgentState) -> Dict[str, Any]:
        """Finalizes reply, persists message to DB, and logs reply to traces."""
        session: Session = self.session_factory()
        try:
            rider_id = state["rider_id"]
            audit = state["audit_result"] or {}
            reply_text = audit.get("reply", "Aapka anurodh process kar liya gaya hai.")
            received_at = state["received_at"]
            message_id = state["message_id"]

            # Save to conversation_messages
            rec_dt = to_ist_datetime(received_at)
            msg = ConversationMessage(
                message_id=message_id,
                rider_id=rider_id,
                text=state["text"],
                received_at=rec_dt,
                reply=reply_text
            )
            session.add(msg)

            # Record reply in Trace
            trace = Trace(
                rider_id=rider_id,
                at=datetime.now(IST).isoformat(),
                type="reply",
                name="agent_reply",
                input=None,
                output=reply_text
            )
            session.add(trace)
            session.commit()

            return {"reply": reply_text}
        finally:
            session.close()

    def handle_message(self, rider_id: str, message_id: str, text: str, received_at: str) -> str:
        """Executes the LangGraph pipeline for an incoming rider message."""
        initial_state: AgentState = {
            "rider_id": rider_id,
            "message_id": message_id,
            "text": text,
            "received_at": received_at,
            "conversation_history": [],
            "audit_result": None,
            "reply": ""
        }
        final_state = self.workflow.invoke(initial_state)
        return final_state["reply"]

