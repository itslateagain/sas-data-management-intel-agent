from datetime import date

import yaml

from churn_agent import engine
from churn_agent.engine import Card, add_months, five24_count, hard_rules, load_cards

PROFILE = yaml.safe_load((engine.HERE / "profile.example.yaml").read_text())
OFFERS = {o["id"]: o for o in yaml.safe_load(engine.DEFAULT_OFFERS.read_text())["offers"]}
CARDS = load_cards(PROFILE)


def test_add_months_clamps_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 10, 24), 24) == date(2026, 10, 24)


def test_five24_counts_personal_and_au_not_business():
    # Alex: Aviator, IHG, Boundless, Amex Gold AU, Citi AA Plat. Biz cards excluded.
    assert five24_count(CARDS, "alex", date(2026, 10, 4)) == 5
    # Aviator (10/24/24) stops counting the day after its 24-month anniversary.
    assert five24_count(CARDS, "alex", date(2026, 10, 24)) == 5
    assert five24_count(CARDS, "alex", date(2026, 10, 25)) == 4


def test_sam_not_chase_eligible_until_store_card_drops():
    united = OFFERS["chase_united_business"]
    assert any("5/24" in r for r in hard_rules(united, "sam", CARDS, date(2026, 11, 15)))
    first_ok, _ = engine.earliest_eligible(united, "sam", CARDS, date(2026, 10, 4))
    assert first_ok == date(2027, 3, 27)


def test_citi_65_day_rule():
    aa_biz = OFFERS["citi_aa_business"]
    # Sam opened a Citi card 8/23/26; one more Citi app in 65 days is fine,
    # but two apps in the window blocks.
    cards = CARDS + [Card(person="sam", issuer="Citi", product="X", type="personal",
                          family="other", opened=date(2026, 9, 20))]
    assert any("2/65" in r for r in hard_rules(aa_biz, "sam", cards, date(2026, 10, 1)))
    assert not hard_rules(aa_biz, "sam", CARDS, date(2026, 11, 1))


def test_amex_family_lower_tier_blocked_higher_tier_ok():
    plat = OFFERS["amex_delta_platinum_business"]
    gold = dict(plat, tier=1, id="delta_gold_biz")
    assert not hard_rules(plat, "sam", CARDS, date(2026, 11, 1))
    assert any("lifetime" in r for r in hard_rules(gold, "sam", CARDS, date(2026, 11, 1)))


def test_authorized_user_counts_for_5_24_but_not_as_application():
    au = [c for c in CARDS if c.authorized_user]
    assert au and au[0].counts_toward_5_24() and au[0].app_date is None


def test_pipeline_flags_conflicts():
    # Two Citi business cards 30 days apart: the second hits the 95-day rule.
    profile = dict(PROFILE, pipeline=[
        {"offer": "citi_aa_business", "person": "sam", "target_date": "2026-11-01"},
        {"offer": "citi_other_business", "person": "sam", "target_date": "2026-12-01"},
    ])
    offers = dict(OFFERS, citi_other_business=dict(OFFERS["citi_aa_business"], family="citi_other"))
    first, second = engine.check_pipeline(profile, offers, CARDS)
    assert first["ok"]
    assert not second["ok"] and any("95 days" in r for r in second["reasons"])
    assert second["earliest_ok"] > date(2027, 2, 3)


def test_plastiq_only_for_mastercard():
    big = {"msr": 30000, "msr_months": 3, "network": "visa"}
    feas, fee, _ = engine.msr_feasibility(big, PROFILE, date(2027, 2, 1))
    assert feas < 1 and fee == 0
    mc = {"msr": 24000, "msr_months": 3, "network": "mastercard"}
    feas, fee, note = engine.msr_feasibility(mc, PROFILE, date(2027, 2, 1))
    assert fee > 0 and "Plastiq" in note


def test_report_renders():
    rep = engine.build_report(PROFILE, {"offers": list(OFFERS.values())}, date(2026, 10, 4))
    md = engine.render_markdown(rep)
    assert "Ranked next cards" in md and "5/24 status" in md


def test_chase_no_fee_inks_share_a_lifetime():
    ink_cash = OFFERS["chase_ink_business_cash"]
    # Alex has Ink Business Unlimited, so the Ink Cash bonus is blocked for good.
    assert any("lifetime" in r for r in hard_rules(ink_cash, "alex", CARDS, date(2027, 6, 1)))
    first_ok, _ = engine.earliest_eligible(ink_cash, "alex", CARDS, date(2026, 10, 4))
    assert first_ok is None


def test_msr_cap_excludes_big_spend_cards():
    profile = dict(PROFILE, spend=dict(PROFILE["spend"], max_msr_per_month=4000))
    ranked = engine.rank_offers(profile, [OFFERS["amex_business_platinum"]], date(2026, 10, 4))
    assert all(r.earliest is None and "limit" in r.blocked_now[0] for r in ranked)


def test_dashboard_builds_from_example_profile():
    from churn_agent import build_dashboard
    html = build_dashboard.build(PROFILE, {"offers": list(OFFERS.values())}, date(2026, 10, 4))
    assert "__DATA__" not in html and '"people":["alex","sam"]' in html


def test_all_types_ranks_personal_and_reporting_business_cards():
    profile = dict(PROFILE, card_types=["business"])
    offers = [OFFERS["chase_aeroplan"], OFFERS["c1_venture_x_business"]]
    default = engine.rank_offers(profile, offers, date(2026, 10, 4))
    assert all("preference" in r.blocked_now[0] for r in default)
    everything = engine.rank_offers(profile, offers, date(2026, 10, 4), all_types=True)
    assert not any("card (tracking" in b or "reports to personal" in b
                   for r in everything for b in r.blocked_now)
