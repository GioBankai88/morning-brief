import calendar
import html
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

TZ_IT = ZoneInfo("Europe/Rome")
HEADERS = {"User-Agent": "MorningBriefBot/1.0 (personal RSS reader)"}

# Clickbait / noise filter
BLOCKLIST = [
    "oroscopo", "grande fratello", "sanremo", "gossip", "calciomercato",
    "serie a", "champions league", "offerte amazon", "black friday",
    "codice sconto", "sponsorizzato", "you won't believe", "watch:",
    "live updates:", "jim cramer", "mad money"
]

SECTIONS = [
    {
        "id": "italy",
        "title": "Italia · Governo & Economia",
        "hours": 30,
        "limit": 8,
        "feeds": [
            ("ANSA Politica", "https://www.ansa.it/sito/notizie/politica/politica_rss.xml", "IT", None),
            ("ANSA Economia", "https://www.ansa.it/sito/notizie/economia/economia_rss.xml", "IT", None),
            ("Sole 24 Ore", "https://www.ilsole24ore.com/rss/economia.xml", "IT", None),
            ("Il Post", "https://www.ilpost.it/feed/", "IT", r"governo|parlamento|manovra|economia|pil|fisco|lavoro|bce|decreto|meloni|giorgetti|inflazione"),
        ],
    },
    {
        "id": "markets",
        "title": "Mercati & Opportunità",
        "hours": 30,
        "limit": 10,
        "feeds": [
            ("Sole 24 Finanza", "https://www.ilsole24ore.com/rss/finanza.xml", "IT", None),
            ("CNBC Europe", "https://www.cnbc.com/id/19794221/device/rss/rss.html", "EU", None),
            ("CNBC Investing", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069", "US", None),
            ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "US", None),
        ],
    },
    {
        "id": "strikes",
        "title": "Scioperi & Voli",
        "hours": 72,
        "limit": 8,
        "feeds": [
            ("MIT Scioperi", "https://scioperi.mit.gov.it/mit2/public/scioperi/rss", "MIT", r"aereo|ferroviario|generale|trasporto pubblico|piemonte|lombardia|nazionale"),
            ("Trasporti IT", "https://news.google.com/rss/search?q=(sciopero+OR+scioperi)+(treni+OR+aerei+OR+voli+OR+Malpensa+OR+Trenord+OR+ENAV)+when:3d&hl=it&gl=IT&ceid=IT:it", "IT", None),
            ("Aviation EU", "https://news.google.com/rss/search?q=(%22air+traffic%22+OR+%22flight+disruptions%22+OR+%22airport+strike%22)+Europe+when:2d&hl=en&gl=US&ceid=US:en", "EU", None),
        ],
    },
    {
        "id": "sanity",
        "title": "Sanity Check · Conflitti & Rischi",
        "hours": 36,
        "limit": 6,
        "feeds": [
            ("Crisis Group", "https://www.crisisgroup.org/rss", "RISK", None),
            ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "WORLD", r"war|conflict|ukraine|russia|nato|gaza|israel|iran|china|taiwan|missile|ceasefire|nuclear|cyberattack|defense"),
        ],
    },
    {
        "id": "tech",
        "title": "Tech · Solo Rilevante",
        "hours": 36,
        "limit": 5,
        "feeds": [
            ("Hacker News 250+", "https://hnrss.org/newest?points=250", "HN", None),
            ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "TECH", r"ai|security|apple|google|chip|semiconductor|linux|open source|quantum|regulation"),
        ],
    },
    {
        "id": "novara",
        "title": "Novara · Locale",
        "hours": 48,
        "limit": 7,
        "feeds": [
            ("Novara News", "https://news.google.com/rss/search?q=Novara+when:2d&hl=it&gl=IT&ceid=IT:it", "NO", None),
            ("ANSA Piemonte", "https://www.ansa.it/piemonte/notizie/piemonte_rss.xml", "NO", r"novara|novarese|borgomanero|arona|galliate|trecate|malpensa"),
        ],
    },
]


def clean_text(raw: str, max_len: int = 240) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return text


def fetch_feed(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as e:
        print(f"Warn: failed {url}: {e}")
        return None


def entry_dt(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def is_duplicate(title: str, seen_word_sets: list[set[str]]) -> bool:
    words = set(re.findall(r"[a-zàèéìòù0-9]{4,}", title.lower()))
    if not words:
        return False
    for prev in seen_word_sets:
        overlap = len(words & prev) / max(1, min(len(words), len(prev)))
        if overlap >= 0.65:
            return True
    seen_word_sets.append(words)
    return False


def collect_section(sec: dict) -> tuple[list[dict], bool]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=sec["hours"])
    items = []
    seen_words = []
    mit_failed = False

    for source_label, url, tag, regex_filter in sec["feeds"]:
        feed = fetch_feed(url)
        if not feed or not getattr(feed, "entries", None):
            if tag == "MIT":
                mit_failed = True
            continue

        for entry in feed.entries:
            title = clean_text(entry.get("title", ""), 180)
            link = entry.get("link", "#")
            if not title:
                continue

            # Clean Google News trailing " - Source"
            display_source = source_label
            if "news.google.com" in url and " - " in title:
                title_part, src_part = title.rsplit(" - ", 1)
                title = title_part.strip()
                display_source = src_part.strip()

            dt = entry_dt(entry)
            if dt < cutoff and tag != "MIT":
                continue

            summary = clean_text(entry.get("summary") or entry.get("description") or "")
            if summary.lower().startswith(title.lower()[:30]):
                summary = ""  # Avoid Google News repeating the exact title

            blob = f"{title} {summary}".lower()
            if any(bad in blob for bad in BLOCKLIST):
                continue
            if regex_filter and not re.search(regex_filter, blob, re.IGNORECASE):
                continue
            if is_duplicate(title, seen_words):
                continue

            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": display_source,
                "tag": tag,
                "dt": dt,
            })

    items.sort(key=lambda x: x["dt"], reverse=True)
    return items[: sec["limit"]], mit_failed


def maybe_ai_summary(data: dict) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return ""

    digest_input = []
    for sec_id, items in data.items():
        if not items:
            continue
        lines = [f"- {it['title']} ({it['summary'][:120]})" for it in items[:6]]
        digest_input.append(f"[{sec_id.upper()}]\n" + "\n".join(lines))

    prompt = (
        "Sei un analista sintetico e pragmatico. Scrivi un briefing mattutino in italiano "
        "(massimo 5 punti brevi, zero fuffa, tono calmo e fattuale) basato SOLO su queste notizie:\n"
        "1) Governo ed economia Italia\n2) Mercati EU/US/IT\n3) Scioperi/voli\n"
        "4) Sanity check conflitti\n5) Novara (solo se c'è qualcosa di concreto).\n\n"
        + "\n\n".join(digest_input)
    )

    try:
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
        )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()
    except Exception as e:
        print(f"Warn: Gemini summary skipped: {e}")
        return ""


def build_html():
    now_it = datetime.now(TZ_IT)
    collected = {}
    mit_warn = False

    for sec in SECTIONS:
        items, failed = collect_section(sec)
        collected[sec["id"]] = items
        if sec["id"] == "strikes" and failed:
            mit_warn = True

    ai_brief = maybe_ai_summary(collected)

    # Strike status check
    strike_items = collected.get("strikes", [])
    strike_alert = any(
        re.search(r"sciopero|cancellat|ritard|blocc|fermo|revocat|protesta", it["title"], re.I)
        for it in strike_items
    )
    strike_badge = (
        '<span class="badge warn">Possibili disagi / scioperi</span>'
        if strike_alert
        else '<span class="badge ok">Nessuna criticità rilevata</span>'
    )

    nav_links = "".join(
        f'<a class="pill" href="#{sec["id"]}">{html.escape(sec["title"].split("·")[0].strip())}</a>'
        for sec in SECTIONS
    )

    ai_html = ""
    if ai_brief:
        formatted = "<br>".join(html.escape(line) for line in ai_brief.splitlines() if line.strip())
        ai_html = f"""
        <section class="card brief-card">
          <div class="sec-head"><h2>Executive Summary</h2><span class="badge ok">AI Digest</span></div>
          <div class="brief-body">{formatted}</div>
        </section>"""

    sections_html = []
    for sec in SECTIONS:
        items = collected[sec["id"]]
        extra_badge = strike_badge if sec["id"] == "strikes" else f'<span class="count">{len(items)}</span>'
        mit_note = '<p class="muted">Nota: feed ufficiale MIT momentaneamente lento, dati integrati da news.</p>' if (sec["id"] == "strikes" and mit_warn) else ""

        if not items:
            body_html = '<p class="muted">Nessun aggiornamento rilevante nelle ultime ore.</p>'
        else:
            cards = []
            for idx, it in enumerate(items):
                uid = f"{sec['id']}-{idx}-{abs(hash(it['title'])) % 100000}"
                time_str = it["dt"].astimezone(TZ_IT).strftime("%d/%m %H:%M")
                summary_block = f'<p class="summary">{html.escape(it["summary"])}</p>' if it["summary"] else ""
                cards.append(f"""
                <article class="item" data-id="{uid}">
                  <details>
                    <summary>
                      <div class="meta">
                        <span class="tag">{html.escape(it["tag"])}</span>
                        <span>{html.escape(it["source"])}</span>
                        <span>· {time_str}</span>
                      </div>
                      <div class="headline">{html.escape(it["title"])}</div>
                    </summary>
                    <div class="drawer">
                      {summary_block}
                      <a class="ext" href="{html.escape(it['link'], quote=True)}" target="_blank" rel="noopener">Apri fonte originale ↗</a>
                    </div>
                  </details>
                </article>""")
            body_html = "".join(cards)

        sections_html.append(f"""
        <section id="{sec['id']}" class="card">
          <div class="sec-head">
            <h2>{html.escape(sec['title'])}</h2>
            {extra_badge}
          </div>
          {mit_note}
          {body_html}
        </section>""")

    date_label = now_it.strftime("%a %d %b · %H:%M")

    page = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Brief">
<link rel="manifest" href="manifest.json">
<title>Morning Brief</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f4f4f6; --card: #ffffff; --text: #111318; --muted: #6b7280;
    --border: rgba(0,0,0,0.08); --accent: #2563eb; --ok-bg: #dcfce7; --ok-fg: #166534;
    --warn-bg: #fef3c7; --warn-fg: #92400e; --pill: #e5e7eb;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0b0d11; --card: #151922; --text: #f3f4f6; --muted: #9ca3af;
      --border: rgba(255,255,255,0.09); --accent: #60a5fa; --ok-bg: #052e16; --ok-fg: #86efac;
      --warn-bg: #451a03; --warn-fg: #fcd34d; --pill: #1f2430;
    }}
  }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html {{ scroll-behavior: smooth; scroll-padding-top: 108px; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    padding-bottom: calc(2rem + env(safe-area-inset-bottom));
    line-height: 1.45;
  }}
  header {{
    position: sticky; top: 0; z-index: 20;
    background: color-mix(in srgb, var(--bg) 88%, transparent);
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    padding: calc(0.65rem + env(safe-area-inset-top)) 1rem 0.65rem;
    border-bottom: 1px solid var(--border);
  }}
  .top-row {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.5rem; }}
  h1 {{ font-size: 1.15rem; margin: 0; font-weight: 700; letter-spacing: -0.02em; }}
  .date {{ font-size: 0.78rem; color: var(--muted); }}
  nav {{ display: flex; gap: 0.4rem; overflow-x: auto; scrollbar-width: none; }}
  nav::-webkit-scrollbar {{ display: none; }}
  .pill {{
    flex: 0 0 auto; font-size: 0.78rem; font-weight: 600;
    padding: 0.32rem 0.7rem; border-radius: 999px;
    background: var(--pill); color: var(--text); text-decoration: none;
  }}
  main {{ max-width: 640px; margin: 0 auto; padding: 0.85rem; display: flex; flex-direction: column; gap: 0.9rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 0.95rem 1rem; }}
  .brief-card {{ border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); }}
  .brief-body {{ font-size: 0.92rem; line-height: 1.55; }}
  .sec-head {{ display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; margin-bottom: 0.65rem; }}
  h2 {{ font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin: 0; }}
  .count {{ font-size: 0.75rem; color: var(--muted); font-weight: 600; }}
  .badge {{ font-size: 0.72rem; font-weight: 600; padding: 0.2rem 0.55rem; border-radius: 999px; }}
  .badge.ok {{ background: var(--ok-bg); color: var(--ok-fg); }}
  .badge.warn {{ background: var(--warn-bg); color: var(--warn-fg); }}
  .item {{ border-top: 1px solid var(--border); padding: 0.65rem 0; }}
  .item:first-of-type {{ border-top: none; padding-top: 0.15rem; }}
  .item:last-of-type {{ padding-bottom: 0.1rem; }}
  .item.read {{ opacity: 0.45; }}
  details summary {{ list-style: none; cursor: pointer; }}
  details summary::-webkit-details-marker {{ display: none; }}
  .meta {{ font-size: 0.72rem; color: var(--muted); display: flex; align-items: center; gap: 0.35rem; margin-bottom: 0.22rem; }}
  .tag {{ font-weight: 700; color: var(--accent); }}
  .headline {{ font-size: 0.96rem; font-weight: 600; letter-spacing: -0.01em; }}
  .drawer {{ margin-top: 0.5rem; padding-left: 0.65rem; border-left: 2px solid var(--border); }}
  .summary {{ font-size: 0.88rem; color: var(--muted); margin: 0 0 0.5rem; }}
  .ext {{ font-size: 0.82rem; font-weight: 600; color: var(--accent); text-decoration: none; }}
  .muted {{ font-size: 0.85rem; color: var(--muted); margin: 0.25rem 0; }}
</style>
</head>
<body>
  <header>
    <div class="top-row">
      <h1>Morning Brief</h1>
      <span class="date">{html.escape(date_label)}</span>
    </div>
    <nav>{nav_links}</nav>
  </header>
  <main>
    {ai_html}
    {''.join(sections_html)}
  </main>
<script>
  try {{
    const key = "brief_read_v1";
    const read = new Set(JSON.parse(localStorage.getItem(key) || "[]"));
    document.querySelectorAll(".item").forEach(el => {{
      const id = el.dataset.id;
      if (read.has(id)) el.classList.add("read");
      el.querySelector("details").addEventListener("toggle", e => {{
        if (e.target.open) {{
          read.add(id);
          el.classList.add("read");
          localStorage.setItem(key, JSON.stringify([...read].slice(-200)));
        }}
      }});
    }});
  }} catch (_) {{}}
</script>
</body>
</html>"""

    os.makedirs("dist", exist_ok=True)
    with open("dist/index.html", "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    build_html()