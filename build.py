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
        "short": "Italia",
        "accent": "#2563eb",
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
        "short": "Mercati",
        "accent": "#10b981",
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
        "short": "Trasporti",
        "accent": "#f59e0b",
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
        "title": "Sanity Check · Rischi & Conflitti",
        "short": "Rischi",
        "accent": "#ef4444",
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
        "short": "Tech",
        "accent": "#8b5cf6",
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
        "short": "Novara",
        "accent": "#06b6d4",
        "hours": 48,
        "limit": 7,
        "feeds": [
            ("Novara News", "https://news.google.com/rss/search?q=Novara+when:2d&hl=it&gl=IT&ceid=IT:it", "NO", None),
            ("ANSA Piemonte", "https://www.ansa.it/piemonte/notizie/piemonte_rss.xml", "NO", r"novara|novarese|borgomanero|arona|galliate|trecate|malpensa"),
        ],
    },
]

ICONS = {
    "italy": '<svg viewBox="0 0 24 24"><path d="M3 21h18M4 18h16M6 18V9m4 9V9m4 9V9m4 9V9M2 9l10-6 10 6H2z"/></svg>',
    "markets": '<svg viewBox="0 0 24 24"><path d="M3 17l6-6 4 4 8-8M17 7h4v4"/></svg>',
    "strikes": '<svg viewBox="0 0 24 24"><path d="M17.8 19.2L16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.2-1.1.6L3 8l6 4-3 3-3-.5L2 16l4 2 2 4 1.5-1-.5-3 3-3 4 6 1.2-.7c.4-.2.7-.6.6-1.1z"/></svg>',
    "sanity": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M12 3a14 14 0 000 18M12 3a14 14 0 010 18"/></svg>',
    "tech": '<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/></svg>',
    "novara": '<svg viewBox="0 0 24 24"><path d="M12 21s-6-5.3-6-10a6 6 0 1112 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/></svg>',
}

TAG_COLORS = {
    "IT": "tag-it",
    "EU": "tag-eu",
    "US": "tag-us",
    "MIT": "tag-warn",
    "RISK": "tag-warn",
    "WORLD": "tag-eu",
    "HN": "tag-tech",
    "TECH": "tag-tech",
    "NO": "tag-no",
}

HTML_TEMPLATE = """<!DOCTYPE html>
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
  :root {
    color-scheme: light dark;
    --bg: #f4f2ed;
    --surface: #ffffff;
    --surface-2: #f8f7f4;
    --text: #14171f;
    --muted: #64748b;
    --border: rgba(15, 23, 42, 0.08);
    --shadow: 0 2px 12px rgba(15, 23, 42, 0.04);
    --accent: #1d4ed8;
    --ok-bg: #dcfce7; --ok-fg: #15803d;
    --warn-bg: #fef3c7; --warn-fg: #b45309;
    --pill: rgba(15, 23, 42, 0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #080a0f;
      --surface: #121620;
      --surface-2: #181e2c;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --border: rgba(255, 255, 255, 0.08);
      --shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
      --accent: #38bdf8;
      --ok-bg: rgba(34, 197, 94, 0.14); --ok-fg: #4ade80;
      --warn-bg: rgba(245, 158, 11, 0.16); --warn-fg: #fbbf24;
      --pill: rgba(255, 255, 255, 0.08);
    }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { scroll-behavior: smooth; scroll-padding-top: 118px; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    padding-bottom: calc(2.5rem + env(safe-area-inset-bottom));
    line-height: 1.45;
  }
  header {
    position: sticky; top: 0; z-index: 30;
    background: color-mix(in srgb, var(--bg) 82%, transparent);
    backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
    padding: calc(0.75rem + env(safe-area-inset-top)) 1rem 0.7rem;
    border-bottom: 1px solid var(--border);
  }
  .masthead {
    max-width: 660px; margin: 0 auto 0.65rem;
    display: flex; justify-content: space-between; align-items: center; gap: 0.75rem;
  }
  .brand-wrap h1 {
    font-family: "New York", Georgia, "Times New Roman", serif;
    font-size: 1.45rem; font-weight: 700; margin: 0; letter-spacing: -0.02em;
    line-height: 1.1;
  }
  .dateline {
    font-size: 0.72rem; color: var(--muted); font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.06em; margin-top: 0.15rem;
  }
  .actions { display: flex; gap: 0.45rem; align-items: center; }
  .icon-btn {
    border: 1px solid var(--border); background: var(--surface); color: var(--text);
    border-radius: 999px; padding: 0.38rem 0.7rem; font-size: 0.74rem; font-weight: 600;
    cursor: pointer; display: inline-flex; align-items: center; gap: 0.3rem;
  }
  .icon-btn.active { background: var(--accent); color: #fff; border-color: transparent; }
  nav {
    max-width: 660px; margin: 0 auto;
    display: flex; gap: 0.42rem; overflow-x: auto; scrollbar-width: none;
  }
  nav::-webkit-scrollbar { display: none; }
  .pill {
    flex: 0 0 auto; font-size: 0.76rem; font-weight: 600;
    padding: 0.34rem 0.78rem; border-radius: 999px;
    background: var(--pill); color: var(--text); text-decoration: none;
    transition: opacity 0.15s;
  }
  .pill:active { opacity: 0.6; }

  main {
    max-width: 660px; margin: 0 auto; padding: 0.9rem 0.9rem 0;
    display: flex; flex-direction: column; gap: 1rem;
  }

  /* Glance Strip */
  .glance {
    display: grid; grid-template-columns: 1.3fr 1fr 1fr; gap: 0.55rem;
  }
  .tile {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; padding: 0.65rem 0.75rem; box-shadow: var(--shadow);
  }
  .tile-label {
    font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); font-weight: 600; margin-bottom: 0.25rem;
  }
  .tile-val {
    font-size: 0.92rem; font-weight: 700; display: flex; align-items: center; gap: 0.38rem;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .dot.ok { background: #22c55e; box-shadow: 0 0 0 3px var(--ok-bg); }
  .dot.warn { background: #f59e0b; box-shadow: 0 0 0 3px var(--warn-bg); }

  /* Cards */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 18px; padding: 1rem 1.05rem; box-shadow: var(--shadow);
    border-top: 3px solid var(--sec-accent, var(--border));
  }
  .brief-card {
    background: linear-gradient(145deg, var(--surface), var(--surface-2));
    border-top: 3px solid var(--accent);
  }
  .brief-body { font-size: 0.91rem; line-height: 1.58; }

  .sec-head {
    display: flex; justify-content: space-between; align-items: center;
    gap: 0.5rem; margin-bottom: 0.7rem;
  }
  .sec-title {
    display: flex; align-items: center; gap: 0.5rem;
  }
  .sec-icon {
    width: 26px; height: 26px; border-radius: 8px;
    background: color-mix(in srgb, var(--sec-accent, var(--accent)) 14%, transparent);
    color: var(--sec-accent, var(--accent));
    display: grid; place-items: center; flex-shrink: 0;
  }
  .sec-icon svg {
    width: 15px; height: 15px; fill: none; stroke: currentColor;
    stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
  }
  h2 {
    font-size: 0.92rem; font-weight: 700; letter-spacing: -0.01em; margin: 0;
  }
  .count {
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 0.72rem; color: var(--muted); font-weight: 600;
    background: var(--pill); padding: 0.16rem 0.48rem; border-radius: 999px;
  }
  .badge { font-size: 0.7rem; font-weight: 700; padding: 0.22rem 0.58rem; border-radius: 999px; }
  .badge.ok { background: var(--ok-bg); color: var(--ok-fg); }
  .badge.warn { background: var(--warn-bg); color: var(--warn-fg); }

  /* Articles */
  .item { border-top: 1px solid var(--border); padding: 0.72rem 0; }
  .item:first-of-type { border-top: none; padding-top: 0.15rem; }
  .item:last-of-type { padding-bottom: 0.1rem; }
  .item.read { opacity: 0.42; }
  body.hide-read .item.read { display: none; }

  details summary { list-style: none; cursor: pointer; }
  details summary::-webkit-details-marker { display: none; }
  .row-top {
    display: flex; justify-content: space-between; align-items: center;
    gap: 0.5rem; margin-bottom: 0.28rem;
  }
  .meta {
    font-size: 0.72rem; color: var(--muted);
    display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap;
  }
  .time { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.68rem; }
  .tag {
    font-size: 0.64rem; font-weight: 800; letter-spacing: 0.04em;
    padding: 0.1rem 0.4rem; border-radius: 5px;
  }
  .tag-it { background: rgba(37, 99, 235, 0.14); color: #3b82f6; }
  .tag-eu { background: rgba(99, 102, 241, 0.15); color: #818cf8; }
  .tag-us { background: rgba(16, 185, 129, 0.14); color: #10b981; }
  .tag-warn { background: rgba(239, 68, 68, 0.15); color: #f87171; }
  .tag-tech { background: rgba(139, 92, 246, 0.15); color: #a78bfa; }
  .tag-no { background: rgba(6, 182, 212, 0.15); color: #22d3ee; }

  .headline-row {
    display: flex; justify-content: space-between; align-items: flex-start; gap: 0.6rem;
  }
  .headline {
    font-size: 0.96rem; font-weight: 600; letter-spacing: -0.012em; line-height: 1.38;
  }
  .chev {
    color: var(--muted); font-size: 1rem; line-height: 1.2;
    transition: transform 0.18s ease; flex-shrink: 0;
  }
  details[open] .chev { transform: rotate(90deg); color: var(--accent); }

  .drawer {
    margin-top: 0.6rem; padding: 0.68rem 0.8rem;
    background: var(--surface-2); border-radius: 12px;
    border-left: 3px solid var(--sec-accent, var(--accent));
  }
  .summary { font-size: 0.87rem; color: var(--muted); margin: 0 0 0.6rem; line-height: 1.5; }
  .ext {
    display: inline-block; font-size: 0.78rem; font-weight: 600;
    color: var(--accent); text-decoration: none;
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    padding: 0.34rem 0.72rem; border-radius: 999px;
  }
  .muted { font-size: 0.85rem; color: var(--muted); margin: 0.25rem 0; }
  footer {
    text-align: center; font-size: 0.72rem; color: var(--muted);
    padding: 0.6rem 1rem 0;
  }
</style>
</head>
<body>
  <header>
    <div class="masthead">
      <div class="brand-wrap">
        <h1>Morning Brief</h1>
        <div class="dateline">__DATE_LABEL__</div>
      </div>
      <div class="actions">
        <button id="toggleRead" class="icon-btn" type="button">Solo nuovi</button>
        <button id="refreshBtn" class="icon-btn" type="button">↻</button>
      </div>
    </div>
    <nav>__NAV_LINKS__</nav>
  </header>

  <main>
    <div class="glance">
      <div class="tile">
        <div class="tile-label">Trasporti</div>
        <div class="tile-val"><span class="dot __STRIKE_CLASS__"></span><span>__STRIKE_SHORT__</span></div>
      </div>
      <div class="tile">
        <div class="tile-label">Edizione</div>
        <div class="tile-val">__TOTAL_COUNT__ news</div>
      </div>
      <div class="tile">
        <div class="tile-label">Novara</div>
        <div class="tile-val">__NOVARA_COUNT__ locali</div>
      </div>
    </div>

    __AI_HTML__
    __SECTIONS_HTML__

    <footer>Aggiornato alle __TIME_LABEL__ (ora di Milano) · Auto-refresh 08–20</footer>
  </main>

<script>
  // Cache-busting reload for iPhone Home Screen app
  document.getElementById("refreshBtn").addEventListener("click", () => {
    window.location.replace(window.location.pathname + "?v=" + Date.now());
  });

  // Hide-read toggle & read tracking
  try {
    const key = "brief_read_v1";
    const hideKey = "brief_hide_read";
    const read = new Set(JSON.parse(localStorage.getItem(key) || "[]"));
    const toggleBtn = document.getElementById("toggleRead");

    const applyHide = (on) => {
      document.body.classList.toggle("hide-read", on);
      toggleBtn.classList.toggle("active", on);
    };
    let hideOn = localStorage.getItem(hideKey) === "1";
    applyHide(hideOn);

    toggleBtn.addEventListener("click", () => {
      hideOn = !hideOn;
      localStorage.setItem(hideKey, hideOn ? "1" : "0");
      applyHide(hideOn);
    });

    document.querySelectorAll(".item").forEach(el => {
      const id = el.dataset.id;
      if (read.has(id)) el.classList.add("read");
      el.querySelector("details").addEventListener("toggle", e => {
        if (e.target.open) {
          read.add(id);
          el.classList.add("read");
          localStorage.setItem(key, JSON.stringify([...read].slice(-250)));
        }
      });
    });
  } catch (_) {}
</script>
</body>
</html>"""


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


def format_it_time(dt_utc: datetime, now_it: datetime) -> str:
    dt_it = dt_utc.astimezone(TZ_IT)
    if dt_it.date() == now_it.date():
        return f"Oggi {dt_it.strftime('%H:%M')}"
    if dt_it.date() == (now_it - timedelta(days=1)).date():
        return f"Ieri {dt_it.strftime('%H:%M')}"
    return dt_it.strftime("%d/%m %H:%M")


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
                summary = ""

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
        "Sei un analista sintetico e pragmatico. Scrivi un briefing in italiano "
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
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
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

    strike_items = collected.get("strikes", [])
    strike_alert = any(
        re.search(r"sciopero|cancellat|ritard|blocc|fermo|revocat|protesta", it["title"], re.I)
        for it in strike_items
    )
    strike_class = "warn" if strike_alert else "ok"
    strike_short = "Allerta" if strike_alert else "Regolari"
    strike_badge = (
        '<span class="badge warn">Possibili disagi</span>'
        if strike_alert
        else '<span class="badge ok">Tutto regolare</span>'
    )

    nav_links = "".join(
        f'<a class="pill" href="#{sec["id"]}">{html.escape(sec["short"])}</a>'
        for sec in SECTIONS
    )

    ai_html = ""
    if ai_brief:
        formatted = "<br>".join(html.escape(line) for line in ai_brief.splitlines() if line.strip())
        ai_html = (
            '<section class="card brief-card">'
            '<div class="sec-head">'
            '<div class="sec-title"><h2>Executive Summary</h2></div>'
            '<span class="badge ok">AI Digest</span>'
            '</div>'
            f'<div class="brief-body">{formatted}</div>'
            '</section>'
        )

    sections_html = []
    for sec in SECTIONS:
        items = collected[sec["id"]]
        extra_badge = strike_badge if sec["id"] == "strikes" else f'<span class="count">{len(items)}</span>'
        mit_note = '<p class="muted">Nota: feed MIT momentaneamente lento, dati integrati da news.</p>' if (sec["id"] == "strikes" and mit_warn) else ""

        if not items:
            body_html = '<p class="muted">Nessun aggiornamento rilevante nelle ultime ore.</p>'
        else:
            cards = []
            for idx, it in enumerate(items):
                uid = f"{sec['id']}-{idx}-{abs(hash(it['title'])) % 100000}"
                time_str = format_it_time(it["dt"], now_it)
                tag_cls = TAG_COLORS.get(it["tag"], "tag-it")
                summary_block = f'<p class="summary">{html.escape(it["summary"])}</p>' if it["summary"] else ""
                cards.append(f"""
                <article class="item" data-id="{uid}">
                  <details>
                    <summary>
                      <div class="row-top">
                        <div class="meta">
                          <span class="tag {tag_cls}">{html.escape(it["tag"])}</span>
                          <span>{html.escape(it["source"])}</span>
                        </div>
                        <span class="meta time">{html.escape(time_str)}</span>
                      </div>
                      <div class="headline-row">
                        <div class="headline">{html.escape(it["title"])}</div>
                        <span class="chev">›</span>
                      </div>
                    </summary>
                    <div class="drawer">
                      {summary_block}
                      <a class="ext" href="{html.escape(it['link'], quote=True)}" target="_blank" rel="noopener">Leggi su {html.escape(it["source"])} ↗</a>
                    </div>
                  </details>
                </article>""")
            body_html = "".join(cards)

        sections_html.append(f"""
        <section id="{sec['id']}" class="card" style="--sec-accent: {sec['accent']}">
          <div class="sec-head">
            <div class="sec-title">
              <div class="sec-icon">{ICONS.get(sec['id'], '')}</div>
              <h2>{html.escape(sec['title'])}</h2>
            </div>
            {extra_badge}
          </div>
          {mit_note}
          {body_html}
        </section>""")

    total_count = sum(len(v) for v in collected.values())
    novara_count = len(collected.get("novara", []))
    days_it = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    months_it = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]
    date_label = f"{days_it[now_it.weekday()]} {now_it.day} {months_it[now_it.month - 1]} · {now_it.strftime('%H:%M')}"

    page = (
        HTML_TEMPLATE
        .replace("__DATE_LABEL__", html.escape(date_label))
        .replace("__TIME_LABEL__", now_it.strftime("%H:%M"))
        .replace("__STRIKE_CLASS__", strike_class)
        .replace("__STRIKE_SHORT__", strike_short)
        .replace("__TOTAL_COUNT__", str(total_count))
        .replace("__NOVARA_COUNT__", str(novara_count))
        .replace("__NAV_LINKS__", nav_links)
        .replace("__AI_HTML__", ai_html)
        .replace("__SECTIONS_HTML__", "".join(sections_html))
    )

    os.makedirs("dist", exist_ok=True)
    with open("dist/index.html", "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    build_html()
