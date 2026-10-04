"""
engine.py — Deterministic rules + ranking for the churn agent.

The weekly agent does the fuzzy work (reading the Google Sheet/Doc, checking
issuer sites for current offers). This module does the math the same way
every week: 5/24 counts, issuer application rules, MSR capacity, upcoming
deadlines, and a ranked list of next cards.

Usage:
    python -m churn_agent.engine                       # markdown report, today
    python -m churn_agent.engine --as-of 2026-10-04
    python -m churn_agent.engine --format json
    python -m churn_agent.engine --profile p.yaml --offers o.yaml
"""

from __future__ import annotations

import argparse
import calendar
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).parent
DEFAULT_PROFILE = HERE / "profile.yaml"          # real data: gitignored, pulled from Drive
EXAMPLE_PROFILE = HERE / "profile.example.yaml"
DEFAULT_OFFERS = HERE / "offers.yaml"

# Business cards from these issuers show up on personal reports (and count
# toward Chase 5/24).
REPORTING_BIZ_ISSUERS = {"Capital One", "Discover", "TD"}
# Chase Ink bonuses are once per lifetime, and the no-fee Inks share one
# lifetime (having had Ink Unlimited blocks the Ink Cash bonus and vice versa).
CHASE_LIFETIME_FAMILIES = {
    "ink_preferred": {"ink_preferred"},
    "ink_premier": {"ink_premier"},
    "ink_cash": {"ink_cash", "ink_unlimited"},
    "ink_unlimited": {"ink_cash", "ink_unlimited"},
}
SEARCH_HORIZON_DAYS = 540
DEADLINE_HORIZON_DAYS = 60
WAIT_DISCOUNT_PER_MONTH = 0.97
BEST_WINDOW_MONTHS = 6


# ── DATE HELPERS ──────────────────────────────────────────────────────────────

def to_date(v) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def months_between(a: date, b: date) -> float:
    return (b - a).days / 30.44


# ── DATA MODEL ────────────────────────────────────────────────────────────────

@dataclass
class Card:
    person: str
    issuer: str
    product: str
    type: str
    family: str = ""
    tier: int = 1
    opened: date | None = None
    applied: date | None = None
    bonus_date: date | None = None
    status: str = "open"
    counts_5_24: bool | None = None
    authorized_user: bool = False
    simulated: bool = False

    @property
    def app_date(self) -> date | None:
        if self.authorized_user:
            return None
        return self.opened or self.applied

    @property
    def effective_bonus_date(self) -> date | None:
        if self.bonus_date:
            return self.bonus_date
        if self.opened and self.status != "denied":
            return self.opened + timedelta(days=90)
        return None

    def counts_toward_5_24(self) -> bool:
        if self.counts_5_24 is not None:
            return self.counts_5_24
        if not self.opened:
            return False
        return self.type == "personal" or self.issuer in REPORTING_BIZ_ISSUERS


def load_cards(profile: dict) -> list[Card]:
    cards = []
    for c in profile.get("cards", []):
        cards.append(Card(
            person=c["person"], issuer=c["issuer"], product=c["product"],
            type=c.get("type", "personal"), family=c.get("family", ""),
            tier=c.get("tier", 1), opened=to_date(c.get("opened")),
            applied=to_date(c.get("applied")),
            bonus_date=to_date(c.get("bonus_date")),
            status=c.get("status", "open"), counts_5_24=c.get("counts_5_24"),
            authorized_user=c.get("authorized_user", False),
        ))
    return cards


def offer_as_card(offer: dict, person: str, on: date) -> Card:
    return Card(
        person=person, issuer=offer["issuer"], product=offer["product"],
        type=offer.get("type", "personal"), family=offer.get("family", ""),
        tier=offer.get("tier", 1), opened=on, status="open",
        counts_5_24=(True if offer.get("type", "personal") == "personal"
                     else offer.get("reports_personal")),
        simulated=True,
    )


# ── 5/24 ──────────────────────────────────────────────────────────────────────

def five24_cards(cards: list[Card], person: str, on: date) -> list[Card]:
    """Cards counting toward 5/24 for `person` on date `on`.

    A card stops counting the day after its 24-month anniversary.
    """
    out = []
    for c in cards:
        if c.person != person or not c.counts_toward_5_24():
            continue
        if c.opened <= on < add_months(c.opened, 24) + timedelta(days=1):
            out.append(c)
    return out


def five24_count(cards, person, on) -> int:
    return len(five24_cards(cards, person, on))


def five24_timeline(cards, person, on) -> list[tuple[date, int]]:
    """Upcoming (date, new_count) points where the count drops."""
    points = []
    for c in sorted(five24_cards(cards, person, on), key=lambda c: c.opened):
        drop = add_months(c.opened, 24) + timedelta(days=1)
        points.append((drop, five24_count(cards, person, drop)))
    return points


# ── ISSUER RULES ──────────────────────────────────────────────────────────────
# hard_rules() returns the reasons an application would be blocked (empty = OK).
# soft_risk() returns a (multiplier, notes) approval-risk adjustment instead.

def _person_cards(cards, person):
    return [c for c in cards if c.person == person and c.app_date]


def _issuer_apps_within(cards, person, issuer, on, days) -> list[Card]:
    lo = on - timedelta(days=days)
    return [c for c in _person_cards(cards, person)
            if c.issuer == issuer and lo < c.app_date <= on]


def _family_bonus_within(cards, person, family, on, months) -> Card | None:
    for c in _person_cards(cards, person):
        b = c.effective_bonus_date
        if c.family == family and b and b <= on and on < add_months(b, months):
            return c
    return None


def hard_rules(offer: dict, person: str, cards: list[Card], on: date) -> list[str]:
    issuer, fam = offer["issuer"], offer.get("family", "")
    reasons = []

    if issuer == "Chase":
        n = five24_count(cards, person, on)
        if n >= 5:
            reasons.append(f"Chase 5/24: at {n}/24")
        if fam in CHASE_LIFETIME_FAMILIES:
            had = [c for c in _person_cards(cards, person) if c.issuer == "Chase"
                   and c.family in CHASE_LIFETIME_FAMILIES[fam] and c.status != "denied"]
            if had:
                reasons.append(f"Chase Ink lifetime rule: already had {had[0].product}")
        else:
            lookback = 48 if fam == "sapphire" else 24
            hit = _family_bonus_within(cards, person, fam, on, lookback)
            if hit:
                reasons.append(f"Chase: {fam} bonus within {lookback} mo ({hit.product})")
        recent = _issuer_apps_within(cards, person, "Chase", on, 30)
        if offer.get("type") == "business" and any(c.type == "business" for c in recent):
            reasons.append("Chase: business card opened in last 30 days")
        if len(recent) >= 2:
            reasons.append("Chase: 2 apps in last 30 days")

    elif issuer == "Amex":
        tier = offer.get("tier", 1)
        for c in _person_cards(cards, person):
            if c.issuer == "Amex" and c.family == fam and c.tier >= tier and c.status != "denied":
                reasons.append(f"Amex lifetime/family rule: already had {c.product}")
                break
        if len(_issuer_apps_within(cards, person, "Amex", on, 90)) >= 2:
            reasons.append("Amex: 2 cards in last 90 days")
        if _issuer_apps_within(cards, person, "Amex", on, 5):
            reasons.append("Amex: card in last 5 days")

    elif issuer == "Citi":
        if _issuer_apps_within(cards, person, "Citi", on, 8):
            reasons.append("Citi 1/8: Citi card in last 8 days")
        if len(_issuer_apps_within(cards, person, "Citi", on, 65)) >= 2:
            reasons.append("Citi 2/65: two Citi cards in last 65 days")
        if offer.get("type") == "business" and any(
                c.type == "business" for c in _issuer_apps_within(cards, person, "Citi", on, 95)):
            reasons.append("Citi: 1 business card per 95 days")
        hit = _family_bonus_within(cards, person, fam, on, 48)
        if hit:
            reasons.append(f"Citi 48-month rule: {hit.product} bonus")

    elif issuer == "Capital One":
        if _issuer_apps_within(cards, person, "Capital One", on, 182):
            reasons.append("Capital One: 1 card per 6 months")

    elif issuer == "BofA":
        apps = lambda d: len(_issuer_apps_within(cards, person, "BofA", on, d))
        if apps(61) >= 2 or apps(365) >= 3 or apps(730) >= 4:
            reasons.append("BofA 2/3/4 rule")
        all12 = [c for c in _person_cards(cards, person)
                 if on - timedelta(days=365) < c.app_date <= on]
        if len(all12) >= 7:
            reasons.append("BofA 7/12: 7+ new accounts in 12 months")

    elif issuer == "Barclays":
        if _issuer_apps_within(cards, person, "Barclays", on, 182):
            reasons.append("Barclays: wait 6 months between Barclays apps")

    # Generic: same family bonus in last 24 months, any issuer we don't model.
    if issuer not in {"Chase", "Amex", "Citi"} and fam:
        hit = _family_bonus_within(cards, person, fam, on, 24)
        if hit:
            reasons.append(f"{issuer}: {fam} bonus within 24 mo")
    return reasons


def soft_risk(offer: dict, person: str, cards: list[Card], on: date,
              profile: dict) -> tuple[float, list[str]]:
    mult, notes = 1.0, []
    issuer = offer["issuer"]
    last12 = [c for c in _person_cards(cards, person)
              if on - timedelta(days=365) < c.app_date <= on]

    if issuer == "Chase":
        chase12 = [c for c in last12 if c.issuer == "Chase"]
        if len(chase12) >= 3:
            mult *= 0.8
            notes.append(f"{len(chase12)} Chase accounts in 12 mo — velocity risk")
    if issuer == "Citi" and _issuer_apps_within(cards, person, "Citi", on, 65):
        mult *= 0.9
        notes.append("Citi app in last 65 days — waiting until 65 days have passed is safer")
    if issuer == "US Bank":
        declared = profile.get("people", {}).get(person, {}).get("hard_inquiries_12mo")
        simulated = len([c for c in last12 if c.simulated])
        inq = declared + simulated if declared is not None else len(last12)
        if inq >= 4:
            mult *= 0.75
            notes.append(f"~{inq} inquiries in 12 mo — US Bank is inquiry-sensitive")
    if issuer == "Barclays":
        n24 = len(five24_cards(cards, person, on))
        if n24 >= 6:
            mult *= 0.7
            notes.append(f"{n24} new accounts in 24 mo — Barclays 6/24 sensitivity")
    blockers = profile.get("people", {}).get(person, {}).get("blockers") or []
    if blockers:
        mult *= 0.85
        notes.extend(f"Blocker: {b}" for b in blockers)
    return mult, notes


# ── SPEND CAPACITY ────────────────────────────────────────────────────────────

def committed_msr(profile: dict, on: date) -> tuple[float, list[str]]:
    """Monthly spend already committed to open MSRs as of `on`."""
    total, lines = 0.0, []
    for c in profile.get("cards", []):
        msr = c.get("msr")
        if not msr:
            continue
        deadline = to_date(msr["deadline"])
        if deadline <= on:
            continue
        remaining = msr["amount"] - (msr.get("spent") or 0)
        if remaining <= 0:
            continue
        months = max(months_between(on, deadline), 0.5)
        total += remaining / months
        lines.append(f"{c['person'].title()} {c['product']}: ${remaining:,.0f} left by {deadline}")
    return total, lines


@dataclass
class Ranked:
    offer_id: str
    person: str
    product: str
    issuer: str
    earliest: date | None
    net_value: float
    score: float
    feasibility: float
    plastiq_fee: float
    blocked_now: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    verified: str | None = None
    best_date: date | None = None


def value_offer(offer: dict, profile: dict) -> float:
    cpp = profile["valuations_cpp"].get(offer.get("currency", "cash"), 1.0)
    pts_value = offer.get("bonus_points", 0) * cpp / 100
    return (pts_value + offer.get("bonus_cash", 0) + offer.get("extra_value", 0)
            - offer.get("af_first_year", 0))


def msr_feasibility(offer: dict, profile: dict, on: date) -> tuple[float, float, str]:
    """Return (feasibility 0-1, plastiq_fee, note)."""
    spend = profile["spend"]
    committed, _ = committed_msr(profile, on)
    free_regular = max(spend["regular_monthly"] - committed, 0)
    need = offer.get("msr", 0) / max(offer.get("msr_months", 3), 1)
    if need <= free_regular:
        return 1.0, 0.0, f"needs ${need:,.0f}/mo of ${free_regular:,.0f}/mo free organic spend"

    plastiq_ok = offer.get("network") in spend.get("plastiq_networks", [])
    shortfall = need - free_regular
    if plastiq_ok and shortfall <= spend["mortgage_monthly"]:
        months = offer.get("msr_months", 3)
        fee = shortfall * months * spend["plastiq_fee_pct"] / 100 \
            + months * spend.get("plastiq_fee_per_payment", 0)
        conf = 1.0 if spend.get("plastiq_validated") else 0.9
        return conf, fee, (f"needs ${need:,.0f}/mo; ${shortfall:,.0f}/mo via Plastiq mortgage "
                           f"(~${fee:,.0f} fees{'' if spend.get('plastiq_validated') else ', Plastiq not yet validated'})")
    ratio = (free_regular + (spend["mortgage_monthly"] if plastiq_ok else 0)) / need
    return max(min(ratio, 1.0), 0.0) ** 2, 0.0, (
        f"needs ${need:,.0f}/mo but only ~${free_regular:,.0f}/mo free — tight")


def goal_multiplier(offer: dict, profile: dict) -> tuple[float, list[str]]:
    cur = offer.get("currency")
    hits = [g["where"] for g in profile.get("travel_goals", []) if cur in g.get("programs", [])]
    return (1.0 + min(0.05 * len(hits), 0.15)), hits


def earliest_eligible(offer, person, cards, start: date) -> tuple[date | None, list[str]]:
    now_reasons = hard_rules(offer, person, cards, start)
    if not now_reasons:
        return start, []
    for i in range(1, SEARCH_HORIZON_DAYS):
        d = start + timedelta(days=i)
        if not hard_rules(offer, person, cards, d):
            return d, now_reasons
    return None, now_reasons


def _score_at(o, person, cards, profile, on, when) -> dict:
    feas, fee, feas_note = msr_feasibility(o, profile, when)
    risk, risk_notes = soft_risk(o, person, cards, when, profile)
    goal, goal_hits = goal_multiplier(o, profile)
    net = value_offer(o, profile) - fee
    slot_note = None
    if offer_as_card(o, person, when).counts_toward_5_24():
        n = five24_count(cards, person, when)
        net -= profile.get("five24_slot_cost", 0)
        slot_note = f"uses a 5/24 slot ({n}→{n + 1}/24)"
    wait_months = max(months_between(on, when), 0)
    score = net * feas * risk * goal * WAIT_DISCOUNT_PER_MONTH ** wait_months
    notes = [feas_note] + ([slot_note] if slot_note else []) \
        + ([f"fits: {', '.join(goal_hits)}"] if goal_hits else [])
    return {"when": when, "feas": feas, "fee": fee, "risks": risk_notes,
            "net": net, "score": score, "notes": notes}


def rank_offers(profile: dict, offers: list[dict], on: date,
                cards: list[Card] | None = None, *,
                apply_card_preferences: bool = True) -> list[Ranked]:
    cards = cards if cards is not None else load_cards(profile)
    people = list(profile.get("people", {}).keys())
    out = []
    for o in offers:
        for person in o.get("for", people):
            r = Ranked(offer_id=o["id"], person=person, product=o["product"],
                       issuer=o["issuer"], earliest=None, net_value=0, score=0,
                       feasibility=0, plastiq_fee=0, verified=str(o.get("last_verified") or "") or None)
            if apply_card_preferences and o.get("reports_personal") and o.get("type") == "business":
                r.blocked_now = ["Excluded by preference: business card reports to personal credit"]
                out.append(r)
                continue
            allowed = profile.get("card_types")
            if apply_card_preferences and allowed and o.get("type", "personal") not in allowed:
                r.blocked_now = [f"Excluded by preference: {o.get('type', 'personal')} card (tracking {', '.join(allowed)} only)"]
                out.append(r)
                continue
            cap = profile.get("spend", {}).get("max_msr_per_month")
            need = o.get("msr", 0) / max(o.get("msr_months", 3), 1)
            if cap and need > cap:
                r.blocked_now = [f"Excluded by preference: MSR needs ${need:,.0f}/mo, above your ${cap:,.0f}/mo limit"]
                out.append(r)
                continue
            r.earliest, r.blocked_now = earliest_eligible(o, person, cards, on)
            if not r.earliest:
                out.append(r)
                continue
            # Try the earliest date and each month after it; keep the best window
            # (e.g. a big MSR scores better once current MSRs are finished).
            windows = [r.earliest] + [add_months(r.earliest, k) for k in range(1, BEST_WINDOW_MONTHS + 1)]
            best = None
            for when in windows:
                if hard_rules(o, person, cards, when):
                    continue
                cand = _score_at(o, person, cards, profile, on, when)
                if best is None or cand["score"] > best["score"] * 1.02:
                    best = cand
            r.best_date = best["when"]
            r.feasibility, r.plastiq_fee, r.risks = best["feas"], best["fee"], best["risks"]
            r.net_value, r.score, r.notes = best["net"], best["score"], best["notes"]
            out.append(r)
    out.sort(key=lambda r: (-r.score, r.earliest or date.max))
    return out


# ── PIPELINE SIMULATION ───────────────────────────────────────────────────────

def check_pipeline(profile: dict, offers_by_id: dict, cards: list[Card]) -> list[dict]:
    """Walk the planned applications in date order, adding each to history,
    and report any that would be blocked by an earlier planned card."""
    sim = list(cards)
    results = []
    for p in sorted(profile.get("pipeline", []), key=lambda p: to_date(p["target_date"])):
        o = offers_by_id.get(p["offer"])
        d = to_date(p["target_date"])
        if not o:
            results.append({**p, "ok": False, "reasons": ["offer not in offers.yaml"]})
            continue
        reasons = hard_rules(o, p["person"], sim, d)
        earliest, _ = earliest_eligible(o, p["person"], sim, d) if reasons else (d, [])
        results.append({"offer": p["offer"], "product": o["product"], "person": p["person"],
                        "target_date": d, "ok": not reasons, "reasons": reasons,
                        "earliest_ok": earliest, "five24_at_target": five24_count(sim, p["person"], d)})
        sim.append(offer_as_card(o, p["person"], d if not reasons else (earliest or d)))
    return results


# ── DEADLINES ─────────────────────────────────────────────────────────────────

def upcoming_deadlines(profile: dict, cards: list[Card], on: date,
                       horizon: int = DEADLINE_HORIZON_DAYS) -> list[tuple[date, str]]:
    end = on + timedelta(days=horizon)
    items = []
    for c in profile.get("cards", []):
        who = c["person"].title()
        if c.get("action_by") and c.get("status") == "open":
            items.append((to_date(c["action_by"]), f"{who} — {c['product']}: {c.get('action', 'review')}"))
        if c.get("msr"):
            m = c["msr"]
            spent = m.get("spent")
            left = f"${m['amount'] - spent:,.0f} left" if spent is not None else "spend progress not logged"
            items.append((to_date(m["deadline"]), f"{who} — {c['product']} MSR ${m['amount']:,} deadline ({left}) → {m.get('bonus', '')}"))
    for p in profile.get("plays", []):
        for k, label in (("hold_until", "hold ends"), ("bonus_expected_by", "bonus should have posted"),
                         ("action_by", p.get("action", "action"))):
            if p.get(k):
                items.append((to_date(p[k]), f"{p['name']}: {label}"))
    for p in profile.get("pipeline", []):
        items.append((to_date(p["target_date"]), f"Planned apply — {p['person'].title()}: {p['offer']}"))
    for person in profile.get("people", {}):
        for d, n in five24_timeline(cards, person, on):
            items.append((d, f"{person.title()} drops to {n}/24"))
    return sorted(i for i in items if i[0] <= end)


# ── REPORT ────────────────────────────────────────────────────────────────────

def build_report(profile: dict, offers_doc: dict, on: date) -> dict:
    cards = load_cards(profile)
    offers = offers_doc.get("offers", [])
    by_id = {o["id"]: o for o in offers}
    ranked = rank_offers(profile, offers, on, cards)
    committed, committed_lines = committed_msr(profile, on)
    return {
        "as_of": on,
        "five24": {p: {"count": five24_count(cards, p, on),
                       "cards": [f"{c.product} ({c.opened})" for c in five24_cards(cards, p, on)],
                       "timeline": five24_timeline(cards, p, on)}
                   for p in profile.get("people", {})},
        "committed_msr_monthly": committed,
        "committed_msr": committed_lines,
        "deadlines": upcoming_deadlines(profile, cards, on),
        "pipeline": check_pipeline(profile, by_id, cards),
        "ranked": ranked,
        "unverified": [o["id"] for o in offers if not o.get("last_verified")],
    }


def render_markdown(rep: dict) -> str:
    on = rep["as_of"]
    L = [f"# Churn engine — {on}", ""]

    L += ["## 5/24 status", ""]
    for p, s in rep["five24"].items():
        tl = ", ".join(f"{d:%-m/%-d/%y} → {n}/24" for d, n in s["timeline"][:4])
        L.append(f"- **{p.title()}: {s['count']}/24** ({'; '.join(s['cards']) or 'none'})"
                 + (f". Drops: {tl}" if tl else ""))
    L.append("")

    L += [f"## Next {DEADLINE_HORIZON_DAYS} days", ""]
    for d, text in rep["deadlines"]:
        flag = "⚠️ OVERDUE " if d < on else ""
        L.append(f"- {flag}**{d:%a %-m/%-d}** — {text}")
    if not rep["deadlines"]:
        L.append("- Nothing due.")
    L.append("")

    L += ["## MSR load", "",
          f"- Committed to open MSRs: **${rep['committed_msr_monthly']:,.0f}/mo**"]
    L += [f"  - {x}" for x in rep["committed_msr"]]
    L.append("")

    L += ["## Pipeline check (simulated in order)", ""]
    for p in rep["pipeline"]:
        if p.get("ok"):
            L.append(f"- ✅ {p['target_date']:%-m/%-d/%y} {p['person'].title()} — {p['product']} "
                     f"(5/24 at apply: {p['five24_at_target']})")
        else:
            when = f"; earliest OK {p['earliest_ok']:%-m/%-d/%y}" if p.get("earliest_ok") else ""
            L.append(f"- ❌ {p['target_date']:%-m/%-d/%y} {p['person'].title()} — {p.get('product', p['offer'])}: "
                     f"{'; '.join(p['reasons'])}{when}")
    L.append("")

    L += ["## Ranked next cards", "",
          "| # | Who | Card | Eligible | Best window | Net $ | Score | Notes |",
          "|---|-----|------|----------|-------------|------:|------:|-------|"]
    i = 0
    for r in rep["ranked"]:
        if not r.earliest:
            continue
        i += 1
        fmt = lambda d: "now" if d == on else f"{d:%-m/%-d/%y}"
        notes = "; ".join(r.notes + r.risks + ([] if r.verified else ["⚠️ offer unverified"]))
        L.append(f"| {i} | {r.person.title()} | {r.issuer} {r.product} | {fmt(r.earliest)} | {fmt(r.best_date)} | "
                 f"{r.net_value:,.0f} | {r.score:,.0f} | {notes} |")
    blocked = [r for r in rep["ranked"] if not r.earliest]
    if blocked:
        L += ["", "**Not eligible within 18 months / excluded:**", ""]
        L += [f"- {r.person.title()} — {r.issuer} {r.product}: {'; '.join(r.blocked_now)}" for r in blocked]
    return "\n".join(L) + "\n"


def _json_default(o):
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Ranked):
        return o.__dict__
    raise TypeError(type(o))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--offers", type=Path, default=DEFAULT_OFFERS)
    ap.add_argument("--format", choices=["md", "json"], default="md")
    args = ap.parse_args(argv)
    path = args.profile
    if path == DEFAULT_PROFILE and not path.exists():
        print(f"(no {path.name} — using {EXAMPLE_PROFILE.name})\n")
        path = EXAMPLE_PROFILE
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    offers = yaml.safe_load(args.offers.read_text(encoding="utf-8"))
    rep = build_report(profile, offers, args.as_of)
    if args.format == "json":
        print(json.dumps(rep, default=_json_default, indent=2))
    else:
        print(render_markdown(rep))


if __name__ == "__main__":
    main()
