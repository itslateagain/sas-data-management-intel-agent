"""
build_dashboard.py — Render the Churn Card Finder page from the profile and offers.

The page shows the points wallet, plays in flight, recent opens, and the
filterable card finder ranked by the engine. All personal data comes from the
(gitignored) profile, so the output file must never be committed.

Usage:
    python -m churn_agent.build_dashboard --out /tmp/churn-card-finder.html
    python -m churn_agent.build_dashboard --as-of 2026-10-04 --out page.html
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import yaml

from churn_agent import engine as e

TEMPLATE = e.HERE / "dashboard" / "template.html"


def _iso(d):
    return d.isoformat() if d else None


def _progress(start: date, end: date, on: date) -> float:
    return round(max(0.0, min(1.0, (on - start).days / max(1, (end - start).days))), 3)


def offers_data(profile: dict, offers: list[dict], on: date, cards) -> list[dict]:
    ranked = e.rank_offers(profile, offers, on, cards)
    by_id: dict[str, dict] = {}
    for r in ranked:
        by_id.setdefault(r.offer_id, {})[r.person] = dict(
            earliest=_iso(r.earliest), best=_iso(r.best_date), net=round(r.net_value),
            score=round(r.score), blocked=r.blocked_now, risks=r.risks, notes=r.notes,
            fee=round(r.plastiq_fee))
    cpp = profile["valuations_cpp"]
    out = []
    for o in offers:
        cur = o.get("currency", "cash")
        out.append(dict(
            id=o["id"], issuer=o["issuer"], product=o["product"], type=o.get("type"),
            network=o.get("network"), currency=cur, points=o.get("bonus_points", 0),
            cash=o.get("bonus_cash", 0), extra=o.get("extra_value", 0),
            bonusValue=round(o.get("bonus_points", 0) * cpp.get(cur, 1) / 100
                             + o.get("bonus_cash", 0) + o.get("extra_value", 0)),
            cpp=cpp.get(cur, 1), msr=o.get("msr"), months=o.get("msr_months"),
            af1=o.get("af_first_year", 0), af=o.get("af_ongoing", 0), url=o.get("url"),
            verified=str(o["last_verified"]) if o.get("last_verified") else None,
            source=o.get("source"), notes=o.get("notes", ""),
            reportsPersonal=bool(o.get("reports_personal")),
            goals=[g["where"] for g in profile.get("travel_goals", []) if cur in g.get("programs", [])],
            people=by_id.get(o["id"], {})))
    return out


def opens_data(profile: dict, cards, on: date) -> list[dict]:
    out = []
    for raw, c in zip(profile.get("cards", []), cards):
        when = c.opened or c.applied
        if not when or when < on - timedelta(days=730):
            continue
        out.append(dict(date=when.isoformat(), person=c.person, issuer=c.issuer,
                        product=c.product, type=c.type, status=c.status,
                        counts=c.counts_toward_5_24(), au=c.authorized_user,
                        network=raw.get("network"), note=raw.get("notes", "")
                        if c.authorized_user else ""))
    return sorted(out, key=lambda o: o["date"], reverse=True)


def wips_data(profile: dict, on: date) -> list[dict]:
    """Plays in flight: open MSRs, bank holds, bonuses due, and manual checks."""
    out = []
    for c in profile.get("cards", []):
        m = c.get("msr")
        if not m or c.get("status") != "open" or m.get("bonus_posted"):
            continue
        start, end = e.to_date(c["opened"]), e.to_date(m["deadline"])
        spent = m.get("spent")
        out.append(dict(
            person=c["person"], title=f"{c['issuer']} {c['product']}", reward=m.get("bonus", ""),
            task=f"Spend ${m['amount']:,}" + (f" (${spent:,.0f} done)" if spent is not None else ""),
            start=start.isoformat(), end=end.isoformat(), status="Spending",
            progress=round(min(1, spent / m["amount"]), 3) if spent is not None else _progress(start, end, on),
            byTime=spent is None,
            note=m.get("note") or ("Spend progress not logged." if spent is None else "")))
    for p in profile.get("plays", []):
        st = p.get("status")
        if st == "holding" and p.get("hold_until"):
            end = e.to_date(p["hold_until"])
            start = e.to_date(p.get("started") or end - timedelta(days=90))
            out.append(dict(person=p.get("person", next(iter(profile["people"]))), title=p["name"],
                            reward=p.get("reward", ""), task=f"Hold ${p.get('capital', 0):,}",
                            start=start.isoformat(), end=end.isoformat(), status="Holding",
                            progress=_progress(start, end, on), byTime=True,
                            note=f"Bonus expected by {p['bonus_expected_by']}." if p.get("bonus_expected_by") else ""))
        elif st == "awaiting_bonus" and p.get("bonus_expected_by"):
            end = e.to_date(p["bonus_expected_by"])
            start = e.to_date(p.get("started") or end - timedelta(days=60))
            out.append(dict(person=p.get("person", next(iter(profile["people"]))), title=p["name"],
                            reward=p.get("reward", ""), task="Bonus should post",
                            start=start.isoformat(), end=end.isoformat(), status="Awaiting bonus",
                            progress=_progress(start, end, on), byTime=True, note=p.get("note", "")))
    for k in profile.get("checks", []):
        if k.get("done"):
            continue
        start, end = e.to_date(k["start"]), e.to_date(k["due"])
        out.append(dict(person=k["person"], title=k["title"], reward=k.get("unlocks", ""),
                        task=k.get("task", "Check"), start=start.isoformat(), end=end.isoformat(),
                        status="Check", progress=_progress(start, end, on), byTime=True,
                        note=k.get("note", "")))
    return out


def wallet_data(profile: dict) -> list[dict]:
    cpp = profile["valuations_cpp"]
    out = []
    for w in profile.get("wallet", []):
        rate = cpp.get(w["currency"], 1.0)
        out.append(dict(
            program=w["program"], cur=w["currency"], person=w["person"], bal=w["balance"],
            asof=str(w["as_of"]), src=w.get("source", ""), est=bool(w.get("estimate")),
            stale=bool(w.get("stale")), pending=w.get("pending", 0),
            pendingNote=w.get("pending_note", ""),
            value=round(w["balance"] * rate / 100),
            pendingValue=round(w.get("pending", 0) * rate / 100)))
    return out


def build(profile: dict, offers_doc: dict, on: date) -> str:
    cards = e.load_cards(profile)
    offers = offers_doc.get("offers", [])
    data = dict(
        asOf=on.isoformat(),
        people=list(profile.get("people", {})),
        offers=offers_data(profile, offers, on, cards),
        five24={p: dict(count=e.five24_count(cards, p, on),
                        timeline=[(d.isoformat(), n) for d, n in e.five24_timeline(cards, p, on)][:4])
                for p in profile.get("people", {})},
        deadlines=[(d.isoformat(), t) for d, t in e.upcoming_deadlines(profile, cards, on)],
        cap=profile.get("spend", {}).get("max_msr_per_month") or 8000,
        goals=[g["where"] for g in profile.get("travel_goals", [])],
        opens=opens_data(profile, cards, on),
        wips=wips_data(profile, on),
        wallet=wallet_data(profile),
    )
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("__DATA__", blob)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    ap.add_argument("--profile", type=Path, default=e.DEFAULT_PROFILE)
    ap.add_argument("--offers", type=Path, default=e.DEFAULT_OFFERS)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    path = args.profile if args.profile.exists() else e.EXAMPLE_PROFILE
    html = build(yaml.safe_load(path.read_text()), yaml.safe_load(args.offers.read_text()), args.as_of)
    args.out.write_text(html)
    print(f"wrote {args.out} ({len(html):,} bytes) from {path.name}")


if __name__ == "__main__":
    main()
