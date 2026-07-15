# SAS Data Management — Competitive Intelligence Feed

Monitors 10 competitors (per the Data Management Competitive Landscape Guide: direct, indirect, and legacy-battlecard segments) via RSS (Google News + direct blog feeds), analyzes developments with Claude, and delivers a structured JSON report to Streamlit and/or email.

**Runs:** Monday at 9:00 AM ET via GitHub Actions (weekly), with a 7-day lookback window.

---

## Files

| File | Purpose |
|------|---------|
| `config.py` | Competitor list, RSS feed URLs, Claude system prompt |
| `fetch_intel.py` | Main script: fetches feeds → Claude analysis → saves JSON → emails |
| `app.py` | Streamlit dashboard reading `intel_feed.json` |
| `intel_feed.json` | Output file (committed by GitHub Actions after each run) |
| `.github/workflows/intel_schedule.yml` | GitHub Actions cron schedule |

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_ORG/sas-intel.git
cd sas-intel
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your Anthropic API key and (optionally) email credentials
```

### 3. Run manually

```bash
python fetch_intel.py          # full run (default 7-day lookback)
python fetch_intel.py --dry-run  # fetch feeds only, no Claude call
python fetch_intel.py --hours 336  # extend lookback to 14 days, e.g. after a missed run
```

### 4. Launch Streamlit

```bash
streamlit run app.py
```

### 5. Set up GitHub Actions (for scheduled runs)

Add these as **repository secrets** (Settings → Secrets → Actions):

| Secret | Value |
|--------|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `EMAIL_FROM` | Gmail address to send from |
| `EMAIL_TO` | Address to receive the digest |
| `EMAIL_PASSWORD` | Gmail App Password (not your account password) |

Email is optional — the schedule will still run and commit `intel_feed.json` without it.

---

## Customizing

**Add a competitor:**
Edit `COMPETITORS` in `config.py`. Each entry needs a `name`, `segment`, and `google_news_query`. `blog_rss` is optional.

**Change the schedule:**
Edit the `cron` lines in `.github/workflows/intel_schedule.yml`. Uses UTC — adjust for your timezone.

**Tune the analysis:**
Edit `SYSTEM_PROMPT` in `config.py` to adjust what Claude focuses on or the JSON schema it returns.

---

## Email setup (Gmail)

Gmail requires an App Password when 2FA is enabled (which it should be):

1. Go to https://myaccount.google.com/apppasswords
2. Create a new app password for "Mail"
3. Use that 16-character password as `EMAIL_PASSWORD`

If you use a different mail provider, set `SMTP_HOST` and `SMTP_PORT` in `.env`.
