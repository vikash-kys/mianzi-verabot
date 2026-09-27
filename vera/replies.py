"""Multi-turn reply handling: classify the inbound message, then pick send / wait / end.

Priorities (checked in this order):
  1. opt-out / hostile           -> end (never argue)
  2. WhatsApp Business auto-reply -> nudge once, then wait, then end
  3. explicit commitment          -> switch to action mode immediately (no more qualifying)
  4. "later / busy"               -> wait
  5. off-topic ask (GST, loans..) -> decline politely, steer back to the open thread
  6. question                     -> answer from context, keep one CTA
  7. anything else                -> brief acknowledgement + one next step
"""
from __future__ import annotations

import re
from typing import Any

from .store import Conversation, Store

AUTO_REPLY_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)",
    r"(our|the) team will (respond|get back|contact|reach)",
    r"we (will|'ll) get back to you",
    r"(currently|presently) (unavailable|away|closed|out of office)",
    r"(business|working|office) hours",
    r"this is an automated", r"automated (assistant|message|reply|response)",
    r"auto[- ]?reply", r"do not reply",
    r"aapki (jaankari|madad) ke liye (bahut[- ]bahut )?shukriya",
    r"hum jaldi (hi )?(aapse )?(sampark|contact)",
    r"we have received your (message|query|request)",
]
OPT_OUT_PATTERNS = [
    r"\bstop\b", r"unsubscribe", r"not interested", r"no interest", r"don'?t (message|text|contact|send|bother)",
    r"do not (message|text|contact|send|bother)", r"leave me alone", r"band karo", r"mat bhejo", r"nahi chahiye",
    r"remove me", r"block (you|this|me)", r"never (message|contact)",
]
HOSTILE_PATTERNS = [r"\bspam\b", r"useless", r"stupid", r"idiot", r"nonsense", r"bakwas", r"fraud", r"scam", r"shut up",
                    r"bothering me", r"irritat", r"waste of (my )?time", r"pagal", r"bloody", r"damn"]
COMMIT_PATTERNS = [
    r"^(ok(ay)?|yes|yeah|yep|sure|haan|han|ha|ji|done|confirm(ed)?|go|go ahead|chalo|theek hai|thik hai|y)\b",
    r"let'?s do (it|this)", r"go ahead", r"do it", r"sounds good", r"please (do|proceed|go ahead|send|share|draft|start)",
    r"send (it|me|the)", r"i want to (join|start|do|go ahead|sign up|renew)", r"sign me up", r"karo\b", r"kar do", r"bhej do",
    r"judna hai", r"judrna hai", r"proceed", r"what'?s next", r"whats next", r"how do (i|we) start", r"\bconfirm\b",
    r"\bbook (it|me)\b", r"^\s*[12]\s*$",
]
LATER_PATTERNS = [r"\blater\b", r"\bbusy\b", r"baad (mein|me)", r"abhi nahi", r"not now", r"tomorrow", r"kal\b",
                  r"after (some|a) (time|while)", r"in a meeting", r"call you back", r"give me (some )?time"]
OFF_TOPIC_PATTERNS = [r"\bgst\b", r"income tax", r"\bitr\b", r"\btax(es)? (filing|return)", r"\bloan\b", r"insurance",
                      r"\bca\b", r"accountant", r"legal notice", r"lawyer", r"visa", r"passport", r"electricity bill",
                      r"my (son|daughter)'?s? (homework|admission)", r"stock (tips|market)", r"crypto", r"cricket score"]
QUESTION_WORDS = r"(\?|^(what|how|why|when|which|where|who|can|could|will|is|are|do|does|kya|kaise|kitna|kab|kaun))"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s?']", " ", (s or "").lower())).strip()


def matches(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify(message: str, conv: Conversation | None, store: Store, merchant_id: str | None) -> str:
    t = norm(message)
    if not t:
        return "empty"
    # repetition across turns/conversations = auto-reply even if the wording is unusual
    seen = store.merchant_auto_replies.get(merchant_id or "", {})
    repeated = seen.get(t, 0) >= 1 or (conv and sum(1 for x in conv.turns[:-1] if x["from"] != "bot" and norm(x["body"]) == t) >= 1)
    if matches(AUTO_REPLY_PATTERNS, t) or (repeated and len(t) > 25):
        return "auto_reply"
    if matches(OPT_OUT_PATTERNS, t):
        return "opt_out"
    hostile = matches(HOSTILE_PATTERNS, t)
    off = matches(OFF_TOPIC_PATTERNS, t)
    if hostile and not off:
        return "hostile"
    if off:
        return "off_topic"
    if matches(LATER_PATTERNS, t) and not matches(COMMIT_PATTERNS, t):
        return "later"
    if matches(COMMIT_PATTERNS, t):
        return "commit"
    if re.search(QUESTION_WORDS, t):
        return "question"
    if re.match(r"^(no|nope|nahi|na)\b", t):
        return "decline"
    return "other"


# ---------------------------------------------------------------------------

def _first_name(merchant: dict | None, category_slug: str | None) -> str:
    ident = (merchant or {}).get("identity") or {}
    o = (ident.get("owner_first_name") or "").strip()
    if category_slug == "dentists" and o and not o.lower().startswith("dr"):
        return f"Dr. {o}"
    return o


def _hindi(merchant: dict | None) -> bool:
    return "hi" in [str(x).lower() for x in (((merchant or {}).get("identity") or {}).get("languages") or [])]


def _offer_phrase(conv: Conversation) -> str:
    """Verb phrase for what Vera offered, e.g. 'draft 3 fresh Google posts for you to approve'."""
    return conv.last_offer or "take care of the next step"


def _unique(conv: Conversation, candidates: list[str]) -> str:
    """Never repeat a body verbatim inside one conversation (anti-repetition penalty)."""
    sent = {x["body"] for x in conv.turns if x["from"] == "bot"}
    for c in candidates:
        if c not in sent:
            return c
    return candidates[-1] + f" (step {conv.bot_sends + 1})"


def respond(store: Store, conv: Conversation, message: str, from_role: str) -> dict[str, Any]:
    merchant = store.get("merchant", conv.merchant_id)
    category = store.category_for(merchant)
    slug = (category or {}).get("slug") or (merchant or {}).get("category_slug")
    name = _first_name(merchant, slug)
    hi = _hindi(merchant)
    customer_side = from_role == "customer" or conv.send_as == "merchant_on_behalf"
    label = classify(message, conv, store, conv.merchant_id)

    # bookkeeping for auto-reply detection across conversations
    if label == "auto_reply":
        bucket = store.merchant_auto_replies.setdefault(conv.merchant_id or "", {})
        bucket[norm(message)] = bucket.get(norm(message), 0) + 1
        conv.auto_reply_count += 1
        total_seen = max(conv.auto_reply_count, sum(bucket.values()))
    else:
        total_seen = 0
        store.merchant_auto_replies.pop(conv.merchant_id or "", None)   # a human replied: reset the auto-reply streak

    if label == "opt_out" or label == "hostile":
        conv.ended = True
        if customer_side and conv.customer_id:
            store.opted_out_customers.add(conv.customer_id)
        elif conv.merchant_id:
            store.opted_out_merchants.add(conv.merchant_id)
        return {"action": "end",
                "rationale": f"Merchant/customer signalled {'hostility' if label == 'hostile' else 'opt-out'}; closing politely and suppressing further proactive sends to them."}

    if label == "auto_reply":
        if total_seen <= 1:
            body = _unique(conv, [
                "Looks like an auto-reply 🙂 Whenever the owner sees this — just reply YES and I'll take it from there.",
                "Auto-reply noted. When you're back, a one-word YES is all I need to continue.",
            ])
            return {"action": "send", "body": body, "cta": "binary_yes_no",
                    "rationale": "Detected a WhatsApp Business canned auto-reply; one short flag for the owner, no new pitch."}
        if total_seen == 2:
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Same canned auto-reply again: owner isn't at the phone. Backing off 24h instead of burning turns."}
        conv.ended = True
        return {"action": "end",
                "rationale": f"Auto-reply {total_seen}x with no human response; closing the conversation to avoid spamming."}

    if label == "later":
        return {"action": "wait", "wait_seconds": 14400 if "tomorrow" not in norm(message) and "kal" not in norm(message) else 86400,
                "rationale": "Merchant asked for time; backing off rather than pushing."}

    if label == "empty":
        return {"action": "wait", "wait_seconds": 3600, "rationale": "Empty inbound message; nothing to respond to yet."}

    if customer_side:
        return _customer_reply(conv, label, merchant, message)

    if label == "commit":
        conv.action_mode = True
        what = conv.last_offer
        steps = [
            (f"Done — I'll {what} right away." if what else "Done — starting right away.")
            + " You'll get it here in a few minutes; reply CONFIRM once you've checked it and I'll publish.",
            "On it. Next step: I send you the final version for a quick look — one CONFIRM from you and it goes live.",
            "All set from my side — sending the final version now. Reply CONFIRM to publish, or tell me what to change.",
        ]
        if hi:
            steps[0] = (f"Done {name} — kaam shuru kar diya hai{' (' + what + ')' if what else ''}. Draft yahin bhejti hoon; "
                        "check karke CONFIRM reply kar dijiye, main publish kar dungi.")
        return {"action": "send", "body": _unique(conv, steps), "cta": "binary_confirm_cancel",
                "rationale": "Explicit commitment detected — switched from pitching to executing; next step is a concrete confirm, not another qualifying question."}

    if label == "off_topic":
        back = _offer_phrase(conv)
        body = _unique(conv, [
            f"That one's outside what I can help with — your CA is the right person for it. Coming back to my earlier offer: shall I {back}? Reply YES.",
            f"I'll have to leave that to a specialist, sorry. Meanwhile I'm ready to {back} whenever you are — reply YES to proceed.",
        ])
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": "Out-of-scope request politely declined; steered back to the open thread with a single CTA."}

    if label == "decline":
        conv.ended = True
        return {"action": "end", "rationale": "Merchant declined the proposal; exiting gracefully without re-pitching."}

    if label == "question":
        return {"action": "send", "body": _unique(conv, _answer(conv, message, merchant, category, name)),
                "cta": "binary_yes_no",
                "rationale": "Merchant asked a question; answered from the merchant/category context only and closed with one clear next step."}

    # other / free text
    if conv.bot_sends >= 4:
        conv.ended = True
        return {"action": "end", "rationale": "Several turns without a clear commitment; closing politely to respect the merchant's time."}
    body = _unique(conv, [
        f"Got it{', ' + name if name else ''}. Simplest next step: I {_offer_phrase(conv)} — just reply YES to go ahead.",
        f"Noted, thanks. If it helps, I can {_offer_phrase(conv)} today — one YES from you is enough.",
    ])
    return {"action": "send", "body": body, "cta": "binary_yes_no",
            "rationale": "Acknowledged the reply and re-offered a single low-effort next step (effort externalisation)."}


def _answer(conv: Conversation, message: str, merchant: dict | None, category: dict | None, name: str) -> list[str]:
    t = norm(message)
    perf = (merchant or {}).get("performance") or {}
    peer = (category or {}).get("peer_stats") or {}
    offers = [o["title"] for o in (merchant or {}).get("offers") or [] if o.get("status") == "active" and o.get("title")]
    what = _offer_phrase(conv)
    if re.search(r"(cost|price|charge|fee|kitna|paisa|pay)", t):
        # no pricing data in context -> don't claim a price; say what we do know and hand off
        sub = (merchant or {}).get("subscription") or {}
        plan = f" (you're on the {sub['plan']} plan)" if sub.get("plan") and sub.get("status") == "active" else ""
        return [f"I'll confirm exact pricing for your account{plan} before anything is billed — nothing is charged or published without your OK. Meanwhile, want me to {what}? Reply YES.",
                f"Pricing gets confirmed with you first{plan}; drafting commits you to nothing. Shall I {what}? Reply YES."]
    if re.search(r"(how|kaise|work|kya hoga|what happens)", t):
        return [f"Simple: I {what}, send it here for your OK, and publish only after you say CONFIRM. Nothing goes out without you. Start now? Reply YES.",
                f"You approve everything before it goes live — I just do the drafting and posting. Shall I {what}? Reply YES."]
    if re.search(r"(number|data|views|calls|ctr|performance|result)", t) and perf:
        bits = []
        if perf.get("views") is not None:
            bits.append(f"{perf['views']:,} views")
        if perf.get("calls") is not None:
            bits.append(f"{perf['calls']} calls")
        if perf.get("ctr") is not None:
            bits.append(f"CTR {perf['ctr'] * 100:.1f}%" + (f" (peer avg {peer['avg_ctr'] * 100:.1f}%)" if peer.get("avg_ctr") else ""))
        return [f"Last 30 days: {', '.join(bits)}. The quickest lever I see: let me {what}. Go ahead? Reply YES."]
    if re.search(r"(offer|discount|deal)", t) and offers:
        return [f"Your live offer{'s are' if len(offers) > 1 else ' is'}: {', '.join(offers)}. I'd build on {offers[0]} rather than add a discount. Want me to? Reply YES."]
    return [f"Good question{', ' + name if name else ''}. Short answer: I do the work and you only approve the final version. Want me to {what}? Reply YES.",
            f"Easier to show than explain — shall I {what} so you can see it? Reply YES."]


def _customer_reply(conv: Conversation, label: str, merchant: dict | None, message: str) -> dict[str, Any]:
    biz = ((merchant or {}).get("identity") or {}).get("name") or "us"
    t = norm(message)
    if label == "commit":
        conv.action_mode = True
        slot = ""
        if re.fullmatch(r"\s*1\s*", t):
            slot = " for the first slot"
        elif re.fullmatch(r"\s*2\s*", t):
            slot = " for the second slot"
        body = f"Confirmed{slot} ✅ You'll get a reminder a few hours before. Reply CHANGE anytime if plans move."
        return {"action": "send", "body": _unique(conv, [body, "All set ✅ See you then — reply CHANGE if you need to move it."]),
                "cta": "none", "rationale": "Customer accepted; confirming the booking/dispatch immediately without further questions."}
    if label == "question":
        return {"action": "send", "body": _unique(conv, [
            f"Happy to help — someone from {biz} will call you shortly with the details. Or reply YES to go ahead as proposed.",
            f"{biz} team will get back to you on that today. Reply YES if you'd like us to hold the slot meanwhile."]),
                "cta": "binary_yes_no", "rationale": "Customer question routed to the merchant's team; slot held open with a single CTA."}
    if label == "decline":
        conv.ended = True
        return {"action": "end", "rationale": "Customer declined; closing without further follow-up."}
    if conv.bot_sends >= 3:
        conv.ended = True
        return {"action": "end", "rationale": "No clear intent after several turns; closing to avoid over-messaging a customer."}
    return {"action": "send", "body": _unique(conv, [
        "Thanks for replying! Just reply YES to confirm, or tell us a day/time that suits you better.",
        "Noted 🙂 Share a convenient day/time and we'll lock it in."]),
            "cta": "binary_yes_no", "rationale": "Ambiguous customer reply; asking for one simple confirmation."}
