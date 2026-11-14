import calendar
import hashlib
import html
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# CONFIGURAZIONE & PROFILO
# ──────────────────────────────────────────────────────────────────────────
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

READER_PROFILE = (
    "professionista italiano dei servizi finanziari (post-trade), vive a Novara e fa il pendolare "
    "in treno verso Milano; segue economia italiana, mercati, trasporti e tecnologia di uso quotidiano."
)

AI_CANDIDATES = 32   # Candidati analizzati dall'AI per ciascuna sezione
MIN_ITEMS = 5        # Minimo garantito di notizie per sezione


def has_translator() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def P(*terms: str) -> "re.Pattern":
    """Regex case-insensitive con confini di parola."""
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)


def norm_title_key(title: str) -> str:
    """Chiave normalizzata del titolo: lowercase e rimozione caratteri non alfanumerici."""
    return re.sub(r"[^a-z0-9àèéìòù]", "", title.lower())


def clean_html(raw_html: str) -> str:
    """Rimuove i tag HTML e decodifica le entità per normalizzare il testo."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    return html.unescape(soup.get_text(separator=" "))


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
    "meteo weekend", "previsioni meteo",
)


def is_blocked(blob: str) -> bool:
    return any(s in blob for s in BLOCK_SUBSTR) or bool(BLOCK_WORDS.search(blob))


# ──────────────────────────────────────────────────────────────────────────
# REGEX E REGOLE SPECIFICHE
# ──────────────────────────────────────────────────────────────────────────
TECH_OUTAGE = P(
    r"outage", r"outages", r"disservizi?", r"down", r"non funziona", r"non funzionano",
    r"malfunzionament\w*", r"guast\w*", r"blackout", r"data breach", r"attacco informatico",
    r"ransomware", r"vulnerabilit\w*", r"zero-day", r"cve-\d+"
)

NOVARA_CHIP = P(r"chip", r"microchip", r"chiplet", r"semiconduttor\w*", r"silicon box", r"vulcan project")

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
    if not TECH_OUTAGE.search(it["blob"]):
        return None
    for name, rx in SERVICE_FAMILIES:
        if rx.search(it["blob"]):
            return name
    return None


def tech_must(it) -> bool:
    return bool(it.get("topic")) and it["coverage"] >= 3


def novara_must(it) -> bool:
    return bool(NOVARA_CHIP.search(it["blob"]))


# ──────────────────────────────────────────────────────────────────────────
# DEFINIZIONE FONTI E SEZIONI
# ──────────────────────────────────────────────────────────────────────────
def feed(label, url, tag, lang="it", w=1.0, only=None, hours=None, fixed=False):
    return {
        "label": label, "url": url, "tag": tag, "lang": lang, "weight": w,
        "only": only, "hours": hours, "fixed": fixed,
    }


def gnews(query: str, when: str = "1d", lang: str = "it") -> str:
    q = quote_plus(f"{query} when:{when}")
    hl, gl, ceid = ("it", "IT", "IT:it") if lang == "it" else ("en", "US", "US:en")
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


SECTIONS = [
    {
        "id": "italy",
        "label": "Italia",
        "hours": 36,
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
            feed("Il Post", "https://www.ilpost.it/feed/", "IT", w=0.95,
                 only=P(r"governo", r"parlamento", r"manovra", r"economia", r"pil", r"fisco", r"lavoro", r"bce",
                        r"decret\w*", r"meloni", r"giorgetti", r"inflazione", r"riforma\w*", r"referendum")),
            feed("Google News", gnews('(governo OR parlamento OR manovra OR "legge di bilancio" OR decreto OR Mattarella OR Meloni OR "consiglio dei ministri")'), "IT", w=0.9),
            feed("Google News", gnews('(Istat OR Bankitalia OR Bce OR inflazione OR "conti pubblici" OR fisco OR pensioni)'), "IT", w=0.9),
        ],
    },
    {
        "id": "markets",
        "label": "Mercati",
        "hours": 36,
        "limit": 9,
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
            feed("Google News", gnews('(Diasorin OR Amplifon OR Webuild OR Leonardo) (titolo OR azioni OR conti OR trimestrale OR guidance)', "3d"), "TITOLI", hours=72),
            feed("CNBC Europe", "https://www.cnbc.com/id/19794221/device/rss/rss.html", "EU", lang="en", w=0.85),
            feed("CNBC Investing", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069", "US", lang="en", w=0.8),
            feed("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "US", lang="en", w=0.75),
        ],
    },
    {
        "id": "strikes",
        "label": "Trasporti",
        "hours": 96,
        "limit": 8,
        "editorial": (
            "Trasporti: scioperi proclamati, confermati o in corso (treni, aerei, trasporto pubblico) e disagi alla circolazione, "
            "con priorità per Piemonte, Lombardia, tratta Novara-Milano, Malpensa. Escludi scioperi già conclusi o "
            "revocati e notizie senza impatto pratico."
        ),
        "priority": [
            (P(r"sciopero", r"scioperi", r"scioperare", r"agitazione sindacale"), 3.5),
            (P(r"cancellat\w*", r"soppress\w*", r"sospes\w*", r"guasto", r"blocc\w*", r"ritard\w*", r"interrott\w*",
               r"disagi\w*", r"nebbia", r"neve", r"allerta", r"traffico", r"autostrad\w*", r"a4"), 2.0),
            (P(r"novara", r"rho", r"milano", r"torino", r"piemonte", r"lombardia", r"malpensa", r"linate", r"trenord",
               r"trenitalia", r"rfi", r"italo", r"enav", r"nazionale", r"generale", r"ferroviari\w*", r"aere\w*"), 2.0),
        ],
        "feeds": [
            feed("MIT Scioperi", "https://scioperi.mit.gov.it/mit2/public/scioperi/rss", "MIT", fixed=True, w=1.25),
            feed("Google News", gnews('(sciopero OR scioperi) (treni OR ferrovie OR Trenord OR Trenitalia OR Italo OR aerei OR voli OR Malpensa OR Linate OR ENAV OR "trasporto pubblico")', "5d"), "IT", w=1.1, hours=120),
            feed("Google News", gnews('(Trenord OR Trenitalia OR RFI OR "circolazione ferroviaria" OR A4) (Novara OR Rho OR Milano OR Torino OR Piemonte OR Lombardia) (ritardi OR guasto OR sospesa OR cancellati OR sciopero OR lavori)', "2d"), "IT", w=1.15, hours=48),
            feed("Google News", gnews('(Malpensa OR Linate OR ENAV OR ENAC) (voli OR cancellati OR ritardi OR sciopero OR "controllori di volo" OR passeggeri)', "3d"), "EU", hours=72),
            feed("Aviation EU", gnews('("air traffic" OR "flight disruptions" OR "airport strike" OR "rail strike") Europe', "3d", lang="en"), "EU", lang="en", w=0.75, hours=72),
        ],
    },
    {
        "id": "sanity",
        "label": "Rischi",
        "hours": 48,
        "limit": 7,
        "editorial": (
            "Rischi globali: conflitti, tensioni geopolitiche, nucleare, sanzioni, attacchi informatici di grande portata, "
            "shock energetici e sicurezza europea. Solo sviluppi concreti; escludi commenti e allarmismo."
        ),
        "priority": [
            (P(r"guerra", r"war", r"ucraina", r"ukraine", r"russia\w*", r"nato", r"israele", r"israel", r"gaza", r"iran",
               r"taiwan", r"cina", r"china", r"missil\w*", r"nucle\w*", r"cessate il fuoco", r"ceasefire", r"sanzion\w*",
               r"sanctions", r"attacco", r"attack", r"cyber\w*", r"hormuz", r"mar rosso", r"red sea", r"golpe", r"coup",
               r"difesa", r"defen[cs]e", r"tregua", r"raid"), 2.8),
            (P(r"energia", r"gas", r"petrolio", r"oil", r"pandemia", r"terror\w*", r"dazi", r"tariffs"), 1.8),
        ],
        "feeds": [
            feed("ANSA Mondo", "https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml", "WORLD", w=1.05,
                 only=P(r"guerra", r"ucraina", r"russia\w*", r"nato", r"israele", r"gaza", r"iran", r"taiwan", r"cina",
                        r"missil\w*", r"nucle\w*", r"cessate il fuoco", r"sanzion\w*", r"attacco", r"difesa", r"raid", r"tregua", r"energia", r"gas")),
            feed("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "WORLD", lang="en", w=1.0,
                 only=P(r"war", r"conflict", r"ukraine", r"russia", r"nato", r"gaza", r"israel", r"iran", r"china",
                        r"taiwan", r"missile", r"ceasefire", r"nuclear", r"cyberattack", r"defen[cs]e", r"sanctions", r"energy")),
            feed("Google News", gnews('(guerra OR Ucraina OR "Medio Oriente" OR Gaza OR Iran OR Taiwan OR Nato OR sanzioni)'), "WORLD", w=1.0),
        ],
    },
    {
        "id": "tech",
        "label": "Tech",
        "hours": 36,
        "limit": 8,
        "editorial": (
            "Tecnologia, infrastrutture digitali e intelligenza artificiale: evoluzione dei modelli AI (OpenAI, Anthropic, Google), "
            "cloud computing, semiconduttori e disservizi critici su larga scala (outage di servizi essenziali)."
        ),
        "priority": [
            (P(r"openai", r"anthropic", r"nvidia", r"chatgpt", r"gemini", r"intelligenza artificiale", r"ai act",
               r"semiconduttor\w*", r"chip", r"cybersecurity"), 3.5),
            (TECH_OUTAGE, 3.0),
            (P(r"cloud", r"aws", r"azure", r"microsoft", r"apple", r"linux", r"open source"), 2.0),
        ],
        "feeds": [
            feed("Google News Tech", gnews('("intelligenza artificiale" OR OpenAI OR Anthropic OR NVIDIA OR semiconduttori OR cybersecurity)'), "TECH", w=1.1),
            feed("Google News Outage", gnews('(outage OR disservizio OR down OR "non funziona" OR Tim OR Vodafone OR Microsoft)'), "OUTAGE", w=1.2),
            feed("Hacker News Top", "https://news.ycombinator.com/rss", "HN", lang="en", w=0.8),
            feed("The Verge", "https://www.theverge.com/rss/index.xml", "VERGE", lang="en", w=0.75),
        ],
    },
    {
        "id": "novara",
        "label": "Novara",
        "hours": 72,
        "limit": 6,
        "editorial": (
            "Notizie locali su Novara e territorio circostante, con particolare attenzione a investimenti industriali strategici "
            "(polo semiconduttori / Silicon Box / Vulcan Project), viabilità, cantieri e decisioni amministrative locali."
        ),
        "priority": [
            (NOVARA_CHIP, 4.0),
            (P(r"novara", r"comune di novara", r"provincia di novara", r"sindaco", r"cantier\w*", r"viabilita", r"ospedale maggiore"), 2.5),
        ],
        "feeds": [
            feed("Google News Novara", gnews('(Novara OR "Silicon Box" OR "Vulcan Project" OR semiconduttori Novara)'), "NOVARA", w=1.2, hours=72),
            feed("Google News Cronaca Novara", gnews('(Novara cronaca OR comune Novara OR viabilità Novara)'), "NOVARA", w=1.0, hours=72),
        ],
    },
]


# ──────────────────────────────────────────────────────────────────────────
# PARSING SICURO E MOTORE DI SCORING
# ──────────────────────────────────────────────────────────────────────────
def parse_feed_safely(feed_conf: dict) -> list[dict]:
    """Scarica e analizza un singolo feed RSS con gestione robusta degli errori e timeout."""
    items = []
    try:
        resp = requests.get(feed_conf["url"], headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return []
        
        parsed_feed = feedparser.parse(resp.content)
        now = datetime.now(timezone.utc)
        max_hours = feed_conf.get("hours", 36)

        for entry in parsed_feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue

            raw_summary = entry.get("summary") or entry.get("description") or ""
            summary = clean_html(raw_summary)
            blob = f"{title} {summary}".lower()

            if is_blocked(blob):
                continue

            pub_date = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass
            
            if not pub_date:
                pub_date = now

            age_hours = (now - pub_date).total_seconds() / 3600.0
            if age_hours > max_hours and not feed_conf.get("fixed", False):
                continue

            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "blob": blob,
                "source": feed_conf["label"],
                "tag": feed_conf["tag"],
                "weight": feed_conf.get("weight", 1.0),
                "pub_date": pub_date,
                "age_hours": age_hours,
                "only": feed_conf.get("only")
            })
    except Exception:
        pass
    return items


def compute_item_score(item: dict, section: dict) -> float:
    """Calcola lo score composito basato su peso fonte, priorità, copertura e freschezza."""
    score = item["weight"] * 10.0

    if item.get("only") and not item["only"].search(item["blob"]):
        return 0.0

    matched_priority = False
    for rx, multiplier in section.get("priority", []):
        if rx.search(item["blob"]):
            score *= multiplier
            matched_priority = True
            break

    if not matched_priority and section.get("strict_priority", False):
        score *= 0.3

    coverage = item.get("coverage", 1)
    if coverage > 1:
        score *= (1.0 + (coverage * 0.15))

    age = item.get("age_hours", 0)
    decay_factor = max(0.5, 1.0 - (age / (section.get("hours", 36) * 1.5)))
    score *= decay_factor

    return round(score, 3)


def deduplicate_and_rank_items(all_items: list[dict], section: dict) -> list[dict]:
    """Deduplica notizie simili e calcola la copertura cross-source."""
    grouped = {}
    
    for it in all_items:
        key = norm_title_key(it["title"])
        topic = tech_topic(it) if section["id"] == "tech" else key
        group_key = topic if topic else key

        if group_key not in grouped:
            grouped[group_key] = {
                "item": it,
                "coverage": 1,
                "sources": {it["source"]}
            }
        else:
            grouped[group_key]["sources"].add(it["source"])
            grouped[group_key]["coverage"] += 1
            if it["pub_date"] > grouped[group_key]["item"]["pub_date"]:
                grouped[group_key]["item"] = it

    processed_items = []
    for _, data in grouped.items():
        it = data["item"]
        it["coverage"] = data["coverage"]
        it["sources_list"] = list(data["sources"])
        
        it["score"] = compute_item_score(it, section)
        if it["score"] > 0:
            processed_items.append(it)

    processed_items.sort(key=lambda x: x["score"], reverse=True)
    return processed_items[:AI_CANDIDATES]


# Esempio di esecuzione del pipeline di raccolta feed per sezione
if __name__ == "__main__":
    print(f"Avvio MyBrief Engine per il profilo: {READER_PROFILE}")
    for sec in SECTIONS:
        print(f"\nElaborazione sezione: {sec['label']} ({sec['id']})")
        section_items = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(parse_feed_safely, f): f for f in sec["feeds"]}
            for future in as_completed(futures):
                section_items.extend(future.result())
        
        ranked = deduplicate_and_rank_items(section_items, sec)
        print(f"-> Articoli filtrati e rankati: {len(ranked)} (Target limit: {sec['limit']})")
