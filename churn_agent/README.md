# Churn Agent

A personal weekly credit-card churning agent. Every Sunday morning (ET) it:

1. Re-reads the live **churn tracker Sheet** and **Churning Plays Doc** in Google Drive
2. Checks current sign-up offers on the issuers' sites, falling back to recent secondary sources when a site is blocked, and looks for new elevated offers and bank bonuses
3. Runs a deterministic rules engine for 5/24, issuer rules, MSR capacity, deadlines, and a simulation of your planned pipeline
4. Recommends the **next best move** and **ranks the next cards to get**, with the best window for each
5. **Overwrites your Business Cards Churn Table** (Google Sheet) with current offers, eligibility, and the recommended order. It only writes input cells; your formulas keep computing.
6. Saves `Churn Agent Weekly — <date>` to Google Drive and sends the summary as a push and email notification

It never applies for cards, moves money, or edits your card-tracker Sheet or Churning Plays Doc. It tells you what to change there. The churn table is the one file it updates.

## Files

| File | Purpose |
|------|---------|
| `PLAYBOOK.md` | The step-by-step instructions the weekly run follows |
| `profile.example.yaml` | Template and test fixture with a fictional household. The **real** profile lives in your private Google Drive (`churn_agent_profile.yaml`). The run downloads it to `profile.yaml`, which is gitignored because this repo is public. |
| `offers.yaml` | Candidate cards with bonus, MSR, fee, and network, plus `last_verified` and source. The weekly run refreshes it. |
| `engine.py` | Rules and ranking engine (5/24, Chase, Amex, Citi, Cap One, BofA, and Barclays rules; MSR feasibility; Plastiq; deadlines; pipeline check) |
| `test_engine.py` | Tests for the rules |
| `reports/` | Local scratch for reports (gitignored). Reports are archived in Drive. |

## Run it yourself

```bash
pip install -r requirements.txt
python -m churn_agent.engine                     # markdown report for today
python -m churn_agent.engine --as-of 2026-10-28  # "what if I apply on 10/28?"
python -m churn_agent.engine --format json
python -m pytest -q churn_agent
```

## Tuning

Edit `churn_agent_profile.yaml` in Drive (or a local `profile.yaml`):

- **Point values:** `valuations_cpp`
- **Spend capacity:** `spend.regular_monthly`, plus `spend.max_msr_per_month` (the per-card limit; cards above it are excluded). Set `spend.plastiq_validated: true` once the mortgage tests confirm MSR credit.
- **How much a 5/24 slot is worth to you:** `five24_slot_cost`
- **Card types to rank:** `card_types` (e.g. `[business]`)
- **Travel goals:** `travel_goals` (cards earning those currencies rank higher)
- **The plan:** `pipeline` is checked in order every week for conflicts

## Network note

The cloud environment's network policy currently blocks issuer domains, so the
run verifies offers through web search. To let it read issuer pages directly,
add these domains under Allowed domains in the environment's network settings:
`creditcards.chase.com`, `www.theexplorercard.com`, `www.americanexpress.com`,
`www.citi.com`, `www.usbank.com`, `cards.barclaycardus.com`,
`www.bankofamerica.com`, `www.capitalone.com`, and
`www.doctorofcredit.com`.
