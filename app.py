"""
app.py — SAS Competitive Intelligence Dashboard
Run with: streamlit run app.py
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

FEED_FILE = "intel_feed.json"

st.set_page_config(
    page_title="SAS Competitive Intel",
    page_icon="🔵",
    layout="wide",
)

# ── LOAD DATA ─────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_data() -> dict | None:
    p = Path(FEED_FILE)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def run_refresh() -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "fetch_intel.py"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return result.returncode == 0, result.stdout + result.stderr


# ── HEADER ────────────────────────────────────────────────────────────────────

st.markdown(
    "<h1 style='margin-bottom:0'>SAS Data Management</h1>"
    "<p style='color:#64748b;margin-top:4px;font-size:1.1rem'>Competitive Intelligence Feed</p>",
    unsafe_allow_html=True,
)

data = load_data()

col_meta, col_btn = st.columns([4, 1])
with col_meta:
    if data:
        gen = data.get("generated_at", "")
        try:
            dt = datetime.fromisoformat(gen)
            st.caption(f"Last updated: {dt.strftime('%A, %B %d %Y at %H:%M UTC')}")
        except Exception:
            st.caption(f"Last updated: {gen}")
    else:
        st.caption("No data yet. Run `python fetch_intel.py` or click Refresh.")

with col_btn:
    if st.button("🔄 Refresh Now", type="primary", use_container_width=True):
        with st.spinner("Running intelligence sweep... (may take 30–60s)"):
            ok, log = run_refresh()
        if ok:
            st.success("Updated successfully.")
            st.cache_data.clear()
            st.rerun()
        else:
            st.error("Fetch failed.")
            with st.expander("Show log"):
                st.code(log)

if not data:
    st.info("Run `python fetch_intel.py` from the terminal to populate the feed, or click **Refresh Now** above.")
    st.stop()

st.divider()

# ── MARKET SIGNALS ────────────────────────────────────────────────────────────

signals = data.get("market_signals", [])
if signals:
    st.subheader("📡 Market Signals")
    cols = st.columns(min(len(signals), 3))
    for i, sig in enumerate(signals):
        cols[i % 3].info(sig)
    st.divider()

# ── COMPETITOR FEED ───────────────────────────────────────────────────────────

st.subheader("Competitor Feed")

threat_order = {"high": 0, "medium": 1, "low": 2}
threat_icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
threat_labels = {"high": "HIGH THREAT", "medium": "MED THREAT", "low": "LOW THREAT"}

competitors = sorted(
    data.get("competitors", []),
    key=lambda c: threat_order.get(c.get("threat_level", "low"), 2),
)

for comp in competitors:
    threat = comp.get("threat_level", "low")
    icon = threat_icons.get(threat, "🟢")
    label = threat_labels.get(threat, "LOW THREAT")
    headline = comp.get("headline", "No headline")
    name = comp.get("name", "Unknown")

    with st.expander(f"{icon} **{name}** · {headline}  `{label}`"):
        posture = comp.get("strategic_posture", "")
        if posture:
            st.markdown(f"*{posture}*")
            st.markdown("")

        developments = comp.get("developments", [])
        if not developments:
            st.markdown("*No significant developments found in this scan window.*")
        else:
            for dev in developments:
                st.markdown(f"**{dev.get('title', '')}**  `{dev.get('date', '')}`")
                st.markdown(dev.get("summary", ""))
                impact = dev.get("sas_impact", "")
                if impact:
                    st.warning(f"**SAS Impact:** {impact}")
                st.markdown("---")

st.divider()

# ── RECOMMENDATIONS ───────────────────────────────────────────────────────────

recs = data.get("recommendations", [])
if recs:
    st.subheader("Strategic Recommendations")

    for i, rec in enumerate(recs, 1):
        pri = rec.get("priority", "medium")
        area = rec.get("area", "")
        action = rec.get("action", "")
        rationale = rec.get("rationale", "")

        content = f"**[{pri.upper()}] {area}**\n\n{action}\n\n*{rationale}*"

        if pri == "critical":
            st.error(content)
        elif pri == "high":
            st.warning(content)
        else:
            st.info(content)

# ── FOOTER ────────────────────────────────────────────────────────────────────

st.divider()
st.caption("SAS Intel Feed · Scheduled Mon & Thu via GitHub Actions · Powered by Claude")
