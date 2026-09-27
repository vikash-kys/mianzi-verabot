"""Vera bot — HTTP server for the magicpin AI Challenge judge harness.

Endpoints: POST /v1/context, POST /v1/tick, POST /v1/reply, GET /v1/healthz, GET /v1/metadata
(+ optional POST /v1/teardown). Run:  uvicorn bot:app --host 0.0.0.0 --port 8080

Also exposes the brief's `compose(category, merchant, trigger, customer)` contract at module level.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from vera import __version__
from vera import composer, llm
from vera.replies import respond
from vera.store import SCOPES, Conversation, Store, utc_now_iso
from vera.text import parse_dt

log = logging.getLogger("vera")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

MAX_ACTIONS_PER_TICK = 20
MAX_CONTEXT_BYTES = 500 * 1024

store = Store()
app = FastAPI(title="Vera — magicpin AI Challenge bot", version=__version__)


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """The brief's §7.1 contract: returns body, cta, send_as, suppression_key, rationale."""
    out = composer.compose(category, merchant, trigger, customer)
    out = llm.maybe_polish(out, category, merchant, trigger, customer)
    return {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str | None = None


class TickBody(BaseModel):
    now: str | None = None
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str = "merchant"
    message: str = ""
    received_at: str | None = None
    turn_number: int | None = None


# ---------------------------------------------------------------------------
# Health + metadata
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"service": "vera-bot", "version": __version__, "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply"]}


@app.get("/v1/healthz")
def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - store.started), "contexts_loaded": store.counts()}


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Team Vera"),
        "team_members": [m.strip() for m in os.getenv("TEAM_MEMBERS", "Jhugan").split(",") if m.strip()],
        "model": llm.model_label(),
        "approach": ("Deterministic composer dispatched by trigger.kind over the 4 contexts (category/merchant/trigger/customer); "
                     "every fact is pulled from pushed context, never invented; category voice + Hindi-English code-mix; "
                     "rule-based reply state machine (auto-reply detection, intent handoff, opt-out, off-topic)"
                     + ("; optional temperature-0 LLM polish behind a fact-preservation guard" if llm.enabled() else "")),
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": __version__,
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-27T00:00:00Z"),
    }


# ---------------------------------------------------------------------------
# Context push
# ---------------------------------------------------------------------------

@app.post("/v1/context")
async def push_context(request: Request):
    raw = await request.body()
    if len(raw) > MAX_CONTEXT_BYTES:
        return JSONResponse(status_code=413, content={"accepted": False, "reason": "payload_too_large", "details": f"{len(raw)} bytes"})
    try:
        body = ContextBody.model_validate_json(raw)
    except ValidationError as e:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed", "details": str(e)[:300]})
    if body.scope not in SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {SCOPES}"})
    accepted, current = store.put(body.scope, body.context_id, body.version, body.payload)
    if not accepted:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": current})
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": utc_now_iso()}


@app.post("/v1/teardown")
def teardown():
    store.reset()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tick: decide what (if anything) to send
# ---------------------------------------------------------------------------

def _conversation_id(trigger: dict, merchant_id: str, customer_id: str | None) -> str:
    who = customer_id or merchant_id
    short = re.sub(r"[^a-z0-9]+", "_", who.lower())[:32].strip("_")
    kind = re.sub(r"[^a-z0-9]+", "_", str(trigger.get("kind", "msg")).lower())
    h = hashlib.sha1(str(trigger.get("id", "")).encode()).hexdigest()[:6]
    return f"conv_{short}_{kind}_{h}"


def _plan_tick(now: str | None, trigger_ids: list[str]) -> list[dict]:
    started = time.time()
    now_dt = parse_dt(now) if now else None
    candidates = []
    for tid in dict.fromkeys(trigger_ids):          # de-dupe, keep order
        trg = store.get("trigger", tid)
        if not trg:
            continue
        sk = trg.get("suppression_key")
        if sk and sk in store.sent_suppression:
            continue
        exp = parse_dt(trg.get("expires_at"))
        if now_dt and exp and exp < now_dt:
            continue
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = store.get("merchant", mid)
        if not merchant:
            continue
        category = store.category_for(merchant)
        if not category:
            continue
        cid = trg.get("customer_id")
        customer = store.get("customer", cid) if cid else None
        if trg.get("scope") == "customer":
            if not customer or cid in store.opted_out_customers:
                continue
            consent = customer.get("consent") or {}
            prefs = customer.get("preferences") or {}
            if not consent.get("scope") and prefs.get("reminder_opt_in") is False:
                continue   # no consent on record: restraint beats a policy breach
        elif mid in store.opted_out_merchants:
            continue
        candidates.append((trg, merchant, category, customer))

    # most urgent first; at most one merchant-facing message per merchant per tick (restraint)
    candidates.sort(key=lambda c: (-(c[0].get("urgency") or 0), str(c[0].get("id"))))
    actions, merchants_messaged = [], set()
    for trg, merchant, category, customer in candidates:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break
        mid = merchant.get("merchant_id") or trg.get("merchant_id")
        customer_scope = trg.get("scope") == "customer"
        if not customer_scope and mid in merchants_messaged:
            continue                                    # stays available for a later tick
        out = composer.compose(category, merchant, trg, customer if customer_scope else None, now=now)
        if time.time() - started < 12:              # keep the whole tick well inside the 30s budget
            out = llm.maybe_polish(out, category, merchant, trg, customer if customer_scope else None)
        conv_id = _conversation_id(trg, mid, customer.get("customer_id") if customer_scope and customer else None)
        recipient = (customer or {}).get("customer_id") if customer_scope else mid
        if store.conversation(conv_id) or (recipient, out["body"]) in store.sent_bodies:
            store.sent_suppression.add(out["suppression_key"])   # duplicate of something already sent: drop it
            continue
        action = {
            "conversation_id": conv_id,
            "merchant_id": mid,
            "customer_id": (customer or {}).get("customer_id") if customer_scope else None,
            "send_as": out["send_as"],
            "trigger_id": trg.get("id"),
            "template_name": out["template_name"],
            "template_params": out["template_params"],
            "body": out["body"],
            "cta": out["cta"],
            "suppression_key": out["suppression_key"],
            "rationale": out["rationale"],
        }
        conv = Conversation(conversation_id=conv_id, merchant_id=mid, customer_id=action["customer_id"],
                            trigger_id=trg.get("id"), kind=trg.get("kind"), send_as=out["send_as"],
                            last_offer=out.get("_next_step"))
        conv.turns.append({"from": "bot", "body": out["body"]})
        conv.bot_sends = 1
        store.open_conversation(conv)
        store.sent_suppression.add(out["suppression_key"])
        store.sent_bodies.add((recipient, out["body"]))
        if not customer_scope:
            merchants_messaged.add(mid)
        actions.append(action)
    return actions


@app.post("/v1/tick")
def tick(body: TickBody):
    try:
        with store.lock:
            actions = _plan_tick(body.now, body.available_triggers)
    except Exception:                                   # never fail the judge's call
        log.exception("tick failed")
        actions = []
    log.info("tick now=%s triggers=%d -> actions=%d", body.now, len(body.available_triggers), len(actions))
    return {"actions": actions}


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------

@app.post("/v1/reply")
def reply(body: ReplyBody):
    with store.lock:
        conv = store.conversation(body.conversation_id)
        if conv is None:                                # judge may open a conversation we never started
            conv = Conversation(conversation_id=body.conversation_id, merchant_id=body.merchant_id,
                                customer_id=body.customer_id, trigger_id=None, kind=None,
                                send_as="merchant_on_behalf" if body.from_role == "customer" else "vera")
            store.open_conversation(conv)
        if conv.merchant_id is None and body.merchant_id:
            conv.merchant_id = body.merchant_id
        if conv.ended:
            return {"action": "end", "rationale": "Conversation already closed (opt-out, auto-reply loop or decline); not re-engaging."}
        conv.turns.append({"from": body.from_role, "body": body.message})
        try:
            out = respond(store, conv, body.message, body.from_role)
        except Exception:
            log.exception("reply failed")
            out = {"action": "wait", "wait_seconds": 1800, "rationale": "Internal error while composing; backing off instead of sending something wrong."}
        if out["action"] == "send":
            conv.turns.append({"from": "bot", "body": out["body"]})
            conv.bot_sends += 1
        return out
