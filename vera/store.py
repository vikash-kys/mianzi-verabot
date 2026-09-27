"""In-memory, thread-safe context + conversation store.

Contexts are versioned per (scope, context_id): the same version is a no-op,
a higher version replaces atomically, a lower version is rejected as stale.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCOPES = ("category", "merchant", "customer", "trigger")


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: str | None
    customer_id: str | None
    trigger_id: str | None
    kind: str | None
    send_as: str
    turns: list[dict] = field(default_factory=list)   # {"from": "bot"|"merchant"|"customer", "body": str}
    auto_reply_count: int = 0
    bot_sends: int = 0
    last_offer: str | None = None                      # what the bot proposed ("the patient-ed WhatsApp draft")
    ended: bool = False
    action_mode: bool = False


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.started = time.time()
        self._ctx: dict[tuple[str, str], dict[str, Any]] = {}
        self.conversations: dict[str, Conversation] = {}
        self.sent_suppression: set[str] = set()
        self.sent_bodies: set[tuple[str, str]] = set()   # (recipient, body): never send the same text twice
        self.opted_out_merchants: set[str] = set()
        self.opted_out_customers: set[str] = set()
        # merchant_id -> {normalised auto-reply text: count}, catches auto-replies spread over conversations
        self.merchant_auto_replies: dict[str, dict[str, int]] = {}

    # ---- contexts -------------------------------------------------------
    def put(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, int | None]:
        """Returns (accepted, current_version_if_rejected). Same version = idempotent no-op (accepted)."""
        with self._lock:
            key = (scope, context_id)
            cur = self._ctx.get(key)
            if cur is not None and cur["version"] == version:
                return True, None
            if cur is not None and cur["version"] > version:
                return False, cur["version"]
            self._ctx[key] = {"version": version, "payload": payload}
            return True, None

    def get(self, scope: str, context_id: str | None) -> dict | None:
        if not context_id:
            return None
        with self._lock:
            cur = self._ctx.get((scope, context_id))
            return cur["payload"] if cur else None

    def counts(self) -> dict[str, int]:
        with self._lock:
            out = {s: 0 for s in SCOPES}
            for scope, _ in self._ctx:
                out[scope] = out.get(scope, 0) + 1
            return out

    def category_for(self, merchant: dict | None) -> dict | None:
        if not merchant:
            return None
        return self.get("category", merchant.get("category_slug"))

    # ---- conversations --------------------------------------------------
    def conversation(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            return self.conversations.get(conversation_id)

    def open_conversation(self, conv: Conversation) -> None:
        with self._lock:
            self.conversations[conv.conversation_id] = conv

    def reset(self) -> None:
        with self._lock:
            self._ctx.clear()
            self.conversations.clear()
            self.sent_suppression.clear()
            self.sent_bodies.clear()
            self.opted_out_merchants.clear()
            self.opted_out_customers.clear()
            self.merchant_auto_replies.clear()

    @property
    def lock(self) -> threading.RLock:
        return self._lock


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
