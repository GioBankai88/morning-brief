import calendar
import hashlib
import html
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf
from bs4 import BeautifulSoup

TZ_IT = ZoneInfo("Europe/Rome")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MyBriefBot/1.0)"}

# ──────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ──────────────────────────────────────────────────────────────────────────
# Modello Gemini configurabile via variabile d'ambiente GEMINI_MODEL
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Serve all'AI per capire cosa è rilevante. Modificalo liberamente.
READER_PROFILE = (
    "professionista italiano dei servizi finanziari (post-trade), vive a Novara e fa il pendolare "
    "in treno verso Milano; segue economia italiana, mercati, trasporti e tecnologia di uso quotidiano."
)

AI_CANDIDATES = 28   # quanti candidati (i migliori per punteggio) vede l'AI per ogni sezione
MIN_ITEMS = 3        # minimo di notizie per sezione, anche se l'AI è molto selettivo


def has_translator() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def P(*terms: str) -> "re.Pattern":
    """Regex case-insensitive con confini di parola (evita match tipo 'pil' dentro 'pilota')."""
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────────
# FILTRO ANTI-RUMORE
# ──────────────────────────────────────────────────────────────────────────
BLOCK_SUBSTR = [
    "you won't believe", "watch:", "live updates:", "jim cramer", "mad money",
    "offerte amazon", "black friday", "codice sconto", "sponsorizzato",
]
BLOCK_WORDS = P(
    "oroscopo", "grande fratello", "sanremo", "gossip", "calciomercato", "serie a",
    "champions league", "superenalotto", "isola dei famosi", "temptation island", "ricetta",
)


def is_blocked(blob: str) -> bool:
    return any(s in blob for s in BLOCK_SUBSTR) or bool(BLOCK_WORDS.search(blob))


# ──────────────────────────────────────────────────────────────────────────
# FONTI E REGOLE DI RILEVANZA PER SEZIONE
# ──────────────────────────────────────────────────────────────────────────
def feed(label, url, tag, lang="it", w=1.0, only=None, hours=None, fixed=False):
    return {
        "label": label, "url": url, "tag": tag, "lang": lang, "weight": w,
        "only": only, "hours": hours, "fixed": fixed,
    }


def gnews(query: str, when: str = "1d", lang: str = "it") -> str:
    q = quote_plus(f"{query} when:{when}")
    if lang == "it":
        return f"https://news.google.com/rss/search?q={q}&hl=it&gl=IT&ceid=IT:it"
    return f"https://news.google.com/rss/search?q={q}&hl=en&gl=US&ceid=US:en"


TECH_ALERT = P(
    r"outage", r"outages", r"disservizi?", r"down", r"non funziona", r"non funzionano",
    r"malfunzionament\w*", r"guast\w*", r"blackout", r"data breach", r"attacco informatico",
    r"attacchi informatici", r"ransomware", r"vulnerabilit\w*", r"zero-day", r"cve-\d+",
    r"violazione dei dati", r"hack\w*",
)
TECH_SERVICES = P(
    r"outlook", r"microsoft 365", r"office 365", r"microsoft", r"teams", r"azure", r"aws",
    r"amazon web services", r"google", r"gmail", r"cloudflare", r"whatsapp", r"instagram",
    r"facebook", r"icloud", r"apple", r"spid", r"poste", r"pagopa", r"intesa", r"unicredit", r"paypal",
)
TECH_TOPICS = P(
    r"openai", r"anthropic", r"nvidia", r"chatgpt", r"gemini", r"intelligenza artificiale", r"ai act",
    r"semiconduttor\w*", r"chip", r"quantum\w*", r"cybersicurezza", r"cybersecurity",
    r"regolament\w*", r"antitrust", r"dma",
)
NOVARA_CHIP = P(r"chip", r"microchip", r"chiplet", r"semiconduttor\w*", r"silicon box", r"vulcan project")

TECH_OUTAGE = P(
    r"outage", r"outages", r"disservizi?", r"down", r"non funziona", r"non funzionano",
    r"malfunzionament\w*", r"guast\w*", r"blackout",
)
SERVICE_FAMILIES = [
    ("microsoft", P(r"outlook", r"microsoft 365", r"office 365", r"microsoft", r"teams", r"azure")),
    ("amazon", P(r"aws", r"amazon web services")),
    ("google", P(r"google", r"gmail")),
    ("meta", P(r"whatsapp", r"instagram", r"facebook")),
    ("apple", P(r"apple", r"icloud")),
    ("cloudflare", P(r"cloudflare")),
    ("spid", P(r"spid")),
    ("poste", P(r"poste", r"pagopa")),
    ("banche", P(r"intesa", r"unicredit", r"paypal")),
]


def tech_topic(it):
    """Chiave del disservizio (es. 'microsoft'): articoli diversi sullo stesso disservizio la condividono."""
    if not TECH_OUTAGE.search(it["blob"]):
        return None
    for name, rx in SERVICE_FAMILIES:
        if rx.search(it["blob"]):
            return name
    return None


def tech_must(it) -> bool:
    """Disservizio di un servizio digitale diffuso, riportato da almeno 2 testate: deve sempre comparire."""
    return bool(it.get("topic")) and it["coverage"] >= 2


def novara_must(it) -> bool:
    """Notizie sull'investimento chip/semiconduttori a Novara: devono sempre comparire."""
    return bool(NOVARA_CHIP.search(it["blob"]))


SECTIONS = [
    {
        "id": "italy",
        "label": "Italia",
        "hours": 30,
        "limit": 8,
        "editorial": (
            "Politica ed economia italiana: decisioni di governo e Parlamento, manovra, fisco, lavoro, "
            "conti pubblici, Istat/Bankitalia/BCE. Escludi polemiche di partito senza fatti, gossip e cronaca locale."
        ),
        "priority": [
            (P(r"manovra", r"legge di bilancio", r"decret\w*", r"fiducia", r"consiglio dei ministri", r"mattarella",
               r"meloni", r"giorgetti", r"istat", r"bankitalia", r"bce", r"spread", r"inflazione", r"pil", r"fisco",
               r"pension\w*", r"stipendi", r"dazi", r"sanzion\w*", r"referendum", r"elezioni", r"riforma\w*"), 3.0),
            (P(r"corte costituzionale", r"corte dei conti", r"consob", r"antitrust", r"bruxelles",
               r"commissione europea", r"nato"), 1.5),
        ],
        "feeds": [
            feed("ANSA Politica", "https://www.ansa.it/sito/notizie/politica/politica_rss.xml", "IT"),
            feed("ANSA Economia", "https://www.ansa.it/sito/notizie/economia/economia_rss.xml", "IT"),
            feed("Sole 24 Ore", "https://www.ilsole24ore.com/rss/economia.xml", "IT"),
            feed("Il Post", "https://www.ilpost.it/feed/", "IT", w=0.9,
                 only=P(r"governo", r"parlamento", r"manovra", r"economia", r"pil", r"fisco", r"lavoro", r"bce",
                        r"decret\w*", r"meloni", r"giorgetti", r"inflazione")),
            feed("Google News", gnews('(governo OR parlamento OR manovra OR "legge di bilancio" OR decreto OR Mattarella OR Meloni OR "consiglio dei ministri")'), "IT", w=0.9),
            feed("Google News", gnews('(Istat OR Bankitalia OR Bce OR inflazione OR "conti pubblici" OR fisco OR pensioni)'), "IT", w=0.9),
        ],
    },
    {
        "id": "markets",
        "label": "Mercati",
        "hours": 30,
        "limit": 10,
        "editorial": (
            "Mercati finanziari: indici (FTSE MIB, Wall Street), BCE/Fed e tassi, petrolio, spread, risultati societari "
            "rilevanti, regolamentazione finanziaria e post-trading (Euronext, Borsa Italiana, Consob, ESMA, CSDR, T+1). "
            "Escludi consigli d'investimento, pubblicità e opinioni."
        ),
        "priority": [
            (P(r"euronext", r"borsa italiana", r"consob", r"esma", r"csdr", r"t\+1", r"post-trade", r"monte titoli",
               r"t2s", r"regolamento titoli", r"diasorin", r"amplifon", r"webuild", r"leonardo"), 4.0),
            (P(r"piazza affari", r"ftse mib", r"wall street", r"bce", r"ecb", r"fed", r"federal reserve", r"tassi",
               r"rialz\w*", r"ribass\w*", r"spread", r"petrolio", r"oil", r"brent", r"oro", r"gold", r"dollaro",
               r"treasury", r"yields?", r"inflation\w*", r"earnings", r"utili", r"trimestral\w*", r"guidance",
               r"ipo", r"opa", r"rating", r"downgrade", r"upgrade", r"s&p", r"nasdaq", r"dow"), 2.0),
        ],
        "feeds": [
            feed("Sole 24 Finanza", "https://www.ilsole24ore.com/rss/finanza.xml", "IT"),
            feed("Google News", gnews('(Piazza Affari OR "Borsa di Milano" OR "FTSE MIB" OR "Wall Street" OR Bce OR Fed OR spread OR petrolio)'), "IT"),
            feed("Google News", gnews('(Euronext OR "Borsa Italiana" OR Consob OR ESMA OR CSDR OR "T+1" OR post-trade OR "Monte Titoli" OR T2S OR "regolamento titoli")', "3d"), "FIN", hours=72),
            feed("Google News", gnews('(Diasorin OR Amplifon OR Webuild OR Leonardo) (titolo OR azioni OR conti OR trimestrale OR guidance)', "2d"), "TITOLI", hours=48),
            feed("CNBC Europe", "https://www.cnbc.com/id/19794221/device/rss/rss.html", "EU", lang="en", w=0.8),
            feed("CNBC Investing", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069", "US", lang="en", w=0.8),
            feed("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "US", lang="en", w=0.7),
        ],
    },
    {
        "id": "strikes",
        "label": "Trasporti",
        "hours": 72,
        "limit": 8,
        "editorial": (
            "Trasporti: scioperi confermati o in corso (treni, aerei, trasporto pubblico) e disagi alla circolazione, "
            "con priorità per Piemonte, Lombardia, tratta Novara-Milano, Malpensa. Escludi scioperi già conclusi o "
            "revocati e notizie senza impatto pratico."
        ),
        "priority": [
            (P(r"sciopero", r"scioperi", r"scioperare"), 3.0),
            (P(r"cancellat\w*", r"soppress\w*", r"sospes\w*", r"guasto", r"blocc\w*", r"ritard\w*", r"interrott\w*",
               r"disagi\w*", r"nebbia", r"neve", r"allerta"), 2.0),
            (P(r"novara", r"rho", r"milano", r"torino", r"piemonte", r"lombardia", r"malpensa", r"linate", r"trenord",
               r"trenitalia", r"rfi", r"italo", r"enav", r"nazionale", r"generale"), 2.0),
        ],
        "feeds": [
            feed("MIT Scioperi", "https://scioperi.mit.gov.it/mit2/public/scioperi/rss", "MIT", fixed=True,
                 only=P(r"aere\w*", r"ferroviari\w*", r"generale", r"trasporto pubblico", r"piemonte", r"lombardia",
                        r"nazionale", r"novara", r"milano", r"torino", r"trenord")),
            feed("Google News", gnews('(sciopero OR scioperi) (treni OR ferrovie OR Trenord OR Trenitalia OR Italo OR aerei OR voli OR Malpensa OR Linate OR ENAV OR "trasporto pubblico")', "3d"), "IT", hours=72),
            feed("Google News", gnews('(Trenord OR Trenitalia OR RFI OR "circolazione ferroviaria") (Novara OR Rho OR Milano OR Torino OR Piemonte OR Lombardia) (ritardi OR guasto OR sospesa OR cancellati OR sciopero)', "1d"), "IT", w=1.1, hours=36),
            feed("Google News", gnews('(Malpensa OR Linate OR ENAV OR ENAC) (voli OR cancellati OR ritardi OR sciopero OR "controllori di volo")', "2d"), "EU", hours=48),
            feed("Aviation EU", gnews('("air traffic" OR "flight disruptions" OR "airport strike") Europe', "2d", lang="en"), "EU", lang="en", w=0.6, hours=48),
        ],
    },
    {
        "id": "sanity",
        "label": "Rischi",
        "hours": 36,
        "limit": 6,
        "editorial": (
            "Rischi globali: conflitti, tensioni geopolitiche, nucleare, sanzioni, attacchi informatici di grande portata, "
            "shock energetici. Solo sviluppi concreti; escludi commenti, analisi di opinione e allarmismo."
        ),
        "priority": [
            (P(r"guerra", r"war", r"ucraina", r"ukraine", r"russia\w*", r"nato", r"israele", r"israel", r"gaza", r"iran",
               r"taiwan", r"cina", r"china", r"missil\w*", r"nucle\w*", r"cessate il fuoco", r"ceasefire", r"sanzion\w*",
               r"sanctions", r"attacco", r"attack", r"cyber\w*", r"hormuz", r"mar rosso", r"red sea", r"golpe", r"coup"), 2.5),
            (P(r"energia", r"gas", r"petrolio", r"oil", r"pandemia", r"terror\w*"), 1.5),
        ],
        "feeds": [
            feed("Crisis Group", "https://www.crisisgroup.org/rss", "RISK", lang="en", w=1.2),
            feed("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "WORLD", lang="en",
                 only=P(r"war", r"conflict", r"ukraine", r"russia", r"nato", r"gaza", r"israel", r"iran", r"china",
                        r"taiwan", r"missile", r"ceasefire", r"nuclear", r"cyberattack", r"defen[cs]e")),
            feed("Google News", gnews('(guerra OR Ucraina OR "Medio Oriente" OR Gaza OR Iran OR Taiwan OR Nato OR "cessate il fuoco" OR nucleare OR "attacco informatico")'), "WORLD"),
            feed("ANSA Mondo", "https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml", "WORLD",
                 only=P(r"guerra", r"ucraina", r"russia\w*", r"nato", r"israele", r"gaza", r"iran", r"taiwan", r"cina",
                        r"missil\w*", r"nucle\w*", r"cessate il fuoco", r"sanzion\w*", r"attacco")),
        ],
    },
    {
        "id": "tech",
        "label": "Tech",
        "hours": 36,
        "limit": 6,
        "editorial": (
            "Tecnologia: disservizi in corso di servizi digitali di uso comune (Outlook/Microsoft 365, Teams, cloud, "
            "WhatsApp, banche online), attacchi informatici e vulnerabilità gravi, AI e big tech, regolamentazione. "
            "Includi sempre i disservizi in corso. Escludi recensioni, offerte e gadget."
        ),
        "priority": [
            (TECH_ALERT, 5.0),
            (TECH_SERVICES, 2.5),
            (TECH_TOPICS, 1.5),
        ],
        "penalty": [
            (P(r"offert\w*", r"sconti?", r"recension\w*", r"review", r"deals?", r"coupon", r"unboxing", r"hands-on"), -3.0),
        ],
        "topic_key": tech_topic,
        "must": tech_must,
        "must_tag": "ALERT",
        "feeds": [
            feed("Google News", gnews('(Outlook OR "Microsoft 365" OR Teams OR Azure OR AWS OR Cloudflare OR Google OR WhatsApp OR Instagram OR iCloud) (down OR outage OR disservizio OR "non funziona" OR malfunzionamento OR guasto)'), "TECH", w=1.2, hours=36),
            feed("Google News", gnews('(cybersecurity OR "attacco informatico" OR ransomware OR vulnerabilità OR "data breach" OR "violazione dati")'), "SEC"),
            feed("Google News", gnews('(OpenAI OR Anthropic OR Nvidia OR Gemini OR ChatGPT OR "intelligenza artificiale" OR Microsoft OR Apple OR Google) (annuncia OR lancia OR accordo OR regolamento OR investimento OR causa)'), "AI"),
            feed("ANSA Tecnologia", "https://www.ansa.it/sito/notizie/tecnologia/tecnologia_rss.xml", "TECH"),
            feed("BleepingComputer", "https://www.bleepingcomputer.com/feed/", "SEC", lang="en", w=0.9),
            feed("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "TECH", lang="en", w=0.8,
                 only=P(r"ai", r"security", r"apple", r"google", r"microsoft", r"outage", r"chip", r"semiconductor\w*",
                        r"linux", r"open source", r"quantum", r"regulation", r"privacy", r"cyber\w*")),
            feed("Hacker News", "https://hnrss.org/newest?points=250", "HN", lang="en", w=0.7),
        ],
    },
    {
        "id": "novara",
        "label": "Novara",
        "hours": 72,
        "limit": 7,
        "editorial": (
            "Novara e provincia: economia e imprese (investimenti, stabilimenti, chip e semiconduttori, Silicon Box), "
            "lavoro, infrastrutture e trasporti, sanità, istituzioni e viabilità con impatto sulla vita quotidiana. "
            "Escludi piccola cronaca nera senza rilevanza pubblica."
        ),
        "priority": [
            (NOVARA_CHIP, 6.0),
            (P(r"investiment\w*", r"stabiliment\w*", r"azienda", r"aziende", r"imprese", r"lavoro", r"assunzion\w*",
               r"posti di lavoro", r"fabbrica", r"insediament\w*", r"pnrr", r"upo", r"universit\w*", r"ospedale",
               r"sindaco", r"consiglio comunale", r"viabilit\w*", r"tangenziale", r"treni", r"stazione",
               r"malpensa", r"aeroporto"), 2.0),
        ],
        "penalty": [
            (P(r"furto", r"furti", r"rissa", r"truffa", r"spaccio", r"denunciat\w*", r"ubriac\w*", r"ladri"), -1.5),
        ],
        "must": novara_must,
        "must_tag": "CHIP",
        "feeds": [
            feed("Google News", gnews("Novara", "2d"), "NO", w=0.8, hours=48),
            feed("Google News", gnews('(Novara OR novarese OR "Piemonte Orientale") (azienda OR imprese OR investimento OR stabilimento OR lavoro OR assunzioni OR economia)', "3d"), "NO", hours=72),
            feed("Google News", gnews('(chip OR semiconduttori OR "Silicon Box" OR "Vulcan Project") Novara', "7d"), "NO", w=1.1, hours=168),
            feed("Corriere di Novara", "https://www.corrieredinovara.it/feed/", "NO"),
            feed("NovaraToday", "https://www.novaratoday.it/rss/", "NO"),
            feed("ANSA Piemonte", "https://www.ansa.it/piemonte/notizie/piemonte_rss.xml", "NO",
                 only=P(r"novara", r"novarese", r"borgomanero", r"arona", r"galliate", r"trecate", r"malpensa",
                        r"cameri", r"silicon box")),
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


# ──────────────────────────────────────────────────────────────────────────
# METEO / MERCATI (invariati)
# ──────────────────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────────────────
# UTILITY FEED
# ──────────────────────────────────────────────────────────────────────────
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
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            resp.raise_for_status()
            return feedparser.parse(resp.content)
        except Exception as e:
            if attempt == 1:
                print(f"Warn: failed {url}: {e}")
            else:
                time.sleep(1)
    return None


def prefetch_feeds() -> dict:
    """Scarica tutti i feed in parallelo (ogni URL una sola volta)."""
    urls = sorted({f["url"] for sec in SECTIONS for f in sec["feeds"]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_feed, urls))
    return dict(zip(urls, results))


def entry_dt(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def rel_age(dt_utc: datetime, now_utc: datetime) -> str:
    mins = max(1, int((now_utc - dt_utc).total_seconds() // 60))
    if mins < 60:
        return f"{mins}m fa"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h fa"
    return f"{hrs // 24}g fa"


# ──────────────────────────────────────────────────────────────────────────
# LINGUA
# ──────────────────────────────────────────────────────────────────────────
IT_WORDS = {
    "il", "lo", "la", "le", "gli", "un", "uno", "una", "di", "del", "della", "dei", "delle", "degli", "dello",
    "che", "per", "con", "non", "sono", "dal", "dalla", "nel", "nella", "nei", "nelle", "sul", "sulla", "anche",
    "più", "come", "dopo", "tra", "fra", "al", "alla", "ai", "agli", "ha", "hanno", "sarà", "contro", "verso",
    "ma", "se", "si", "è", "dall", "all", "nell", "sull",
}
EN_WORDS = {
    "the", "of", "and", "to", "for", "on", "with", "as", "at", "by", "from", "is", "are", "was", "were", "will",
    "after", "over", "new", "says", "say", "amid", "about", "into", "its", "that", "this", "his", "her", "has",
    "have", "be", "not", "up", "out", "how", "why", "what", "could", "would", "may", "can", "than", "but", "an",
}


def detect_lang(text: str, default: str = "it") -> str:
    words = re.findall(r"[a-zàèéìòù']+", text.lower())
    it = sum(w in IT_WORDS for w in words)
    en = sum(w in EN_WORDS for w in words)
    if en > it:
        return "en"
    if it > en:
        return "it"
    return default


# ──────────────────────────────────────────────────────────────────────────
# RACCOLTA, DEDUPLICA (con conteggio fonti), PUNTEGGIO
# ──────────────────────────────────────────────────────────────────────────
STOPWORDS = {
    "della", "delle", "dello", "degli", "nella", "nelle", "sono", "come", "dopo", "anche", "questo", "questa",
    "with", "from", "this", "that", "have", "will", "after", "over", "into", "dalla", "sulla", "contro",
}


def title_words(title: str) -> set:
    return {w for w in re.findall(r"[a-zàèéìòù0-9]{4,}", title.lower()) if w not in STOPWORDS}


def collect_items(sec: dict, now_utc: datetime, cache: dict) -> list[dict]:
    items = []
    for f in sec["feeds"]:
        parsed = cache.get(f["url"])
        if not parsed or not getattr(parsed, "entries", None):
            continue
        cutoff = now_utc - timedelta(hours=f["hours"] or sec["hours"])
        is_gn = "news.google.com" in f["url"]

        for entry in parsed.entries[:60]:
            title = clean_text(entry.get("title", ""), 400)
            link = entry.get("link", "#")
            if not title:
                continue

            source = f["label"]
            if is_gn and " - " in title:
                title, source = [s.strip() for s in title.rsplit(" - ", 1)]
            title = clean_text(title, 170)
            if not title:
                continue

            dt = entry_dt(entry)
            if dt < cutoff and not f["fixed"]:
                continue

            # Google News non fornisce un vero sommario (solo titolo + testata)
            summary = "" if is_gn else clean_text(entry.get("summary") or entry.get("description") or "", 220)
            if summary.lower().startswith(title.lower()[:28]):
                summary = ""

            blob = f"{title} {summary}".lower()
            if is_blocked(blob):
                continue
            if f["only"] and not f["only"].search(blob):
                continue

            items.append({
                "sec_id": sec["id"],
                "sec_label": sec["label"],
                "title": title,
                "link": link,
                "summary": summary,
                "image": extract_image(entry),
                "source": source,
                "tag": f["tag"],
                "dt": dt,
                "lang": detect_lang(f"{title} {summary}", f["lang"]),
                "weight": f["weight"],
                "fixed": f["fixed"],
                "blob": blob,
                "coverage": 1,
            })
    return items


def cluster_items(items: list[dict]) -> list[dict]:
    """Raggruppa le notizie sullo stesso fatto: ne tiene una sola e conta quante testate la riportano."""
    ordered = sorted(items, key=lambda x: (-x["weight"], -x["dt"].timestamp()))
    clusters = []
    for it in ordered:
        words = title_words(it["title"])
        match = None
        for c in clusters:
            inter = len(words & c["words"])
            need = max(2, min(3, len(words), len(c["words"])))
            if inter >= need and inter / max(1, min(len(words), len(c["words"]))) >= 0.6:
                match = c
                break
        if match:
            match["sources"].add(it["source"])
            rep = match["rep"]
            if not rep["image"] and it["image"]:
                rep["image"] = it["image"]
            if not rep["summary"] and it["summary"] and rep["lang"] == it["lang"]:
                rep["summary"] = it["summary"]
        else:
            clusters.append({"rep": it, "words": words, "sources": {it["source"]}})
    out = []
    for c in clusters:
        c["rep"]["coverage"] = len(c["sources"])
        c["rep"]["sources"] = set(c["sources"])
        out.append(c["rep"])
    return out


def score_item(it: dict, sec: dict, now_utc: datetime) -> float:
    if it["fixed"]:
        recency = 1.5
    else:
        age_h = max(0.0, (now_utc - it["dt"]).total_seconds() / 3600)
        recency = 3.0 * math.exp(-age_h / max(6.0, sec["hours"] / 2.5))
    prio = min(10.0, sum(pts for rx, pts in sec.get("priority", []) if rx.search(it["blob"])))
    pen = sum(pts for rx, pts in sec.get("penalty", []) if rx.search(it["blob"]))
    cov = min(4.5, 1.5 * (it["coverage"] - 1))      # più testate indipendenti = più probabile rilevanza
    src = (it["weight"] - 1.0) * 2
    extra = (0.3 if it["summary"] else 0.0) + (0.2 if it["image"] else 0.0)
    if it["lang"] == "en" and not has_translator():
        extra -= 0.8                                  # senza traduttore, meglio le fonti in italiano
    return recency + prio + pen + cov + src + extra


def rank_section(sec: dict, now_utc: datetime, cache: dict) -> list[dict]:
    cands = cluster_items(collect_items(sec, now_utc, cache))

    # Titoli diversi sullo stesso evento (es. disservizio Outlook): contano come più testate
    topic_fn = sec.get("topic_key")
    if topic_fn:
        groups = {}
        for it in cands:
            it["topic"] = topic_fn(it)
            if it["topic"]:
                groups.setdefault(it["topic"], set()).update(it["sources"])
        for it in cands:
            if it["topic"]:
                it["coverage"] = max(it["coverage"], len(groups[it["topic"]]))

    must_fn = sec.get("must")
    for it in cands:
        it["score"] = score_item(it, sec, now_utc)
        it["must"] = bool(must_fn and must_fn(it))
        if it["must"] and sec.get("must_tag"):
            it["tag"] = sec["must_tag"]
    cands.sort(key=lambda x: x["score"], reverse=True)
    return cands


# ──────────────────────────────────────────────────────────────────────────
# AI (Gemini): solo per SCEGLIERE e TRADURRE. Non riscrive le notizie.
# ──────────────────────────────────────────────────────────────────────────
def gemini(prompt: str, as_json: bool = True, timeout: int = 45, retries: int = 2):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    gen_cfg = {"temperature": 0.1, "thinkingConfig": {"thinkingBudget": 0}}
    if as_json:
        gen_cfg["responseMimeType"] = "application/json"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}

    for attempt in range(retries + 1):
        try:
            r = requests.post(
                GEMINI_URL,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if r.status_code == 400 and "thinkingConfig" in gen_cfg:
                gen_cfg.pop("thinkingConfig")   # alcuni modelli non accettano thinkingBudget
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            if not as_json:
                return text
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
            return json.loads(text)
        except Exception as e:
            print(f"Warn: Gemini ({attempt + 1}/{retries + 1}): {e}")
            if attempt < retries:
                time.sleep(2)
    return None


def ai_pick(sec: dict, pool: list[dict]):
    """L'AI sceglie gli id delle notizie che contano (testo originale intatto)."""
    payload = [
        {
            "id": i,
            "lang": it["lang"],
            "fonti": it["coverage"],
            "titolo": it["title"],
            "testo": it["summary"][:160],
        }
        for i, it in enumerate(pool)
    ]
    prompt = (
        "Sei il caporedattore di un brief informativo personale, sobrio e fattuale.\n"
        f"Lettore: {READER_PROFILE}\n"
        f"Sezione: {sec['label']}. Criterio editoriale: {sec['editorial']}\n\n"
        f"Dai candidati qui sotto scegli le notizie che contano davvero oggi, al massimo {sec['limit']}, "
        "in ordine di importanza. Meglio poche notizie rilevanti che molte irrilevanti.\n"
        "Regole:\n"
        "- Usa solo gli id forniti. Non inventare, non riscrivere, non unire notizie.\n"
        "- Preferisci fatti concreti (decisioni, numeri, eventi, disservizi in corso) a opinioni, rubriche, "
        "curiosità o titoli acchiappaclic.\n"
        "- Se più candidati raccontano lo stesso fatto, scegline uno solo.\n"
        "- Il campo 'fonti' indica quante testate diverse riportano la notizia: più fonti = più probabile rilevanza.\n"
        "- Includi sempre i disservizi, gli scioperi e gli allarmi in corso che possono toccare il lettore.\n"
        "- In 'non_it' elenca, tra gli id scelti, quelli il cui titolo NON è scritto in italiano.\n"
        'Rispondi SOLO con JSON valido: {"ids": [id in ordine di importanza], "non_it": [id]}\n\n'
        "Candidati:\n" + json.dumps(payload, ensure_ascii=False)
    )
    data = gemini(prompt)
    if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
        return None
    seen, chosen = set(), []
    for v in data["ids"]:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(pool) and i not in seen:
            seen.add(i)
            chosen.append(pool[i])
    for v in data.get("non_it") or []:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if i in seen:
            pool[i]["lang"] = "en"
    return chosen[: sec["limit"]] or None


def heuristic_pick(sec: dict, ranked: list[dict]) -> list[dict]:
    """Selezione senza AI: punteggio + massimo qualche articolo per testata."""
    cap = 3 if sec["limit"] >= 6 else 2
    chosen, skipped, per_src = [], [], {}
    for it in ranked:
        if len(chosen) >= sec["limit"]:
            break
        if len(chosen) >= MIN_ITEMS and it["score"] < 1.0:
            continue                                  # solo ciò che conta: scarta il rumore
        n = per_src.get(it["source"], 0)
        if n >= cap:
            skipped.append(it)
            continue
        chosen.append(it)
        per_src[it["source"]] = n + 1
    for it in skipped:
        if len(chosen) >= sec["limit"]:
            break
        chosen.append(it)
    chosen.sort(key=lambda x: x["score"], reverse=True)
    return chosen


def select_section(sec: dict, ranked: list[dict]) -> list[dict]:
    if not ranked:
        return []

    chosen = None
    if has_translator():
        chosen = ai_pick(sec, ranked[:AI_CANDIDATES])

    if chosen:
        floor = min(MIN_ITEMS, len(ranked))
        if len(chosen) < floor:
            present = {id(x) for x in chosen}
            for it in heuristic_pick(sec, ranked):
                if id(it) not in present:
                    chosen.append(it)
                    if len(chosen) >= floor:
                        break
    else:
        chosen = heuristic_pick(sec, ranked)

    # Le notizie "must" (disservizi diffusi, investimento chip a Novara) non possono essere scartate
    present = {id(x) for x in chosen}
    forced = [it for it in ranked if it["must"] and id(it) not in present][:2]

    # Stesso evento raccontato con titoli diversi (es. disservizio Outlook): ne resta uno solo
    result, seen_topics = [], set()
    for it in forced + chosen:
        topic = it.get("topic")
        if topic and topic in seen_topics:
            continue
        if topic:
            seen_topics.add(topic)
        result.append(it)
    return result[: sec["limit"]]


def translate_to_italian(items: list[dict]) -> None:
    """Traduzione fedele dei titoli/sommari non italiani. Senza chiave API restano nell'originale."""
    todo = [it for it in items if it["lang"] == "en"]
    if not todo or not has_translator():
        return
    for start in range(0, len(todo), 25):
        chunk = todo[start:start + 25]
        payload = [{"id": i, "title": it["title"], "summary": it["summary"]} for i, it in enumerate(chunk)]
        prompt = (
            "Traduci in italiano i seguenti titoli e sommari di notizie.\n"
            "Regole: traduzione fedele e letterale; non riassumere, non aggiungere né togliere informazioni, "
            "non cambiare il tono; mantieni invariati nomi propri, sigle, cifre e valute. "
            "Se un testo è già in italiano restituiscilo identico. Se 'summary' è vuoto lascialo vuoto.\n"
            'Rispondi SOLO con JSON valido: {"items": [{"id": 0, "title": "...", "summary": "..."}]}\n\n'
            + json.dumps(payload, ensure_ascii=False)
        )
        data = gemini(prompt)
        rows = data.get("items") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            try:
                it = chunk[int(row["id"])]
                new_title = clean_text(str(row.get("title") or ""), 190)
                new_sum = clean_text(str(row.get("summary") or ""), 240) if it["summary"] else ""
            except Exception:
                continue
            if not new_title:
                continue
            if new_title.lower() != it["title"].lower():
                it["source"] = f"{it['source']} (trad.)"
            it["title"] = new_title
            it["summary"] = new_sum
            it["lang"] = "it"


# ──────────────────────────────────────────────────────────────────────────
# "TODAY IN 15 SECONDS"
# ──────────────────────────────────────────────────────────────────────────
def build_brief_bullets(collected: dict, strike_alert: bool) -> list[tuple[str, str]]:
    if has_translator():
        lines = []
        for sec_id, items in collected.items():
            for it in items[:4]:
                extra = f" — {it['summary'][:100]}" if it["summary"] else ""
                lines.append(f"[{sec_id.upper()}] {it['title']}{extra}")
        prompt = (
            "Scrivi ESATTAMENTE 4 righe brevissime (max 11 parole l'una) in italiano come sintesi esecutiva "
            "su: 1) Italia governo/economia, 2) Mercati, 3) Trasporti/Rischi, 4) Tech/Novara.\n"
            "Usa SOLO fatti presenti nei titoli forniti: nessun numero, nome o dettaglio che non compaia. "
            "Se per un tema non ci sono notizie rilevanti scrivilo in modo esplicito (tono [GREEN]).\n"
            "Nella riga 4 dai priorità a un eventuale disservizio in corso di servizi digitali.\n"
            "Inizia ogni riga con [RED], [AMBER] o [GREEN] in base alla criticità/tono.\n"
            "Nessuna introduzione o testo extra.\n\n" + "\n".join(lines)
        )
        raw = gemini(prompt, as_json=False, timeout=30)
        if raw:
            parsed = []
            for ln in raw.splitlines():
                m = re.match(r"\[(RED|AMBER|GREEN)\]\s*(.+)", ln.strip(), re.I)
                if m:
                    parsed.append((m.group(1).lower(), m.group(2).strip()))
            if len(parsed) >= 3:
                return parsed[:4]

    # Fallback deterministico (nessuna chiave AI necessaria)
    bullets = []
    if collected["italy"]:
        bullets.append(("amber", collected["italy"][0]["title"]))
    if collected["markets"]:
        bullets.append(("green", collected["markets"][0]["title"]))
    if strike_alert and collected["strikes"]:
        bullets.append(("red", f"Trasporti: {collected['strikes'][0]['title']}"))
    else:
        bullets.append(("green", "Trasporti e voli: nessuna criticità immediata rilevata"))
    tech_alert = next((it for it in collected["tech"] if it["must"]), None)
    if tech_alert:
        bullets.append(("amber", f"Tech: {tech_alert['title']}"))
    elif collected["novara"]:
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
      “__EDITION_NOTE__”
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


def stable_id(text: str) -> str:
    """ID stabile tra una build e l'altra (hash() di Python cambia a ogni esecuzione)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def build_html():
    now_utc = datetime.now(timezone.utc)
    now_it = now_utc.astimezone(TZ_IT)

    weather_icon, weather_temp = fetch_weather_novara()
    market_cards = fetch_markets()

    # 1) Raccolta + deduplica + punteggio  2) selezione (AI se disponibile, altrimenti euristica)
    cache = prefetch_feeds()
    collected = {}
    for sec in SECTIONS:
        ranked = rank_section(sec, now_utc, cache)
        collected[sec["id"]] = select_section(sec, ranked)
        print(f"[{sec['id']}] candidati={len(ranked)} selezionati={len(collected[sec['id']])}")
        if has_translator():
            time.sleep(0.5)

    # 3) Traduzione fedele di ciò che non è in italiano
    all_chosen = [it for items in collected.values() for it in items]
    translate_to_italian(all_chosen)
    for it in all_chosen:
        it["age"] = rel_age(it["dt"], now_utc)

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

    # Top Stories = le 2 notizie più importanti di ogni sezione; il resto va in Late News
    all_articles = []
    all_dts = []
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
        for pos, it in enumerate(collected[sec["id"]]):
            idx = len(all_articles)
            cov = it.get("coverage", 1)
            source_label = f"{it['source']} +{cov - 1}" if cov > 1 else it["source"]
            all_articles.append({
                "sec_id": it["sec_id"],
                "sec_label": it["sec_label"],
                "title": it["title"],
                "link": it["link"],
                "summary": it["summary"],
                "image": it["image"],
                "source": source_label,
                "tag": it["tag"],
                "age": it["age"],
            })
            all_dts.append(it["dt"])
            (top_indices if pos < 2 else late_indices).append(idx)

    late_indices.sort(key=lambda i: all_dts[i], reverse=True)

    # Top Stories HTML
    top_cards = []
    for idx in top_indices:
        art = all_articles[idx]
        tone_cls, dot_col, badge_txt = tone_map.get(art["sec_id"], ("tone-blue", "var(--cyan)", art["sec_label"]))
        if art["sec_id"] == "strikes" and strike_alert:
            tone_cls, dot_col, badge_txt = "tone-red", "var(--red)", "SCIOPERO / DISAGI"
        uid = f"t-{stable_id(art['link'])}"
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
        uid = f"l-{stable_id(art['link'])}"
        late_rows.append(f"""
        <div class="late-row" data-idx="{idx}" data-sec="{art['sec_id']}" data-uid="{uid}">
          <span class="late-age">{html.escape(art['age'])}</span>
          <span class="late-title">{html.escape(art['title'])}</span>
          <span class="late-cat">{html.escape(art['sec_label'])}</span>
        </div>""")

    days_it = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    months_it = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
    date_label = f"{days_it[now_it.weekday()]} {now_it.day} {months_it[now_it.month - 1]}"
    stability_pct = 55 if strike_alert else 90
    stability_label = "Attenzione" if strike_alert else "Regolare"

    if has_translator():
        edition_note = (
            "Aggiornato ogni ora 08:00–20:00 (Milano). Fonti dirette e titoli originali: "
            "l'AI sceglie e traduce, non riscrive."
        )
    else:
        edition_note = (
            "Aggiornato ogni ora 08:00–20:00 (Milano). Fonti dirette e titoli originali, "
            "ordinati per rilevanza."
        )

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
        .replace("__EDITION_NOTE__", html.escape(edition_note))
        .replace("__ARTICLES_JSON__", articles_json)
    )

    os.makedirs("dist", exist_ok=True)
    with open("dist/index.html", "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    build_html()