import calendar
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf
from bs4 import BeautifulSoup

TZ_IT = ZoneInfo("Europe/Rome")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MyBriefBot/1.0)"}

BLOCKLIST = [
    "oroscopo", "grande fratello", "sanremo", "gossip", "calciomercato",
    "serie a", "champions league", "offerte amazon", "black friday",
    "codice sconto", "sponsorizzato", "you won't believe", "watch:",
    "live updates:", "jim cramer", "mad money"
]

SECTIONS = [
    {
        "id": "italy",
        "label": "Italia",
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
        "label": "Mercati",
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
        "label": "Trasporti",
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
        "label": "Rischi",
        "hours": 36,
        "limit": 6,
        "feeds": [
            ("Crisis Group", "https://www.crisisgroup.org/rss", "RISK", None),
            ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "WORLD", r"war|conflict|ukraine|russia|nato|gaza|israel|iran|china|taiwan|missile|ceasefire|nuclear|cyberattack|defense"),
        ],
    },
    {
        "id": "tech",
        "label": "Tech",
        "hours": 36,
        "limit": 5,
        "feeds": [
            ("Hacker News", "https://hnrss.org/newest?points=250", "HN", None),
            ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "TECH", r"ai|security|apple|google|chip|semiconductor|linux|open source|quantum|regulation"),
        ],
    },
    {
        "id": "novara",
        "label": "Novara",
        "hours": 48,
        "limit": 7,
        "feeds": [
            ("Novara News", "https://news.google.com/rss/search?q=Novara+when:2d&hl=it&gl=IT&ceid=IT:it", "NO", None),
            ("ANSA Piemonte", "https://www.ansa.it/piemonte/notizie/piemonte_rss.xml", "NO", r"novara|novarese|borgomanero|arona|galliate|trecate|malpensa"),
        ],
    },
]

MARKET_TICKERS = [
    ("FTSE MIB", "FTSEMIB.MI", "{:,.0f}"),
    ("NASDAQ", "^IXIC", "{:,.0f}"),
    ("EURONEXT", "ENX.PA", "{:,.2f}"),
    ("DIASORIN", "DIA.MI", "{:,.2f}"),
    ("AMPLIFON", "AMP.MI", "{:,.2f}"),
    ("WEBUILD", "WBD.MI", "{:,.2f}"),
    ("LEONARDO", "LDO.MI", "{:,.2f}"),
]


def fetch_weather_novara() -> tuple[str, str]:
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 45.4469,
                "longitude": 8.6222,
                "current": "temperature_2m,weather_code",
                "timezone": "Europe/Rome",
            },
            timeout=8,
        )
        r.raise_for_status()
        cur = r.json()["current"]
        temp = round(cur["temperature_2m"])
        code = cur["weather_code"]
        if code == 0:
            icon = "☀"
        elif code in (1, 2, 3):
            icon = "⛅"
        elif code in (45, 48):
            icon = "🌫"
        elif code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
            icon = "🌧"
        elif code in (71, 73, 75, 85, 86):
            icon = "❄"
        elif code >= 95:
            icon = "⛈"
        else:
            icon = "☁"
        return icon, f"{temp}°"
    except Exception:
        return "·", ""


def build_sparkline_svg(points: list[float], color: str, gid: str) -> str:
    if len(points) < 2:
        return ""
    mn, mx = min(points), max(points)
    span = (mx - mn) or 1.0
    coords = []
    n = len(points)
    for i, p in enumerate(points):
        x = (i / (n - 1)) * 100
        y = 30 - ((p - mn) / span) * 24
        coords.append((round(x, 1), round(y, 1)))
    line_pts = " ".join(f"{x},{y}" for x, y in coords)
    area_pts = f"0,34 {line_pts} 100,34"
    return (
        f'<svg class="spark" viewBox="0 0 100 34" preserveAspectRatio="none">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity="0.35"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0"/>'
        f'</linearGradient></defs>'
        f'<polygon points="{area_pts}" fill="url(#{gid})"/>'
        f'<polyline points="{line_pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


def fetch_markets() -> list[dict]:
    cards = []
    for idx, (label, symbol, fmt) in enumerate(MARKET_TICKERS):
        try:
            tk = yf.Ticker(symbol)
            hist_1h = tk.history(period="5d", interval="1h")
            hist_1d = tk.history(period="5d", interval="1d")
            closes_1h = hist_1h["Close"].dropna().tolist()
            closes_1d = hist_1d["Close"].dropna().tolist()
            if not closes_1h and not closes_1d:
                continue
            price = closes_1h[-1] if closes_1h else closes_1d[-1]
            prev = closes_1d[-2] if len(closes_1d) >= 2 else (closes_1h[0] if closes_1h else price)
            pct = ((price - prev) / prev) * 100 if prev else 0.0

            is_vix = symbol == "^VIX"
            up = pct >= 0
            # For VIX, rising volatility is amber/red; for normal assets, up is green
            if is_vix:
                tone = "down" if pct > 3 else ("up" if pct < -2 else "flat")
            else:
                tone = "up" if up else "down"

            color = "#22c55e" if tone == "up" else ("#ef4444" if tone == "down" else "#60a5fa")
            spark_pts = closes_1h[-28:] if len(closes_1h) >= 4 else closes_1d
            sign = "+" if pct >= 0 else ""
            cards.append({
                "label": label,
                "price": fmt.format(price),
                "pct": f"{sign}{pct:.1f}%",
                "pct_num": pct,
                "tone": tone,
                "svg": build_sparkline_svg(spark_pts, color, f"sg{idx}"),
            })
        except Exception as e:
            print(f"Warn: ticker {symbol}: {e}")
    return cards


def clean_text(raw: str, max_len: int = 240) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return text


def extract_image(entry) -> str:
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media and isinstance(media, list):
            url = media[0].get("url", "")
            if url.startswith("http"):
                return url
    for enc in entry.get("enclosures", []) or []:
        if "image" in enc.get("type", "") and enc.get("href", "").startswith("http"):
            return enc["href"]
    raw_html = entry.get("summary") or entry.get("description") or ""
    if "<img" in raw_html:
        soup = BeautifulSoup(raw_html, "html.parser")
        img = soup.find("img")
        if img and img.get("src", "").startswith("http"):
            src = img["src"]
            if "googleusercontent" not in src and "gstatic" not in src:
                return src
    return ""


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


def rel_age(dt_utc: datetime, now_utc: datetime) -> str:
    mins = max(1, int((now_utc - dt_utc).total_seconds() // 60))
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h ago"
    return f"{hrs // 24}d ago"


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


def collect_section(sec: dict, now_utc: datetime) -> list[dict]:
    cutoff = now_utc - timedelta(hours=sec["hours"])
    items = []
    seen_words = []

    for source_label, url, tag, regex_filter in sec["feeds"]:
        feed = fetch_feed(url)
        if not feed or not getattr(feed, "entries", None):
            continue

        for entry in feed.entries:
            title = clean_text(entry.get("title", ""), 160)
            link = entry.get("link", "#")
            if not title:
                continue

            display_source = source_label
            if "news.google.com" in url and " - " in title:
                title_part, src_part = title.rsplit(" - ", 1)
                title = title_part.strip()
                display_source = src_part.strip()

            dt = entry_dt(entry)
            if dt < cutoff and tag != "MIT":
                continue

            summary = clean_text(entry.get("summary") or entry.get("description") or "", 220)
            if summary.lower().startswith(title.lower()[:28]):
                summary = ""

            blob = f"{title} {summary}".lower()
            if any(bad in blob for bad in BLOCKLIST):
                continue
            if regex_filter and not re.search(regex_filter, blob, re.IGNORECASE):
                continue
            if is_duplicate(title, seen_words):
                continue

            items.append({
                "sec_id": sec["id"],
                "sec_label": sec["label"],
                "title": title,
                "link": link,
                "summary": summary,
                "image": extract_image(entry),
                "source": display_source,
                "tag": tag,
                "dt": dt,
                "age": rel_age(dt, now_utc),
            })

    items.sort(key=lambda x: x["dt"], reverse=True)
    return items[: sec["limit"]]


def build_brief_bullets(collected: dict, strike_alert: bool) -> list[tuple[str, str]]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        lines = []
        for sec_id, items in collected.items():
            for it in items[:4]:
                lines.append(f"[{sec_id.upper()}] {it['title']} — {it['summary'][:100]}")
        prompt = (
            "Scrivi ESATTAMENTE 4 righe brevissime (max 11 parole l'una) in italiano come sintesi esecutiva "
            "su: 1) Italia governo/economia, 2) Mercati, 3) Scioperi/Rischi, 4) Novara/Tech.\n"
            "Inizia ogni riga con [RED], [AMBER] o [GREEN] in base alla criticità/tono.\n"
            "Nessuna introduzione o testo extra.\n\n" + "\n".join(lines)
        )
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=18,
            )
            r.raise_for_status()
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            parsed = []
            for ln in raw.splitlines():
                m = re.match(r"\[(RED|AMBER|GREEN)\]\s*(.+)", ln.strip(), re.I)
                if m:
                    parsed.append((m.group(1).lower(), m.group(2).strip()))
            if len(parsed) >= 3:
                return parsed[:4]
        except Exception as e:
            print(f"Warn: Gemini brief skipped: {e}")

    # Deterministic fallback (no AI key needed)
    bullets = []
    if collected["italy"]:
        bullets.append(("amber", collected["italy"][0]["title"]))
    if collected["markets"]:
        bullets.append(("green", collected["markets"][0]["title"]))
    if strike_alert and collected["strikes"]:
        bullets.append(("red", f"Trasporti: {collected['strikes'][0]['title']}"))
    else:
        bullets.append(("green", "Trasporti e voli: nessuna criticità immediata rilevata"))
    if collected["novara"]:
        bullets.append(("green", f"Novara: {collected['novara'][0]['title']}"))
    elif collected["sanity"]:
        bullets.append(("amber", collected["sanity"][0]["title"]))
    return bullets[:4]


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Brief">
<link rel="manifest" href="manifest.json">
<title>MyBrief Intelligence</title>
<style>
  :root {
    --bg: #050811;
    --card: #0c1322;
    --card-2: #111b30;
    --border: rgba(148, 163, 184, 0.14);
    --text: #f1f5f9;
    --muted: #8b9bb4;
    --blue: #3b82f6;
    --cyan: #38bdf8;
    --green: #22c55e;
    --amber: #f59e0b;
    --red: #ef4444;
    --purple: #a855f7;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0;
    background: radial-gradient(circle at 20% 0%, #0f1d38 0%, var(--bg) 52%);
    background-attachment: fixed;
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", system-ui, sans-serif;
    padding-bottom: calc(5.2rem + env(safe-area-inset-bottom));
    line-height: 1.4;
  }

  /* Sticky Top Header */
  header {
    position: sticky; top: 0; z-index: 30;
    background: rgba(5, 8, 17, 0.84);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    padding: calc(0.65rem + env(safe-area-inset-top)) 1rem 0.7rem;
    border-bottom: 1px solid var(--border);
  }
  .top-bar {
    max-width: 620px; margin: 0 auto 0.65rem;
    display: flex; justify-content: space-between; align-items: center;
  }
  .brand-title {
    font-size: 1.38rem; font-weight: 800; letter-spacing: -0.03em; line-height: 1;
  }
  .brand-title span { color: var(--blue); }
  .brand-sub {
    font-size: 0.56rem; font-weight: 600; letter-spacing: 0.28em;
    color: var(--muted); margin-top: 0.22rem; text-transform: uppercase;
  }
  .weather-box { text-align: right; font-size: 0.76rem; line-height: 1.35; }
  .weather-date { color: var(--muted); font-weight: 500; }
  .weather-loc { font-weight: 600; color: var(--text); }

  /* Filter Pills */
  .pills {
    max-width: 620px; margin: 0 auto;
    display: flex; gap: 0.42rem; overflow-x: auto; scrollbar-width: none;
  }
  .pills::-webkit-scrollbar { display: none; }
  .pill {
    flex: 0 0 auto; border: 1px solid var(--border);
    background: rgba(255,255,255,0.04); color: var(--muted);
    font-size: 0.75rem; font-weight: 600; padding: 0.36rem 0.8rem;
    border-radius: 999px; cursor: pointer; transition: all 0.15s ease;
  }
  .pill.active {
    background: var(--blue); color: #fff; border-color: var(--blue);
    box-shadow: 0 0 14px rgba(59, 130, 246, 0.35);
  }

  main {
    max-width: 620px; margin: 0 auto; padding: 0.9rem 0.9rem 0;
    display: flex; flex-direction: column; gap: 1.05rem;
  }
  .hidden { display: none !important; }

  /* Today in 15 Seconds */
  .summary-box {
    background: linear-gradient(160deg, rgba(16, 28, 52, 0.92), rgba(9, 14, 27, 0.96));
    border: 1px solid rgba(56, 189, 248, 0.34);
    box-shadow: 0 0 24px rgba(56, 189, 248, 0.08);
    border-radius: 16px; padding: 0.9rem 1rem;
  }
  .box-head {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 0.65rem;
  }
  .box-label {
    font-size: 0.7rem; font-weight: 800; letter-spacing: 0.1em;
    color: var(--cyan); display: flex; align-items: center; gap: 0.4rem;
    text-transform: uppercase;
  }
  .bullet-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
  .bullet-list li {
    font-size: 0.86rem; color: #e2e8f0; display: flex; align-items: flex-start; gap: 0.55rem;
    line-height: 1.35;
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; margin-top: 0.36rem; flex-shrink: 0; }
  .dot.green { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .dot.amber { background: var(--amber); box-shadow: 0 0 8px var(--amber); }
  .dot.red { background: var(--red); box-shadow: 0 0 8px var(--red); }

  .signal-bar-row {
    margin-top: 0.8rem; padding-top: 0.65rem; border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 0.65rem; font-size: 0.72rem;
  }
  .signal-bar-row span:first-child { color: var(--muted); }
  .signal-bar-row strong { color: var(--text); }
  .bar-track {
    flex: 1; height: 6px; border-radius: 999px; background: rgba(255,255,255,0.08); overflow: hidden;
  }
  .bar-fill {
    height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, var(--blue), var(--green));
  }

  /* Section Titles */
  .sec-row {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 0.55rem;
  }
  .sec-title {
    font-size: 0.72rem; font-weight: 800; letter-spacing: 0.1em;
    text-transform: uppercase; color: var(--muted);
    display: flex; align-items: center; gap: 0.4rem;
  }
  .sec-action { font-size: 0.74rem; color: var(--cyan); font-weight: 600; cursor: pointer; }

  /* Market Radar */
  .radar-scroll {
    display: flex; gap: 0.55rem; overflow-x: auto; scrollbar-width: none; padding-bottom: 0.15rem;
  }
  .radar-scroll::-webkit-scrollbar { display: none; }
  .m-card {
    flex: 0 0 112px; background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 0.65rem 0.7rem 0.5rem;
  }
  .m-name { font-size: 0.65rem; font-weight: 700; color: var(--muted); letter-spacing: 0.04em; }
  .m-pct { font-size: 0.95rem; font-weight: 800; margin: 0.15rem 0 0.05rem; }
  .m-pct.up { color: var(--green); }
  .m-pct.down { color: var(--red); }
  .m-pct.flat { color: var(--cyan); }
  .m-price { font-size: 0.7rem; color: var(--muted); font-family: ui-monospace, monospace; margin-bottom: 0.3rem; }
  .spark { width: 100%; height: 28px; display: block; }

  /* Key Indicators 4-col */
  .kpi-grid {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem;
  }
  .kpi {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 13px; padding: 0.6rem 0.55rem;
  }
  .kpi-name { font-size: 0.64rem; color: var(--muted); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .kpi-val { font-size: 1.05rem; font-weight: 800; margin: 0.2rem 0 0.35rem; }
  .kpi-val.ok { color: var(--green); }
  .kpi-val.warn { color: var(--amber); }
  .kpi-val.blue { color: var(--cyan); }
  .kpi-val.red { color: var(--red); }
  .kpi-bar { height: 4px; border-radius: 99px; background: rgba(255,255,255,0.08); overflow: hidden; }
  .kpi-bar > div { height: 100%; border-radius: 99px; }

  /* Top Stories Feature Cards */
  .stories-stack { display: flex; flex-direction: column; gap: 0.72rem; }
  .story-card {
    background: linear-gradient(155deg, var(--card-2), var(--card));
    border-radius: 16px; padding: 0.82rem 0.9rem;
    border: 1px solid var(--border); cursor: pointer;
    transition: transform 0.12s ease;
  }
  .story-card:active { transform: scale(0.985); }
  .story-card.tone-red { border-color: rgba(239, 68, 68, 0.45); box-shadow: inset 0 0 24px rgba(239,68,68,0.06); }
  .story-card.tone-amber { border-color: rgba(245, 158, 11, 0.45); box-shadow: inset 0 0 24px rgba(245,158,11,0.06); }
  .story-card.tone-green { border-color: rgba(34, 197, 94, 0.42); box-shadow: inset 0 0 24px rgba(34,197,94,0.05); }
  .story-card.tone-blue { border-color: rgba(56, 189, 248, 0.4); box-shadow: inset 0 0 24px rgba(56,189,248,0.05); }
  .story-card.read { opacity: 0.5; }

  .story-top {
    display: flex; align-items: center; gap: 0.45rem;
    font-size: 0.65rem; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase;
    margin-bottom: 0.45rem;
  }
  .story-top .age { color: var(--muted); font-weight: 500; text-transform: none; letter-spacing: 0; margin-left: 0.25rem; }
  .story-body { display: flex; gap: 0.75rem; align-items: center; }
  .story-text { flex: 1; min-width: 0; }
  .story-title { font-size: 1rem; font-weight: 700; line-height: 1.28; margin: 0 0 0.3rem; letter-spacing: -0.015em; }
  .story-desc {
    font-size: 0.78rem; color: var(--muted); margin: 0;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .story-thumb {
    width: 84px; height: 68px; border-radius: 10px; object-fit: cover; flex-shrink: 0;
    background: rgba(255,255,255,0.05);
  }
  .story-foot { display: flex; gap: 0.4rem; margin-top: 0.65rem; flex-wrap: wrap; }
  .chip {
    font-size: 0.68rem; font-weight: 600; padding: 0.2rem 0.52rem;
    border-radius: 6px; background: rgba(255,255,255,0.06); color: #cbd5e1;
    border: 1px solid var(--border);
  }
  .chip.tag-badge { color: var(--cyan); border-color: rgba(56,189,248,0.28); }

  /* Late News Compact Table */
  .late-box {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; overflow: hidden;
  }
  .late-row {
    display: flex; align-items: center; gap: 0.65rem;
    padding: 0.7rem 0.85rem; border-top: 1px solid var(--border); cursor: pointer;
  }
  .late-row:first-child { border-top: none; }
  .late-row:active { background: rgba(255,255,255,0.03); }
  .late-row.read { opacity: 0.45; }
  .late-age {
    flex: 0 0 46px; font-size: 0.68rem; color: var(--muted);
    font-family: ui-monospace, monospace;
  }
  .late-title {
    flex: 1; font-size: 0.83rem; font-weight: 500; line-height: 1.32;
  }
  .late-cat {
    flex-shrink: 0; font-size: 0.64rem; font-weight: 700; color: var(--muted);
    background: rgba(255,255,255,0.05); border: 1px solid var(--border);
    padding: 0.18rem 0.48rem; border-radius: 6px;
  }

  /* Quote Card */
  .quote-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; padding: 0.9rem 1rem; color: #cbd5e1;
    font-size: 0.82rem; font-style: italic;
  }
  .quote-card span { display: block; margin-top: 0.35rem; font-style: normal; color: var(--muted); font-size: 0.72rem; text-align: right; }

  /* Bottom iOS Tab Bar */
  .tabbar {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 40;
    background: rgba(7, 11, 20, 0.9);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    border-top: 1px solid var(--border);
    padding: 0.45rem 0.5rem calc(0.45rem + env(safe-area-inset-bottom));
    display: flex; justify-content: space-around;
  }
  .tab {
    background: none; border: none; color: var(--muted);
    display: flex; flex-direction: column; align-items: center; gap: 0.18rem;
    font-size: 0.63rem; font-weight: 600; cursor: pointer; padding: 0.15rem 0.6rem;
  }
  .tab svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .tab.active { color: var(--blue); }

  /* Slide-Up Reader Modal */
  .modal-bg {
    position: fixed; inset: 0; z-index: 60;
    background: rgba(0, 0, 0, 0.68); backdrop-filter: blur(6px);
    display: none; align-items: flex-end; justify-content: center;
  }
  .modal-bg.open { display: flex; }
  .sheet {
    width: 100%; max-width: 620px; max-height: 82vh; overflow-y: auto;
    background: #0f172a; border-top: 1px solid rgba(56, 189, 248, 0.3);
    border-radius: 22px 22px 0 0; padding: 1.1rem 1.15rem calc(1.5rem + env(safe-area-inset-bottom));
    animation: slideUp 0.18s ease-out;
  }
  @keyframes slideUp { from { transform: translateY(30px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
  .sheet-bar { width: 38px; height: 4px; border-radius: 99px; background: rgba(255,255,255,0.2); margin: 0 auto 0.9rem; }
  .sheet-meta { font-size: 0.72rem; color: var(--cyan); font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.45rem; }
  .sheet-title { font-size: 1.15rem; font-weight: 800; line-height: 1.3; margin: 0 0 0.75rem; }
  .sheet-img { width: 100%; max-height: 200px; object-fit: cover; border-radius: 12px; margin-bottom: 0.85rem; }
  .sheet-summary { font-size: 0.92rem; color: #cbd5e1; line-height: 1.55; margin: 0 0 1.1rem; }
  .sheet-actions { display: flex; gap: 0.6rem; }
  .btn-primary {
    flex: 1; text-align: center; text-decoration: none;
    background: var(--blue); color: #fff; font-weight: 700; font-size: 0.88rem;
    padding: 0.75rem 1rem; border-radius: 12px;
  }
  .btn-close {
    background: rgba(255,255,255,0.08); color: var(--text); border: none;
    font-weight: 600; font-size: 0.88rem; padding: 0.75rem 1rem; border-radius: 12px; cursor: pointer;
  }
</style>
</head>
<body>
  <header>
    <div class="top-bar">
      <div>
        <div class="brand-title">AI<span>Brief</span></div>
        <div class="brand-sub">PERSONAL INTELLIGENCE</div>
      </div>
      <div class="weather-box">
        <div class="weather-date">__DATE_LABEL__</div>
        <div class="weather-loc">Novara &nbsp;__WEATHER_ICON__ __WEATHER_TEMP__</div>
      </div>
    </div>
    <div class="pills" id="pillBar">
      <button class="pill active" data-cat="all">For you</button>
      <button class="pill" data-cat="italy">Italia</button>
      <button class="pill" data-cat="markets">Mercati</button>
      <button class="pill" data-cat="strikes">Trasporti</button>
      <button class="pill" data-cat="sanity">Rischi</button>
      <button class="pill" data-cat="tech">Tech</button>
      <button class="pill" data-cat="novara">Novara</button>
    </div>
  </header>

  <main>
    <!-- TODAY IN 15 SECONDS -->
    <section class="summary-box" id="secSummary">
      <div class="box-head">
        <div class="box-label">⚡ Today in 15 Seconds</div>
        <span style="font-size:0.7rem;color:var(--muted)">__UPDATE_TIME__</span>
      </div>
      <ul class="bullet-list">__BULLETS_HTML__</ul>
      <div class="signal-bar-row">
        <span>Stabilità &amp; Copertura</span>
        <div class="bar-track"><div class="bar-fill" style="width:__STABILITY_PCT__%"></div></div>
        <strong>__STABILITY_LABEL__</strong>
      </div>
    </section>

    <!-- MARKET RADAR -->
    <section id="secRadar">
      <div class="sec-row">
        <div class="sec-title">📈 Market Radar</div>
        <span class="sec-action" data-jump="markets">Vedi news ›</span>
      </div>
      <div class="radar-scroll">__RADAR_HTML__</div>
    </section>

    <!-- KEY INDICATORS -->
    <section id="secKpi">
      <div class="sec-row">
        <div class="sec-title">🧭 Key Indicators</div>
      </div>
      <div class="kpi-grid">__KPI_HTML__</div>
    </section>

    <!-- TOP STORIES -->
    <section id="secTop">
      <div class="sec-row">
        <div class="sec-title">📌 Top Stories</div>
        <span class="sec-action" id="filterLabel">Tutte</span>
      </div>
      <div class="stories-stack" id="topStories">__TOP_STORIES_HTML__</div>
    </section>

    <!-- LATE NEWS -->
    <section id="secLate">
      <div class="sec-row">
        <div class="sec-title">🕒 Late News</div>
      </div>
      <div class="late-box" id="lateNews">__LATE_NEWS_HTML__</div>
    </section>

    <div class="quote-card">
      “Aggiornato ogni ora 08:00–20:00 (Milano). Zero ads, zero algoritmo, solo fonti dirette.”
      <span>Edizione generata alle __UPDATE_TIME__</span>
    </div>
  </main>

  <nav class="tabbar">
    <button class="tab active" data-tab="all">
      <svg viewBox="0 0 24 24"><path d="M3 9.5L12 3l9 6.5V20a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1V9.5z"/></svg>
      <span>Home</span>
    </button>
    <button class="tab" data-tab="markets">
      <svg viewBox="0 0 24 24"><path d="M3 17l6-6 4 4 8-8M17 7h4v4"/></svg>
      <span>Mercati</span>
    </button>
    <button class="tab" data-tab="signals">
      <svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      <span>Segnali</span>
    </button>
    <button class="tab" data-tab="novara">
      <svg viewBox="0 0 24 24"><path d="M12 21s-6-5.3-6-10a6 6 0 1112 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2"/></svg>
      <span>Novara</span>
    </button>
    <button class="tab" id="refreshTab">
      <svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>
      <span>Aggiorna</span>
    </button>
  </nav>

  <!-- Reader Sheet Modal -->
  <div class="modal-bg" id="modalBg">
    <div class="sheet" id="sheet">
      <div class="sheet-bar"></div>
      <div class="sheet-meta" id="mMeta"></div>
      <h3 class="sheet-title" id="mTitle"></h3>
      <img class="sheet-img" id="mImg" alt="">
      <p class="sheet-summary" id="mSummary"></p>
      <div class="sheet-actions">
        <a class="btn-primary" id="mLink" href="#" target="_blank" rel="noopener">Leggi fonte originale ↗</a>
        <button class="btn-close" id="mClose" type="button">Chiudi</button>
      </div>
    </div>
  </div>

<script id="articles-json" type="application/json">__ARTICLES_JSON__</script>
<script>
  const ARTICLES = JSON.parse(document.getElementById("articles-json").textContent);
  const READ_KEY = "mybrief_read_v2";
  let readSet = new Set();
  try { readSet = new Set(JSON.parse(localStorage.getItem(READ_KEY) || "[]")); } catch (_) {}

  // Mark previously read items
  document.querySelectorAll("[data-uid]").forEach(el => {
    if (readSet.has(el.dataset.uid)) el.classList.add("read");
  });

  // Filter logic
  const pills = document.querySelectorAll(".pill");
  const tabs = document.querySelectorAll(".tab[data-tab]");
  const secSummary = document.getElementById("secSummary");
  const secRadar = document.getElementById("secRadar");
  const secKpi = document.getElementById("secKpi");
  const filterLabel = document.getElementById("filterLabel");

  function setFilter(cat) {
    pills.forEach(p => p.classList.toggle("active", p.dataset.cat === cat));
    tabs.forEach(t => t.classList.toggle("active", t.dataset.tab === cat));

    const isAll = cat === "all";
    const isSignals = cat === "signals";
    secSummary.classList.toggle("hidden", !isAll);
    secKpi.classList.toggle("hidden", !isAll && !isSignals);
    secRadar.classList.toggle("hidden", !isAll && cat !== "markets");

    const labels = { all: "Tutte", italy: "Italia", markets: "Mercati", strikes: "Trasporti", sanity: "Rischi", tech: "Tech", novara: "Novara", signals: "Trasporti & Rischi" };
    filterLabel.textContent = labels[cat] || "Tutte";

    document.querySelectorAll("[data-sec]").forEach(el => {
      const s = el.dataset.sec;
      const show = isAll || (isSignals ? (s === "strikes" || s === "sanity") : s === cat);
      el.classList.toggle("hidden", !show);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  pills.forEach(p => p.addEventListener("click", () => setFilter(p.dataset.cat)));
  tabs.forEach(t => t.addEventListener("click", () => setFilter(t.dataset.tab)));
  document.querySelectorAll("[data-jump]").forEach(el => el.addEventListener("click", () => setFilter(el.dataset.jump)));

  // Refresh button (cache-busting reload for iOS PWA)
  document.getElementById("refreshTab").addEventListener("click", () => {
    window.location.replace(window.location.pathname + "?v=" + Date.now());
  });

  // Modal reader
  const modalBg = document.getElementById("modalBg");
  const mMeta = document.getElementById("mMeta");
  const mTitle = document.getElementById("mTitle");
  const mImg = document.getElementById("mImg");
  const mSummary = document.getElementById("mSummary");
  const mLink = document.getElementById("mLink");

  function openArticle(idx, el) {
    const art = ARTICLES[idx];
    if (!art) return;
    mMeta.textContent = `${art.sec_label} · ${art.source} · ${art.age}`;
    mTitle.textContent = art.title;
    if (art.image) {
      mImg.src = art.image;
      mImg.style.display = "block";
    } else {
      mImg.style.display = "none";
    }
    mSummary.textContent = art.summary || "Apri la fonte originale per leggere l'articolo completo.";
    mLink.href = art.link;
    modalBg.classList.add("open");

    if (el && el.dataset.uid) {
      readSet.add(el.dataset.uid);
      el.classList.add("read");
      try { localStorage.setItem(READ_KEY, JSON.stringify([...readSet].slice(-250))); } catch (_) {}
    }
  }

  document.querySelectorAll("[data-idx]").forEach(el => {
    el.addEventListener("click", () => openArticle(Number(el.dataset.idx), el));
  });
  document.getElementById("mClose").addEventListener("click", () => modalBg.classList.remove("open"));
  modalBg.addEventListener("click", e => { if (e.target === modalBg) modalBg.classList.remove("open"); });
</script>
</body>
</html>"""


def build_html():
    now_utc = datetime.now(timezone.utc)
    now_it = now_utc.astimezone(TZ_IT)

    weather_icon, weather_temp = fetch_weather_novara()
    market_cards = fetch_markets()

    collected = {}
    for sec in SECTIONS:
        collected[sec["id"]] = collect_section(sec, now_utc)

    strike_items = collected.get("strikes", [])
    strike_alert = any(
        re.search(r"sciopero|cancellat|ritard|blocc|fermo|revocat|protesta", it["title"], re.I)
        for it in strike_items
    )

    bullets = build_brief_bullets(collected, strike_alert)
    bullets_html = "".join(
        f'<li><span class="dot {tone}"></span><span>{html.escape(text)}</span></li>'
        for tone, text in bullets
    )

    # Radar HTML
    radar_html = "".join(
        f'<div class="m-card">'
        f'<div class="m-name">{html.escape(c["label"])}</div>'
        f'<div class="m-pct {c["tone"]}">{html.escape(c["pct"])}</div>'
        f'<div class="m-price">{html.escape(c["price"])}</div>'
        f'{c["svg"]}'
        f'</div>'
        for c in market_cards
    ) or '<div class="m-card"><div class="m-name">MARKETS</div><div class="m-price">In attesa dati…</div></div>'

    # Key Indicators (100% real metrics)
    risk_count = len(collected["sanity"])
    novara_count = len(collected["novara"])
    mib = next((c for c in market_cards if c["label"] == "FTSE MIB"), None)
    mib_val = mib["pct"] if mib else f"{len(collected['markets'])}"
    mib_cls = ("ok" if mib["pct_num"] >= 0 else "red") if mib else "blue"
    mib_bar_color = "#22c55e" if mib_cls == "ok" else ("#ef4444" if mib_cls == "red" else "#38bdf8")

    kpis = [
        ("Trasporti", "ALERT" if strike_alert else "OK", "warn" if strike_alert else "ok", "#f59e0b" if strike_alert else "#22c55e", 45 if strike_alert else 92),
        ("Rischi", f"{risk_count}", "warn" if risk_count >= 5 else "blue", "#f59e0b" if risk_count >= 5 else "#38bdf8", min(100, risk_count * 16)),
        ("FTSE MIB", mib_val, mib_cls, mib_bar_color, 72),
        ("Novara", f"{novara_count}", "blue", "#38bdf8", min(100, max(20, novara_count * 15))),
    ]
    kpi_html = "".join(
        f'<div class="kpi">'
        f'<div class="kpi-name">{name}</div>'
        f'<div class="kpi-val {cls}">{html.escape(val)}</div>'
        f'<div class="kpi-bar"><div style="width:{pct}%;background:{col}"></div></div>'
        f'</div>'
        for name, val, cls, col, pct in kpis
    )

    # Pick Top Stories (best 1-2 per section with preference for items with images/alerts)
    all_articles = []
    top_indices = []
    late_indices = []

    tone_map = {
        "strikes": ("tone-amber", "var(--amber)", "TRASPORTI"),
        "sanity": ("tone-red", "var(--red)", "RISK CHECK"),
        "markets": ("tone-green", "var(--green)", "MERCATO"),
        "italy": ("tone-blue", "var(--cyan)", "ITALIA"),
        "tech": ("tone-blue", "var(--purple)", "TECH"),
        "novara": ("tone-green", "var(--cyan)", "NOVARA"),
    }

    for sec in SECTIONS:
        sec_items = collected[sec["id"]]
        # Prioritize stories with images for Top Stories slot, keep chronological for rest
        top_picks = sorted(sec_items[:3], key=lambda x: bool(x["image"]), reverse=True)[:2]
        top_titles = {it["title"] for it in top_picks}
        for it in sec_items:
            idx = len(all_articles)
            all_articles.append({
                "sec_id": it["sec_id"],
                "sec_label": it["sec_label"],
                "title": it["title"],
                "link": it["link"],
                "summary": it["summary"],
                "image": it["image"],
                "source": it["source"],
                "tag": it["tag"],
                "age": it["age"],
            })
            if it["title"] in top_titles and len(top_picks) > 0:
                top_indices.append(idx)
                top_titles.discard(it["title"])
            else:
                late_indices.append(idx)

    # Top Stories HTML
    top_cards = []
    for idx in top_indices:
        art = all_articles[idx]
        tone_cls, dot_col, badge_txt = tone_map.get(art["sec_id"], ("tone-blue", "var(--cyan)", art["sec_label"]))
        if art["sec_id"] == "strikes" and strike_alert:
            tone_cls, dot_col, badge_txt = "tone-red", "var(--red)", "SCIOPERO / DISAGI"
        uid = f"t-{abs(hash(art['title'])) % 999999}"
        img_html = f'<img class="story-thumb" src="{html.escape(art["image"], quote=True)}" loading="lazy" alt="">' if art["image"] else ""
        desc_html = f'<p class="story-desc">{html.escape(art["summary"])}</p>' if art["summary"] else ""
        top_cards.append(f"""
        <article class="story-card {tone_cls}" data-idx="{idx}" data-sec="{art['sec_id']}" data-uid="{uid}">
          <div class="story-top" style="color:{dot_col}">
            <span>● {badge_txt}</span>
            <span class="age">{html.escape(art['age'])}</span>
          </div>
          <div class="story-body">
            <div class="story-text">
              <h3 class="story-title">{html.escape(art['title'])}</h3>
              {desc_html}
            </div>
            {img_html}
          </div>
          <div class="story-foot">
            <span class="chip tag-badge">{html.escape(art['tag'])}</span>
            <span class="chip">{html.escape(art['source'])}</span>
          </div>
        </article>""")

    # Late News HTML
    late_rows = []
    for idx in late_indices:
        art = all_articles[idx]
        uid = f"l-{abs(hash(art['title'])) % 999999}"
        late_rows.append(f"""
        <div class="late-row" data-idx="{idx}" data-sec="{art['sec_id']}" data-uid="{uid}">
          <span class="late-age">{html.escape(art['age'])}</span>
          <span class="late-title">{html.escape(art['title'])}</span>
          <span class="late-cat">{html.escape(art['sec_label'])}</span>
        </div>""")

    days_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    months_en = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    date_label = f"{days_en[now_it.weekday()]}, {now_it.day} {months_en[now_it.month - 1]}"
    stability_pct = 55 if strike_alert else 90
    stability_label = "Attenzione" if strike_alert else "Regolare"

    articles_json = json.dumps(all_articles, ensure_ascii=False).replace("</", "<\\/")

    page = (
        HTML_TEMPLATE
        .replace("__DATE_LABEL__", html.escape(date_label))
        .replace("__WEATHER_ICON__", weather_icon)
        .replace("__WEATHER_TEMP__", html.escape(weather_temp))
        .replace("__UPDATE_TIME__", now_it.strftime("%H:%M"))
        .replace("__BULLETS_HTML__", bullets_html)
        .replace("__STABILITY_PCT__", str(stability_pct))
        .replace("__STABILITY_LABEL__", stability_label)
        .replace("__RADAR_HTML__", radar_html)
        .replace("__KPI_HTML__", kpi_html)
        .replace("__TOP_STORIES_HTML__", "".join(top_cards))
        .replace("__LATE_NEWS_HTML__", "".join(late_rows) or '<div class="late-row"><span class="late-title">Nessun altro aggiornamento.</span></div>')
        .replace("__ARTICLES_JSON__", articles_json)
    )

    os.makedirs("dist", exist_ok=True)
    with open("dist/index.html", "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    build_html()
