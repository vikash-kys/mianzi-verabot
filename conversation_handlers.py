"""Brief §7.4 (optional): respond(state, merchant_message) -> dict.

Thin wrapper over the live reply state machine in vera/replies.py, for offline replay.
`state` is a dict: {"conversation_id", "merchant_id", "customer_id"?, "send_as"?, "last_offer"?,
"turns": [{"from": "bot"|"merchant"|"customer", "body": str}], "contexts"?: {scope: {id: payload}}}.
"""
from __future__ import annotations

from vera.replies import respond as _respond
from vera.store import Conversation, Store


def respond(state: dict, merchant_message: str) -> dict:
    store = Store()
    for scope, items in (state.get("contexts") or {}).items():
        for cid, payload in items.items():
            store.put(scope, cid, 1, payload)
    conv = Conversation(conversation_id=state.get("conversation_id", "offline"), merchant_id=state.get("merchant_id"),
                        customer_id=state.get("customer_id"), trigger_id=None, kind=None,
                        send_as=state.get("send_as", "vera"), last_offer=state.get("last_offer"))
    conv.turns = list(state.get("turns") or [])
    conv.bot_sends = sum(1 for t in conv.turns if t.get("from") == "bot")
    conv.turns.append({"from": "customer" if conv.send_as == "merchant_on_behalf" else "merchant", "body": merchant_message})
    return _respond(store, conv, merchant_message, conv.turns[-1]["from"])
