"""
crypto_news_alert.py — Bot de noticias cripto: alertas en Telegram + periódico HTML local.

Fuentes (sin API keys):
  - Posts de Trump en Truth Social (espejo RSS de trumpstruth.org)
  - Anuncios de nuevos listings en Binance
  - RSS de medios cripto: CoinDesk, Cointelegraph, The Block, Decrypt, Blockworks
  - Canales públicos de Telegram (versión web t.me/s/): Wu Blockchain, Watcher Guru
  - Casa Blanca y SEC (RSS oficiales; solo cuentan si hablan de cripto)
  - Google News: '"<político/famoso>" crypto when:1d', una búsqueda por persona y hora como mucho
  - Congreso: operaciones cripto declaradas (STOCK Act, Cámara), una vez al día

Cada noticia recibe:
  - una PUNTUACIÓN (fuente + catalizadores + monedas mencionadas)
  - una ETIQUETA:
        🔥 A SEGUIR        puntuación alta y el precio aún no se ha movido
        👀 ÉCHALE UN OJO   puntuación media
        ⚠️ YA SE HA MOVIDO  la moneda ya subió/bajó fuerte → probablemente llegas tarde
        · Breve            poco relevante, solo sale en el periódico
  - precio actual y cambio 1h / 24h de las monedas detectadas

Telegram: solo recibe "A seguir" (TELEGRAM_LABELS) y la edición de la mañana a las 08:00.
Periódico: periodico.html se regenera en cada ciclo (ábrelo en el navegador, se refresca solo).

Uso (PowerShell):
  pip install requests feedparser
  $env:TELEGRAM_TOKEN="123:abc"
  $env:TELEGRAM_CHAT_ID="-100123456"
  python crypto_news_alert.py              # bucle continuo
  python crypto_news_alert.py --dry        # sin Telegram, alertas por consola
  python crypto_news_alert.py --test       # mensaje de prueba a Telegram y sale
  python crypto_news_alert.py --periodico  # solo regenera periodico.html desde la base de datos
  python crypto_news_alert.py --repuntuar  # repuntúa las noticias guardadas con las reglas actuales
  python crypto_news_alert.py --once       # una sola pasada y sale (lo que usa GitHub Actions)
"""

import calendar
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import requests

# ───────────────────────── CONFIG ─────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_SECONDS = 60          # cada cuánto revisar las fuentes
ALERT_THRESHOLD = 5        # desde aquí: "Échale un ojo" (+ Telegram)
HOT_THRESHOLD = 10         # desde aquí: "A seguir"
# Qué etiquetas llegan a Telegram al momento (el resto solo sale en el periódico).
# Por defecto solo lo más destacado; añade "ojo" o "movido" si quieres más avisos.
TELEGRAM_LABELS = {"seguir"}
BRIEF_MIN = 2              # mínimo para salir como "Breve" en el periódico
MOVED_1H_PCT = 10          # si la moneda ya se movió ±10% en 1h → "Ya se ha movido"
MOVED_24H_PCT = 25         # ... o ±25% en 24h
PERIODICO_HOURS = 48       # cuántas horas de noticias muestra el periódico
MAX_AGE_HOURS = 48         # noticias publicadas hace más de esto se descartan
ALERT_MAX_AGE_HOURS = 6    # noticias publicadas hace más de esto no se avisan por Telegram (solo periódico)
MIN_DEX_LIQUIDITY = 100_000  # ticker desconocido con menos liquidez en DexScreener: se ignora
NO_HOT_COINS = ("BTC", "ETH", "USDC", "USDT")  # siempre tienen muchas noticias: no cuentan como "tema del día"
TZ = ZoneInfo("Europe/Berlin")          # todo lo que se muestra va en hora de Alemania
EDICION_HORA = 8                        # edición de la mañana por Telegram (hora de Alemania)
AGENDA_PATH = os.getenv("AGENDA_PATH", "agenda.json")
AGENDA_DIAS = 14                        # cuántos días de agenda muestra el periódico

DB_PATH = os.getenv("DB_PATH", "seen_news.db")
PERIODICO_PATH = os.getenv("PERIODICO_PATH", "periodico.html")
UA = {"User-Agent": "Mozilla/5.0 (crypto-news-alert)"}

# Fuentes RSS: (nombre, url, peso base)
RSS_FEEDS = [
    ("Trump (Truth Social)", "https://trumpstruth.org/feed", 3),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", 0),
    ("Cointelegraph", "https://cointelegraph.com/rss", 0),
    ("The Block", "https://www.theblock.co/rss.xml", 0),
    ("Decrypt", "https://decrypt.co/feed", 0),
    ("Blockworks", "https://blockworks.co/feed", 0),
    # Gobierno de EE. UU.: solo cuentan si hablan de cripto (GOV_SOURCES). El Tesoro no tiene RSS de notas
    # de prensa que funcione (home.treasury.gov/rss.xml solo trae FAQs viejas): sus noticias llegan por Google News.
    ("Casa Blanca", "https://www.whitehouse.gov/news/feed/", 3),
    ("SEC", "https://www.sec.gov/news/pressreleases.rss", 2),
]
GOV_SOURCES = ("Casa Blanca", "SEC")

# Canales públicos de Telegram (versión web t.me/s/, sin login): (nombre, usuario del canal, peso base)
TELEGRAM_CHANNELS = [
    ("Wu Blockchain", "wublockchainenglish", 1),
    ("Watcher Guru", "WatcherGuru", 1),
]

# Google News: una búsqueda '"<persona>" crypto when:1d' por político/famoso, como mucho una vez por hora
GNEWS_URL = "https://news.google.com/rss/search"
GNEWS_EVERY = 3600        # segundos entre búsquedas de la misma persona (hora guardada en la tabla meta)
GNEWS_PER_QUERY = 15      # entradas que se miran de cada búsqueda

# Watchlist: símbolo -> (id CoinGecko, [alias / nombres del proyecto])
WATCHLIST = {
    "BTC":  ("bitcoin", ["bitcoin", "btc"]),
    "ETH":  ("ethereum", ["ethereum", "ether", "eth"]),
    "SOL":  ("solana", ["solana", "sol"]),
    "HYPE": ("hyperliquid", ["hyperliquid", "hype", "trade.xyz", "tradexyz", "hyperevm"]),
    "UNI":  ("uniswap", ["uniswap", "uni"]),
    "XRP":  ("ripple", ["xrp", "ripple"]),
    "DOGE": ("dogecoin", ["dogecoin", "doge"]),
    "BNB":  ("binancecoin", ["bnb"]),
    "LINK": ("chainlink", ["chainlink"]),
    "AAVE": ("aave", ["aave"]),
    "ENA":  ("ethena", ["ethena"]),
    "ONDO": ("ondo-finance", ["ondo"]),
    "PUMP": ("pump-fun", ["pump.fun", "pumpfun"]),
    "WLFI": ("world-liberty-financial", ["world liberty", "wlfi"]),
    "TRUMP": ("official-trump", ["$trump", "trump coin", "trump memecoin"]),
    "SUI":  ("sui", ["sui"]),
    "ARB":  ("arbitrum", ["arbitrum"]),
    "CRV":  ("curve-dao-token", ["curve finance", "crv"]),
    "LDO":  ("lido-dao", ["lido"]),
    "JUP":  ("jupiter-exchange-solana", ["jupiter"]),
    # Proyectos de "TradFi / bancos / tokenización" (tipo Quant)
    "QNT":  ("quant-network", ["quant", "qnt", "quant network", "overledger"]),
    "HBAR": ("hedera-hashgraph", ["hedera", "hbar"]),
    "XLM":  ("stellar", ["stellar", "xlm"]),
    "ALGO": ("algorand", ["algorand"]),
    # Stablecoins: cuentan como moneda mencionada, pero el aviso no lleva precio (siempre ~1 $)
    # Alias en MAYÚSCULAS = distingue mayúsculas ("NEAR" sí, "near" no; "DOT" sí, "dot plot" no)
    "ZEC":  ("zcash", ["zcash", "zec"]),
    "LTC":  ("litecoin", ["litecoin", "ltc"]),
    "DOT":  ("polkadot", ["polkadot", "DOT"]),
    "AVAX": ("avalanche-2", ["avalanche", "avax"]),
    "TON":  ("the-open-network", ["toncoin", "TON"]),
    "TAO":  ("bittensor", ["bittensor", "tao"]),
    "NEAR": ("near", ["near protocol", "NEAR"]),
    "APT":  ("aptos", ["aptos"]),
    "ICP":  ("internet-computer", ["internet computer", "icp"]),
    "USDC": ("usd-coin", ["usdc", "circle"]),
    "USDT": ("tether", ["tether", "usdt"]),
}
STABLECOINS = ("USDC", "USDT")
# "XYZ/USDT", "USDT pairs", "USDT-M"...: es el par de cotización, no una noticia de la stablecoin
STABLE_PAIR_RE = (r"[a-z0-9]+ ?/ ?(usdt|usdc)\b|[a-z0-9]{2,}(usdt|usdc)\b|"
                  r"\b(usdt|usdc)((\s*,\s*|\s+(and|&|or)\s+)[a-z0-9]+)*[- ]?(trading )?"
                  r"(pairs?|markets?|perps?|perpetuals?|m\b|margined|spot|quoted)|"
                  r"\bagainst\b[a-z0-9 ,&]{0,30}\b(usdt|usdc)\b")

# Regulación clave (+5): normas definitivas, leyes aprobadas y los organismos/personas que las sacan.
# Solo se busca en el TITULAR (muchos artículos citan "GENIUS Act" de pasada en el texto).
# "Treasury" solo cuando es claramente el Tesoro de EE. UU. (no "bitcoin treasury", "digital asset treasury"...)
_US_TREASURY = (r"\b(u\.?s\.? |us )treasury\b|\btreasury (secretary|department|dept\.?|official|guidance)|"
                r"\bthe treasury\b(?! (compan|firm|strateg))|\btreasury['’]s\b|"
                r"\btreasury (says|said|issues?|issued|proposes?|proposed|releases?|released|finali[sz]es?|seeks?)\b")
_DECISIVE_REG = (r"\bfinal rules?\b|\bfinali[sz](e|es|ed|ing)\b.{0,40}\brul(e|es|ing)\b|\bsign(s|ed)?\b.{0,40}\binto law\b|"
                 r"\bpass(es|ed)?( the)? (senate|house)\b|\b(senate|house) pass(es|ed)\b")
KEY_REG_RE = (_DECISIVE_REG + r"|\bgenius act\b|\bclarity act\b|\bmarket structure (bill|legislation|law)\b|"
              r"\bocc\b|\bcomptroller\b|\bbessent\b|\bstablecoin rules?\b|" + _US_TREASURY)
# Misma historia sin moneda: comparten uno de estos catalizadores + STORY_MIN_WORDS palabras clave del título
STRONG_CATALYSTS = {"regulación clave", "hack / exploit", "listing"}
STORY_MIN_WORDS = 2
# Palabras que no identifican una historia (artículos, verbos genéricos, "act", "crypto"...)
STORY_STOPWORDS = set("""a an the and or but of to in on for with by at from as is are was were be been its it this that
these those his her their our your new says said say after over into about amid ahead than more most up out not no yes
will would could should can may might has have had how why what when who which week weekly daily today report reports
act us u s crypto cryptocurrency cryptocurrencies market markets news first top latest just now via vs amp""".split())
# Acciones que reaccionan a la regulación cripto/stablecoins (línea extra en los 🔥 de regulación)
RELATED_STOCKS = ["CRCL", "COIN", "MSTR", "HOOD"]

# Catalizadores: (regex, puntos, etiqueta, sentido) sentido: +1 alcista, -1 bajista, 0 neutro
CATALYSTS = [
    (r"\bfee switch\b|\bprotocol fees?\b", 4, "fee switch / fees", 1),
    (r"\bbuy ?backs?\b|\btoken burn\b|\bburns?\b", 3, "buyback / burn", 1),
    (r"\betfs?\b", 3, "ETF", 1),
    (r"\bapprov(e|es|ed|al)\b", 2, "aprobación", 1),
    (r"\blist(s|ed|ing)\b|\bwill list\b|\bnew listing\b", 3, "listing", 1),
    (r"\bstrategic (bitcoin |crypto )?reserve\b", 3, "reserva estratégica", 1),
    (r"\bblackrock\b|\bfidelity\b|\bgrayscale\b|\bvanguard\b", 2, "institucional", 1),
    (r"\bbanks?\b|\bbanking\b|\bclearing house\b|\bjpmorgan\b|\bswift\b|\bdtcc\b|\bvisa\b|\bmastercard\b|\bnasdaq\b|\bciti(group)?\b|\bgoldman\b|"
     r"\bintercontinental exchange\b|\bnyse\b|\bcme group\b|\bequity stake\b|\bstrategic investment\b|"
     r"\btakes? (an? )?(\$?[\d.,]+\s?(%|[mb]illion|[mb]n?) )?(equity |minority |majority )?stake\b", 4, "bancos / TradFi", 1),
    (r"\bselect(s|ed)\b|\bchose\b|\bchosen\b|\bpick(s|ed)\b|\bteams up\b|\bwins\b.*\b(contract|deal|role)\b", 2, "elegido / gana contrato", 1),
    (r"\btokeni[sz](ed|ation|e|es)\b|\brwa\b|\breal[- ]world assets?\b", 2, "tokenización / RWA", 1),
    (r"\bpartnership\b|\bpartners with\b|\bintegrat(es|ion)\b", 1, "partnership", 1),
    (r"\bacquir(e|es|ed)\b|\bacquisition\b", 2, "adquisición", 1),
    (r"\bairdrop\b|\btoken launch\b|\btge\b", 2, "airdrop / TGE", 1),
    (r"\bmainnet\b|\bupgrade\b", 1, "upgrade", 1),
    (KEY_REG_RE, 5, "regulación clave", 0),
    (_DECISIVE_REG, 3, "norma definitiva", 0),   # extra si es un paso firme: final rule, ley firmada o aprobada
    (r"\bsec\b|\bcftc\b|\batkins\b", 2, "regulador", 0),
    (r"\bexecutive order\b|\bsenate\b|\bcongress\b|\bclarity act\b|\bgenius act\b", 2, "política / ley", 0),
    (r"\bhack(ed)?\b|\bexploit(ed)?\b|\bdrain(ed)?\b|\bstolen\b", 4, "hack / exploit", -1),
    (r"\bdelist(s|ed|ing)?\b", 4, "delisting", -1),
    (r"\blawsuit\b|\bsues?\b|\bcharged\b|\bindict", 2, "demanda", -1),
    (r"\bunlock\b", 1, "token unlock", -1),
]

# Palabras que hacen que un post de Trump sea relevante (si no tiene ninguna, se ignora).
# Si solo tiene palabras macro (aranceles, Fed...) y ninguna cripto, pierde el peso de fuente oficial.
TRUMP_CRYPTO_WORDS = r"\b(crypto\w*|bitcoin|btc|\w*coins?|tokens?|blockchain|stablecoins?|defi|digital assets?|" \
                     r"hyperliquid|solana|ethereum|xrp|world liberty)\b"
TRUMP_MACRO_WORDS = r"\b(reserve|sec|fed|interest rates?|rate cuts?|tariffs?)\b"

# ── Políticos de EE. UU. y famosos ──
# (nombre que sale en la etiqueta, regex de alias). Se buscan solo en el TITULAR, sin distinguir mayúsculas.
POLITICIANS = [
    ("Trump", r"\b(president |donald )?trump\b(?!['’]s? (media|family|sons?|jr)|\s+(jr|media|coin|memecoin|token))"
              r"(?<!eric trump)(?<!lara trump)(?<!barron trump)(?<!\$trump)"),
    ("JD Vance", r"\bj\.? ?d\.? vance\b|\bvice president vance\b|\bvance\b"),
    ("David Sacks", r"\bdavid sacks\b|\bczar sacks\b|\bsacks says\b"),
    ("Scott Bessent", r"\bbessent\b"),
    ("Howard Lutnick", r"\blutnick\b"),
    ("Paul Atkins", r"\bpaul atkins\b|\b(sec|chair(man)?) atkins\b|\batkins\b"),
    ("Jonathan Gould", r"\bjonathan gould\b|\bcomptroller gould\b|\bgould\b"),
    ("Jerome Powell", r"\bjerome powell\b|\bpowell\b"),
    ("Michael Selig", r"\bselig\b"),     # presidente de la CFTC desde el 22/12/2025
    ("Cynthia Lummis", r"\blummis\b"),
    ("Tim Scott", r"\b(sen(ator|\.)? )?tim scott\b"),
    ("Bill Hagerty", r"\bhagerty\b"),
    ("Elizabeth Warren", r"\belizabeth warren\b|\b(sen(ator|\.)? )warren\b|\bwarren\b(?! buffett| davidson)"),
]
POL_COIN_PTS, POL_ALT_EXTRA, POL_GENERIC_PTS = 6, 2, 2   # +6 con moneda, +2 si no es BTC/ETH, +2 si solo "crypto"
POL_GENERIC_RE = r"\b(crypto\w*|bitcoin|btc|stablecoins?|digital assets?|blockchain|tokens?)\b"
# Famosos: (nombre, regex, puntos si nombran una moneda concreta en el titular)
FAMOUS = [
    # muy raros
    ("Stanley Druckenmiller", r"\bdruckenmiller\b", 8),
    ("Larry Fink", r"\blarry fink\b|\bfink\b", 8),
    ("Jamie Dimon", r"\bdimon\b", 8),
    ("Ray Dalio", r"\bdalio\b", 8),
    ("Paul Tudor Jones", r"\b(paul )?tudor jones\b", 8),
    ("Bill Ackman", r"\backman\b", 8),
    ("Peter Thiel", r"\bthiel\b", 8),
    ("Warren Buffett", r"\bbuffett\b", 8),
    # raros
    ("Elon Musk", r"\belon musk\b|\bmusk\b", 6),
    # habituales
    ("Michael Saylor", r"\bsaylor\b", 2),
    ("Cathie Wood", r"\bcathie wood\b", 2),
    ("CZ", r"\bchangpeng zhao\b|(?-i:\bCZ\b)", 2),
    ("Vitalik Buterin", r"\bvitalik\b|\bbuterin\b", 2),
    ("Mark Cuban", r"\bmark cuban\b", 2),
]
FAMOUS_LOW_PAIRS = {("Elon Musk", "DOGE"): 2}   # Musk + DOGE es casi diario: solo +2
PEOPLE_TAG_PREFIXES = ("🏛️", "⭐")              # etiquetas de políticos/famosos (sección propia del periódico)


# Oración de relativo hasta la siguiente coma/punto ("who sued Ripple", "whose firm holds XRP").
# No incluye "that": en "Musk says that Dogecoin..." la moneda sí es lo que dice la persona.
RELATIVE_CLAUSE_RE = r"\b(who|whom|whose|which)\b[^,;:.!?]*"


def people_in(title, people):
    """Nombres de `people` que aparecen en el titular (en orden de la lista)."""
    return [p[0] for p in people if re.search(p[1], title, re.I)]


def people_bonus(item, coins):
    """(puntos, [etiquetas]) por políticos y famosos que hablan de una moneda en el TITULAR."""
    title = item["title"]
    # Las monedas dentro de una oración de relativo describen a otra persona, no lo que dice el político:
    # "Trump Taps Jay Clayton, the SEC Chair Who Sued Ripple, to Lead AI Push" -> sin XRP
    main = re.sub(RELATIVE_CLAUSE_RE, " ", title, flags=re.I)
    tcoins = [c for c in detect_coins(main, main) if c in coins and c in WATCHLIST and c not in STABLECOINS]
    if not re.search(r"\bdogecoin\b", title, re.I):   # "DOGE" con un político suele ser el departamento de Musk
        tcoins = [c for c in tcoins if c != "DOGE"]
    pts, tags = 0, []
    pols = people_in(title, POLITICIANS)
    if item["source"].startswith("Trump"):   # el propio post de Trump ya tiene "Trump menciona proyecto"
        pols = [p for p in pols if p != "Trump"]
    if pols:
        coin = next((c for c in tcoins if c != "BTC"), None)   # "bitcoin" a secas cuenta como genérico
        if coin:
            pts += POL_COIN_PTS + (POL_ALT_EXTRA if coin not in TOP_COINS else 0)
            tags.append(f"🏛️ {pols[0]} menciona {coin}")
        elif re.search(POL_GENERIC_RE, title, re.I):
            pts += POL_GENERIC_PTS
            tags.append(f"🏛️ {pols[0]} habla de cripto")
    best = None
    for name, rx, p in FAMOUS:
        if tcoins and re.search(rx, title, re.I):
            coin = tcoins[0]
            p = FAMOUS_LOW_PAIRS.get((name, coin), p)
            if best is None or p > best[0]:
                best = (p, name, coin)
    if best:   # varios famosos en el mismo titular: cuenta el de más peso
        pts += best[0]
        tags.append(f"⭐ {best[1]} menciona {best[2]}")
    return pts, tags


# Categoría de cada catalizador (la del catalizador con más peso manda)
TAG_CAT = {
    "fee switch / fees": "Tokenomics", "buyback / burn": "Tokenomics", "token unlock": "Tokenomics",
    "airdrop / TGE": "Tokenomics", "ETF": "Institucional", "institucional": "Institucional",
    "bancos / TradFi": "Institucional", "elegido / gana contrato": "Adopción",
    "tokenización / RWA": "Adopción", "partnership": "Adopción", "adquisición": "Adopción",
    "listing": "Exchange", "delisting": "Exchange", "aprobación": "Regulación",
    "regulador": "Regulación", "política / ley": "Regulación", "regulación clave": "Regulación",
    "norma definitiva": "Regulación", "reserva estratégica": "Regulación",
    "hack / exploit": "Seguridad", "demanda": "Regulación", "upgrade": "Tecnología",
    "Trump menciona proyecto": "Política",
}

# Fuentes primarias: el anuncio ES la noticia, no hace falta confirmación de terceros
OFFICIAL_SOURCES = ("Binance Listing", "Trump (Truth Social)", "Casa Blanca", "SEC")
# Canales de Telegram: nunca oficiales, salen "sin confirmar" salvo que otra fuente hable de la misma moneda
TELEGRAM_SOURCES = {name for name, _, _ in TELEGRAM_CHANNELS}

# Acciones tokenizadas: no son cripto, puntúan 0
TOKENIZED_STOCKS_RE = r"tokeni[sz]ed (\S+ ){0,2}stocks?|xstocks|stock tokens?|equit(y|ies)"
# Flujos diarios/semanales de ETF: dato rutinario, no catalizador
ETF_FLOWS_RE = r"\bnet (in|out)flows?\b"
# Compras corporativas para la tesorería ("adds $169M bitcoin", "acquires 334 BTC", "bitcoin treasury company")
_TREASURY_COINS = r"(bitcoin|btc|ether|eth|ethereum|solana|sol|xrp|bnb|hype|crypto)"
TREASURY_BUY_RE = (r"\b(adds?|buys|bought|purchases?|purchased|acquires?|acquired)\s+(another\s+|an additional\s+)?"
                   r"\$?\d[\d,.]*\s?(k|m|b|mn|bn|million|billion)?\s+(worth of\s+|in\s+|of\s+)?" + _TREASURY_COINS + r"\b"
                   r"|\b" + _TREASURY_COINS + r"\s+treasur(y|ies)\b|\btreasury (company|companies|firm|firms|strategy)\b")
# "Nasdaq-listed", "NYSE-listed"...: describe a la empresa, no es un listing ni noticia de TradFi
LISTED_DESCRIPTOR_RE = r"\b[\w.]+-listed\b|\bpublicly (traded and )?listed\b|\b(listed|trades?|traded) on (the )?(nasdaq|nyse)\b"
# Rumores y predicciones: se penalizan (regla editorial: hecho ≠ opinión ≠ predicción)
RUMOR_RE = r"\brumou?rs?\b|\breportedly\b|\bsources say\b|\bunconfirmed\b|\bspeculat"
PREDICTION_RE = (r"price prediction|price forecast|price analysis|could (reach|hit|soar|explode)|"
                 r"will (reach|hit) \$|\bto \$\d|\d+x (gains|potential)|next 100x|\b\d{2,}x\b|top \d+ (altcoins|coins|cryptos) to buy")

LABELS = {
    "seguir": ("🔥", "A SEGUIR"),
    "ojo":    ("👀", "ÉCHALE UN OJO"),
    "movido": ("⚠️", "YA SE HA MOVIDO"),
    "breve":  ("·", "Breve"),
}

# ───────────────────────── BASE DE DATOS ─────────────────────────

def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, ts INTEGER)")
    con.execute("""CREATE TABLE IF NOT EXISTS news (
        id TEXT PRIMARY KEY, ts INTEGER, source TEXT, title TEXT, text TEXT, link TEXT,
        score INTEGER, label TEXT, sentiment INTEGER, tags TEXT, coins TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS track (
        id TEXT PRIMARY KEY, ts INTEGER, sym TEXT, label TEXT, score INTEGER, sentiment INTEGER,
        title TEXT, p0 REAL, p24 REAL, p7 REAL)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, ts INTEGER, sym TEXT, tags TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS congress_trades (
        id TEXT PRIMARY KEY, chamber TEXT, member TEXT, bioguide TEXT, party TEXT, committees TEXT,
        asset TEXT, ticker TEXT, kind TEXT, amount TEXT, amount_min INTEGER, tx_date TEXT, disc_date TEXT,
        link TEXT, alerted INTEGER, first_seen INTEGER)""")
    return con


def meta_get(con, k):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
    con.commit()


def is_seen(con, item_id):
    return con.execute("SELECT 1 FROM seen WHERE id=?", (item_id,)).fetchone() is not None


def mark_seen(con, item_id):
    con.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (item_id, int(time.time())))


def save_news(con, it):
    con.execute("INSERT OR REPLACE INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
        it["id"], it["ts"], it["source"], it["title"], it["text"], it["link"],
        it["score"], it["label"], it["sentiment"],
        json.dumps(it["tags"], ensure_ascii=False), json.dumps(it["coins"])))


def start_tracking(con, it):
    """Guarda el precio de la moneda principal en el momento de la alerta."""
    c = next((c for c in it["coins"] if c.get("price")), None)
    if it["label"] == "breve" or not c:
        return
    con.execute("INSERT OR IGNORE INTO track VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                (it["id"], int(time.time()), c["sym"], it["label"], it["score"], it["sentiment"],
                 it["title"], c["price"]))


CATALYST_TAGS = {tag for _, _, tag, _ in CATALYSTS}
# Instituciones concretas (no "bank" genérico): bolsas, grandes bancos y tomas de participación
TRADFI_BIG_RE = (r"\bintercontinental exchange\b|\bnyse\b|\bcme group\b|\bnasdaq\b|\bjpmorgan\b|\bgoldman\b|"
                 r"\bciti(group)?\b|\bdtcc\b|\bswift\b|\bvisa\b|\bmastercard\b|\bblackrock\b|\bequity stake\b|"
                 r"\bstrategic investment\b|\btakes? (an? )?(\$?[\d.,]+\s?(%|[mb]illion|[mb]n?) )?(equity |minority |majority )?stake\b")
TITLE_ONLY_TAGS = {"regulación clave", "norma definitiva", "listing"}   # solo cuentan si están en el titular
TITLE_ONLY_EXCEPT = {"listing": ("Binance Listing",)}   # ...salvo en estas fuentes (el anuncio de Binance es la noticia)
TOP_COINS = ("BTC", "ETH")


def main_coin(it):
    c = it["coins"][0] if it["coins"] else None
    return (c["sym"] if isinstance(c, dict) else c) or ""


def title_words(title):
    """Palabras clave del título: sin artículos ni palabras genéricas; plural/3ª persona -> singular
    ("rules" = "rule", "OCC's" = "occ", "stablecoins" = "stablecoin")."""
    t = re.sub(r"['’]s\b", "", title.lower())
    out = set()
    for w in re.findall(r"[a-z0-9$]+", t):
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) >= 3 and w not in STORY_STOPWORDS:
            out.add(w)
    return out


def same_story(a, b):
    """¿Dos noticias cuentan la misma historia?
    - Misma moneda principal: algún catalizador en común + STORY_MIN_WORDS palabras clave del título.
    - Si alguna no tiene moneda: un catalizador fuerte en común (regulación clave, hack, listing) + lo mismo."""
    ca, cb = main_coin(a), main_coin(b)
    shared = CATALYST_TAGS.intersection(a["tags"], b["tags"])
    common = title_words(a["title"]) & title_words(b["title"])
    if ca and cb:
        if ca != cb or not shared:
            return False
        # el nombre de la moneda ya coincide por definición: no cuenta como palabra en común
        common -= {ca.lower(), *(w for al in WATCHLIST.get(ca, ("", []))[1] for w in title_words(al))}
    elif not (shared & STRONG_CATALYSTS):
        return False
    return len(common) >= STORY_MIN_WORDS


def already_alerted(con, it):
    """¿Ya se avisó en 24 h de esta historia?
    Con moneda: misma moneda principal y algún catalizador en común.
    Sin moneda: misma historia (catalizador fuerte + 2 palabras clave del título en común)."""
    cats = CATALYST_TAGS.intersection(it["tags"])
    if not cats:
        return False
    since = int(time.time()) - 86400
    sym = main_coin(it)
    if sym:
        rows = con.execute("SELECT tags FROM alerts WHERE sym=? AND ts>=?", (sym, since)).fetchall()
        return any(cats & set(json.loads(r[0])) for r in rows)
    rows = con.execute("""SELECT a.tags, n.title, n.coins FROM alerts a JOIN news n ON n.id = a.id
                          WHERE a.ts>=? AND a.id != ?""", (since, it["id"])).fetchall()
    return any(same_story(it, {"tags": json.loads(t), "title": title, "coins": json.loads(coins)})
               for t, title, coins in rows)


def record_alert(con, it):
    sym = main_coin(it)
    con.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?)",
                (it["id"], int(time.time()), sym, json.dumps(it["tags"], ensure_ascii=False)))


def update_tracking(con):
    """Rellena el precio a las 24 h y a los 7 días de cada alerta cuando toca."""
    now = int(time.time())
    due = con.execute("""SELECT id, sym, ts, p24, p7 FROM track
                         WHERE (p24 IS NULL AND ts <= ?) OR (p7 IS NULL AND ts <= ?)""",
                      (now - 86400, now - 7 * 86400)).fetchall()
    if not due:
        return
    px = prices(sorted({r[1] for r in due}))
    for id_, sym, ts, p24, p7 in due:
        p = (px.get(sym) or {}).get("price")
        if not p:
            continue
        if p24 is None and ts <= now - 86400:
            con.execute("UPDATE track SET p24=? WHERE id=?", (p, id_))
        if p7 is None and ts <= now - 7 * 86400:
            con.execute("UPDATE track SET p7=? WHERE id=?", (p, id_))
    con.commit()


def sources_for(con, coins, ts, exclude_id=None):
    """Fuentes distintas que hablan de la misma moneda en ±24 h de esta noticia."""
    srcs = set()
    for c in coins[:1]:   # moneda principal
        sym = c["sym"] if isinstance(c, dict) else c
        rows = con.execute("SELECT source FROM news WHERE ts BETWEEN ? AND ? AND coins LIKE ? AND id != ?",
                           (ts - 86400, ts + 86400, f'%"sym": "{sym}"%', exclude_id or "")).fetchall()
        srcs |= {r[0] for r in rows}
    return srcs


def verification(con, it):
    """('oficial'|'confirmado'|'sin confirmar', n_fuentes)."""
    if it["source"] in OFFICIAL_SOURCES:
        return "oficial", 1
    if "rumor" in it["tags"]:
        return "sin confirmar", 1
    if not it["coins"] and it["source"] in TELEGRAM_SOURCES:   # canal de Telegram sin moneda: nada que confirmar
        return "sin confirmar", 1
    if not it["coins"]:
        return "", 1
    srcs = sources_for(con, it["coins"], it["ts"], it["id"]) | {it["source"]}
    return ("confirmado" if len(srcs) >= 2 else "sin confirmar"), len(srcs)


def is_people(it):
    """¿Noticia de un político o famoso (bloques B/C)? Va a la sección "Políticos, famosos y Congreso"."""
    return any(t.startswith(PEOPLE_TAG_PREFIXES) or t == "Trump menciona proyecto" for t in it["tags"])


def category(it):
    best = None
    for pattern, p, tag, _ in CATALYSTS:
        if tag in it["tags"] and (best is None or p > best[0]):
            best = (p, TAG_CAT.get(tag, "Mercado"))
    if "Trump menciona proyecto" in it["tags"] or is_people(it):
        return "Política"
    return best[1] if best else "Mercado"


def load_news(con, hours):
    since = int(time.time()) - hours * 3600
    rows = con.execute("SELECT * FROM news WHERE ts>=? ORDER BY ts DESC", (since,)).fetchall()
    keys = ["id", "ts", "source", "title", "text", "link", "score", "label", "sentiment", "tags", "coins"]
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["tags"], d["coins"] = json.loads(d["tags"]), json.loads(d["coins"])
        out.append(d)
    return out

# ───────────────────────── FUENTES ─────────────────────────

def fetch_rss():
    items = []
    for name, url, base in RSS_FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=15)
            feed = feedparser.parse(r.content)
            for e in feed.entries[:30]:
                title = html.unescape(e.get("title", "")).strip()
                summary = re.sub(r"<[^>]+>", " ", html.unescape(e.get("summary", "")))
                summary = re.sub(r"\s+", " ", summary).strip()[:600]
                link = e.get("link", "")
                when = e.get("published_parsed") or e.get("updated_parsed")
                items.append({"id": f"{name}|{e.get('id') or link or title}", "source": name,
                              "title": title, "text": summary, "link": link, "base": base,
                              "published": calendar.timegm(when) if when else None})
        except Exception as ex:
            log(f"Error en {name}: {ex}")
    return items


def fetch_binance_listings():
    """Anuncios 'New Cryptocurrency Listing' de Binance (catalogId 48)."""
    url = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
           "?type=1&catalogId=48&pageNo=1&pageSize=10")
    items = []
    try:
        data = requests.get(url, headers=UA, timeout=15).json()
        for cat in (data.get("data") or {}).get("catalogs", []):
            for a in cat.get("articles", []):
                items.append({"id": f"binance|{a.get('code')}", "source": "Binance Listing",
                              "title": a.get("title", ""), "text": "",
                              "link": f"https://www.binance.com/en/support/announcement/{a.get('code')}",
                              "base": 5,
                              "published": a["releaseDate"] // 1000 if a.get("releaseDate") else None})
    except Exception as ex:
        log(f"Error en Binance: {ex}")
    return items


def fetch_telegram_channels():
    """Mensajes de los canales públicos de TELEGRAM_CHANNELS (t.me/s/<usuario>)."""
    items = []
    for name, user, base in TELEGRAM_CHANNELS:
        try:
            r = requests.get(f"https://t.me/s/{user}", headers=UA, timeout=15)
            r.raise_for_status()
            for block in re.split(r'(?=<div class="tgme_widget_message[ "][^>]*data-post=")', r.text)[1:]:
                post = re.search(r'data-post="([^"]+)"', block).group(1)
                when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
                body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
                if not when or not body:
                    continue
                text = re.sub(r"<br\s*/?>", "\n", body.group(1))
                text = html.unescape(re.sub(r"<[^>]+>", "", text))
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                if not lines:
                    continue
                items.append({"id": post, "source": name, "title": lines[0],
                              "text": " ".join(lines[1:])[:600],
                              "link": f"https://t.me/{post}", "base": base,
                              "published": int(datetime.fromisoformat(when.group(1)).timestamp())})
        except Exception as ex:
            log(f"Error en {name} (Telegram): {ex}")
    return items


def fetch_google_news(con):
    """Google News RSS: '"<persona>" crypto when:1d' para cada político y famoso, cada GNEWS_EVERY como mucho.
    Solo se quedan los titulares que nombran a alguna de esas personas, y no los de medios que ya leemos por RSS."""
    if con is None:
        return []
    own = tuple(n.lower() for n, _, _ in RSS_FEEDS)
    people = [p[:2] for p in POLITICIANS] + [p[:2] for p in FAMOUS]
    now = int(time.time())
    items, seen_ids = [], set()
    for name, _ in people:
        key = f"gnews|{name}"
        if now - int(meta_get(con, key) or 0) < GNEWS_EVERY:
            continue
        try:
            r = requests.get(GNEWS_URL, params={"q": f'"{name}" crypto when:1d', "hl": "en-US", "gl": "US",
                                                "ceid": "US:en"}, headers=UA, timeout=15)
            if r.status_code in (403, 429, 503):
                log(f"Google News bloquea ({r.status_code}): se reintenta en la próxima pasada")
                break
            r.raise_for_status()
            for e in feedparser.parse(r.content).entries[:GNEWS_PER_QUERY]:
                pub = (e.get("source") or {}).get("title", "") or ""
                title = html.unescape(e.get("title", "")).strip()
                if pub and title.endswith(" - " + pub):
                    title = title[: -len(pub) - 3].strip()
                link = e.get("link", "")
                iid = f"gnews|{e.get('id') or link}"
                if iid in seen_ids or pub.lower().startswith(own) or not people_in(title, people):
                    continue
                if not (re.search(POL_GENERIC_RE, title, re.I) or detect_coins(title)):
                    continue   # la persona sale, pero el titular no habla de cripto (tipos, Irán, aranceles...)
                seen_ids.add(iid)
                when = e.get("published_parsed")
                items.append({"id": iid, "source": f"{pub or 'Google News'} (Google News)", "title": title,
                              "text": "", "link": link, "base": 0,
                              "published": calendar.timegm(when) if when else None})
            meta_set(con, key, str(now))
        except Exception as ex:
            log(f"Error en Google News ({name}): {ex}")
    return items


# ───────────────────────── CONGRESO (STOCK Act) ─────────────────────────
# Cámara: espejo en GitHub de las declaraciones PTR del House Clerk (el mismo que usa congress_replicator;
# housestockwatcher.com está caído). Senado: el espejo senate-stock-watcher-data dejó de actualizarse en 2021
# y el buscador oficial (efdsearch.senate.gov) exige aceptar sus condiciones de uso: de momento no se usa.
CONGRESS_HOUSE_URL = ("https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data"
                      "/master/data/all_transactions.json")
LEGISLATORS_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
COMMITTEES_URL = "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"
# Comités que vigilan cripto: Servicios Financieros / Banca y Agricultura (CFTC), de las dos cámaras
WATCH_COMMITTEES = {"HSBA": "Servicios Financieros", "HSAG": "Agricultura",
                    "SSBK": "Banca (Senado)", "SSAF": "Agricultura (Senado)"}
CONGRESS_MIN_BUY = 15_000          # compras por encima de esto avisan aunque no sea de los comités
CONGRESS_ALERT_DAYS = 14           # solo se avisan declaraciones presentadas en los últimos N días
CONGRESS_HOURS = 24                # revisar las declaraciones una vez al día
CRYPTO_ETFS = {"IBIT", "FBTC", "GBTC", "ETHA", "BITB", "ARKB", "BTCO", "HODL", "EZBC", "BRRR", "BTCW",
               "BITO", "ETHE", "FETH", "ETHW", "BSOL", "GSOL", "XRPR", "MSBT"}
CRYPTO_STOCKS = {"COIN", "MSTR", "CRCL", "HOOD", "MARA", "RIOT"}
CRYPTO_ASSET_RE = (r"\b(bitcoin|ethereum|ether|solana|xrp|dogecoin|litecoin|cryptocurrenc(y|ies)|crypto asset|"
                   r"digital asset|coinbase|microstrategy|strategy inc|robinhood markets|circle internet|"
                   r"marathon digital|mara holdings|riot platforms)\b")


def _amount_min(s):
    m = re.search(r"\$\s?([\d,]+)", s or "")
    return int(m.group(1).replace(",", "")) if m else 0


def _us_date(s):
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
    except Exception:
        return ""


def _norm(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s)


def legislator_index():
    """{apellido normalizado: [(nombres posibles, bioguide, partido, cámara)]} desde congress-legislators."""
    idx = {}
    for p in requests.get(LEGISLATORS_URL, headers=UA, timeout=30).json():
        n, term = p["name"], p["terms"][-1]
        firsts = {_norm(n.get("first", "")).strip(), _norm(n.get("nickname", "")).strip()} - {""}
        idx.setdefault(_norm(n["last"]).strip(), []).append(
            (firsts, p["id"]["bioguide"], term.get("party", ""), term.get("type", "")))
    return idx


def match_legislator(idx, full_name, chamber="rep"):
    """(bioguide, partido) de "Gilbert Cisneros" / "Hon. Marjorie Taylor Greene" / "Rob Bresnahan, Jr."."""
    toks = [t for t in _norm(full_name).split() if t not in ("hon", "jr", "sr", "ii", "iii", "iv", "mr", "mrs", "ms", "dr")]
    for i in range(len(toks)):   # apellidos compuestos: "taylor greene", "van orden"...
        cands = [c for c in idx.get(" ".join(toks[i:]), []) if c[3] == chamber]
        if not cands:
            continue
        first = toks[0] if i else ""
        best = [c for c in cands if first in c[0]] or [c for c in cands if any(f[:1] == first[:1] for f in c[0])] or cands
        if len(best) == 1 or first:
            return best[0][1], best[0][2]
    return None, ""


def committee_index():
    """{bioguide: [nombres de los comités vigilados]}."""
    data = requests.get(COMMITTEES_URL, headers=UA, timeout=30).json()
    out = {}
    for code, name in WATCH_COMMITTEES.items():
        for m in data.get(code, []):
            out.setdefault(m["bioguide"], []).append(name)
    return out


def is_crypto_trade(r):
    t = (r.get("ticker") or "").upper().strip()
    return t in CRYPTO_ETFS or t in CRYPTO_STOCKS or \
        re.search(CRYPTO_ASSET_RE, (r.get("asset_description") or "").lower()) is not None


def fetch_congress_trades():
    """Operaciones cripto (cripto directa, ETFs cripto, acciones cripto) declaradas en la Cámara."""
    raw = requests.get(CONGRESS_HOUSE_URL, headers=UA, timeout=90).json()
    idx, comms = legislator_index(), committee_index()
    out = []
    for r in raw:
        if not is_crypto_trade(r):
            continue
        bio, party = match_legislator(idx, r.get("representative", ""))
        ticker = (r.get("ticker") or "").upper().strip()
        asset = re.sub(r"\s+", " ", r.get("asset_description") or "").strip()[:120]
        key = "|".join(str(r.get(k, "")) for k in ("filing_id", "ticker", "transaction_date", "type", "amount", "owner"))
        out.append({"id": key, "chamber": "Cámara", "member": r.get("representative", ""), "bioguide": bio or "",
                    "party": {"Democrat": "D", "Republican": "R", "Independent": "I"}.get(party, party[:1]),
                    "committees": ", ".join(comms.get(bio, [])), "asset": asset,
                    "ticker": ticker if ticker not in ("", "--", "N/A") else "",
                    "kind": {"Purchase": "compra", "Sale": "vende", "Sale (Full)": "vende", "Sale (Partial)": "vende",
                             "Exchange": "canjea"}.get(r.get("type", ""), (r.get("type") or "").lower()),
                    "amount": r.get("amount", ""), "amount_min": _amount_min(r.get("amount")),
                    "tx_date": _us_date(r.get("transaction_date")), "disc_date": _us_date(r.get("disclosure_date")),
                    "link": r.get("source_url", "")})
    return out


def congress_alert_worthy(t):
    return (t["kind"] == "compra" and t["amount_min"] > CONGRESS_MIN_BUY) or bool(t["committees"])


def format_congress_msg(t):
    esc = lambda s: html.escape(s or "")
    verb = {"compra": "compra", "vende": "vende"}.get(t["kind"], t["kind"])
    what = t["ticker"] or t["asset"][:40]
    lines = [f"🏛️💰 <b>{esc(t['member'])}</b> {verb} <b>{esc(what)}</b>",
             f"{esc(t['asset'][:100])}",
             f"Importe: {esc(t['amount'])}",
             f"Partido: {esc(t['party'] or '?')} · {t['chamber']}" + (f" · Comité: {esc(t['committees'])}" if t["committees"] else ""),
             f"Operación: {t['tx_date']} · Declarada: {t['disc_date']}"]
    if t["link"]:
        lines.append(f'<a href="{esc(t["link"])}">Ver declaración</a>')
    return "\n".join(lines)


def check_congress(con, dry, force=False):
    """Una vez al día: guarda las operaciones cripto nuevas y avisa por Telegram de las que lo merecen.
    La primera vez solo llena la tabla (sin avisos)."""
    now = int(time.time())
    if not force and now - int(meta_get(con, "congress_check") or 0) < CONGRESS_HOURS * 3600:
        return 0
    try:
        trades = fetch_congress_trades()
    except Exception as ex:
        log(f"Error en declaraciones del Congreso: {ex}")
        return 0
    first = con.execute("SELECT COUNT(*) FROM congress_trades").fetchone()[0] == 0
    known = {r[0] for r in con.execute("SELECT id FROM congress_trades")}
    recent = (datetime.now(TZ).date() - timedelta(days=CONGRESS_ALERT_DAYS)).isoformat()
    sent = 0
    for t in trades:
        if t["id"] in known:
            continue
        alerted = 0
        if not first and t["disc_date"] >= recent and congress_alert_worthy(t):
            if send(format_congress_msg(t), dry):
                alerted = 1
                sent += 1
                log(f"Congreso: {t['member']} {t['kind']} {t['ticker'] or t['asset'][:30]}")
            else:
                continue   # Telegram falló: no se guarda, se reintenta mañana
        con.execute("INSERT OR IGNORE INTO congress_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t["id"], t["chamber"], t["member"], t["bioguide"], t["party"], t["committees"], t["asset"],
                     t["ticker"], t["kind"], t["amount"], t["amount_min"], t["tx_date"], t["disc_date"],
                     t["link"], alerted, now))
    con.commit()
    meta_set(con, "congress_check", str(now))
    log(f"Congreso: {len(trades)} operaciones cripto en la fuente, {sent} avisos.")
    return sent


def load_congress(con, days):
    since = (datetime.now(TZ).date() - timedelta(days=days)).isoformat()
    keys = ["id", "chamber", "member", "bioguide", "party", "committees", "asset", "ticker", "kind", "amount",
            "amount_min", "tx_date", "disc_date", "link", "alerted", "first_seen"]
    rows = con.execute("SELECT * FROM congress_trades WHERE disc_date >= ? ORDER BY disc_date DESC, tx_date DESC",
                       (since,)).fetchall()
    return [dict(zip(keys, r)) for r in rows]


def collect(con=None):
    """Todas las fuentes, sin las noticias publicadas hace más de MAX_AGE_HOURS."""
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    items = fetch_binance_listings() + fetch_rss() + fetch_telegram_channels() + fetch_google_news(con)
    return [it for it in items if not it.get("published") or it["published"] >= cutoff]

# ───────────────────────── PUNTUACIÓN ─────────────────────────

def alias_in(alias, low, raw):
    """¿Aparece el alias? En minúsculas = sin distinguir; con mayúsculas = tal cual (DOT, NEAR, TON)."""
    if alias != alias.lower():
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", raw) is not None
    return re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", low) is not None


def coin_positions(title):
    """{SYM: posición de su primera mención en el título} (watchlist y tickers $XYZ / (XYZ))."""
    low = re.sub(STABLE_PAIR_RE + r"|\bavalanche of\b", lambda m: " " * len(m.group(0)), title.lower())
    pos = {}
    for sym, (_, aliases) in WATCHLIST.items():
        for a in aliases:
            raw = a != a.lower()
            m = re.search(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])" if raw else
                          r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", title if raw else low)
            if m:
                pos[sym] = min(pos.get(sym, m.start()), m.start())
    for m in re.finditer(r"\$([A-Z]{2,10})\b|\(([A-Z]{2,10})\)", title):
        pos.setdefault(m.group(1) or m.group(2), m.start())
    return pos


def detect_coins(text, title=None):
    """Monedas mencionadas. Con `title`, la moneda principal es la PRIMERA que aparece en el título, aunque
    el texto hable antes de BTC/ETH: "...crypto ETF market... as Zcash ETF tops $1B" -> ZEC, no BTC."""
    t = " " + re.sub(STABLE_PAIR_RE + r"|\bavalanche of\b", " ", text.lower()) + " "
    found = []
    for sym, (_, aliases) in WATCHLIST.items():
        if any(alias_in(a, t, text) for a in aliases):
            found.append(sym)
    # Tickers tipo $XYZ o (XYZ) que no estén en la watchlist
    for m in re.findall(r"\$([A-Z]{2,10})\b|\(([A-Z]{2,10})\)", text):
        s = m[0] or m[1]
        if s not in found and s not in ("USD", "SEC", "ETF", "CEO", "US", "EU", "UK", "ICE", "ET",
                                        "EST", "UTC", "GMT", "CET", "NYSE", "CME", "FED", "FOMC",
                                        "CPI", "GDP", "IPO", "AI", "OCC", "KST", "JST", "HKT", "SGT", "FDIC", "ABA", "OFAC",
                                        "CRCL", "COIN", "MSTR", "HOOD") \
                and not (s in STABLECOINS and s.lower() not in t):
            found.append(s)
    if title is not None:   # primero las del título, por orden de aparición; luego el resto
        pos = coin_positions(title)
        found.sort(key=lambda c: pos.get(c, 10 ** 6))   # sort estable: el resto mantiene su orden
    # Las stablecoins van al final: la moneda principal (precio, repetidos) debe ser la otra si la hay
    return [c for c in found if c not in STABLECOINS] + [c for c in found if c in STABLECOINS]


def score(item, ignore=()):
    """Devuelve (puntos, [etiquetas], sentimiento, [monedas]). `ignore`: tickers a no contar."""
    text = f"{item['title']} {item['text']}"
    low = text.lower()
    trump = item["source"].startswith("Trump")
    pts, tags, sent = item["base"], [], 0
    if trump and not re.search(TRUMP_CRYPTO_WORDS, low):
        if not re.search(TRUMP_MACRO_WORDS, low):
            return 0, [], 0, []
        pts = 0   # post solo macro (aranceles, Fed...): sin el peso de fuente oficial
    if item["source"] in GOV_SOURCES and not re.search(TRUMP_CRYPTO_WORDS, low):
        return 0, [], 0, []   # nota de la Casa Blanca / SEC que no habla de cripto

    cat_low = re.sub(LISTED_DESCRIPTOR_RE, " ", low)
    title_low = re.sub(LISTED_DESCRIPTOR_RE, " ", item["title"].lower())
    # "ICE" solo en mayúsculas y como palabra = Intercontinental Exchange (dueña del NYSE), nunca ticker
    if re.search(r"\bICE\b", item["title"]):
        title_low += " intercontinental exchange"
    if re.search(r"\bICE\b", text):
        cat_low += " intercontinental exchange"
    for pattern, p, tag, s in CATALYSTS:
        title_only = tag in TITLE_ONLY_TAGS and item["source"] not in TITLE_ONLY_EXCEPT.get(tag, ())
        if re.search(pattern, title_low if title_only else cat_low):
            pts += p
            tags.append(tag)
            sent += s

    coins = [c for c in detect_coins(text, item["title"]) if c not in ignore]
    if trump:   # en Trump solo cuentan monedas de la watchlist; "(BD)" es una empresa, no una moneda
        coins = [c for c in coins if c in WATCHLIST]
    # Acciones tokenizadas (ej. "Adds Adobe (ADBEB), Hewlett Packard (HPEB)..."): no son cripto
    if re.search(TOKENIZED_STOCKS_RE, item["title"].lower()) or \
            len(re.findall(r"\(([A-Z]{2,10})\)", item["title"])) >= 4 or \
            (item["source"] == "Binance Listing" and   # Binance usa sufijo B: ADBEB, HPEB...
             sum(len(c) >= 4 and c.endswith("B") and c not in WATCHLIST for c in coins) >= 2):
        return 0, ["acciones tokenizadas"], 0, coins
    known = [c for c in coins if c in WATCHLIST]
    bonus = min(len(known), 3) * 2
    bonus += min((len(coins) - len(known)) * 2, 2)   # tickers fuera de la watchlist: máx. 2 puntos
    if len(coins) >= 4:   # listas de monedas ("cita BTC, ETH, SOL, XLM..."): no es noticia de ninguna
        bonus = min(bonus, 2)
    pts += bonus

    # Banco / bolsa TradFi + moneda concreta en el MISMO titular ("NYSE owner ICE teams up with Trade.xyz")
    title_coins = [c for c in detect_coins(item["title"]) if c in coins and c in WATCHLIST
                   and c not in TOP_COINS and c not in STABLECOINS]
    if title_coins and re.search(TRADFI_BIG_RE, title_low):
        pts += 2
        tags.append(f"TradFi + {title_coins[0]} en titular")

    p_pts, p_tags = people_bonus(item, coins)
    pts += p_pts
    tags += p_tags
    sent += 1 if p_pts else 0

    if trump and coins:
        pts += 5
        tags.append("Trump menciona proyecto")
        sent += 1

    if re.search(PREDICTION_RE, low):          # predicciones de precio: no son noticia
        return 0, ["predicción"], 0, coins
    if re.search(ETF_FLOWS_RE, low) and "etf" in low:
        pts -= 4
        tags.append("flujos ETF")
    if re.search(TREASURY_BUY_RE, low):
        pts -= 4
        tags.append("compra treasury")
    if re.search(RUMOR_RE, low):
        pts -= 3
        tags.append("rumor")
    return pts, tags, sent, coins

# ───────────────────────── PRECIOS ─────────────────────────

def dex_pair(sym, cache):
    """Par de DexScreener con más liquidez para este símbolo (None si no hay). `cache`: dict de la pasada."""
    if sym not in cache:
        try:
            pairs = requests.get("https://api.dexscreener.com/latest/dex/search",
                                 params={"q": sym}, headers=UA, timeout=10).json().get("pairs") or []
        except Exception:
            return None
        pairs = [p for p in pairs if (p.get("baseToken") or {}).get("symbol", "").upper() == sym]
        cache[sym] = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0) if pairs else None
    return cache[sym]


def prices(symbols, dex_cache=None):
    """{SYM: {"price", "ch1h", "ch24h"}} — CoinGecko para la watchlist, DexScreener para el resto."""
    dex_cache = {} if dex_cache is None else dex_cache
    out = {}
    known = [s for s in symbols if s in WATCHLIST and s not in STABLECOINS]   # stablecoins: sin precio
    if known:
        ids = {WATCHLIST[s][0]: s for s in known}
        try:
            data = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                                params={"vs_currency": "usd", "ids": ",".join(ids),
                                        "price_change_percentage": "1h,24h"},
                                headers=UA, timeout=15).json()
            for c in data if isinstance(data, list) else []:
                out[ids[c["id"]]] = {"price": c.get("current_price"),
                                     "ch1h": c.get("price_change_percentage_1h_in_currency"),
                                     "ch24h": c.get("price_change_percentage_24h_in_currency")}
        except Exception as ex:
            log(f"Error CoinGecko: {ex}")
    for s in [s for s in symbols if s not in WATCHLIST][:10]:
        p = dex_pair(s, dex_cache)
        if p:
            pc = p.get("priceChange") or {}
            out[s] = {"price": float(p.get("priceUsd") or 0), "ch1h": pc.get("h1"), "ch24h": pc.get("h24")}
    return out


def label_for(pts, coins_px, check_moved=True):
    moved = False
    if check_moved:
        for c in coins_px:
            if abs(c.get("ch1h") or 0) >= MOVED_1H_PCT or abs(c.get("ch24h") or 0) >= MOVED_24H_PCT:
                moved = True
    if pts >= ALERT_THRESHOLD and moved:
        return "movido"
    if pts >= HOT_THRESHOLD:
        return "seguir"
    if pts >= ALERT_THRESHOLD:
        return "ojo"
    return "breve"


def coverage_24h(con, sym, ts=None, exclude_id=""):
    """Cuántas noticias de las 24 h anteriores a `ts` mencionaban esta moneda (para detectar 'tema del día')."""
    if con is None:
        return 0
    ts = ts or int(time.time())
    return con.execute("SELECT COUNT(*) FROM news WHERE ts BETWEEN ? AND ? AND coins LIKE ? AND id != ?",
                       (ts - 86400, ts, f'%"sym": "{sym}"%', exclude_id)).fetchone()[0]


def illiquid_tickers(raw_items, dex_cache):
    """Tickers desconocidos cuyo mejor par en DexScreener tiene menos de MIN_DEX_LIQUIDITY."""
    unknown = sorted({c for it in raw_items if not it["source"].startswith("Trump")
                      for c in detect_coins(f"{it['title']} {it['text']}") if c not in WATCHLIST})
    out = set()
    for s in unknown[:25]:
        p = dex_pair(s, dex_cache)
        if p and ((p.get("liquidity") or {}).get("usd") or 0) < MIN_DEX_LIQUIDITY:
            out.add(s)
    return out


def process(raw_items, check_moved=True, con=None):
    """Puntúa, busca precios (en bloque) y etiqueta. Devuelve solo lo que llega a 'Breve'."""
    now = int(time.time())
    dex_cache = {}
    ignore = illiquid_tickers(raw_items, dex_cache)
    scored = []
    for it in raw_items:
        ts = min(it.get("published") or now, now)   # fecha real de publicación
        pts, tags, sent, coins = score(it, ignore)
        # Tema del día: si una moneda acumula noticias, la narrativa está creciendo (BTC/ETH siempre las tienen)
        hot = max((coverage_24h(con, c, ts, it["id"]) for c in coins if c not in NO_HOT_COINS), default=0)
        if hot >= 2 and "flujos ETF" not in tags:   # los flujos de ETF no son tema del día
            pts += 3
            tags.append(f"tema del día ({hot + 1} noticias en 24 h)")
        if pts >= BRIEF_MIN:
            scored.append((it, ts, pts, tags, sent, coins, hot))
    wanted = sorted({c for (_, _, pts, _, _, coins, _) in scored if pts >= ALERT_THRESHOLD for c in coins})
    px = prices(wanted, dex_cache) if wanted else {}
    out = []
    for it, ts, pts, tags, sent, coins, hot in scored:
        cpx = [{"sym": c, **px.get(c, {})} for c in coins[:5]]
        out.append({**it, "ts": ts, "score": pts, "tags": tags, "sentiment": sent,
                    "coins": cpx, "hot": hot, "label": label_for(pts, cpx, check_moved)})
    return out

# ───────────────────────── TELEGRAM ─────────────────────────

def fmt_pct(v):
    return "–" if v is None else f"{v:+.1f}%"


def coin_txt(c):
    """Texto de una moneda en el aviso: las stablecoins van sin precio."""
    if c["sym"] in STABLECOINS:
        return f"<b>{c['sym']}</b> (stablecoin)"
    return f"<b>{c['sym']}</b> {fmt_price(c.get('price'))} (1h {fmt_pct(c.get('ch1h'))} · 24h {fmt_pct(c.get('ch24h'))})"


def stock_quotes(symbols):
    """{SYM: (precio, cambio % del día)} desde Yahoo Finance (sin clave). Lo que falle se omite."""
    out = {}
    for s in symbols:
        for host in ("query1", "query2"):
            try:
                r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{s}",
                                 params={"range": "1d", "interval": "1d"},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
                m = r.json()["chart"]["result"][0]["meta"]
                p = m.get("regularMarketPrice")
                ch = m.get("regularMarketChangePercent")
                prev = m.get("chartPreviousClose") or m.get("previousClose")
                if ch is None and p and prev:
                    ch = (p / prev - 1) * 100
                if p:
                    out[s] = (p, ch)
                    break
            except Exception:
                continue
    return out


def related_stocks_line(symbols=RELATED_STOCKS):
    """'Acciones relacionadas: CRCL $86.32 (-3.8%) · ...'. Si Yahoo falla, solo los tickers."""
    try:
        q = stock_quotes(symbols)
    except Exception:
        q = {}
    parts = [f"<b>{s}</b> {fmt_price(q[s][0])} ({fmt_pct(q[s][1])})" if s in q else f"<b>{s}</b>" for s in symbols]
    return "🏛 Acciones relacionadas: " + " · ".join(parts)


def fmt_price(v):
    if not v:
        return "–"
    return f"${v:,.2f}" if v >= 1 else f"${v:.6g}"


def format_msg(it):
    esc = lambda s: html.escape(s or "")
    emo, name = LABELS[it["label"]]
    v, n = it.get("verif", ("", 1))
    vtxt = {"oficial": "✔ fuente oficial", "confirmado": f"✔ confirmado ({n} fuentes)",
            "sin confirmar": "❓ SIN CONFIRMAR"}.get(v, "")
    lines = [f"{emo} <b>{name}</b>  ·  score {it['score']}  ·  {esc(it['source'])}",
             f"<i>{category(it)}</i>" + (f"  ·  {vtxt}" if vtxt else ""),
             "", f"<b>{esc(it['title'][:300])}</b>"]
    body = body_without_title(it)
    if it["source"].startswith("Trump") and body:
        lines.append(esc(body[:400]))
    if it["tags"]:
        sent = "🟢 alcista" if it["sentiment"] > 0 else "🔴 bajista" if it["sentiment"] < 0 else "⚪ neutro"
        lines.append(f"\n{sent} · " + " · ".join(esc(t) for t in it["tags"]))
    if it["coins"]:
        lines.append("💰 " + "\n💰 ".join(coin_txt(c) for c in it["coins"]))
    if it["label"] == "seguir" and category(it) == "Regulación":
        lines.append(related_stocks_line())
    if it["label"] == "movido":
        if it.get("hot", 0) >= 2:
            lines.append("\n⚠️ <i>El precio ya ha reaccionado, pero la noticia sigue creciendo: "
                         "narrativa en marcha. Mira el gráfico antes de decidir.</i>")
        else:
            lines.append("\n⚠️ <i>El precio ya ha reaccionado: cuidado con entrar en el pico.</i>")
    if it["link"]:
        lines.append(f'\n<a href="{esc(it["link"])}">Abrir noticia</a>')
    return "\n".join(lines)


def send(msg, dry=False):
    """Manda el mensaje a Telegram. Devuelve True solo si Telegram lo aceptó."""
    if dry:
        print("\n" + html.unescape(re.sub(r"<[^>]+>", "", msg)) + "\n" + "─" * 50)
        return True
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=15)
        data = r.json()
        if r.ok and data.get("ok"):
            return True
        log(f"Error de Telegram ({r.status_code}): {data.get('description', '')}")
    except Exception as ex:
        log(f"Error enviando a Telegram: {type(ex).__name__}")
    return False

# ───────────────────────── PERIÓDICO HTML ─────────────────────────

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]


def hace(ts):
    m = int((time.time() - ts) // 60)
    if m < 1:
        return "ahora"
    if m < 60:
        return f"hace {m} min"
    if m < 1440:
        return f"hace {m // 60} h"
    return datetime.fromtimestamp(ts, TZ).strftime("%d/%m %H:%M")


def coins_html(coins):
    if not coins:
        return ""
    chips = []
    for c in coins:
        if c["sym"] in STABLECOINS:
            chips.append(f'<span class="coin"><b>{html.escape(c["sym"])}</b> <small>stablecoin</small></span>')
            continue
        ch = c.get("ch1h")
        cls = "up" if (ch or 0) > 0 else "down" if (ch or 0) < 0 else ""
        chips.append(f'<span class="coin {cls}"><b>{html.escape(c["sym"])}</b> '
                     f'{fmt_price(c.get("price"))} <small>1h {fmt_pct(ch)} · 24h {fmt_pct(c.get("ch24h"))}</small></span>')
    return f'<div class="coins">{"".join(chips)}</div>'


def meta_html(it):
    sent = "alcista" if it["sentiment"] > 0 else "bajista" if it["sentiment"] < 0 else ""
    sent_html = f'<span class="sent {sent}">{"▲" if sent == "alcista" else "▼"} {sent}</span>' if sent else ""
    tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in it["tags"])
    v, n = it.get("verif", ("", 1))
    verif = {"oficial": '<span class="verif ok">✔ fuente oficial</span>',
             "confirmado": f'<span class="verif ok">✔ confirmado · {n} fuentes</span>',
             "sin confirmar": '<span class="verif no">SIN CONFIRMAR</span>'}.get(v, "")
    return (f'<div class="meta"><span class="src">{html.escape(it["source"])}</span> · {hace(it["ts"])}'
            f' {sent_html} {verif}<span class="score" title="puntuación">{it["score"]}</span></div>'
            f'<div class="tags">{tags}</div>')


def title_html(it, tag="h3"):
    t = html.escape(it["title"])
    if it["link"]:
        t = f'<a href="{html.escape(it["link"])}" target="_blank" rel="noopener">{t}</a>'
    return f"<{tag}>{t}</{tag}>"


def body_without_title(it):
    """Texto sin el título repetido al principio (Truth Social repite el post en título y texto,
    a veces con espacios distintos: "U.S.!Beckton" / "U.S.! Beckton")."""
    text, title = it["text"].strip(), re.sub(r"\s+", "", it["title"]).rstrip("….")
    i = j = 0
    while i < len(text) and j < len(title):
        if text[i].isspace():
            i += 1
        elif text[i] == title[j]:
            i += 1
            j += 1
        else:
            return text          # no empieza por el título
    return text[i:].strip()      # si el texto entero era el título, queda vacío


def article(it, big=False):
    emo, name = LABELS[it["label"]]
    body = ""
    text = body_without_title(it)
    if text and (big or it["source"].startswith("Trump")):
        body = f'<p class="lede">{html.escape(text[:420 if big else 220])}</p>'
    return (f'<article class="{it["label"]}{" big" if big else ""}">'
            f'<div class="kicker">{emo} {name}</div><span class="cat">{html.escape(category(it))}</span>{title_html(it, "h2" if big else "h3")}'
            f'{meta_html(it)}{body}{coins_html(it["coins"])}{more_html(it)}</article>')


def group_stories(news):
    """Agrupa las noticias de la misma historia (same_story, ±24 h). Devuelve solo la de más puntuación
    de cada historia, con las demás en n["more"] (de la más reciente a la más antigua)."""
    leaders = []
    for n in sorted(news, key=lambda n: (-n["score"], n["ts"])):
        g = next((l for l in leaders
                  if any(abs(n["ts"] - m["ts"]) <= 86400 and same_story(n, m) for m in [l] + l["more"])), None)
        if g:
            g["more"].append(n)
        else:
            n["more"] = []
            leaders.append(n)
    for l in leaders:
        l["more"].sort(key=lambda n: -n["ts"])
    return leaders


def more_html(it, limit=8):
    more = it.get("more") or []
    if not more:
        return ""
    lis = "".join(f'<li>{title_html(n, "span")} <small>{html.escape(n["source"])} · {hace(n["ts"])}</small></li>'
                  for n in more[:limit])
    extra = f'<li><small>… y {len(more) - limit} más</small></li>' if len(more) > limit else ""
    word = "noticia más" if len(more) == 1 else "noticias más"
    return f'<div class="more"><span>y {len(more)} {word} sobre esto</span><ul>{lis}{extra}</ul></div>'


def marcador_html(con):
    rows = con.execute("SELECT ts, sym, label, score, sentiment, title, p0, p24, p7 FROM track "
                       "ORDER BY ts DESC").fetchall()
    if not rows:
        return ('<p class="empty">Aún no hay alertas con resultado. El marcador se llena solo: '
                'cada alerta se mide a las 24 h y a los 7 días.</p>')

    def chg(p0, p):
        return None if not (p0 and p) else (p / p0 - 1) * 100

    def hit(sent, ch):
        return ch > 0 if sent >= 0 else ch < 0   # en noticias bajistas, acertar es que baje

    trs = []
    for key in ("seguir", "ojo", "movido"):
        r = [x for x in rows if x[2] == key]
        c24 = [(x[4], chg(x[6], x[7])) for x in r if x[7]]
        c7 = [(x[4], chg(x[6], x[8])) for x in r if x[8]]

        def stats(lst):
            if not lst:
                return "–", "–"
            ac = sum(hit(s_, c) for s_, c in lst) / len(lst) * 100
            avg = sum(c for _, c in lst) / len(lst)
            return f"{ac:.0f}%", fmt_pct(avg)
        a24, m24 = stats(c24)
        a7, m7 = stats(c7)
        emo, name = LABELS[key]
        trs.append(f"<tr><td>{emo} {name.capitalize()}</td><td>{len(r)}</td>"
                   f"<td>{a24}</td><td>{m24}</td><td>{a7}</td><td>{m7}</td></tr>")

    recent = []
    for ts, sym, label, sc, sent, title, p0, p24, p7 in rows[:12]:
        c24, c7 = chg(p0, p24), chg(p0, p7)
        cls = lambda c: "" if c is None else ("up" if c > 0 else "down")
        recent.append(f'<li><span class="score s">{sc}</span>{LABELS[label][0]} <b>{html.escape(sym)}</b> '
                      f'{html.escape(title[:90])} <small>{hace(ts)} · {fmt_price(p0)}</small> '
                      f'<span class="res {cls(c24)}">24h {fmt_pct(c24) if c24 is not None else "pendiente"}</span> '
                      f'<span class="res {cls(c7)}">7d {fmt_pct(c7) if c7 is not None else "pendiente"}</span></li>')
    return (f'<table><thead><tr><th>Etiqueta</th><th>Alertas</th><th>Aciertos 24h</th><th>Media 24h</th>'
            f'<th>Aciertos 7d</th><th>Media 7d</th></tr></thead><tbody>{"".join(trs)}</tbody></table>'
            f'<p class="hint">Acierto = el precio fue en la dirección de la noticia (sube si es alcista, baja si es bajista). '
            f'Se mide la moneda principal de cada alerta.</p><ul class="track">{"".join(recent)}</ul>')


CONGRESS_PERIODICO_DAYS = 60   # declaraciones del Congreso que salen en el periódico


def politicos_html(con, people_news):
    """Sección "Políticos, famosos y Congreso": noticias con 🏛️/⭐ + operaciones cripto declaradas."""
    people_news = sorted(people_news, key=lambda n: (-n["score"], -n["ts"]))[:15]
    lis = []
    for n in people_news:
        who = " · ".join(html.escape(t) for t in n["tags"] if t.startswith(PEOPLE_TAG_PREFIXES))
        more = f' · y {len(n["more"])} más sobre esto' if n["more"] else ""
        lis.append(f'<li><span class="score s">{n["score"]}</span>{LABELS[n["label"]][0]} {title_html(n, "span")}'
                   f' <small>{who} · {html.escape(n["source"])} · {hace(n["ts"])}{more}</small></li>')
    news_html = "".join(lis) or '<li class="empty">Ningún político ni famoso ha hablado de una moneda en estas horas.</li>'
    trades = load_congress(con, CONGRESS_PERIODICO_DAYS)
    trs = []
    for t in trades[:15]:
        cls = "up" if t["kind"] == "compra" else "down" if t["kind"] == "vende" else ""
        what = html.escape(t["ticker"] or t["asset"][:30])
        member = html.escape(t["member"])
        if t["link"]:
            member = f'<a href="{html.escape(t["link"])}" target="_blank" rel="noopener">{member}</a>'
        flag = " 🔔" if t["alerted"] else ""
        trs.append(f'<tr><td>🏛️💰 {member} <small>({html.escape(t["party"] or "?")})</small>{flag}</td>'
                   f'<td class="res {cls}">{html.escape(t["kind"])}</td><td><b>{what}</b></td>'
                   f'<td>{html.escape(t["amount"])}</td><td>{t["tx_date"]}</td><td>{t["disc_date"]}</td>'
                   f'<td>{html.escape(t["committees"] or "–")}</td></tr>')
    trades_html = (f'<table><thead><tr><th>Congresista</th><th></th><th>Activo</th><th>Importe</th>'
                   f'<th>Operación</th><th>Declarada</th><th>Comité</th></tr></thead><tbody>{"".join(trs)}</tbody></table>'
                   if trs else f'<p class="empty">Ninguna operación cripto declarada en los últimos {CONGRESS_PERIODICO_DAYS} días.</p>')
    return (f'<ul class="pol">{news_html}</ul><h5>Congresistas que compran o venden cripto '
            f'<small>(Cámara de Representantes, STOCK Act · últimos {CONGRESS_PERIODICO_DAYS} días · '
            f'🔔 = avisado por Telegram)</small></h5>{trades_html}')


def build_periodico(con):
    news = load_news(con, PERIODICO_HOURS)
    for n in news:
        n["verif"] = verification(con, n)
    news = group_stories(news)   # una entrada por historia; el resto, debajo como "y N noticias más"
    by = {k: [n for n in news if n["label"] == k] for k in LABELS}
    for k in ("seguir", "ojo", "movido"):
        by[k].sort(key=lambda n: (-n["score"], -n["ts"]))

    day = [n for n in news if n["ts"] >= time.time() - 86400 and n["label"] != "breve"]
    portada = max(day, key=lambda n: (n["label"] == "seguir", n["score"], n["ts"]), default=None)
    pid = portada["id"] if portada else None

    def section(key, title, hint, limit=12):
        items = [n for n in by[key] if n["id"] != pid][:limit]
        inner = "".join(article(n) for n in items) or '<p class="empty">Nada por ahora.</p>'
        return f'<section class="col col-{key}"><h4>{title}</h4><p class="hint">{hint}</p>{inner}</section>'

    breves = "".join(
        f'<li><span class="score s">{n["score"]}</span>{title_html(n, "span")}'
        f' <small>{html.escape(n["source"])} · {hace(n["ts"])}'
        + (f' · y {len(n["more"])} más sobre esto' if n["more"] else "") + '</small></li>'
        for n in by["breve"][:30] if not is_people(n)) or '<li class="empty">Sin breves.</li>'

    now_dt = datetime.now(TZ)
    fecha = f"{DIAS[now_dt.weekday()].capitalize()}, {now_dt.day} de {MESES[now_dt.month - 1]} de {now_dt.year}"
    stats = (f'{len(by["seguir"])} a seguir · {len(by["ojo"])} para echar un ojo · '
             f'{len(by["movido"])} ya movidas · {len(by["breve"])} breves')
    front = article(portada, big=True) if portada else \
        '<article class="big empty"><h2>Sin noticias destacadas en las últimas 24 h</h2></article>'

    page = PERIODICO_TEMPLATE.format(
        refresh=POLL_SECONDS, fecha=fecha, hora=now_dt.strftime("%H:%M"), stats=stats, portada=front,
        seguir=section("seguir", "🔥 A seguir", "Puntuación alta y el precio todavía no ha reaccionado."),
        ojo=section("ojo", "👀 Échale un ojo", "Relevante, pero menos clara."),
        movido=section("movido", "⚠️ Ya se ha movido", f"El precio ya se movió ±{MOVED_1H_PCT}% en 1 h o ±{MOVED_24H_PCT}% en 24 h. Probablemente tarde.", 8),
        breves=breves, horas=PERIODICO_HOURS, marcador=marcador_html(con), agenda=agenda_html(),
        politicos=politicos_html(con, [n for n in news if is_people(n)]))
    os.makedirs(os.path.dirname(PERIODICO_PATH) or ".", exist_ok=True)
    tmp = PERIODICO_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, PERIODICO_PATH)


# ───────────────────────── AGENDA ─────────────────────────
# agenda.json: [{"fecha":"2026-10-14","hora":"08:30","tz":"America/New_York",
#                "evento":"IPC EE. UU.","impacto":"alto","tipo":"macro"}, ...]
# La hora se escribe en la zona del evento y se convierte a hora de Alemania con zoneinfo:
# así el desfase EE. UU./Alemania es correcto aunque cambien de horario en días distintos.

def load_agenda():
    try:
        with open(AGENDA_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as ex:
        log(f"Error leyendo {AGENDA_PATH}: {ex}")
        return []
    out = []
    for e in raw:
        try:
            hh, mm = (e.get("hora") or "00:00").split(":")
            src = datetime.fromisoformat(e["fecha"]).replace(hour=int(hh), minute=int(mm),
                                                             tzinfo=ZoneInfo(e.get("tz", "America/New_York")))
            out.append({**e, "when": src.astimezone(TZ), "has_time": bool(e.get("hora"))})
        except Exception as ex:
            log(f"Evento de agenda mal formado {e}: {ex}")
    return sorted(out, key=lambda e: e["when"])


def days_until(e, today=None):
    """Días hasta el evento, contando por fecha en Alemania (misma función para preview y producción)."""
    today = today or datetime.now(TZ).date()
    return (e["when"].date() - today).days


def cuando(d):
    return "HOY" if d == 0 else "mañana" if d == 1 else f"en {d} días"


def agenda_html():
    evs = [e for e in load_agenda() if 0 <= days_until(e) <= AGENDA_DIAS]
    if not evs:
        return '<p class="empty">Sin eventos en los próximos días (añádelos en agenda.json).</p>'
    lis = []
    for e in evs[:10]:
        d = days_until(e)
        w = e["when"]
        hora = w.strftime("%H:%M") if e["has_time"] else ""
        cls = " ".join(x for x in (e.get("impacto", ""), "hoy" if d == 0 else "") if x)
        lis.append(f'<li class="{cls}"><b>{DIAS[w.weekday()][:3]} {w.day}/{w.month}</b> {hora} · '
                   f'{html.escape(e["evento"])} <small>({cuando(d)})</small></li>')
    return f"<ul>{''.join(lis)}</ul>"


def morning_edition(con, dry):
    """Una vez al día, a partir de EDICION_HORA en Alemania: agenda (T-0 y T-3) + top noticias + marcador."""
    now = datetime.now(TZ)
    key = now.strftime("%Y-%m-%d")
    if now.hour < EDICION_HORA or meta_get(con, "edicion") == key:
        return
    lines = [f"☀️ <b>Edición de la mañana</b> · {DIAS[now.weekday()]} {now.day} de {MESES[now.month - 1]}"]

    hoy = [e for e in load_agenda() if days_until(e) == 0]
    t3 = [e for e in load_agenda() if days_until(e) == 3]
    if hoy or t3:
        lines.append("\n📅 <b>Agenda</b>")
        for e in hoy:
            h = e["when"].strftime("%H:%M") if e["has_time"] else ""
            lines.append(f"🔴 HOY {h} · {html.escape(e['evento'])}")
        for e in t3:
            lines.append(f"🟡 En 3 días ({e['when'].day}/{e['when'].month}) · {html.escape(e['evento'])}")

    news = [n for n in group_stories(load_news(con, 24)) if n["label"] in ("seguir", "ojo")]
    news.sort(key=lambda n: (n["label"] == "seguir", n["score"]), reverse=True)
    if news:
        lines.append("\n📰 <b>Lo más importante de las últimas 24 h</b>")
        for n in news[:5]:
            emo = LABELS[n["label"]][0]
            t = html.escape(n["title"][:110])
            more = f" · +{len(n['more'])} más" if n["more"] else ""
            lines.append(f'{emo} <a href="{html.escape(n["link"])}">{t}</a> ({n["score"]}{more})' if n["link"]
                         else f"{emo} {t} ({n['score']}{more})")
    else:
        lines.append("\n📰 Noche tranquila: ninguna noticia destacada en 24 h.")

    if send("\n".join(lines), dry):   # si Telegram falla, se reintenta en la siguiente pasada
        log("Edición de la mañana enviada.")
        meta_set(con, "edicion", key)


PERIODICO_TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>El Diario Cripto</title>
<link rel="manifest" href="manifest.json">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="theme-color" content="#1b1a17">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Diario Cripto">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{{--paper:#f6f1e7;--ink:#1b1a17;--muted:#6b665c;--rule:#1b1a17;--soft:#d9d1c1;
  --hot:#b3261e;--hot-bg:#f3dcd5;--eye:#8a5a00;--eye-bg:#f1e4c4;--moved:#5b5b5b;--moved-bg:#e4e0d8;
  --up:#1e6b3a;--down:#b3261e;--chip:#ece5d6}}
@media (prefers-color-scheme:dark){{:root{{--paper:#17150f;--ink:#ece6d8;--muted:#a39d8f;--rule:#ece6d8;--soft:#3a362c;
  --hot:#f08a7e;--hot-bg:#3a1f1a;--eye:#e3b356;--eye-bg:#33290f;--moved:#b7b2a8;--moved-bg:#2a2720;
  --up:#6fcf8d;--down:#f08a7e;--chip:#25221b}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 "Source Serif 4",Georgia,serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:20px 16px 60px}}
a{{color:inherit;text-decoration:none}} a:hover{{text-decoration:underline}}
header{{text-align:center;border-bottom:3px double var(--rule);padding-bottom:10px}}
.topline{{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font:600 12px "IBM Plex Mono",monospace;
  text-transform:uppercase;letter-spacing:.06em;color:var(--muted);border-bottom:1px solid var(--rule);padding-bottom:6px}}
h1{{font:900 clamp(40px,8vw,84px)/1 "Playfair Display",serif;margin:14px 0 6px;letter-spacing:-.01em}}
.stats{{font:13px "IBM Plex Mono",monospace;color:var(--muted);border-top:1px solid var(--rule);padding-top:6px}}
.portada{{padding:22px 0;border-bottom:1px solid var(--rule)}}
article{{padding:14px 0;border-bottom:1px solid var(--soft)}}
article:last-child{{border-bottom:0}}
.kicker{{font:600 11px "IBM Plex Mono",monospace;letter-spacing:.1em;text-transform:uppercase;display:inline-block;padding:2px 6px;border-radius:2px}}
.seguir .kicker{{color:var(--hot);background:var(--hot-bg)}}
.ojo .kicker{{color:var(--eye);background:var(--eye-bg)}}
.movido .kicker{{color:var(--moved);background:var(--moved-bg)}}
h2{{font:900 clamp(28px,4.4vw,48px)/1.08 "Playfair Display",serif;margin:10px 0 8px;max-width:22ch}}
h3{{font:700 20px/1.2 "Playfair Display",serif;margin:8px 0 6px}}
.movido h3{{color:var(--muted)}}
.lede{{margin:6px 0;max-width:70ch}} .big .lede{{font-size:18px}}
.meta{{font:12px "IBM Plex Mono",monospace;color:var(--muted);display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.src{{font-weight:600;color:var(--ink)}}
.score{{margin-left:auto;font:600 12px "IBM Plex Mono",monospace;border:1px solid var(--ink);border-radius:50%;
  min-width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center}}
.score.s{{margin:0 8px 0 0;min-width:24px;height:24px;font-size:11px;flex:none}}
.sent.alcista{{color:var(--up)}} .sent.bajista{{color:var(--down)}}
.tags{{margin-top:4px;display:flex;gap:4px;flex-wrap:wrap}}
.tag{{font:11px "IBM Plex Mono",monospace;border:1px solid var(--soft);padding:0 5px;border-radius:2px;color:var(--muted)}}
.coins{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}
.coin{{font:12px "IBM Plex Mono",monospace;background:var(--chip);padding:3px 7px;border-radius:2px}}
.coin small{{color:var(--muted)}} .coin.up b{{color:var(--up)}} .coin.down b{{color:var(--down)}}
.grid{{display:grid;grid-template-columns:1.25fr 1fr 0.85fr;gap:0;margin-top:6px}}
.col{{padding:14px 18px;border-right:1px solid var(--rule);min-width:0}}
.col:first-child{{padding-left:0}} .col:last-child{{border-right:0;padding-right:0}}
.col h4{{font:900 22px "Playfair Display",serif;margin:0;border-bottom:2px solid var(--rule);padding-bottom:4px}}
.hint{{font-size:13px;color:var(--muted);margin:4px 0 0;font-style:italic}}
.breves{{margin-top:18px;border-top:3px double var(--rule);padding-top:10px}}
.breves h4{{font:900 22px "Playfair Display",serif;margin:0 0 6px}}
.breves ul{{list-style:none;margin:0;padding:0;columns:2;column-gap:32px}}
.breves li{{break-inside:avoid;padding:6px 0;border-bottom:1px dotted var(--soft);display:flex;gap:4px;align-items:baseline;flex-wrap:wrap}}
.breves small{{color:var(--muted);font:11px "IBM Plex Mono",monospace}}
.empty{{color:var(--muted);font-style:italic}}
.marcador{{margin-top:22px;border-top:3px double var(--rule);padding-top:10px}}
.marcador h4{{font:900 22px "Playfair Display",serif;margin:0 0 8px}}
.marcador table{{width:100%;border-collapse:collapse;font:13px "IBM Plex Mono",monospace}}
.marcador th,.marcador td{{text-align:right;padding:6px 8px;border-bottom:1px solid var(--soft)}}
.marcador th:first-child,.marcador td:first-child{{text-align:left}}
.marcador th{{border-bottom:2px solid var(--rule);font-weight:600}}
.track{{list-style:none;padding:0;margin:10px 0 0}}
.track li{{padding:6px 0;border-bottom:1px dotted var(--soft);display:flex;gap:6px;align-items:baseline;flex-wrap:wrap}}
.track small{{color:var(--muted);font:11px "IBM Plex Mono",monospace}}
.res{{font:600 12px "IBM Plex Mono",monospace;color:var(--muted)}} .res.up{{color:var(--up)}} .res.down{{color:var(--down)}}
@media (max-width:600px){{.marcador table{{font-size:11px}}.marcador th,.marcador td{{padding:4px 3px}}}}
.agenda{{padding:12px 0;border-bottom:1px solid var(--rule)}}
.agenda h4{{font:900 18px "Playfair Display",serif;margin:0 0 6px;display:inline-block;margin-right:12px}}
.agenda ul{{list-style:none;margin:0;padding:0;display:flex;gap:8px;flex-wrap:wrap}}
.agenda li{{font:12px "IBM Plex Mono",monospace;background:var(--chip);padding:5px 9px;border-radius:2px;border-left:3px solid var(--soft)}}
.agenda li.alto{{border-left-color:var(--hot)}} .agenda li.hoy{{background:var(--hot-bg)}}
.agenda li b{{font-weight:600}} .agenda li small{{color:var(--muted)}}
.cat{{font:600 11px "IBM Plex Mono",monospace;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-left:8px}}
.verif{{font:600 11px "IBM Plex Mono",monospace;padding:0 5px;border-radius:2px}}
.verif.ok{{color:var(--up)}} .verif.no{{color:var(--eye);background:var(--eye-bg)}}
.more{{margin-top:8px;padding-left:10px;border-left:2px solid var(--soft);font-size:14px}}
.more>span{{font:600 11px "IBM Plex Mono",monospace;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}}
.more ul{{list-style:none;margin:4px 0 0;padding:0}} .more li{{margin:2px 0;line-height:1.35}}
.more small{{color:var(--muted);font-family:"IBM Plex Mono",monospace;font-size:11px}}
.politicos{{margin-top:18px;border-top:3px double var(--rule);padding-top:10px}}
.politicos h4{{font:900 22px "Playfair Display",serif;margin:0 0 6px}}
.politicos h5{{font:700 16px "Playfair Display",serif;margin:14px 0 6px}}
.politicos h5 small{{font:11px "IBM Plex Mono",monospace;color:var(--muted)}}
.pol{{list-style:none;margin:0;padding:0}}
.pol li{{padding:6px 0;border-bottom:1px dotted var(--soft);display:flex;gap:4px;align-items:baseline;flex-wrap:wrap}}
.pol small{{color:var(--muted);font:11px "IBM Plex Mono",monospace}}
.politicos table{{width:100%;border-collapse:collapse;font:12px "IBM Plex Mono",monospace;display:block;overflow-x:auto}}
.politicos th,.politicos td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--soft);white-space:nowrap}}
.politicos th{{border-bottom:2px solid var(--rule);font-weight:600}}
footer{{margin-top:24px;font:11px "IBM Plex Mono",monospace;color:var(--muted);text-align:center}}
@media (max-width:900px){{.grid{{grid-template-columns:1fr}}.col{{border-right:0;padding:14px 0;border-bottom:1px solid var(--rule)}}
  .breves ul{{columns:1}}}}
</style></head><body><div class="wrap">
<header>
  <div class="topline"><span>{fecha}</span><span>Edición en vivo · {hora}</span></div>
  <h1>El Diario Cripto</h1>
  <div class="stats">{stats} · últimas {horas} h</div>
</header>
<div class="agenda"><h4>📅 Agenda</h4>{agenda}</div>
<div class="portada">{portada}</div>
<div class="grid">{seguir}{ojo}{movido}</div>
<div class="politicos"><h4>🏛️ Políticos, famosos y Congreso</h4>{politicos}</div>
<div class="breves"><h4>Breves</h4><ul>{breves}</ul></div>
<div class="marcador"><h4>📊 Marcador de aciertos</h4>{marcador}</div>
<footer>Generado por crypto_news_alert.py · se actualiza solo · no es consejo financiero</footer>
</div></body></html>"""

# ───────────────────────── BUCLE ─────────────────────────

def log(msg):
    print(f"[{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def cycle(con, dry, first=False):
    new = []
    for it in collect(con):
        if not is_seen(con, it["id"]):
            mark_seen(con, it["id"])
            new.append(it)
    # En el primer arranque las noticias son viejas: van al periódico pero sin Telegram
    # y sin la etiqueta "ya se ha movido" (el precio actual no dice nada de ellas).
    # Las publicadas hace más de ALERT_MAX_AGE_HOURS tampoco se avisan: llegan tarde.
    for it in process(new, check_moved=not first, con=con):
        save_news(con, it)
        it["verif"] = verification(con, it)
        if first or it["ts"] < time.time() - ALERT_MAX_AGE_HOURS * 3600:
            continue
        start_tracking(con, it)
        if it["label"] not in TELEGRAM_LABELS:
            continue
        msg = f"{LABELS[it['label']][1]} ({it['score']}) {it['source']}: {it['title'][:80]}"
        if already_alerted(con, it):
            log(f"Repetida, solo periódico: {msg}")
        elif send(format_msg(it), dry):
            record_alert(con, it)
            log(msg)
    con.commit()
    update_tracking(con)
    check_congress(con, dry)   # una vez al día (la primera vez, sin avisos)
    if not first:
        morning_edition(con, dry)
    build_periodico(con)
    return len(new)


def rescore(con):
    """Vuelve a puntuar las noticias guardadas con las reglas actuales (sin avisar por Telegram).
    Las que bajan de BRIEF_MIN salen del periódico; las 'ya se ha movido' lo siguen siendo."""
    bases = {"Binance Listing": 5, **{n: b for n, _, b in RSS_FEEDS}, **{n: b for n, _, b in TELEGRAM_CHANNELS}}
    old = {n["id"]: n for n in load_news(con, 24 * 365)}
    items = [{"id": n["id"], "source": n["source"], "title": n["title"], "text": n["text"],
              "link": n["link"], "base": bases.get(n["source"], 0), "published": n["ts"]} for n in old.values()]
    out = process(items, check_moved=False, con=con)
    for it in out:
        if old[it["id"]]["label"] == "movido" and it["score"] >= ALERT_THRESHOLD:
            it["label"] = "movido"
        save_news(con, it)
    kept = {it["id"] for it in out}
    con.executemany("DELETE FROM news WHERE id=?", [(i,) for i in old if i not in kept])
    con.commit()
    return len(old), len(out)


def main():
    dry = "--dry" in sys.argv
    if "--test" in sys.argv:
        send("✅ crypto_news_alert conectado correctamente.", dry)
        return
    con = db()
    if "--repuntuar" in sys.argv:
        total, kept = rescore(con)
        build_periodico(con)
        print(f"Repuntuadas {total} noticias ({kept} siguen en el periódico). Periódico regenerado.")
        return
    if "--periodico" in sys.argv:
        build_periodico(con)
        print(f"Periódico regenerado: {os.path.abspath(PERIODICO_PATH)}")
        return
    if not dry and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID (o usa --dry).")

    first = con.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    if first:
        n = cycle(con, dry, first=True)
        log(f"Arranque inicial: {n} noticias cargadas en el periódico (sin avisar por Telegram).")

    if "--once" in sys.argv:
        if not first:
            n = cycle(con, dry)
            log(f"Pasada única: {n} noticias nuevas.")
        return

    log(f"Vigilando {len(RSS_FEEDS) + 1} fuentes cada {POLL_SECONDS}s. Periódico: {os.path.abspath(PERIODICO_PATH)}")
    while True:
        try:
            cycle(con, dry)
        except Exception as ex:
            log(f"Error en el ciclo: {ex}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
