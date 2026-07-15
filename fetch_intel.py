"""
fetch_intel.py — SAS Competitive Intelligence Feed

Usage:
    python fetch_intel.py              # full run
    python fetch_intel.py --dry-run    # fetch feeds only, skip Claude + email
    python fetch_intel.py --hours 168  # extend lookback window
    python fetch_intel.py --file report.pdf  # attach context document
"""

import argparse
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import anthropic
import feedparser
from dotenv import load_dotenv

from config import COMPETITORS, FEED_USER_AGENT, SYSTEM_PROMPT

load_dotenv()

FEED_FILE = "intel_feed.json"
LOOKBACK_HOURS = 168  # 7 days, matched to the weekly run cadence
MAX_ITEMS_PER_FEED = 8
MAX_ITEMS_PER_COMPETITOR = 15

SOURCE_LABELS = {
    "blog":     "📝 Thought Leadership",
    "newsroom": "📣 Product / Company Update",
    "google":   "📰 Trade Press",
}

SOURCE_ICONS = {
    "blog":        ("📝", "Thought Leadership",   "#2563eb"),
    "newsroom":    ("📣", "Product / Co. Update", "#7c3aed"),
    "trade_press": ("📰", "Trade Press",          "#0891b2"),
}

PLATFORM_ICONS = {
    "LinkedIn": "💼",
    "Reddit":   "🔴",
    "Twitter":  "𝕏",
}

PRI_COLORS = {"critical": "#dc2626", "high": "#d97706", "medium": "#2563eb"}
THREAT_LABELS = {"high": "🔴 HIGH", "medium": "🟡 MED", "low": "🟢 LOW"}
THREAT_ORDER = {"high": 0, "medium": 1, "low": 2}


# ── RSS FETCHING ──────────────────────────────────────────────────────────────

def google_news_url(query: str) -> str:
    return (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )


def fetch_feed(url: str, feed_type: str, cutoff: datetime) -> list[dict]:
    items = []
    try:
        feed = feedparser.parse(url, agent=FEED_USER_AGENT)
        status = getattr(feed, "status", 0)
        if status in (403, 404, 410):
            print(f"      ↳ HTTP {status}: {url[:65]}")
            return []
        for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub:
                pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "title": title,
                "summary": (entry.get("summary") or "")[:600].strip(),
                "link": entry.get("link", ""),
                "published": entry.get("published", "unknown date"),
                "source_type": feed_type,
            })
    except Exception as e:
        print(f"      ↳ Feed error: {e}")
    return items


def fetch_competitor_items(competitor: dict, hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    all_items = []

    for feed_cfg in competitor.get("feeds", []):
        items = fetch_feed(feed_cfg["url"], feed_cfg["type"], cutoff)
        if items:
            label = SOURCE_LABELS.get(feed_cfg["type"], feed_cfg["type"])
            print(f"      ✓ {len(items)} item(s) [{label}]")
        all_items.extend(items)
        time.sleep(0.3)

    for query in competitor.get("google_news_queries", []):
        items = fetch_feed(google_news_url(query), "google", cutoff)
        if items:
            print(f"      ✓ {len(items)} item(s) [📰 Trade Press] via: '{query}'")
        all_items.extend(items)
        time.sleep(0.3)

    seen, deduped = set(), []
    for item in all_items:
        key = item["title"].lower()[:80]
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped[:MAX_ITEMS_PER_COMPETITOR]


# ── CLAUDE ANALYSIS ───────────────────────────────────────────────────────────

def build_prompt(all_items: dict, context_files: list | None) -> str:
    lines = [
        "Recent RSS items from SAS Data Management competitors.",
        "Each item tagged: blog | newsroom | google\n",
    ]
    for comp_name, items in all_items.items():
        lines.append(f"## {comp_name}")
        if not items:
            lines.append("No recent articles found.\n")
            continue
        for item in items:
            lines.append(
                f"- [{item['source_type'].upper()}] {item['title']} ({item['published']})"
            )
            if item["summary"]:
                lines.append(f"  {item['summary'][:350]}")
        lines.append("")
    if context_files:
        lines.append("\n## Additional Context Documents")
        for p in context_files:
            lines.append(f"- {p} (attached)")
    lines.append("\nReturn only the JSON. No preamble.")
    return "\n".join(lines)


def parse_json(raw: str) -> dict:
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw[start:end + 1])
        raise ValueError(f"Could not parse JSON from response:\n{raw[:400]}")


def run_claude_analysis(all_items: dict, context_files: list | None = None) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt_text = build_prompt(all_items, context_files)
    message_content = [{"type": "text", "text": prompt_text}]

    for path in (context_files or []):
        try:
            with open(path, "rb") as f:
                data = base64.standard_b64encode(f.read()).decode("utf-8")
            ext = path.rsplit(".", 1)[-1].lower()
            media_type = {
                "pdf": "application/pdf",
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
            }.get(ext, "application/octet-stream")
            message_content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            })
            print(f"  Attached: {path}")
        except Exception as e:
            print(f"  Warning — could not attach {path}: {e}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message_content}],
    )

    if response.stop_reason == "max_tokens":
        print("  ⚠ Response truncated — retrying in concise mode...")
        concise_text = prompt_text + (
            "\n\nIMPORTANT: Be very concise. Max 1 development per competitor, "
            "1 sentence each. Max 3 marketing actions total. Return only the JSON."
        )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [{"type": "text", "text": concise_text}]}],
        )

    return parse_json(response.content[0].text)


# ── JSON OUTPUT ───────────────────────────────────────────────────────────────

def save_json(data: dict, path: str = FEED_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"✓ Saved → {path}")


# ── EMAIL BUILDER ─────────────────────────────────────────────────────────────

def _card(content: str, border_color: str = "#e2e8f0", bg: str = "#f8fafc") -> str:
    return (
        f"<div style='border-left:3px solid {border_color};"
        f"padding:10px 14px;margin-bottom:12px;background:{bg}'>"
        f"{content}</div>"
    )


def _h2(text: str) -> str:
    return f"<h2 style='color:#1e40af;margin-top:28px;margin-bottom:12px'>{text}</h2>"


def build_email_html(data: dict) -> str:
    generated = data.get("generated_at", "")
    sections = []

    # ── Market Signals ────────────────────────────────────────────────────────
    signals = data.get("market_signals", [])
    if signals:
        bullets = "".join(f"<li style='margin-bottom:6px'>{s}</li>" for s in signals)
        sections.append(
            _h2("📡 Market Signals")
            + f"<ul style='padding-left:20px;margin:0'>{bullets}</ul>"
        )

    # ── Competitor Snapshot ───────────────────────────────────────────────────
    competitors = sorted(
        data.get("competitors", []),
        key=lambda c: THREAT_ORDER.get(c.get("threat_level", "low"), 2),
    )

    rows = ""
    for c in competitors:
        label = THREAT_LABELS.get(c.get("threat_level", "low"), "🟢 LOW")
        act = c.get("content_activity", {})
        act_str = (
            f"📝 {act.get('blog_count', 0)}&nbsp;"
            f"📣 {act.get('newsroom_count', 0)}&nbsp;"
            f"📰 {act.get('trade_press_count', 0)}"
        )
        rows += (
            f"<tr>"
            f"<td style='padding:10px;border:1px solid #e2e8f0;vertical-align:top'>"
            f"<strong>{c['name']}</strong><br>"
            f"<span style='color:#6b7280;font-size:11px'>{c.get('strategic_posture', '')}</span></td>"
            f"<td style='padding:10px;border:1px solid #e2e8f0'>{c.get('headline', '—')}</td>"
            f"<td style='padding:10px;border:1px solid #e2e8f0;text-align:center;font-size:11px'>{act_str}</td>"
            f"<td style='padding:10px;border:1px solid #e2e8f0;text-align:center'>{label}</td>"
            f"</tr>"
        )

    sections.append(
        _h2("Competitor Snapshot")
        + f"<table style='width:100%;border-collapse:collapse;font-size:13px'>"
        f"<thead><tr style='background:#f8fafc'>"
        f"<th style='padding:10px;border:1px solid #e2e8f0;text-align:left'>Competitor</th>"
        f"<th style='padding:10px;border:1px solid #e2e8f0;text-align:left'>Latest Development</th>"
        f"<th style='padding:10px;border:1px solid #e2e8f0;text-align:center'>Activity</th>"
        f"<th style='padding:10px;border:1px solid #e2e8f0'>Threat</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        f"<p style='font-size:11px;color:#94a3b8;margin-top:6px'>"
        f"📝 Blog &nbsp;·&nbsp; 📣 Press / Product &nbsp;·&nbsp; 📰 Trade Press</p>"
    )

    # ── High Threat Detail ────────────────────────────────────────────────────
    high_threat = [c for c in competitors if c.get("threat_level") == "high"]
    if high_threat:
        dev_html = ""
        for c in high_threat:
            dev_html += f"<h3 style='color:#dc2626;margin-bottom:6px'>🔴 {c['name']}</h3>"
            for dev in c.get("developments", []):
                src = dev.get("source_type", "google")
                icon, lbl, color = SOURCE_ICONS.get(src, ("📰", "Trade Press", "#0891b2"))
                dev_html += _card(
                    f"<span style='font-size:11px;color:{color};font-weight:bold'>{icon} {lbl}</span><br>"
                    f"<strong style='font-size:13px'>{dev.get('title', '')}</strong> "
                    f"<span style='color:#94a3b8;font-size:11px'>{dev.get('date', '')}</span><br>"
                    f"<span style='font-size:13px'>{dev.get('summary', '')}</span><br>"
                    f"<span style='font-size:12px;color:#d97706'>"
                    f"<strong>SAS Impact:</strong> {dev.get('sas_impact', '')}</span>",
                    border_color=color,
                )
        sections.append(_h2("High Threat — Detail") + dev_html)

    # ── Strategic Recommendations ─────────────────────────────────────────────
    recs = data.get("recommendations", [])
    if recs:
        rec_html = ""
        for r in recs:
            color = PRI_COLORS.get(r.get("priority", "medium"), "#2563eb")
            rec_html += (
                f"<li style='margin-bottom:14px'>"
                f"<span style='color:{color};font-weight:bold'>[{r.get('priority','').upper()}]</span> "
                f"<strong>{r.get('area', '')}</strong><br>"
                f"<span style='font-size:13px'>{r.get('action', '')}</span><br>"
                f"<em style='color:#6b7280;font-size:12px'>{r.get('rationale', '')}</em>"
                f"</li>"
            )
        sections.append(
            _h2("Strategic Recommendations")
            + f"<ol style='padding-left:20px'>{rec_html}</ol>"
        )

    # ── Marketing Actions ─────────────────────────────────────────────────────
    actions = data.get("marketing_actions", [])
    if actions:
        actions_html = ""
        for action in actions:
            comp = action.get("competitor", "")
            trigger = action.get("trigger", "")
            blog = action.get("blog_angle", {})
            socials = action.get("social_talking_points", [])
            battlecard = action.get("battlecard_flag")
            demo = action.get("demo_scenario", "")
            demand_gen = action.get("demand_gen_hook", "")

            inner = f"<strong style='font-size:14px'>↳ {comp}</strong><br>"
            inner += f"<em style='color:#6b7280;font-size:12px'>Trigger: {trigger}</em>"

            # Blog angle
            if blog:
                tags_html = " ".join(
                    f"<span style='background:#dbeafe;color:#1e40af;font-size:10px;"
                    f"padding:2px 6px;border-radius:3px;margin-right:4px'>{t}</span>"
                    for t in blog.get("suggested_tags", [])
                )
                inner += (
                    f"<div style='margin-top:10px'>"
                    f"<span style='font-size:11px;font-weight:bold;color:#1e40af'>📝 BLOG ANGLE</span><br>"
                    f"<strong style='font-size:13px'>{blog.get('title', '')}</strong><br>"
                    f"<span style='font-size:12px;color:#374151'>{blog.get('hook', '')}</span><br>"
                    f"<div style='margin-top:5px'>{tags_html}</div>"
                    f"</div>"
                )

            # Social talking points
            if socials:
                social_html = ""
                for s in socials:
                    platform = s.get("platform", "")
                    p_icon = PLATFORM_ICONS.get(platform, "💬")
                    social_html += (
                        f"<div style='margin-bottom:8px'>"
                        f"<span style='font-size:11px;font-weight:bold;color:#374151'>"
                        f"{p_icon} {platform}</span>"
                        f"<span style='font-size:11px;color:#6b7280'> · {s.get('community', '')}</span><br>"
                        f"<span style='font-size:12px;color:#374151'>{s.get('message', '')}</span>"
                        f"</div>"
                    )
                inner += (
                    f"<div style='margin-top:10px'>"
                    f"<span style='font-size:11px;font-weight:bold;color:#1e40af'>💬 SOCIAL TALKING POINTS</span><br>"
                    f"<div style='margin-top:6px'>{social_html}</div>"
                    f"</div>"
                )

            # Demo scenario
            if demo:
                inner += (
                    f"<div style='margin-top:10px'>"
                    f"<span style='font-size:11px;font-weight:bold;color:#1e40af'>🎯 DEMO SCENARIO</span><br>"
                    f"<span style='font-size:12px;color:#374151'>{demo}</span>"
                    f"</div>"
                )

            # Demand gen hook
            if demand_gen:
                inner += (
                    f"<div style='margin-top:10px'>"
                    f"<span style='font-size:11px;font-weight:bold;color:#1e40af'>📣 DEMAND GEN HOOK</span><br>"
                    f"<span style='font-size:12px;color:#374151'>{demand_gen}</span>"
                    f"</div>"
                )

            # Battlecard flag
            if battlecard and battlecard.lower() != "null":
                inner += (
                    f"<div style='margin-top:10px;padding:6px 10px;background:#fef3c7;"
                    f"border:1px solid #f59e0b;font-size:12px'>"
                    f"⚠️ <strong>Battlecard Update:</strong> {battlecard}"
                    f"</div>"
                )

            actions_html += _card(inner, border_color="#6366f1", bg="#fafafa")

        sections.append(_h2("🚀 Marketing Actions") + actions_html)

    # ── Assemble ──────────────────────────────────────────────────────────────
    return (
        "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif;"
        "max-width:800px;margin:0 auto;padding:28px;color:#1e293b'>"
        "<h1 style='color:#1e40af;margin-bottom:4px'>SAS Data Management</h1>"
        "<h2 style='font-weight:normal;color:#64748b;margin-top:0'>"
        "Competitive Intelligence &amp; Marketing Actions</h2>"
        f"<p style='color:#94a3b8;font-size:12px;border-bottom:1px solid #e2e8f0;"
        f"padding-bottom:16px'>Generated: {generated}</p>"
        + "\n".join(sections)
        + "<hr style='border:none;border-top:1px solid #e2e8f0;margin:28px 0'>"
        "<p style='font-size:11px;color:#94a3b8'>"
        "SAS Intel Feed &middot; Mon &amp; Thu &middot; Powered by Claude</p>"
        "</body></html>"
    )


# ── EMAIL SEND ────────────────────────────────────────────────────────────────

def send_email(data: dict) -> None:
    api_key = os.getenv("SENDGRID_API_KEY")
    from_addr = os.getenv("EMAIL_FROM")
    to_addr = os.getenv("EMAIL_TO")

    if not all([api_key, from_addr, to_addr]):
        print("⚠  SendGrid env vars not set (SENDGRID_API_KEY / EMAIL_FROM / EMAIL_TO). Skipping.")
        return

    recipients = [addr.strip() for addr in to_addr.split(",")]

    import sendgrid
    from sendgrid.helpers.mail import Mail, To

    msg = Mail(
        from_email=from_addr,
        to_emails=[To(r) for r in recipients],
        subject=f"SAS Competitive Intel — {datetime.now().strftime('%b %d, %Y')}",
        html_content=build_email_html(data),
    )

    try:
        sg = sendgrid.SendGridAPIClient(api_key=api_key)
        response = sg.send(msg)
        print(f"✓ Email sent → {', '.join(recipients)} (status {response.status_code})")
    except Exception as e:
        print(f"✗ Email failed: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SAS Intel Feed")
    parser.add_argument("--dry-run", action="store_true", help="Fetch feeds only, skip Claude + email")
    parser.add_argument("--hours", type=int, default=LOOKBACK_HOURS, help="Lookback window in hours")
    parser.add_argument("--file", nargs="*", metavar="PATH", help="PDF/image files to attach as context")
    args = parser.parse_args()

    print(f"\nSAS Intel Feed  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Lookback: {args.hours}h  |  Competitors: {len(COMPETITORS)}\n")

    all_items: dict[str, list] = {}
    for comp in COMPETITORS:
        print(f"  {comp['name']}")
        items = fetch_competitor_items(comp, hours=args.hours)
        all_items[comp["name"]] = items
        counts = {"blog": 0, "newsroom": 0, "google": 0}
        for item in items:
            counts[item.get("source_type", "google")] += 1
        print(
            f"    → {len(items)} items  "
            f"[📝 {counts['blog']} blog  "
            f"📣 {counts['newsroom']} newsroom  "
            f"📰 {counts['google']} trade press]\n"
        )

    total = sum(len(v) for v in all_items.values())
    print(f"Total items collected: {total}")

    if args.dry_run:
        print("\n[dry-run] Skipping Claude analysis and email.")
        return

    print("\nRunning Claude analysis...")
    result = run_claude_analysis(all_items, context_files=args.file)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()

    save_json(result)
    send_email(result)

    print("\nDone.\n")


if __name__ == "__main__":
    main()
