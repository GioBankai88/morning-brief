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


def norm_title_key(title: str) -> str:
    """Chiave normalizzata del titolo: lowercase e rimozione caratteri non alfanumerici."""
    return re.sub(r"[^a-z0-9àèéìòù]", "", title.lower())


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
    r"amd", r"intel", r"tsmc", r"arm", r"claude", r"copilot", r"robotica", r"robotics",
    r"privacy", r"open source", r"linux", r"kubernetes", r"docker", r"github",
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
    """Disservizio di un servizio digitale diffuso, riportato da almeno 4 testate e con topic identificato."""
    return bool(it.get("topic")) and it["coverage"] >= 4


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
            "rilevanti, regolamentazione finanziaria
