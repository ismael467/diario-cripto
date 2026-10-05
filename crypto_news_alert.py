"""
crypto_news_alert.py — Bot de noticias cripto: alertas en Telegram + periódico HTML local.

Fuentes (sin API keys):
  - Posts de Trump en Truth Social (espejo RSS de trumpstruth.org)
  - Anuncios de nuevos listings en Binance
  - RSS de medios cripto: CoinDesk, Cointelegraph, The Block, Decrypt, Blockworks

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
  python crypto_news_alert.py --once       # una sola pasada y sale (lo que usa GitHub Actions)
"""

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
]

# Watchlist: símbolo -> (id CoinGecko, [alias / nombres del proyecto])
WATCHLIST = {
    "BTC":  ("bitcoin", ["bitcoin", "btc"]),
    "ETH":  ("ethereum", ["ethereum", "ether", "eth"]),
    "SOL":  ("solana", ["solana", "sol"]),
    "HYPE": ("hyperliquid", ["hyperliquid", "hype"]),
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
}

# Catalizadores: (regex, puntos, etiqueta, sentido) sentido: +1 alcista, -1 bajista, 0 neutro
CATALYSTS = [
    (r"\bfee switch\b|\bprotocol fees?\b", 4, "fee switch / fees", 1),
    (r"\bbuy ?backs?\b|\btoken burn\b|\bburns?\b", 3, "buyback / burn", 1),
    (r"\betfs?\b", 3, "ETF", 1),
    (r"\bapprov(e|es|ed|al)\b", 2, "aprobación", 1),
    (r"\blist(s|ed|ing)\b|\bwill list\b|\bnew listing\b", 3, "listing", 1),
    (r"\bstrategic (bitcoin |crypto )?reserve\b", 3, "reserva estratégica", 1),
    (r"\bblackrock\b|\bfidelity\b|\bgrayscale\b|\bvanguard\b", 2, "institucional", 1),
    (r"\bbanks?\b|\bbanking\b|\bclearing house\b|\bjpmorgan\b|\bswift\b|\bdtcc\b|\bvisa\b|\bmastercard\b|\bnasdaq\b|\bciti(group)?\b|\bgoldman\b", 4, "bancos / TradFi", 1),
    (r"\bselect(s|ed)\b|\bchose\b|\bchosen\b|\bpick(s|ed)\b|\bteams up\b|\bwins\b.*\b(contract|deal|role)\b", 2, "elegido / gana contrato", 1),
    (r"\btokeni[sz](ed|ation|e|es)\b|\brwa\b|\breal[- ]world assets?\b", 2, "tokenización / RWA", 1),
    (r"\bpartnership\b|\bpartners with\b|\bintegrat(es|ion)\b", 1, "partnership", 1),
    (r"\bacquir(e|es|ed)\b|\bacquisition\b", 2, "adquisición", 1),
    (r"\bairdrop\b|\btoken launch\b|\btge\b", 2, "airdrop / TGE", 1),
    (r"\bmainnet\b|\bupgrade\b", 1, "upgrade", 1),
    (r"\bsec\b|\bcftc\b|\batkins\b", 2, "regulador", 0),
    (r"\bexecutive order\b|\bsenate\b|\bcongress\b|\bclarity act\b|\bgenius act\b", 2, "política / ley", 0),
    (r"\bhack(ed)?\b|\bexploit(ed)?\b|\bdrain(ed)?\b|\bstolen\b", 4, "hack / exploit", -1),
    (r"\bdelist(s|ed|ing)?\b", 4, "delisting", -1),
    (r"\blawsuit\b|\bsues?\b|\bcharged\b|\bindict", 2, "demanda", -1),
    (r"\bunlock\b", 1, "token unlock", -1),
]

# Palabras que hacen que un post de Trump sea relevante (si no, se ignora)
TRUMP_CRYPTO_WORDS = r"crypto|bitcoin|btc|coin|token|blockchain|stablecoin|defi|digital asset|" \
                     r"hyperliquid|solana|ethereum|xrp|world liberty|reserve|sec\b|fed\b|rate|tariff"

# Categoría de cada catalizador (la del catalizador con más peso manda)
TAG_CAT = {
    "fee switch / fees": "Tokenomics", "buyback / burn": "Tokenomics", "token unlock": "Tokenomics",
    "airdrop / TGE": "Tokenomics", "ETF": "Institucional", "institucional": "Institucional",
    "bancos / TradFi": "Institucional", "elegido / gana contrato": "Adopción",
    "tokenización / RWA": "Adopción", "partnership": "Adopción", "adquisición": "Adopción",
    "listing": "Exchange", "delisting": "Exchange", "aprobación": "Regulación",
    "regulador": "Regulación", "política / ley": "Regulación", "reserva estratégica": "Regulación",
    "hack / exploit": "Seguridad", "demanda": "Regulación", "upgrade": "Tecnología",
    "Trump menciona proyecto": "Política",
}

# Fuentes primarias: el anuncio ES la noticia, no hace falta confirmación de terceros
OFFICIAL_SOURCES = ("Binance Listing", "Trump (Truth Social)")

# Rumores y predicciones: se penalizan (regla editorial: hecho ≠ opinión ≠ predicción)
TOKENIZED_STOCKS_RE = r"tokeni[sz]ed stocks?|xstocks|stock tokens?|equit(y|ies)"
RUMOR_RE = r"\brumou?rs?\b|\breportedly\b|\bsources say\b|\bunconfirmed\b|\bspeculat"
PREDICTION_RE = (r"price prediction|price forecast|price analysis|could (reach|hit|soar|explode)|"
                 r"will (reach|hit) \$|\bto \$\d|\d+x (gains|potential)|next 100x|top \d+ (altcoins|coins|cryptos) to buy")

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
                (it["id"], it["ts"], c["sym"], it["label"], it["score"], it["sentiment"],
                 it["title"], c["price"]))


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
    if not it["coins"]:
        return "", 1
    srcs = sources_for(con, it["coins"], it["ts"], it["id"]) | {it["source"]}
    return ("confirmado" if len(srcs) >= 2 else "sin confirmar"), len(srcs)


def category(it):
    best = None
    for pattern, p, tag, _ in CATALYSTS:
        if tag in it["tags"] and (best is None or p > best[0]):
            best = (p, TAG_CAT.get(tag, "Mercado"))
    if "Trump menciona proyecto" in it["tags"]:
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
                items.append({"id": f"{name}|{e.get('id') or link or title}", "source": name,
                              "title": title, "text": summary, "link": link, "base": base})
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
                              "base": 5})
    except Exception as ex:
        log(f"Error en Binance: {ex}")
    return items


def collect():
    return fetch_binance_listings() + fetch_rss()

# ───────────────────────── PUNTUACIÓN ─────────────────────────

def detect_coins(text):
    t = " " + text.lower() + " "
    found = []
    for sym, (_, aliases) in WATCHLIST.items():
        for a in aliases:
            if re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", t):
                found.append(sym)
                break
    # Tickers tipo $XYZ o (XYZ) que no estén en la watchlist
    for m in re.findall(r"\$([A-Z]{2,10})\b|\(([A-Z]{2,10})\)", text):
        s = m[0] or m[1]
        if s not in found and s not in ("USD", "SEC", "ETF", "CEO", "US", "EU", "UK"):
            found.append(s)
    return found


def score(item):
    """Devuelve (puntos, [etiquetas], sentimiento, [monedas])."""
    text = f"{item['title']} {item['text']}"
    low = text.lower()
    if item["source"].startswith("Trump") and not re.search(TRUMP_CRYPTO_WORDS, low):
        return 0, [], 0, []

    pts, tags, sent = item["base"], [], 0
    for pattern, p, tag, s in CATALYSTS:
        if re.search(pattern, low):
            pts += p
            tags.append(tag)
            sent += s

    coins = detect_coins(text)
    # Acciones tokenizadas (ej. "Adds Adobe (ADBEB), Hewlett Packard (HPEB)..."): no son cripto
    if re.search(TOKENIZED_STOCKS_RE, item["title"].lower()) or \
            len(re.findall(r"\(([A-Z]{2,10})\)", item["title"])) >= 4 or \
            (item["source"] == "Binance Listing" and   # Binance usa sufijo B: ADBEB, HPEB...
             any(len(c) >= 4 and c.endswith("B") and c not in WATCHLIST for c in coins)):
        return 0, ["acciones tokenizadas"], 0, coins
    known = [c for c in coins if c in WATCHLIST]
    pts += min(len(known), 3) * 2
    pts += min((len(coins) - len(known)) * 2, 2)   # tickers fuera de la watchlist: máx. 2 puntos

    if item["source"].startswith("Trump") and coins:
        pts += 5
        tags.append("Trump menciona proyecto")
        sent += 1

    if re.search(PREDICTION_RE, low):          # predicciones de precio: no son noticia
        return 0, ["predicción"], 0, coins
    if re.search(RUMOR_RE, low):
        pts -= 3
        tags.append("rumor")
    return pts, tags, sent, coins

# ───────────────────────── PRECIOS ─────────────────────────

def prices(symbols):
    """{SYM: {"price", "ch1h", "ch24h"}} — CoinGecko para la watchlist, DexScreener para el resto."""
    out = {}
    known = [s for s in symbols if s in WATCHLIST]
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
        try:
            pairs = requests.get("https://api.dexscreener.com/latest/dex/search",
                                 params={"q": s}, headers=UA, timeout=10).json().get("pairs") or []
            pairs = [p for p in pairs if (p.get("baseToken") or {}).get("symbol", "").upper() == s]
            if pairs:
                p = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
                pc = p.get("priceChange") or {}
                out[s] = {"price": float(p.get("priceUsd") or 0), "ch1h": pc.get("h1"), "ch24h": pc.get("h24")}
        except Exception:
            pass
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


def coverage_24h(con, sym):
    """Cuántas noticias de las últimas 24 h ya mencionaban esta moneda (para detectar 'tema del día')."""
    if con is None:
        return 0
    since = int(time.time()) - 86400
    return con.execute("SELECT COUNT(*) FROM news WHERE ts>=? AND coins LIKE ?",
                       (since, f'%"sym": "{sym}"%')).fetchone()[0]


def process(raw_items, check_moved=True, con=None):
    """Puntúa, busca precios (en bloque) y etiqueta. Devuelve solo lo que llega a 'Breve'."""
    scored = []
    for it in raw_items:
        pts, tags, sent, coins = score(it)
        # Tema del día: si una moneda acumula noticias, la narrativa está creciendo
        hot = max((coverage_24h(con, c) for c in coins), default=0)
        if hot >= 2:
            pts += 3
            tags.append(f"tema del día ({hot + 1} noticias en 24 h)")
        if pts >= BRIEF_MIN:
            scored.append((it, pts, tags, sent, coins, hot))
    wanted = sorted({c for (_, pts, _, _, coins, _) in scored if pts >= ALERT_THRESHOLD for c in coins})
    px = prices(wanted) if wanted else {}
    out = []
    for it, pts, tags, sent, coins, hot in scored:
        cpx = [{"sym": c, **px.get(c, {})} for c in coins[:5]]
        out.append({**it, "ts": int(time.time()), "score": pts, "tags": tags, "sentiment": sent,
                    "coins": cpx, "hot": hot, "label": label_for(pts, cpx, check_moved)})
    return out

# ───────────────────────── TELEGRAM ─────────────────────────

def fmt_pct(v):
    return "–" if v is None else f"{v:+.1f}%"


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
    if it["source"].startswith("Trump") and it["text"]:
        lines.append(esc(it["text"][:400]))
    if it["tags"]:
        sent = "🟢 alcista" if it["sentiment"] > 0 else "🔴 bajista" if it["sentiment"] < 0 else "⚪ neutro"
        lines.append(f"\n{sent} · " + " · ".join(esc(t) for t in it["tags"]))
    if it["coins"]:
        parts = [f"<b>{c['sym']}</b> {fmt_price(c.get('price'))} (1h {fmt_pct(c.get('ch1h'))} · 24h {fmt_pct(c.get('ch24h'))})"
                 for c in it["coins"]]
        lines.append("💰 " + "\n💰 ".join(parts))
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
    if dry:
        print("\n" + html.unescape(re.sub(r"<[^>]+>", "", msg)) + "\n" + "─" * 50)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=15)
    except Exception as ex:
        log(f"Error enviando a Telegram: {ex}")

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


def article(it, big=False):
    emo, name = LABELS[it["label"]]
    body = ""
    text = it["text"].strip()
    if text.startswith(it["title"].strip()):          # Truth Social repite el título en el texto
        text = text[len(it["title"].strip()):].strip()
    if text and (big or it["source"].startswith("Trump")):
        body = f'<p class="lede">{html.escape(text[:420 if big else 220])}</p>'
    return (f'<article class="{it["label"]}{" big" if big else ""}">'
            f'<div class="kicker">{emo} {name}</div><span class="cat">{html.escape(category(it))}</span>{title_html(it, "h2" if big else "h3")}'
            f'{meta_html(it)}{body}{coins_html(it["coins"])}</article>')


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


def build_periodico(con):
    news = load_news(con, PERIODICO_HOURS)
    for n in news:
        n["verif"] = verification(con, n)
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
        f' <small>{html.escape(n["source"])} · {hace(n["ts"])}</small></li>'
        for n in by["breve"][:30]) or '<li class="empty">Sin breves.</li>'

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
        breves=breves, horas=PERIODICO_HOURS, marcador=marcador_html(con), agenda=agenda_html())
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

    news = [n for n in load_news(con, 24) if n["label"] in ("seguir", "ojo")]
    news.sort(key=lambda n: (n["label"] == "seguir", n["score"]), reverse=True)
    if news:
        lines.append("\n📰 <b>Lo más importante de las últimas 24 h</b>")
        for n in news[:5]:
            emo = LABELS[n["label"]][0]
            t = html.escape(n["title"][:110])
            lines.append(f'{emo} <a href="{html.escape(n["link"])}">{t}</a> ({n["score"]})' if n["link"] else f"{emo} {t} ({n['score']})")
    else:
        lines.append("\n📰 Noche tranquila: ninguna noticia destacada en 24 h.")

    if len(lines) > 1:
        send("\n".join(lines), dry)
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
<div class="breves"><h4>Breves</h4><ul>{breves}</ul></div>
<div class="marcador"><h4>📊 Marcador de aciertos</h4>{marcador}</div>
<footer>Generado por crypto_news_alert.py · se actualiza solo · no es consejo financiero</footer>
</div></body></html>"""

# ───────────────────────── BUCLE ─────────────────────────

def log(msg):
    print(f"[{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def cycle(con, dry, first=False):
    new = []
    for it in collect():
        if not is_seen(con, it["id"]):
            mark_seen(con, it["id"])
            new.append(it)
    # En el primer arranque las noticias son viejas: van al periódico pero sin Telegram
    # y sin la etiqueta "ya se ha movido" (el precio actual no dice nada de ellas).
    for it in process(new, check_moved=not first, con=con):
        save_news(con, it)
        it["verif"] = verification(con, it)
        if not first:
            start_tracking(con, it)
        if not first and it["label"] in TELEGRAM_LABELS:
            send(format_msg(it), dry)
            log(f"{LABELS[it['label']][1]} ({it['score']}) {it['source']}: {it['title'][:80]}")
    con.commit()
    update_tracking(con)
    if not first:
        morning_edition(con, dry)
    build_periodico(con)
    return len(new)


def main():
    dry = "--dry" in sys.argv
    if "--test" in sys.argv:
        send("✅ crypto_news_alert conectado correctamente.", dry)
        return
    con = db()
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
