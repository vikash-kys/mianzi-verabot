"""Deterministic message composer: compose(category, merchant, trigger, customer?, now?) -> dict.

Design rules (from the challenge brief):
  * every number / name / date in a message comes from one of the four contexts — nothing invented;
  * one clear ask, landing in the last sentence;
  * voice follows the category (clinical peer for dentists, operator-to-operator for restaurants, ...);
  * Hindi-English code-mix when the merchant (or customer) speaks Hindi;
  * dispatch by trigger.kind, with a grounded generic fallback for kinds we have never seen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .text import (clean, ctr, days_between, first_sentence, humanize, nice_date, nice_time, num,
                   parse_dt, pct, price_in, rupees)

# ---------------------------------------------------------------------------
# Context view
# ---------------------------------------------------------------------------

CATEGORY_NOUN = {
    "dentists": ("clinic", "patients", "dentist"),
    "salons": ("salon", "clients", "salon"),
    "restaurants": ("restaurant", "diners", "restaurant"),
    "gyms": ("studio", "members", "gym"),
    "pharmacies": ("pharmacy", "customers", "pharmacy"),
}

CUSTOMER_EMOJI = {"dentists": "🦷", "salons": "✨", "restaurants": "🍽️", "gyms": "💪", "pharmacies": "💊"}


@dataclass
class View:
    category: dict
    merchant: dict
    trigger: dict
    customer: dict | None
    now: datetime
    facts: list[str] = field(default_factory=list)   # human-readable provenance for the rationale
    next_step: str | None = None                      # what Vera offered to do (used by the reply handler)

    # --- identity -------------------------------------------------------
    @property
    def slug(self) -> str:
        return self.category.get("slug") or self.merchant.get("category_slug") or ""

    @property
    def ident(self) -> dict:
        return self.merchant.get("identity") or {}

    @property
    def biz(self) -> str:
        return self.ident.get("name") or "your business"

    @property
    def owner(self) -> str:
        return (self.ident.get("owner_first_name") or "").strip()

    @property
    def locality(self) -> str:
        return self.ident.get("locality") or self.ident.get("city") or ""

    @property
    def place(self) -> str:
        loc, city = self.ident.get("locality"), self.ident.get("city")
        return ", ".join(x for x in (loc, city) if x)

    def salutation(self) -> str:
        o = self.owner
        if self.slug == "dentists":
            if not o:
                return "Doctor"
            return o if o.lower().startswith("dr") else f"Dr. {o}"
        return o or f"{self.biz} team"

    @property
    def noun(self) -> tuple[str, str, str]:
        return CATEGORY_NOUN.get(self.slug, ("business", "customers", "business"))

    # --- language -------------------------------------------------------
    @property
    def merchant_hindi(self) -> bool:
        langs = [str(x).lower() for x in (self.ident.get("languages") or [])]
        return "hi" in langs

    @property
    def customer_hindi(self) -> bool:
        if not self.customer:
            return False
        pref = str((self.customer.get("identity") or {}).get("language_pref") or "").lower()
        return pref.startswith("hi") or "hindi" in pref

    # --- data -----------------------------------------------------------
    @property
    def perf(self) -> dict:
        return self.merchant.get("performance") or {}

    @property
    def peer(self) -> dict:
        return self.category.get("peer_stats") or {}

    @property
    def agg(self) -> dict:
        return self.merchant.get("customer_aggregate") or {}

    @property
    def payload(self) -> dict:
        p = dict(self.trigger.get("payload") or {})
        if p.get("placeholder"):
            p.pop("placeholder", None)
            p.pop("metric_or_topic", None)
        return p

    @property
    def kind(self) -> str:
        return str(self.trigger.get("kind") or "")

    @property
    def signals(self) -> list[str]:
        return [str(s) for s in (self.merchant.get("signals") or [])]

    def active_offers(self) -> list[str]:
        return [o.get("title") for o in (self.merchant.get("offers") or [])
                if o.get("status") == "active" and o.get("title")]

    def expired_offers(self) -> list[str]:
        return [o.get("title") for o in (self.merchant.get("offers") or [])
                if o.get("status") in ("expired", "paused") and o.get("title")]

    def offer_like(self, words: list[str], active_only: bool = True) -> str | None:
        pool = self.active_offers() if active_only else self.active_offers() + self.expired_offers()
        for w in words:
            for t in pool:
                if w.lower() in t.lower():
                    return t
        return None

    def catalog_offer(self, words: list[str] | None = None, types: tuple[str, ...] = ("service_at_price",)) -> str | None:
        cat = self.category.get("offer_catalog") or []
        if words:
            for w in words:
                for o in cat:
                    if w.lower() in str(o.get("title", "")).lower():
                        return o["title"]
        for o in cat:
            if o.get("type") in types and o.get("title"):
                return o["title"]
        return cat[0]["title"] if cat and cat[0].get("title") else None

    def digest_item(self, item_id: str | None = None, kinds: tuple[str, ...] = ()) -> dict | None:
        items = self.category.get("digest") or []
        if item_id:
            for d in items:
                if d.get("id") == item_id:
                    return d
        if kinds:
            for d in items:
                if d.get("kind") in kinds:
                    return d
        return None

    def seasonal_beat(self, month: int | None = None) -> dict | None:
        month = month or self.now.month
        for b in self.category.get("seasonal_beats") or []:
            if month_in_range(month, str(b.get("month_range", ""))):
                return b
        return None

    def top_trend(self) -> dict | None:
        """Most relevant rising search trend: overlap with this merchant's offers/reviews/name/city beats raw growth."""
        ts = [t for t in (self.category.get("trend_signals") or []) if isinstance(t.get("delta_yoy"), (int, float))]
        if not ts:
            return None
        own = " ".join(self.active_offers() + self.expired_offers()
                       + [str(t.get("theme", "")) for t in self.merchant.get("review_themes") or []]
                       + [self.biz, str(self.ident.get("city", "")), self.locality]).lower().replace("_", " ")
        stop = {"near", "me", "price", "cost", "offer", "delhi", "classes"}

        def score(t: dict) -> tuple:
            words = [w for w in re.findall(r"[a-z]+", str(t.get("query", "")).lower()) if w not in stop and len(w) > 2]
            hits = sum(1 for w in words if w in own)
            city = str(self.ident.get("city", "")).lower()
            city_hit = 1 if city and city in str(t.get("query", "")).lower() else 0
            return (hits + city_hit, t["delta_yoy"])
        return max(ts, key=score)

    def last_merchant_message(self) -> dict | None:
        for turn in reversed(self.merchant.get("conversation_history") or []):
            if turn.get("from") == "merchant":
                return turn
        return None

    def last_vera_message(self) -> dict | None:
        for turn in reversed(self.merchant.get("conversation_history") or []):
            if turn.get("from") == "vera":
                return turn
        return None

    def fact(self, s: str) -> None:
        if s and s not in self.facts:
            self.facts.append(s)


def possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def month_in_range(month: int, rng: str) -> bool:
    names = {m.lower(): i + 1 for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
    found = [names[t.lower()[:3]] for t in re.findall(r"[A-Za-z]{3,}", rng) if t.lower()[:3] in names]
    if not found:
        return False
    if len(found) == 1:
        return month == found[0]
    a, b = found[0], found[1]
    return a <= month <= b if a <= b else (month >= a or month <= b)


# ---------------------------------------------------------------------------
# Reusable sentence builders (all grounded)
# ---------------------------------------------------------------------------

def perf_gap(v: View) -> str | None:
    """Biggest verifiable gap vs peer benchmark, as one clause."""
    p, peer = v.perf, v.peer
    if isinstance(p.get("ctr"), (int, float)) and isinstance(peer.get("avg_ctr"), (int, float)) and p["ctr"] < peer["avg_ctr"]:
        v.fact(f"ctr {p['ctr']} vs peer {peer['avg_ctr']}")
        return f"your profile CTR is {ctr(p['ctr'])} vs {ctr(peer['avg_ctr'])} for similar {v.slug}"
    if isinstance(p.get("calls"), (int, float)) and isinstance(peer.get("avg_calls_30d"), (int, float)) and p["calls"] < peer["avg_calls_30d"]:
        v.fact(f"calls {p['calls']} vs peer {peer['avg_calls_30d']}")
        return f"you got {num(p['calls'])} calls in 30 days vs a peer average of {num(peer['avg_calls_30d'])}"
    if isinstance(p.get("views"), (int, float)) and isinstance(peer.get("avg_views_30d"), (int, float)) and p["views"] < peer["avg_views_30d"]:
        v.fact(f"views {p['views']} vs peer {peer['avg_views_30d']}")
        return f"your profile had {num(p['views'])} views in 30 days vs a peer average of {num(peer['avg_views_30d'])}"
    return None


def perf_strength(v: View) -> str | None:
    p, peer = v.perf, v.peer
    if isinstance(p.get("ctr"), (int, float)) and isinstance(peer.get("avg_ctr"), (int, float)) and p["ctr"] > peer["avg_ctr"]:
        v.fact(f"ctr {p['ctr']} above peer {peer['avg_ctr']}")
        return f"your CTR ({ctr(p['ctr'])}) is already above the {ctr(peer['avg_ctr'])} peer average"
    if isinstance(p.get("calls"), (int, float)) and isinstance(peer.get("avg_calls_30d"), (int, float)) and p["calls"] > peer["avg_calls_30d"]:
        v.fact(f"calls {p['calls']} above peer {peer['avg_calls_30d']}")
        return f"{num(p['calls'])} calls in 30 days is above the {num(peer['avg_calls_30d'])} peer average"
    return None


def delta_extreme(v: View, negative: bool) -> tuple[str, float] | None:
    d = v.perf.get("delta_7d") or {}
    items = [(k.replace("_pct", ""), val) for k, val in d.items() if isinstance(val, (int, float))]
    items = [i for i in items if (i[1] < 0 if negative else i[1] > 0)]
    if not items:
        return None
    return min(items, key=lambda i: i[1]) if negative else max(items, key=lambda i: i[1])


def signal_fix(v: View) -> tuple[str, str] | None:
    """(problem clause, what Vera will do) from merchant signals — only signals actually present."""
    for s in v.signals:
        if s.startswith("stale_posts"):
            days = s.split(":")[1] if ":" in s else None
            v.fact(f"signal {s}")
            return (f"your last Google post was {days.replace('d', ' days')} ago" if days else "your Google posts have gone stale",
                    "draft 3 fresh Google posts for you to approve")
    if "no_active_offers" in v.signals or not v.active_offers():
        off = v.catalog_offer()
        if off:
            v.fact(f"no active offers; catalog has {off}")
            return ("you have no live offer on your profile right now",
                    f"set up \"{off}\" (a proven format for {v.slug})")
    for s in v.signals:
        if s.startswith("unverified") or s == "unverified_gbp":
            v.fact("signal unverified_gbp")
            return ("your Google profile is still unverified", "walk you through verification (about 5 minutes)")
    if v.ident.get("verified") is False:
        v.fact("identity.verified=false")
        return ("your Google profile is still unverified", "walk you through verification (about 5 minutes)")
    for s in v.signals:
        if s in ("no_recent_post",):
            v.fact(f"signal {s}")
            return ("there's no recent post on your profile", "draft 3 Google posts for you to approve")
    return None


def yes_cta(v: View, action: str, hindi_action: str | None = None) -> str:
    """Single binary CTA, code-mixed when the merchant speaks Hindi."""
    v.next_step = action
    if v.merchant_hindi and hindi_action:
        return f"{hindi_action[0].upper() + hindi_action[1:]}? Reply YES."
    return f"Want me to {action}? Reply YES."


def pos_review_theme(v: View) -> dict | None:
    th = [t for t in (v.merchant.get("review_themes") or []) if t.get("sentiment") == "pos"]
    return max(th, key=lambda t: t.get("occurrences_30d", 0)) if th else None


def neg_review_theme(v: View) -> dict | None:
    th = [t for t in (v.merchant.get("review_themes") or []) if t.get("sentiment") == "neg"]
    return max(th, key=lambda t: t.get("occurrences_30d", 0)) if th else None


# ---------------------------------------------------------------------------
# Merchant-facing composers  (each returns (body, cta, rationale, template_params))
# ---------------------------------------------------------------------------

Result = tuple[str, str, str, list[str]]


def c_digest(v: View) -> Result:
    p = v.payload
    item = v.digest_item(p.get("top_item_id") or p.get("digest_item_id") or p.get("alert_id") or p.get("item_id"),
                         kinds=("research", "compliance", "trend", "tech", "alert", "supply", "cde"))
    sal = v.salutation()
    if not item:
        return c_generic(v)
    v.fact(f"digest {item.get('id')}: {item.get('title')} ({item.get('source')})")
    k = item.get("kind")
    title = item.get("title", "")
    source = item.get("source", "")
    summary = first_sentence(item.get("summary", ""))
    actionable = item.get("actionable", "")

    tie = None
    seg = str(item.get("patient_segment") or "")
    if "high_risk" in seg and v.agg.get("high_risk_adult_count"):
        tie = f"You have {num(v.agg['high_risk_adult_count'])} high-risk adults on your roster, so this maps straight onto your recall list."
        v.fact(f"high_risk_adult_count {v.agg['high_risk_adult_count']}")
    elif v.slug == "pharmacies" and v.agg.get("chronic_rx_count"):
        tie = f"Relevant for your {num(v.agg['chronic_rx_count'])} chronic-Rx customers."
        v.fact(f"chronic_rx_count {v.agg['chronic_rx_count']}")
    elif v.agg.get("total_active_members"):
        tie = f"Useful context for your {num(v.agg['total_active_members'])} active members."
        v.fact(f"total_active_members {v.agg['total_active_members']}")

    if k in ("compliance",) or v.kind == "regulation_change":
        deadline = p.get("deadline_iso")
        dl = parse_dt(deadline) if deadline else None
        left = days_between(v.now, dl) if dl else None
        when = ""
        if dl and left is not None and left >= 0:
            when = f" You have {left} days (till {nice_date(deadline, False)})."
            v.fact(f"deadline {deadline}, {left} days left")
        body = (f"{sal}, compliance heads-up: {title} — {source}. {summary}{when} "
                f"Suggested step: {actionable.rstrip('.')}. "
                + yes_cta(v, "send a 1-page audit checklist for your setup", "Aapke setup ke liye 1-page audit checklist bhej doon"))
        return body, "binary_yes_no", f"Compliance item from the category digest ({source}) with a hard date; merchant needs a concrete audit step, so the ask is a ready-made checklist.", [sal, title, actionable]

    if k == "alert" or v.kind == "supply_alert":
        batches = p.get("affected_batches") or []
        mol = p.get("molecule")
        mfr = p.get("manufacturer")
        bits = []
        if mol:
            bits.append(mol)
        if batches:
            bits.append("batches " + ", ".join(batches))
        if mfr:
            bits.append(f"by {mfr}")
        spec = " ".join(bits)
        v.fact(f"alert payload {spec}")
        detail = re.sub(r"\s*\([^)]*in alert[^)]*\)", "", item.get("summary", ""))
        detail = " ".join(re.split(r"(?<=[.!?])\s+", detail)[:2])
        lead = f"{sal}, urgent recall alert ({source}): {spec}." if spec else f"{sal}, urgent: {title.rstrip('.')} ({source})."
        body = (f"{lead} {detail} "
                + (tie + " " if tie else "")
                + yes_cta(v, f"filter your repeat-Rx list for {mol or 'the affected stock'} and draft the customer WhatsApp",
                          f"{mol or 'Is molecule'} wale repeat-Rx customers ki list + WhatsApp draft bana doon"))
        return body, "binary_yes_no", "Urgency-5 product recall: batch-level specifics plus the merchant's own chronic-Rx base; offering to do the customer outreach work end to end.", [sal, title, spec]

    if k == "cde" or v.kind == "cde_opportunity":
        date_s = item.get("date") or p.get("date")
        when = f"{nice_date(date_s)}, {nice_time(date_s)}" if date_s and nice_time(date_s) else (nice_date(date_s) if date_s else "")
        credits = p.get("credits") or item.get("credits")
        fee = humanize(p.get("fee")) if p.get("fee") else ""
        details = ", ".join(x for x in [when, f"{credits} CDE credits" if credits else "", fee] if x)
        v.fact(f"cde {details}")
        full = item.get("summary", "")
        summary = full if len(full) <= 200 else summary
        fee_note = f"{actionable.rstrip('.')}." if actionable and not fee else ""
        body = (f"{sal}, {title} — {details}. {summary} {fee_note} "
                + yes_cta(v, "save you a seat and add it to your calendar", "Seat block karke calendar mein add kar doon"))
        return body, "binary_yes_no", f"CDE event from the category digest ({source}); low-effort yes/no to register.", [sal, title, details]

    if k in ("trend", "supply") or v.kind == "category_trend_movement":
        body = (f"{sal}, trend worth acting on in {v.ident.get('city') or 'your city'}: {title} ({source}). {summary} "
                f"Suggested move: {actionable.rstrip('.')}. "
                + (yes_cta(v, f"draft a WhatsApp for your regular {v.noun[1]} about this", f"Regular {v.noun[1]} ke liye iska WhatsApp draft kar doon")
                   if k == "supply" else
                   yes_cta(v, "draft that update for your Google profile", "Google profile ke liye yeh update draft kar doon")))
        return body, "binary_yes_no", "Category trend item with a source; turned into one concrete profile change Vera can draft.", [sal, title, actionable]

    # research / tech / default digest
    n = item.get("trial_n")
    lead = f"{sal}, {source} has one worth 2 minutes: {title}"
    if n:
        lead += f" (n={num(n)})"
        v.fact(f"trial_n {n}")
    body = (f"{lead}. {summary} " + (tie + " " if tie else "")
            + yes_cta(v, "pull the key findings and draft a patient-friendly WhatsApp you can forward" if v.slug == "dentists"
                      else "summarise it and draft a customer-facing post you can share",
                      "Key findings + ek customer-friendly WhatsApp draft bana doon"))
    return body, "binary_yes_no", f"Weekly digest item ({source}) matched to this merchant's own customer base; reciprocity offer (Vera does the summarising/drafting).", [sal, title, summary]


def c_perf_dip(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    metric = p.get("metric")
    delta = p.get("delta_pct")
    if metric is None or delta is None:
        ext = delta_extreme(v, negative=True)
        if ext:
            metric, delta = ext
    seasonal = p.get("is_expected_seasonal") or v.kind == "seasonal_perf_dip"
    window = p.get("window", "7d").replace("d", " days") if isinstance(p.get("window"), str) else "7 days"
    head = f"{sal}, your {metric} are down {pct(delta)} over the last {window}" if metric and delta is not None else f"{sal}, your numbers dipped this week"
    if metric and delta is not None:
        v.fact(f"{metric} {pct(delta, True)} over {window}")
    if p.get("vs_baseline"):
        head += f" (baseline was {num(p['vs_baseline'])})"
        v.fact(f"baseline {p['vs_baseline']}")

    if seasonal:
        beat = v.seasonal_beat()
        item = v.digest_item(kinds=("seasonal",))
        reason = ""
        if beat:
            reason = f"This is the usual {beat['month_range']} pattern — {beat['note']}."
            v.fact(f"seasonal beat {beat['month_range']}: {beat['note']}")
        elif item:
            reason = first_sentence(item.get("summary", ""))
        members = v.agg.get("total_active_members")
        keep = f" The better play right now is keeping your {num(members)} active members engaged." if members else ""
        if members:
            v.fact(f"active members {members}")
        body = (f"{head} — before you worry: {reason or 'this window is seasonally slow for ' + v.slug + '.'}"
                f"{keep} "
                + yes_cta(v, "draft a 4-week attendance challenge to hold retention through the dip" if v.slug == "gyms"
                          else "draft a retention message for your regulars",
                          "Regulars ke liye ek retention plan draft kar doon"))
        return body, "binary_yes_no", "Dip flagged as expected-seasonal: reframe to prevent panic spend, redirect effort to retention using the merchant's own member base.", [sal, head, reason]

    fix = signal_fix(v)
    gap = perf_gap(v)
    sub = v.merchant.get("subscription") or {}
    if not (metric and delta is not None):
        head = f"{sal}, flagging a slowdown on {possessive(v.biz)} listing"
    parts = [head + "."]
    if sub.get("status") == "expired" and sub.get("days_since_expiry"):
        v.fact(f"subscription expired {sub['days_since_expiry']} days ago")
        parts.append(f"Your magicpin plan lapsed {sub['days_since_expiry']} days ago, so profile upkeep has been paused since then.")
    if gap:
        parts.append(f"Also, {gap}.")
    if fix:
        parts.append(f"One likely cause: {fix[0]}.")
        parts.append(yes_cta(v, fix[1], None))
    else:
        parts.append(yes_cta(v, "run a quick profile audit and send the top 3 fixes", "Quick profile audit karke top 3 fixes bhej doon"))
    return " ".join(parts), "binary_yes_no", "Internal perf dip with the actual metric/delta; paired with a concrete, merchant-specific cause from signals and a do-it-for-you fix (loss aversion + effort externalisation).", [sal, head, fix[1] if fix else "profile audit"]


def c_perf_spike(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    metric, delta = p.get("metric"), p.get("delta_pct")
    if metric is None or delta is None:
        ext = delta_extreme(v, negative=False)
        if ext:
            metric, delta = ext
    if metric and delta is not None:
        v.fact(f"{metric} {pct(delta, True)}")
    driver = p.get("likely_driver")
    head = f"{sal}, nice week — {metric} up {pct(delta)} over the last 7 days" if metric else f"{sal}, nice week on your profile"
    if p.get("vs_baseline"):
        head += f" (vs a baseline of {num(p['vs_baseline'])})"
    driver_s = f" Looks like the {humanize(driver)} is what's working." if driver else ""
    if driver:
        v.fact(f"likely driver {driver}")
    strength = perf_strength(v)
    extra = f" And {strength}." if strength else ""
    body = (f"{head}.{driver_s}{extra} Momentum like this fades if nothing new goes up in the next few days. "
            + yes_cta(v, f"draft a follow-up post that doubles down on {humanize(driver) if driver else 'what is working'}",
                      "Isi momentum pe ek follow-up post draft kar doon"))
    return body, "binary_yes_no", "Positive spike: name the number and likely driver, then convert momentum into one more post before it fades.", [sal, head, driver or ""]


def c_milestone(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    metric = humanize(p.get("metric", "")) if p.get("metric") else None
    now_v, target = p.get("value_now"), p.get("milestone_value")
    if metric and now_v is not None and target is not None and now_v < target:
        gap = target - now_v
        v.fact(f"{metric} {now_v} -> {target}")
        head = f"{sal}, you're {gap} {metric.replace(' count', '')}s away from {num(target)} ({num(now_v)} today)"
        why = f" Crossing {num(target)} is a visible trust signal on Google."
        pr = pos_review_theme(v)
        if pr:
            why += f" Your recent reviews keep praising {humanize(pr['theme'])} ({pr.get('occurrences_30d')} mentions in 30 days)."
            v.fact(f"pos review theme {pr['theme']} x{pr.get('occurrences_30d')}")
        body = (head + "." + why + " "
                + yes_cta(v, f"draft a short review-request WhatsApp for this week's happy {v.noun[1]}",
                          f"Is hafte ke happy {v.noun[1]} ke liye review-request message draft kar doon"))
        return body, "binary_yes_no", "Imminent milestone: gap-to-goal creates a small, achievable push; review ask is low effort.", [sal, head, "review request"]
    if metric and now_v is not None:
        v.fact(f"{metric} {now_v}")
        head = f"{sal}, milestone unlocked — {metric} just hit {num(now_v)}"
    else:
        head = None
        for key, label in (("views", "profile views"), ("directions", "direction requests"), ("calls", "calls")):
            val = v.perf.get(key)
            if isinstance(val, (int, float)) and val >= 50:
                marks = [m for m in (50, 100, 250, 500, 750, 1000, 2000, 2500, 5000, 7500, 10000, 20000, 50000) if m <= val]
                v.fact(f"{key} {val}")
                head = f"{sal}, milestone: {v.biz} crossed {num(marks[-1])} {label} in the last 30 days ({num(val)} so far)"
                break
        if not head:
            head = f"{sal}, you've hit a new milestone on your profile"
    body = (head + ". Worth telling your customers — milestone posts get shared more than offers. "
            + yes_cta(v, "draft a thank-you post for Google + WhatsApp", "Ek thank-you post draft kar doon"))
    return body, "binary_yes_no", "Milestone reached: celebrate with the real number and turn it into social proof content.", [sal, head, "thank-you post"]


def c_renewal(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    sub = v.merchant.get("subscription") or {}
    if sub.get("status") == "expired" and "days_remaining" not in p:
        return c_winback_merchant(v)
    days = p.get("days_remaining", sub.get("days_remaining"))
    plan = p.get("plan", sub.get("plan"))
    amt = p.get("renewal_amount")
    perf = v.perf
    if days is None:
        parts = [f"{sal}, your plan is up for renewal"]
    elif days <= 0:
        parts = [f"{sal}, your {plan + ' ' if plan else ''}plan expires today"]
    elif days > 45:
        parts = [f"{sal}, quick plan check-in — {days} days left on your {plan + ' ' if plan else ''}plan"]
    else:
        parts = [f"{sal}, your {plan + ' ' if plan else ''}plan renews in {days} day{'s' if days != 1 else ''}"]
    if amt:
        parts[0] += f" ({rupees(amt)})"
    parts[0] += "."
    v.fact(f"subscription days_remaining {days} plan {plan} amount {amt}")
    got = []
    for k, lab in (("views", "profile views"), ("calls", "calls"), ("directions", "direction requests"), ("leads", "leads")):
        if isinstance(perf.get(k), (int, float)) and perf[k]:
            got.append(f"{num(perf[k])} {lab}")
    if got:
        parts.append(f"In the last 30 days the listing brought you {', '.join(got[:3])}.")
        v.fact("perf 30d " + ", ".join(got[:3]))
    fix = signal_fix(v)
    if days is not None and days > 45:
        if fix:
            parts.append(f"One thing holding it back: {fix[0]}.")
            parts.append(yes_cta(v, fix[1]))
        else:
            parts.append(yes_cta(v, "send a 3-point plan to push those numbers further this month",
                                 "Is mahine numbers aur badhane ka 3-point plan bhej doon"))
        return (" ".join(parts), "binary_yes_no",
                "Renewal far off: value recap from the merchant's own 30-day numbers plus one concrete improvement, no premature hard sell.",
                [sal, str(days), ""])
    if fix:
        parts.append(f"If it lapses, that stops — and {fix[0]}, which I'd fix right after renewal.")
    else:
        parts.append("If it lapses, profile upkeep pauses and that flow slows.")
    parts.append(yes_cta(v, "send the renewal link now", "Renewal link abhi bhej doon"))
    return " ".join(parts), "binary_yes_no", "Renewal due: loss aversion anchored on the merchant's own 30-day results; single yes to get the link.", [sal, str(days), str(amt or "")]


def c_winback_merchant(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    sub = v.merchant.get("subscription") or {}
    d = p.get("days_since_expiry", sub.get("days_since_expiry"))
    parts = [f"{sal}, it's been {d} days since your magicpin plan lapsed" if d else f"{sal}, your magicpin plan is currently inactive"]
    v.fact(f"days_since_expiry {d}")
    if p.get("perf_dip_pct") is not None:
        parts.append(f"— calls are down {pct(p['perf_dip_pct'])} since then")
        v.fact(f"perf_dip_pct {p['perf_dip_pct']}")
    s = " ".join(parts) + "."
    if p.get("lapsed_customers_added_since_expiry"):
        s += f" {p['lapsed_customers_added_since_expiry']} of your customers have also gone quiet in that time — nobody's nudging them back."
        v.fact(f"lapsed customers added {p['lapsed_customers_added_since_expiry']}")
    if p.get("lapsed_customers_added_since_expiry"):
        s += " " + yes_cta(v, "reactivate and send those customers a win-back offer this week",
                           "Plan reactivate karke un customers ko win-back offer bhej doon")
    else:
        gap = perf_gap(v)
        if gap:
            s += f" Meanwhile {gap}."
        s += " " + yes_cta(v, "reactivate the plan and restart your profile upkeep this week",
                           "Plan reactivate karke is hafte profile upkeep dobara shuru kar doon")
    return s, "binary_yes_no", "Lapsed subscriber: quantify what the lapse is costing (from trigger payload) and offer a single restart action.", [sal, str(d), ""]


def c_dormant(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    days = p.get("days_since_last_merchant_message")
    if days is None:
        last = v.last_merchant_message()
        if last and parse_dt(last.get("ts")):
            days = days_between(parse_dt(last["ts"]), v.now)
    hook = None
    trend = v.top_trend()
    item = v.digest_item(kinds=("trend",))
    if item:
        hook = f"{item['title']} ({item.get('source', '')})"
        v.fact(f"digest trend {item['id']}")
    elif trend:
        hook = f"\"{trend['query']}\" searches are up {pct(trend['delta_yoy'])} YoY"
        v.fact(f"trend {trend['query']} {trend['delta_yoy']}")
    opener = f"{sal}, quick one" + (f" after {days} days" if days else "")
    if days:
        v.fact(f"dormant {days} days")
    body = f"{opener} — {hook}." if hook else f"{opener}."
    gap = perf_gap(v)
    if gap:
        body += f" On your side, {gap}."
    body += " " + yes_cta(v, "send a 3-point plan to use this for your listing", "Aapki listing ke liye 3-point plan bhej doon")
    return body, "binary_yes_no", "Dormant merchant: re-open with a curiosity hook from the category digest rather than a reminder, plus one merchant-specific number.", [sal, hook or "", ""]


def c_curious(v: View) -> Result:
    sal = v.salutation()
    trend = v.top_trend()
    guess = ""
    if trend:
        guess = f" My guess is {trend['query'].replace(' near me', '')} — searches are up {pct(trend['delta_yoy'])} YoY."
        v.fact(f"trend {trend['query']} {trend['delta_yoy']}")
    what = {"restaurants": "dish", "pharmacies": "product"}.get(v.slug, "service")
    v.next_step = f"a Google post plus a ready price-enquiry reply for your top {what}"
    q = (f"{sal}, quick question for {v.biz}: which {what} are people asking about most this week?{guess} "
         f"Tell me in one line and I'll turn it into a Google post plus a ready reply for price enquiries — takes 5 minutes.")
    if v.merchant_hindi:
        q += " Bas ek line mein bata dijiye."
    return clean(q), "open_ended", "Curious-ask cadence: low-stakes question (asking-the-merchant lever) with a data-backed guess and upfront reciprocity.", [sal, what, guess.strip()]


def c_festival(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    fest = p.get("festival")
    date_s = p.get("date")
    days = p.get("days_until")
    if days is None and date_s and parse_dt(date_s):
        days = days_between(v.now, parse_dt(date_s))
    beat = None
    if date_s and parse_dt(date_s):
        beat = v.seasonal_beat(parse_dt(date_s).month)
    elif not fest:
        beat = next_festive_beat(v)
    else:
        beat = v.seasonal_beat()
    offer = v.active_offers()[0] if v.active_offers() else v.catalog_offer()
    if fest:
        v.fact(f"festival {fest} {date_s} days_until {days}")
        head = f"{sal}, {fest} is on {nice_date(date_s, False)}" if date_s else f"{sal}, {fest} is coming up"
        if isinstance(days, int) and days > 0:
            head += f" — {days} days out"
    elif beat:
        head = f"{sal}, planning ahead for the {beat['month_range']} window"
    else:
        head = f"{sal}, festive season is coming up"
    beat_s = ""
    if beat:
        beat_s = f" For {v.slug}, {beat['month_range']} is {beat['note']}."
        v.fact(f"seasonal beat {beat['month_range']}: {beat['note']}")
    early = (" Early listings get picked up by Google before the rush."
             if (isinstance(days, int) and days > 30) or (not fest and beat) else "")
    offer_s = f" Your \"{offer}\" is a good anchor for a festive package." if offer else ""
    if offer:
        v.fact(f"offer {offer}")
    body = (f"{head}.{beat_s}{early}{offer_s} "
            + yes_cta(v, f"draft a {fest or 'festive'} package + Google post for you to approve",
                      f"{fest or 'Festive'} package + Google post draft kar doon"))
    return clean(body), "binary_yes_no", "Festival trigger: date + category seasonal beat + merchant's own offer as the anchor for a pre-built campaign.", [sal, fest or "", offer or ""]


FESTIVE_WORDS = ("festival", "wedding", "diwali", "holiday", "christmas", "new year", "valentine", "holi")


def next_festive_beat(v: View) -> dict | None:
    """Nearest upcoming seasonal beat that is actually festive (used when the trigger names no festival)."""
    beats = v.category.get("seasonal_beats") or []
    for ahead in range(0, 12):
        month = (v.now.month - 1 + ahead) % 12 + 1
        for b in beats:
            if any(w in str(b.get("note", "")).lower() for w in FESTIVE_WORDS) and month_in_range(month, str(b.get("month_range", ""))):
                return b
    return None


DAY_IDX = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}


def offer_valid_on(offer: str, dt: datetime | None) -> bool:
    """'Buy 1 Pizza Get 1 Free (Tue-Thu)' is not valid on a Sunday."""
    if not dt:
        return True
    m = re.search(r"\((mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*-\s*(mon|tue|wed|thu|fri|sat|sun)[a-z]*\)", offer.lower())
    if not m:
        return True
    a, b, d = DAY_IDX[m.group(1)], DAY_IDX[m.group(2)], dt.weekday()
    return a <= d <= b if a <= b else (d >= a or d <= b)


def c_ipl(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    match, venue, t = p.get("match"), p.get("venue"), p.get("match_time_iso")
    dt = parse_dt(t)
    weeknight = p.get("is_weeknight")
    if weeknight is None and dt:
        weeknight = dt.weekday() < 4
    v.fact(f"match {match} at {venue} {t} weeknight={weeknight}")
    item = v.digest_item(kinds=("seasonal",))
    ipl_item = None
    for d in v.category.get("digest") or []:
        if "ipl" in (d.get("title", "") + d.get("id", "")).lower():
            ipl_item = d
    when = f"{DAYNAME(dt)} {nice_time(t)}" if dt else "today"
    head = f"{sal}, {match} at {venue} — {when}." if venue else f"{sal}, {match} tonight ({when})."
    offer = v.offer_like(["combo", "pizza", "match", "buy 1"]) or (v.active_offers()[0] if v.active_offers() else None)
    offer_ok = offer_valid_on(offer, dt) if offer else False
    if ipl_item and not weeknight:
        v.fact(f"digest {ipl_item['id']}: {ipl_item['summary'][:80]}")
        advice = f" Heads-up: weekend matches pull people home — {first_sentence(ipl_item['summary'])} So I'd skip a dine-in match promo tonight"
        if offer and offer_ok:
            advice += f" and push \"{offer}\" as a delivery special instead."
        elif offer:
            advice += (f". Your \"{offer}\" doesn't run on a {DAYNAME(dt)}, so tonight I'd go delivery-first with a"
                       f" watch-at-home combo, and save the match-night push for the weekday games.")
        else:
            advice += " and lean on delivery instead."
        ask = yes_cta(v, "set up the delivery banner + a WhatsApp blast to your regulars", "Delivery banner + regulars ko WhatsApp blast set kar doon")
    else:
        if ipl_item:
            v.fact(f"digest {ipl_item['id']}")
            advice = f" Weeknight matches have been good for covers ({first_sentence(ipl_item['summary'])})"
        else:
            advice = " Match nights are a strong window for group orders."
        advice += f" Your \"{offer}\" fits this perfectly." if offer and offer_ok else ""
        ask = yes_cta(v, "put up a match-night post + story before 5pm", "5 baje se pehle match-night post + story daal doon")
    body = head + advice + " " + ask
    if offer:
        v.fact(f"offer {offer}")
    return clean(body), "binary_yes_no", "IPL trigger interpreted with category data (weekday vs weekend covers) — contrarian when the data says so — and routed to the merchant's existing offer.", [sal, str(match), offer or ""]


def DAYNAME(dt: datetime | None) -> str:
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()] if dt else ""


def c_review_theme(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    theme = p.get("theme")
    occ = p.get("occurrences_30d")
    quote = p.get("common_quote")
    if not theme:
        t = neg_review_theme(v)
        if t:
            theme, occ, quote = t.get("theme"), t.get("occurrences_30d"), t.get("common_quote")
    if not theme:
        v.fact("review_theme_emerged trigger without theme details")
        body = (f"{sal}, a new pattern is showing up in {possessive(v.biz)} recent Google reviews. Unanswered reviews shape what "
                f"people see first on your listing. "
                + yes_cta(v, "pull them into a 3-line summary with a suggested reply for each",
                          "Unka 3-line summary + har review ka suggested reply bana doon"))
        return (clean(body), "binary_yes_no",
                "Review-theme trigger with no theme details in context: no invented quotes or counts; offer to summarise and draft replies.",
                [sal, "", ""])
    v.fact(f"review theme {theme} x{occ} quote={quote}")
    trend = f" and {p['trend']}" if p.get("trend") else ""
    head = f"{sal}, {occ} reviews in the last 30 days mention {humanize(theme)}{trend}" if occ else f"{sal}, a review pattern is forming around {humanize(theme)}"
    q = f" — e.g. \"{quote}\"" if quote else ""
    pr = pos_review_theme(v)
    balance = ""
    if pr:
        balance = f" The good news: {pr.get('occurrences_30d')} reviews praise {humanize(pr['theme'])}, so this is fixable, not a reputation problem."
        v.fact(f"pos theme {pr['theme']} x{pr.get('occurrences_30d')}")
    body = (f"{head}{q}.{balance} Unanswered, these start showing up in Google's review summary. "
            + yes_cta(v, "draft polite public replies to each + one line you can add to your profile",
                      "Har review ka polite reply + profile ke liye ek line draft kar doon"))
    return clean(body), "binary_yes_no", "Emerging negative review theme with the real count and quote; balanced with a positive theme; Vera offers to draft the responses.", [sal, humanize(theme), str(occ)]


def c_planning(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    topic = humanize(p.get("intent_topic", "")) or "the plan we discussed"
    last = p.get("merchant_last_message") or (v.last_merchant_message() or {}).get("body")
    v.fact(f"planning topic {topic}; merchant said: {last}")
    lines = [f"{sal}, here's a first draft of the {topic} — edit anything:"]
    # anchor on the merchant's own price if one relates to the topic
    words = [w for w in topic.split() if len(w) > 3]
    anchor = v.offer_like(words) or (v.active_offers()[0] if v.active_offers() else None)
    base = price_in(anchor) if anchor else None
    prior = v.last_vera_message()
    prior_price = price_in(prior.get("body", "")) if prior else None
    if "thali" in topic or "corporate" in topic or "bulk" in topic:
        if base:
            v.fact(f"anchor offer {anchor} price {base}")
            t1, t2, t3 = round(base * 0.9 / 5) * 5, round(base * 0.85 / 5) * 5, round(base * 0.8 / 5) * 5
            lines += [f"• 10–24 meals: {rupees(t1)} each (vs {rupees(base)} retail) + free delivery",
                      f"• 25–49 meals: {rupees(t2)} each",
                      f"• 50+ meals: {rupees(t3)} each",
                      "• Order by 5pm the day before; delivered 12:30–1:30pm"]
        else:
            lines += ["• 3 tiers: 10+, 25+, 50+ meals with a lower per-meal price at each step",
                      "• Order by 5pm the day before; fixed lunch delivery window"]
        lines.append(f"Offices around {v.locality} are the natural first target.")
        ask = yes_cta(v, "draft the WhatsApp you can send to office admins nearby", "Paas ke offices ke admins ke liye WhatsApp draft kar doon")
    elif "kids" in topic or "camp" in topic or "program" in topic:
        price = prior_price or base
        if prior_price:
            v.fact(f"prior Vera proposal price {prior_price}")
        lines += ["• 4 weeks, 3 sessions/week, 45 min each",
                  "• Age bands: 7–9 and 10–12 (small batches)",
                  f"• Fee: {rupees(price)} for the full camp" if price else "• Fee: set per camp, with a sibling discount",
                  "• Free first class so parents can see it"]
        pr = pos_review_theme(v)
        if pr:
            lines.append(f"Lead with what reviews already praise: {humanize(pr['theme'])}.")
        ask = yes_cta(v, "turn this into a Google post + an Instagram carousel", "Isko Google post + Insta carousel bana doon")
    else:
        lines += [f"• What: {topic}" + (f", priced off your \"{anchor}\"" if anchor else ""),
                  f"• Who: your existing {v.noun[1]} first, then new ones via Google",
                  "• Launch: this week, with one post + one WhatsApp"]
        ask = yes_cta(v, "publish the post and draft the WhatsApp", "Post publish karke WhatsApp draft kar doon")
    body = "\n".join(lines) + "\n\n" + ask
    return body, "binary_yes_no", "Merchant already expressed intent — no more qualifying; deliver a concrete draft built on their own prices and move straight to the next action.", [sal, topic, anchor or ""]


def c_unverified(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    path = humanize(p.get("verification_path", "")) if p.get("verification_path") else None
    up = p.get("estimated_uplift_pct")
    v.fact(f"unverified; path={path}; uplift={up}")
    head = f"{sal}, {possessive(v.biz)} Google profile is still unverified"
    why = f" — verified listings typically see about {pct(up)} more visibility" if up else " — unverified listings rank lower and can't post updates"
    how = f". Verification is via {path.replace(' or ', ' or a ')}, and I can walk you through it in about 5 minutes" if path else ". I can walk you through verification in about 5 minutes"
    views = v.perf.get("views")
    extra = f" Your {num(views)} monthly views would count for more once it's done." if isinstance(views, (int, float)) else ""
    body = head + why + how + "." + extra + " " + yes_cta(v, "start the verification now", "Verification abhi shuru karein")
    return clean(body), "binary_yes_no", "Unverified GBP: quantified uplift from the trigger, effort capped at 5 minutes, single yes.", [sal, str(up or ""), path or ""]


def c_competitor(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    name, dist, their = p.get("competitor_name"), p.get("distance_km"), p.get("their_offer")
    opened = p.get("opened_date")
    if name:
        v.fact(f"competitor {name} {dist}km offer {their} opened {opened}")
        head = f"{sal}, {name} opened {dist} km from you" + (f" on {nice_date(opened, False)}" if opened else "")
        if their:
            head += f", leading with \"{their}\""
    else:
        head = f"{sal}, a new {v.noun[2]} listing has come up near {v.locality}"
    mine = v.active_offers()
    pr = pos_review_theme(v)
    angle = ""
    if their and mine:
        tp, mp = price_in(their), price_in(mine[0])
        if tp and mp and tp < mp:
            angle = f" Your \"{mine[0]}\" is {rupees(mp - tp)} higher, so I wouldn't race them on price"
            v.fact(f"my offer {mine[0]}")
        else:
            angle = f" Your \"{mine[0]}\" already holds up on price"
    if pr:
        angle += (" —" if angle else " Better angle:") + f" lean on what {pr.get('occurrences_30d')} reviews this month praise: {humanize(pr['theme'])}."
        v.fact(f"pos theme {pr['theme']}")
    elif angle:
        angle += "."
    body = (head + "." + angle + " "
            + yes_cta(v, "update your Google description + post to highlight that before they build reviews",
                      "Unke reviews badhne se pehle aapka Google description + post update kar doon"))
    return clean(body), "binary_yes_no", "Competitor opened nearby (name/distance/offer from trigger): avoid a price war in a trust category, differentiate on the merchant's own review strengths.", [sal, name or "", their or ""]


def c_category_seasonal(v: View) -> Result:
    p, sal = v.payload, v.salutation()
    trends = p.get("trends") or []
    pretty = []
    for t in trends:
        m = re.match(r"(.+?)_demand_([+-]\d+)", str(t))
        if m:
            pretty.append(f"{humanize(m.group(1))} {m.group(2)}%")
        else:
            pretty.append(humanize(t))
    v.fact(f"seasonal trends {trends}")
    item = v.digest_item(kinds=("seasonal",))
    src = f" ({item['source']})" if item and item.get("source") else ""
    season = humanize(p.get("season", "")) or "this season"
    head = f"{sal}, {season} demand shift is here{src}: " + ", ".join(pretty) + "." if pretty else f"{sal}, the {season} demand shift has started{src}."
    act = f" {item['actionable'].rstrip('.')}." if item and item.get("actionable") else ""
    body = head + act + " " + yes_cta(v, "draft a counter-display checklist + a WhatsApp for your regulars", "Counter display checklist + regulars ke liye WhatsApp draft kar doon")
    return clean(body), "binary_yes_no", "Seasonal category shift with the actual demand deltas; one shelf/outreach action.", [sal, season, ", ".join(pretty)]


def c_generic(v: View) -> Result:
    """Fallback for unseen trigger kinds: say what happened using only payload fields, tie to one merchant number."""
    p, sal = v.payload, v.salutation()
    kind = humanize(v.kind) or "an update"
    details = []
    for k, val in p.items():
        if isinstance(val, (str, int, float)) and not str(k).endswith("_id") and k not in ("category",):
            if isinstance(val, float) and -1 < val < 1 and "pct" in k:
                val = pct(val, True)
            details.append(f"{humanize(k)}: {val}")
    if details:
        v.fact("payload " + "; ".join(details[:4]))
    head = f"{sal}, flagging a {kind} for {v.biz}" + (f" ({'; '.join(details[:3])})" if details else "")
    gap = perf_gap(v)
    fix = signal_fix(v)
    body = head + "."
    if gap:
        body += f" Context: {gap}."
    if fix:
        body += " " + yes_cta(v, fix[1])
    else:
        body += " " + yes_cta(v, "send a short plan for how to use this", "Iska use karne ka short plan bhej doon")
    return clean(body), "binary_yes_no", f"Unrecognised trigger kind '{v.kind}': composed only from payload fields and merchant data to avoid fabrication.", [sal, kind, ""]


# ---------------------------------------------------------------------------
# Customer-facing composers (send_as = merchant_on_behalf)
# ---------------------------------------------------------------------------

def cust_name(v: View) -> str:
    n = str((v.customer or {}).get("identity", {}).get("name") or "").strip()
    m = re.match(r"(.+?)\s*\(parent:\s*(.+?)\)", n)
    if m:
        return m.group(2)
    if n.startswith("("):
        return ""
    return n


def child_name(v: View) -> str | None:
    n = str((v.customer or {}).get("identity", {}).get("name") or "")
    m = re.match(r"(.+?)\s*\(parent:", n)
    return m.group(1).strip() if m else None


def signer(v: View) -> str:
    if v.slug == "dentists" and v.owner:
        return f"{v.salutation()}'s clinic" if "clinic" not in v.biz.lower() else v.biz
    if v.owner:
        return f"{v.owner} from {v.biz}"
    return v.biz


def greet(v: View) -> str:
    name = cust_name(v)
    c = v.customer or {}
    senior = (c.get("identity") or {}).get("senior_citizen")
    if v.customer_hindi:
        if senior or str(name).startswith("Mr."):
            return f"Namaste {name or ''}".strip()
        return f"Hi {name}" if name else "Namaste"
    return f"Hi {name}" if name else "Hello"


def slot_labels(p: dict, key: str = "available_slots") -> list[str]:
    out = []
    for s in p.get(key) or p.get("next_session_options") or []:
        if isinstance(s, dict):
            out.append(s.get("label") or nice_date(s.get("iso")))
        elif isinstance(s, str):
            out.append(s)
    return out


def last_visit_months(v: View) -> int | None:
    rel = (v.customer or {}).get("relationship") or {}
    lv = parse_dt(rel.get("last_visit"))
    d = days_between(lv, v.now)
    if d is None or d < 20:
        return None
    return max(1, round(d / 30))


def pref_slot(v: View) -> str | None:
    s = ((v.customer or {}).get("preferences") or {}).get("preferred_slots")
    if not s:
        return None
    out = humanize(s)
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        out = re.sub(rf"\b{d}\b", d.capitalize(), out)
    return out


def cc_recall(v: View) -> Result:
    p = v.payload
    emoji = CUSTOMER_EMOJI.get(v.slug, "")
    service = humanize(p.get("service_due", "")) if p.get("service_due") else None
    if not service:
        service = {"dentists": "routine check-up", "salons": "next appointment", "gyms": "next session"}.get(v.slug)
    months = last_visit_months(v)
    slots = slot_labels(p)
    offer = v.offer_like(["clean", "check", "consult"] if v.slug == "dentists" else [w for w in (service or "").split()] or ["trial"])
    parts = [f"{greet(v)}, {signer(v)} here {emoji}".strip()]
    if months:
        v.fact(f"last visit ~{months} months ago")
        parts.append(f"It's been about {months} month{'s' if months != 1 else ''} since your last visit" + (f" — your {service} is due." if service else "."))
    elif service:
        parts.append(f"Your {service} is due.")
    if slots:
        v.fact(f"slots {slots}")
        if v.customer_hindi:
            parts.append(f"Aapke liye slots ready hain: {' ya '.join(slots[:2])}.")
        else:
            parts.append(f"We've kept {len(slots[:2])} slot{'s' if len(slots[:2]) > 1 else ''} for you: {' or '.join(slots[:2])}.")
    if offer:
        v.fact(f"offer {offer}")
        parts.append(f"{offer.replace(' @ ', ' at ')} as always.")
    if len(slots) >= 2:
        parts.append("Reply 1 for the first, 2 for the second, or tell us a time that suits you.")
        cta = "multi_choice_slot"
    else:
        pref = pref_slot(v)
        if v.slug == "pharmacies":
            parts.append("Reply YES and we'll keep your regular items ready for pickup or delivery.")
        elif v.slug == "restaurants":
            parts.append("Reply YES and we'll send you this week's specials.")
        else:
            parts.append(f"Reply YES and we'll book a {pref} slot for you." if pref else "Reply YES and we'll share this week's slots.")
        cta = "binary_yes_no"
    return clean(" ".join(parts)), cta, "Customer recall sent on the merchant's behalf; uses only the last-visit gap, slots and live offers actually present in context, in the customer's preferred language.", [cust_name(v), signer(v), ", ".join(slots)]


def cc_appointment(v: View) -> Result:
    p = v.payload
    t = p.get("appointment_iso") or p.get("slot_iso") or p.get("time_iso")
    svc = humanize(p.get("service", "")) if p.get("service") else None
    when = f"tomorrow at {nice_time(t)}" if t and nice_time(t) else "tomorrow"
    v.fact(f"appointment {t or 'tomorrow'} {svc or ''}")
    emoji = CUSTOMER_EMOJI.get(v.slug, "")
    noun = {"restaurants": "table booking", "gyms": "session", "pharmacies": "pharmacist consultation"}.get(v.slug, "appointment")
    body = f"{greet(v)}, {signer(v)} here {emoji} Reminder: your {svc + ' ' if svc else ''}{noun} is {when}."
    if v.customer_hindi:
        body += " Reply YES to confirm, ya RESCHEDULE agar time change karna ho."
    else:
        body += " Reply YES to confirm, or RESCHEDULE if you need another time."
    return clean(body), "binary_confirm_cancel", "Appointment-tomorrow reminder: time from the trigger only, confirm/reschedule in one reply.", [cust_name(v), when, ""]


def cc_lapsed(v: View) -> Result:
    p = v.payload
    days = p.get("days_since_last_visit")
    focus = humanize(p.get("previous_focus", "")) if p.get("previous_focus") else None
    if not focus:
        f = ((v.customer or {}).get("preferences") or {}).get("training_focus") or ((v.customer or {}).get("preferences") or {}).get("health_focus")
        focus = humanize(f) if f else None
    months = last_visit_months(v)
    gap = f"about {round(days / 7)} weeks" if days else (f"about {months} month{'s' if months != 1 else ''}" if months else "a while")
    v.fact(f"lapse gap {gap}; focus {focus}")
    offer = v.offer_like(["trial", "free", "first", "check", "consult"]) or (v.active_offers()[0] if v.active_offers() else None)
    prefs = (v.customer or {}).get("preferences") or {}
    pref = pref_slot(v)
    hi = v.customer_hindi
    parts = [f"{greet(v)} 👋 {signer(v)} here."]
    if v.slug == "gyms":
        parts.append(f"It's been {gap} — happens to everyone, no judgment.")
        if focus:
            parts.append(f"If {focus} is still the goal, we'd love to help you pick it back up.")
    elif v.slug == "dentists":
        parts.append(f"It's been {gap} since your last visit — a quick check-up now keeps small issues small.")
    elif v.slug == "salons":
        parts.append(f"It's been {gap} since your last visit — we've missed you!")
    elif v.slug == "restaurants":
        parts.append(f"It's been {gap} since your last order with us.")
    elif v.slug == "pharmacies":
        parts.append(f"It's been {gap} since your last visit — just checking whether you need any regular items restocked.")
    else:
        parts.append(f"It's been {gap} since we last saw you.")
    if offer:
        v.fact(f"offer {offer}")
        parts.append(f"\"{offer}\" is on right now — no commitment.")
    closers = {
        "dentists": f"Reply YES and we'll book a {pref or 'convenient'} check-up slot this week.",
        "salons": (f"Reply YES and we'll hold a slot with {prefs['preferred_stylist']}." if prefs.get("preferred_stylist")
                   else f"Reply YES and we'll hold a {pref or 'weekend'} slot for you."),
        "restaurants": "Reply YES and we'll send you this week's specials.",
        "pharmacies": ("Reply YES aur hum aapka regular saaman ready rakhenge — pickup ya delivery." if hi
                       else "Reply YES and we'll keep your regular items ready for pickup or delivery."),
        "gyms": f"Reply YES and we'll hold a {pref or 'this-week'} spot for you.",
    }
    parts.append(closers.get(v.slug, "Reply YES and we'll set up a time that works for you."))
    return clean(" ".join(parts)), "binary_yes_no", "Customer win-back on the merchant's behalf: category-appropriate, no-shame tone; references only the lapse gap, past goal and live offer when present; single yes.", [cust_name(v), gap, offer or ""]


def cc_refill(v: View) -> Result:
    p = v.payload
    mols = p.get("molecule_list") or []
    runs_out = p.get("stock_runs_out_iso")
    saved = p.get("delivery_address_saved")
    v.fact(f"refill {mols} runs out {runs_out} saved_address={saved}")
    senior = ((v.customer or {}).get("identity") or {}).get("senior_citizen")
    senior_offer = v.offer_like(["senior"]) if senior else None
    deliv = v.offer_like(["delivery"])
    hindi = v.customer_hindi
    name = cust_name(v)
    if v.slug != "pharmacies" and not mols:
        # refill semantics outside pharmacy: treat as a follow-up due
        return cc_recall(v)
    who = f"{name} ji" if hindi and name else name
    if hindi:
        s = f"Namaste — {v.biz}{', ' + v.locality if v.locality else ''} yahan."
        s += f" {who} ki {len(mols)} medicines ({', '.join(mols)})" if mols else f" {who} ki regular medicines"
        s += f" {nice_date(runs_out, False)} ko khatam hongi." if runs_out else " jaldi khatam hone wali hain."
        s += " Same dose, same pack ready hai."
        if senior_offer:
            s += f" \"{senior_offer}\" apply hoga."
        if saved:
            s += " Saved address pe delivery ho jayegi" + (f" (\"{deliv}\")." if deliv else ".")
        s += " Dispatch ke liye CONFIRM reply karein, ya dose change ho toh bata dijiye."
    else:
        s = f"Hi {name}, {v.biz} here." if name else f"Hello from {v.biz}."
        s += f" Your {len(mols)} regular medicines ({', '.join(mols)})" if mols else " Your regular medicines"
        s += f" run out on {nice_date(runs_out, False)}." if runs_out else " are due for a refill."
        s += " Same dose, same pack is ready."
        if senior_offer:
            s += f" \"{senior_offer}\" applies."
        if saved:
            s += " We can deliver to your saved address" + (f" (\"{deliv}\")." if deliv else ".")
        s += " Reply CONFIRM to dispatch, or tell us if the dose has changed."
    for o in (senior_offer, deliv):
        if o:
            v.fact(f"offer {o}")
    return clean(s), "binary_confirm_cancel", "Chronic refill: exact molecules and run-out date from the trigger, merchant's own senior/delivery offers, confirm-to-dispatch.", [who or "", ", ".join(mols), nice_date(runs_out, False) if runs_out else ""]


def cc_trial_followup(v: View) -> Result:
    p = v.payload
    trial = p.get("trial_date")
    slots = slot_labels(p, "next_session_options")
    child = child_name(v)
    v.fact(f"trial {trial} next {slots}")
    who = f"{child}'s" if child else "your"
    if v.slug in ("gyms", "salons") or trial or slots:
        s = f"{greet(v)}, {signer(v)} here. Thanks for coming in for {who} trial" + (f" on {nice_date(trial, False)}" if trial else "") + "."
    else:
        s = f"{greet(v)}, {signer(v)} here. Thanks for visiting us recently — hope everything went well."
    offer = v.offer_like(["first", "trial", "month"])
    if offer:
        s += f" If you'd like to continue, \"{offer}\" is on right now."
        v.fact(f"offer {offer}")
    if slots:
        s += f" Next session: {slots[0]}. Reply YES to book it."
    elif v.slug in ("gyms", "salons"):
        s += " Reply YES and we'll share the next session time."
    else:
        s += " Reply YES if you'd like us to set anything aside for your next visit."
    return clean(s), "binary_yes_no", "Trial follow-up with the real next session slot; one yes to book.", [cust_name(v), trial or "", slots[0] if slots else ""]


def cc_wedding(v: View) -> Result:
    p = v.payload
    wd = p.get("wedding_date") or ((v.customer or {}).get("preferences") or {}).get("wedding_date")
    days = p.get("days_to_wedding")
    if wd and parse_dt(wd):
        live = days_between(v.now, parse_dt(wd))
        if live is not None and live >= 0:
            days = live
    step = humanize(p.get("next_step_window_open", "")) if p.get("next_step_window_open") else None
    trial = p.get("trial_completed")
    v.fact(f"wedding {wd} days {days} step {step} trial {trial}")
    s = f"Hi {cust_name(v)} 💍 {signer(v)} here."
    if days is not None:
        s += f" {days} days to go till {nice_date(wd, False)}!"
    if trial:
        s += f" Since your bridal trial on {nice_date(trial, False)}, the next step is the {step or 'pre-wedding prep'}."
    elif step:
        s += f" Now's the right window for the {step}."
    pref = pref_slot(v)
    s += f" Shall we block a {pref} slot for the first session? Reply YES." if pref else " Shall we block your first session next week? Reply YES."
    return clean(s), "binary_yes_no", "Bridal follow-up: countdown from the real wedding date, the prep window from the trigger, preferred day honoured.", [cust_name(v), str(days), step or ""]


def cc_generic(v: View) -> Result:
    p = v.payload
    kind = humanize(v.kind)
    offer = v.active_offers()[0] if v.active_offers() else None
    s = f"{greet(v)}, {signer(v)} here."
    months = last_visit_months(v)
    if months:
        s += f" It's been about {months} month{'s' if months != 1 else ''} since we last saw you."
    if offer:
        s += f" \"{offer}\" is on right now."
    s += " Reply YES and we'll set up a time that works for you."
    v.fact(f"customer generic {kind}")
    return clean(s), "binary_yes_no", f"Customer-scope '{v.kind}' without a dedicated composer: grounded in relationship data + live offer only.", [cust_name(v), kind, offer or ""]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

MERCHANT_KINDS: dict[str, Callable[[View], Result]] = {
    "research_digest": c_digest, "research_digest_release": c_digest, "category_research_digest_release": c_digest,
    "regulation_change": c_digest, "compliance_alert": c_digest, "supply_alert": c_digest,
    "cde_opportunity": c_digest, "category_trend_movement": c_digest,
    "perf_dip": c_perf_dip, "seasonal_perf_dip": c_perf_dip,
    "perf_spike": c_perf_spike,
    "milestone_reached": c_milestone,
    "renewal_due": c_renewal,
    "winback_eligible": c_winback_merchant,
    "dormant_with_vera": c_dormant,
    "curious_ask_due": c_curious, "scheduled_recurring": c_curious,
    "festival_upcoming": c_festival,
    "ipl_match_today": c_ipl,
    "review_theme_emerged": c_review_theme,
    "active_planning_intent": c_planning,
    "gbp_unverified": c_unverified,
    "competitor_opened": c_competitor,
    "category_seasonal": c_category_seasonal,
}

CUSTOMER_KINDS: dict[str, Callable[[View], Result]] = {
    "recall_due": cc_recall,
    "appointment_tomorrow": cc_appointment,
    "customer_lapsed_soft": cc_lapsed, "customer_lapsed_hard": cc_lapsed, "winback_eligible": cc_lapsed,
    "chronic_refill_due": cc_refill,
    "trial_followup": cc_trial_followup,
    "wedding_package_followup": cc_wedding, "bridal_followup": cc_wedding,
}

TEMPLATE_PREFIX = {"vera": "vera", "merchant_on_behalf": "merchant"}
DEFAULT_NOW = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)


def compose(category: dict | None, merchant: dict, trigger: dict, customer: dict | None = None,
            now: str | datetime | None = None) -> dict[str, Any]:
    """Pure, deterministic composition. Same inputs (incl. `now`) -> same output."""
    if isinstance(now, str):
        now_dt = parse_dt(now)
    elif isinstance(now, datetime):
        now_dt = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        now_dt = None
    if now_dt is None:
        # deterministic default (never wall-clock): the dataset's reference date
        now_dt = DEFAULT_NOW
    v = View(category=category or {}, merchant=merchant or {}, trigger=trigger or {}, customer=customer, now=now_dt)
    kind = v.kind
    customer_scope = bool(customer) and (trigger.get("scope") == "customer" or trigger.get("customer_id"))
    if customer_scope:
        fn = CUSTOMER_KINDS.get(kind, cc_generic)
        send_as = "merchant_on_behalf"
    else:
        fn = MERCHANT_KINDS.get(kind, c_generic)
        send_as = "vera"
    body, cta, rationale, params = fn(v)
    body = scrub_taboos(body, v.category)
    facts = "; ".join(v.facts[:6])
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}:{(customer or {}).get('customer_id', '')}",
        "rationale": f"{rationale} Facts used: {facts}." if facts else rationale,
        "template_name": f"{TEMPLATE_PREFIX[send_as]}_{kind or 'generic'}_v1",
        "template_params": [str(x) for x in params if x is not None],
        "_next_step": v.next_step,
    }


def scrub_taboos(body: str, category: dict) -> str:
    """Last-line guard: never emit a category taboo phrase."""
    for t in ((category or {}).get("voice") or {}).get("vocab_taboo") or []:
        phrase = re.sub(r"\s*\(.*?\)", "", str(t)).strip()
        if phrase and re.search(re.escape(phrase), body, flags=re.I):
            body = re.sub(re.escape(phrase), "", body, flags=re.I)
    return clean(body)
