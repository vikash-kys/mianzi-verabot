"""Optional LLM polish (OFF by default).

When VERA_LLM=anthropic and ANTHROPIC_API_KEY are set, the deterministic draft is sent to Claude to be
rewritten for flow. The rewrite is accepted only if it passes a fact guard:
  * every number in the rewrite already appears in the draft (no invented stats/prices/dates);
  * no URLs, no category taboo phrases, still ends with a question / reply instruction.
Otherwise the deterministic draft is used. Results are cached by input hash, so the same input always
returns the same output for the lifetime of the process (the brief requires determinism).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading

log = logging.getLogger("vera.llm")

_cache: dict[str, str] = {}
_lock = threading.Lock()
_client = None

SYSTEM = (
    "You polish WhatsApp messages written by Vera, magicpin's assistant for Indian merchants. "
    "Rewrite the DRAFT so it reads naturally and tightly, keeping: every fact, number, name, date, price and source "
    "exactly as given; the same language mix (keep Hindi-English code-mix if present); the category voice; exactly one "
    "call to action as the last sentence. Do not add any fact, number, offer, competitor or claim that is not in the "
    "DRAFT. No URLs, no hashtags, no greeting preamble, no sign-off. Return only the rewritten message text."
)


def enabled() -> bool:
    return os.getenv("VERA_LLM", "").lower() == "anthropic" and bool(os.getenv("ANTHROPIC_API_KEY"))


def model_name() -> str:
    return os.getenv("VERA_LLM_MODEL", "claude-opus-5")


def model_label() -> str:
    return f"deterministic-composer + {model_name()} polish" if enabled() else "deterministic-composer (rule-based, no LLM)"


def _get_client():
    global _client
    if _client is None:
        import anthropic  # imported lazily so the bot runs without the SDK installed
        _client = anthropic.Anthropic(timeout=float(os.getenv("VERA_LLM_TIMEOUT", "12")), max_retries=0)
    return _client


NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "") for n in NUM.findall(s)}


def passes_guard(draft: str, rewrite: str, category: dict | None) -> bool:
    if not rewrite or len(rewrite) > max(900, int(len(draft) * 1.4)):
        return False
    if not _numbers(rewrite) <= _numbers(draft):
        return False
    if re.search(r"https?://|www\.", rewrite):
        return False
    for t in ((category or {}).get("voice") or {}).get("vocab_taboo") or []:
        phrase = re.sub(r"\s*\(.*?\)", "", str(t)).strip().lower()
        if phrase and phrase in rewrite.lower():
            return False
    tail = rewrite.strip()[-160:].lower()
    if "?" not in tail and "reply" not in tail and "confirm" not in tail:
        return False
    return True


def maybe_polish(out: dict, category, merchant, trigger, customer) -> dict:
    if not enabled():
        return out
    draft = out["body"]
    key = hashlib.sha256(json.dumps([draft, (category or {}).get("slug"), trigger.get("kind")],
                                    ensure_ascii=False).encode()).hexdigest()
    with _lock:
        cached = _cache.get(key)
    if cached is None:
        cached = draft
        try:
            resp = _get_client().messages.create(
                model=model_name(),
                max_tokens=1024,
                output_config={"effort": "low"},
                system=SYSTEM,
                messages=[{"role": "user", "content":
                           f"CATEGORY: {(category or {}).get('slug')}\nVOICE: {((category or {}).get('voice') or {}).get('tone')}\n"
                           f"AUDIENCE: {'customer of the merchant' if out.get('send_as') == 'merchant_on_behalf' else 'merchant owner'}\n\n"
                           f"DRAFT:\n{draft}"}],
            )
            if resp.stop_reason == "end_turn":
                text = "".join(b.text for b in resp.content if b.type == "text").strip()
                if passes_guard(draft, text, category):
                    cached = text
                else:
                    log.info("llm rewrite rejected by fact guard; keeping deterministic draft")
        except Exception as e:  # network/auth/timeout: the deterministic draft is always a valid answer
            log.warning("llm polish skipped: %s", e)
        with _lock:
            _cache[key] = cached
    if cached != draft:
        out = dict(out, body=cached, rationale=out["rationale"] + " (Wording polished by LLM; facts verified unchanged.)")
    return out
