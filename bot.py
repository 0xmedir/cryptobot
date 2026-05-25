#!/usr/bin/env python3
"""
Persona Bot – Final Production Build (9‑coin grid, live ticker, maintenance)
"""

import telebot
import requests
import threading
import time
import json
import os
import sys
import re
import sqlite3
import signal
import logging
import html
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import websocket

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN missing")
    sys.exit(1)

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "7458428092").split(",") if x.strip()]
COOLDOWN_SECONDS = 2
MAX_ALERTS_PER_USER = 20
MAX_CA_LENGTH = 100
MAX_TEXT_LEN = 200
MAX_HISTORY = 3
MAX_LIVE_TICKERS = 50

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("PersonaBot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
os.makedirs("data", exist_ok=True)

# =========================
# DATABASE
# =========================
db_path = "data/persona.db"
db_lock = threading.RLock()
conn = sqlite3.connect(db_path, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=ON")

def db_query(query, params=(), fetch_one=False, fetch_all=False, retries=3):
    for attempt in range(retries):
        try:
            with db_lock:
                cur = conn.cursor()
                cur.execute(query, params)
                if fetch_one:
                    row = cur.fetchone()
                    conn.commit()
                    return row
                if fetch_all:
                    rows = cur.fetchall()
                    conn.commit()
                    return rows
                conn.commit()
                return cur.lastrowid
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise
    return None

def init_db():
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='profiles'")
        if cur.fetchone():
            cur.execute("PRAGMA table_info(profiles)")
            columns = [col[1] for col in cur.fetchall()]
            if "streak" in columns:
                log.info("Old profiles schema detected. Dropping and recreating.")
                conn.execute("DROP TABLE profiles")
            elif "username" not in columns:
                log.info("Adding username and first_name columns to profiles.")
                conn.execute("ALTER TABLE profiles ADD COLUMN username TEXT")
                conn.execute("ALTER TABLE profiles ADD COLUMN first_name TEXT")
    except Exception as e:
        log.error(f"Migration check error: {e}")

    db_query("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            coin TEXT,
            target REAL,
            direction TEXT,
            active INTEGER DEFAULT 1,
            created_at INTEGER
        )
    """)
    db_query("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            join_date INTEGER,
            total_interactions INTEGER DEFAULT 0,
            alerts_set INTEGER DEFAULT 0,
            alerts_triggered INTEGER DEFAULT 0,
            username TEXT,
            first_name TEXT
        )
    """)
    db_query("""
        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            command TEXT,
            details TEXT
        )
    """)
    db_query("""
        CREATE TABLE IF NOT EXISTS maintenance (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active INTEGER DEFAULT 0,
            message TEXT DEFAULT ''
        )
    """)
    db_query("INSERT OR IGNORE INTO maintenance(id, active, message) VALUES(1, 0, '')")

init_db()

# =========================
# CACHE
# =========================
class TTLCache:
    def __init__(self, default_ttl=60):
        self.default_ttl = default_ttl
        self.data = {}
        self.lock = threading.RLock()

    def set(self, key, value, ttl=None):
        if ttl is None:
            ttl = self.default_ttl
        with self.lock:
            self.data[key] = (value, time.time() + ttl)

    def get(self, key):
        with self.lock:
            if key not in self.data:
                return None
            value, expires = self.data[key]
            if time.time() > expires:
                del self.data[key]
                return None
            return value

price_cache = TTLCache(3)
coin_cache = TTLCache(3600)

# =========================
# REQUEST SESSION
# =========================
session = requests.Session()
retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({"User-Agent": "PersonaBot/16.0"})

# =========================
# COINGECKO RATE LIMITER
# =========================
coingecko_last_call = 0
coingecko_lock = threading.RLock()

def rate_limit_coingecko():
    with coingecko_lock:
        global coingecko_last_call
        now = time.time()
        elapsed = now - coingecko_last_call
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed + 0.05)
        coingecko_last_call = time.time()

# =========================
# HELPERS
# =========================
def h(value):
    return html.escape("" if value is None else str(value), quote=False)

def fmt_price(p):
    if p is None:
        return "N/A"
    if p >= 1:
        return f"${p:,.4f}"
    if p >= 0.0001:
        return f"${p:,.6f}"
    if p >= 0.000001:
        return f"${p:,.8f}"
    return f"${p:.10f}"

def fmt_currency_value(currency_code, value):
    if value is None:
        return "N/A"
    symbols = {
        "usd": "$", "eur": "€", "gbp": "£", "jpy": "¥",
        "cny": "¥", "aed": "د.إ", "try": "₺",
        "inr": "₹", "krw": "₩", "cad": "C$", "aud": "A$",
    }
    symbol = symbols.get(currency_code, "")
    if currency_code == "aed":
        return f"{symbol} {value:,.2f}"
    if value >= 1:
        return f"{symbol}{value:,.2f}"
    return f"{symbol}{value:.8f}"

def safe_first_200(text):
    return (text or "")[:200]

# =========================
# MAINTENANCE MODE
# =========================
def get_maintenance():
    row = db_query("SELECT active, message FROM maintenance WHERE id=1", fetch_one=True)
    if not row:
        return False, ""
    return bool(row[0]), row[1] or ""

def set_maintenance(active, message=""):
    db_query("UPDATE maintenance SET active=?, message=? WHERE id=1", (1 if active else 0, message))

def maintenance_block(uid, cid):
    if is_admin(uid):
        return False
    active, msg = get_maintenance()
    if not active:
        return False
    bot.send_message(
        cid,
        "🔧 <b>Bot Under Maintenance</b>\n\n"
        + (h(msg) if msg else "We'll be back shortly. Thank you for your patience."),
        disable_web_page_preview=True,
    )
    return True

def broadcast_all(text, skip_uid=None):
    rows = db_query("SELECT DISTINCT user_id FROM profiles", fetch_all=True) or []
    sent = failed = 0
    for (uid,) in rows:
        if skip_uid and uid == skip_uid:
            continue
        try:
            bot.send_message(uid, text)
            sent += 1
            time.sleep(0.05)
        except Exception as e:
            log.error(f"broadcast_all failed for {uid}: {e}")
            failed += 1
    return sent, failed

# =========================
# PROFILES
# =========================
def get_profile(user_id):
    row = db_query("SELECT * FROM profiles WHERE user_id=?", (user_id,), fetch_one=True)
    if not row:
        now = int(time.time())
        db_query("INSERT OR IGNORE INTO profiles(user_id, join_date) VALUES(?,?)", (user_id, now))
        row = db_query("SELECT * FROM profiles WHERE user_id=?", (user_id,), fetch_one=True)
        if not row:
            return {
                "user_id": user_id, "join_date": now,
                "total_interactions": 0, "alerts_set": 0,
                "alerts_triggered": 0, "username": None, "first_name": None,
            }
    return {
        "user_id": row[0], "join_date": row[1],
        "total_interactions": row[2], "alerts_set": row[3],
        "alerts_triggered": row[4], "username": row[5], "first_name": row[6],
    }

def update_profile(user_id, **kwargs):
    for key, value in kwargs.items():
        db_query(f"UPDATE profiles SET {key}=? WHERE user_id=?", (value, user_id))

def log_interaction(uid, uname, fname, cmd, det=""):
    p = get_profile(uid)
    if p["username"] != uname or p["first_name"] != fname:
        update_profile(uid, username=uname, first_name=fname)
    db_query(
        "INSERT INTO analytics(timestamp,user_id,username,first_name,command,details) VALUES(?,?,?,?,?,?)",
        (int(time.time()), uid, uname or "?", fname or "?", cmd, safe_first_200(det)),
    )
    db_query("UPDATE profiles SET total_interactions = total_interactions + 1 WHERE user_id=?", (uid,))

def is_admin(uid):
    return uid in ADMIN_IDS

# =========================
# SERVICES
# =========================
class Binance:
    @staticmethod
    def price(symbol):
        try:
            symbol = symbol.upper()
            r = session.get(
                f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT",
                timeout=10,
            )
            if r.status_code != 200:
                return None, None
            data = r.json()
            price = float(data["lastPrice"])
            change = float(data["priceChangePercent"])
            price_cache.set(f"{symbol}_binance", price, ttl=3)
            return price, change
        except Exception as e:
            log.error(f"Binance price error {symbol}: {e}")
            return None, None

    @staticmethod
    def top_movers():
        cached = price_cache.get("top_movers")
        if cached:
            return cached
        try:
            r = session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
            if r.status_code != 200:
                return [], []
            data = r.json()
            stable = {"USDT", "BUSD", "USDC", "DAI", "FDUSD"}
            filtered = [
                d for d in data
                if d["symbol"].endswith("USDT")
                and d["symbol"].removesuffix("USDT") not in stable
                and float(d.get("quoteVolume", 0)) > 1_000_000
            ]
            sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)
            result = sorted_data[:5], sorted_data[-5:][::-1]
            price_cache.set("top_movers", result, ttl=30)
            return result
        except Exception as e:
            log.error(f"Top movers error: {e}")
            return [], []

class CoinGecko:
    COIN_MAP = {
        "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
        "SOL": "solana", "XRP": "ripple", "DOGE": "dogecoin",
        "ADA": "cardano", "AVAX": "avalanche-2", "LINK": "chainlink",
    }

    @staticmethod
    def resolve_id(symbol):
        symbol = symbol.upper()
        if symbol in CoinGecko.COIN_MAP:
            return CoinGecko.COIN_MAP[symbol]
        rate_limit_coingecko()
        try:
            r = session.get(f"https://api.coingecko.com/api/v3/search?query={symbol}", timeout=10)
            if r.status_code != 200:
                return None
            coins = r.json().get("coins", [])
            return coins[0]["id"] if coins else None
        except Exception as e:
            log.error(f"CoinGecko resolve error: {e}")
            return None

    @staticmethod
    def info(symbol):
        symbol = symbol.upper()
        cached = coin_cache.get(symbol)
        if cached:
            return cached
        coin_id = CoinGecko.resolve_id(symbol)
        if not coin_id:
            return None
        rate_limit_coingecko()
        try:
            r = session.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}",
                params={
                    "localization": "false", "tickers": "false",
                    "community_data": "false", "developer_data": "false", "sparkline": "false",
                },
                timeout=15,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            md = data.get("market_data", {})
            result = {
                "name": data.get("name", "?"),
                "symbol": data.get("symbol", "?").upper(),
                "rank": data.get("market_cap_rank", "N/A"),
                "price": md.get("current_price", {}).get("usd", 0),
                "ath": md.get("ath", {}).get("usd", 0),
                "ath_date": md.get("ath_date", {}).get("usd", "")[:10],
                "atl": md.get("atl", {}).get("usd", 0),
                "atl_date": md.get("atl_date", {}).get("usd", "")[:10],
                "market_cap": md.get("market_cap", {}).get("usd", 0),
                "volume": md.get("total_volume", {}).get("usd", 0),
                "supply": md.get("circulating_supply", 0),
                "max_supply": md.get("max_supply"),
            }
            coin_cache.set(symbol, result)
            return result
        except Exception as e:
            log.error(f"Coin info error {symbol}: {e}")
            return None

    @staticmethod
    def multi_price(symbol):
        coin_id = CoinGecko.resolve_id(symbol)
        if not coin_id:
            return None
        rate_limit_coingecko()
        try:
            r = session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd,eur,gbp,jpy,cny,aed,try,inr,krw,cad,aud"},
                timeout=10,
            )
            if r.status_code != 200:
                return None
            return r.json().get(coin_id)
        except Exception as e:
            log.error(f"Multi price error {symbol}: {e}")
            return None

def live_price(symbol):
    cached = price_cache.get(f"{symbol.upper()}_binance")
    if cached is not None:
        return cached, None, "Binance"
    p, ch = Binance.price(symbol)
    if p is not None:
        return p, ch, "Binance"
    info = CoinGecko.info(symbol)
    if info and info.get("price") is not None:
        return info["price"], None, "CoinGecko"
    return None, None, None

class ContractScanner:
    @staticmethod
    def scan(address):
        if not address:
            return None, "Empty address"
        addr = re.sub(r"\s+", "", address)
        if len(addr) > MAX_CA_LENGTH:
            return None, "Address too long"
        addr_lower = addr.lower()
        try:
            if re.match(r"^0x[a-f0-9]{40}$", addr_lower):
                for chain in [1, 56]:
                    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain}?contract_addresses={addr_lower}"
                    r = session.get(url, timeout=15)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    result = data.get("result", {}).get(addr_lower, {})
                    if result:
                        return result, None
                return None, "Contract not found"
            sol_addr = re.sub(r"[^a-zA-Z0-9]", "", addr)
            if len(sol_addr) < 32 or len(sol_addr) > 44:
                return None, "Invalid Solana address"
            url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={sol_addr}"
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                return None, "API error"
            data = r.json()
            result = data.get("result", {}).get(sol_addr, {})
            if not result:
                return None, "Token not found"
            return result, None
        except Exception as e:
            log.error(f"Scanner error: {e}")
            return None, "Scanner failed"

# =========================
# ALERTS
# =========================
def add_alert(user_id, chat_id, coin, target, direction):
    alert_id = db_query(
        "INSERT INTO alerts(user_id, chat_id, coin, target, direction, created_at) VALUES(?,?,?,?,?,?)",
        (user_id, chat_id, coin.upper(), target, direction, int(time.time())),
    )
    db_query("UPDATE profiles SET alerts_set = alerts_set + 1 WHERE user_id=?", (user_id,))
    return alert_id

def get_active_alerts(chat_id=None):
    if chat_id is None:
        rows = db_query("SELECT id,user_id,chat_id,coin,target,direction FROM alerts WHERE active=1", fetch_all=True)
    else:
        rows = db_query(
            "SELECT id,user_id,chat_id,coin,target,direction FROM alerts WHERE active=1 AND chat_id=?",
            (chat_id,), fetch_all=True,
        )
    return [
        {"id": r[0], "user_id": r[1], "chat_id": r[2], "coin": r[3], "target": r[4], "direction": r[5]}
        for r in rows
    ]

def deactivate_alert(alert_id):
    db_query("UPDATE alerts SET active=0 WHERE id=?", (alert_id,))

def get_alert_count(user_id):
    row = db_query("SELECT COUNT(*) FROM alerts WHERE active=1 AND user_id=?", (user_id,), fetch_one=True)
    return row[0] if row else 0

# =========================
# WEBSOCKET
# =========================
ws_restart = threading.Event()
ws_restart.set()

def rebuild_ws():
    ws_restart.set()

def ws_loop():
    backoff = 1
    while True:
        try:
            alerts = get_active_alerts()
            symbols = sorted({a["coin"].upper() for a in alerts})
            if not symbols:
                symbols = ["BTC"]
            streams = "/".join([f"{s.lower()}usdt@ticker" for s in symbols[:50]])
            if not streams:
                time.sleep(5)
                continue
            ws_restart.clear()
            url = f"wss://stream.binance.com:9443/stream?streams={streams}"

            def on_message(_ws, msg):
                try:
                    d = json.loads(msg)
                    if "data" not in d:
                        return
                    t = d["data"]
                    symbol = t["s"].removesuffix("USDT")
                    price_cache.set(f"{symbol}_binance", float(t["c"]), ttl=3)
                except (KeyError, ValueError, TypeError) as e:
                    log.warning(f"WS on_message parse error: {e} | raw: {msg[:200]}")
                except Exception as e:
                    log.error(f"WS on_message unexpected error: {e}")

            def on_error(_ws, err):
                log.warning(f"WS error: {err}")

            app = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error)
            worker = threading.Thread(
                target=app.run_forever,
                kwargs={"ping_interval": 30, "ping_timeout": 10},
                daemon=True,
            )
            worker.start()
            while worker.is_alive():
                if ws_restart.is_set():
                    try:
                        app.close()
                    except:
                        pass
                    break
                time.sleep(1)
            backoff = 1
        except Exception as e:
            log.error(f"WS loop error: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

threading.Thread(target=ws_loop, daemon=True).start()

def alert_loop():
    last_trigger = {}
    while True:
        try:
            alerts = get_active_alerts()
            now = time.time()
            for a in alerts:
                key = (a["chat_id"], a["id"])
                if key in last_trigger and now - last_trigger[key] < 300:
                    continue
                price = price_cache.get(f"{a['coin']}_binance")
                if price is None:
                    price, _, _ = live_price(a["coin"])
                if price is None:
                    continue
                hit = (a["direction"] == ">" and price >= a["target"]) or \
                      (a["direction"] == "<" and price <= a["target"])
                if not hit:
                    continue
                deactivate_alert(a["id"])
                last_trigger[key] = now
                db_query("UPDATE profiles SET alerts_triggered = alerts_triggered + 1 WHERE user_id=?", (a["user_id"],))
                bot.send_message(
                    a["chat_id"],
                    f"🚨 <b>PRICE ALERT TRIGGERED</b>\n\n"
                    f"<b>{h(a['coin'])}</b> {h(a['direction'])} <b>{a['target']:,.2f}</b>\n"
                    f"💵 Current: <b>{fmt_price(price)}</b>",
                )
        except Exception as e:
            log.error(f"Alert loop error: {e}")
        time.sleep(5)

threading.Thread(target=alert_loop, daemon=True).start()

# =========================
# LIVE TICKER MANAGER
# =========================
active_tickers = {}
tickers_lock = threading.RLock()

def start_live_ticker(chat_id, user_id, symbol, msg_id):
    key = (chat_id, user_id)

    with tickers_lock:
        if len(active_tickers) >= MAX_LIVE_TICKERS and key not in active_tickers:
            bot.send_message(chat_id, "🚫 Too many live tickers active. Try again shortly.")
            return

        if key in active_tickers:
            active_tickers[key].set()

        stop_event = threading.Event()
        active_tickers[key] = stop_event

    threading.Thread(
        target=_live_ticker_updater,
        args=(chat_id, user_id, symbol, msg_id, stop_event),
        daemon=True,
    ).start()

def _live_ticker_updater(chat_id, user_id, symbol, msg_id, stop_event):
    key = (chat_id, user_id)
    source_is_binance = True

    test_price, test_change = Binance.price(symbol)
    if test_price is None:
        source_is_binance = False

    last_price = None

    while not stop_event.is_set():
        try:
            if source_is_binance:
                price, change = Binance.price(symbol)
            else:
                info = CoinGecko.info(symbol)
                price = info["price"] if info else None
                change = None

            if price is None:
                text = f"❌ <b>{h(symbol)}</b> — price unavailable"
            else:
                if change is not None:
                    arrow = "🟢▲" if change >= 0 else "🔴▼"
                    change_str = f"{arrow} {abs(change):.2f}%"
                elif last_price is not None:
                    arrow = "🟢▲" if price >= last_price else "🔴▼"
                    change_str = arrow
                else:
                    change_str = "⏳"
                last_price = price
                src = "Binance" if source_is_binance else "CoinGecko"
                text = (
                    f"💵 <b>{h(symbol)}</b>  <i>live</i>\n\n"
                    f"{fmt_price(price)}\n"
                    f"{change_str}\n\n"
                    f"<i>Source: {src} · updates every 3s</i>"
                )

            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML")

        except Exception as e:
            log.warning(f"Live ticker edit failed ({key}): {e}")
            break

        stop_event.wait(timeout=3)

    with tickers_lock:
        if active_tickers.get(key) is stop_event:
            del active_tickers[key]

# =========================
# MESSAGE HISTORY
# =========================
msg_queue = {}
q_lock = threading.RLock()

def send_and_track(chat_id, text, markup=None):
    sent = bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
    with q_lock:
        msg_queue.setdefault(chat_id, []).append(sent.message_id)
        while len(msg_queue[chat_id]) > MAX_HISTORY:
            old = msg_queue[chat_id].pop(0)
            try:
                bot.delete_message(chat_id, old)
            except:
                pass
    return sent

# =========================
# UI / MENUS (9‑coin grid, updated text)
# =========================
def back_button():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⬅️ Back to cockpit", callback_data="back_main"))
    return kb

def coin_grid(prefix, coins):
    kb = InlineKeyboardMarkup()
    row = []
    for i, coin in enumerate(coins, 1):
        row.append(InlineKeyboardButton(coin, callback_data=f"{prefix}_{coin}"))
        if i % 3 == 0:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    return kb

def main_menu():
    text = "⚡ <b>PERSONA</b>\n\nFast crypto tools: prices, alerts, intel."
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("💰 Price check", callback_data="menu_price"),
           InlineKeyboardButton("🔔 Alert traps", callback_data="menu_alerts"))
    kb.row(InlineKeyboardButton("🚀 Gainers", callback_data="gainers"),
           InlineKeyboardButton("📉 Losers", callback_data="losers"))
    kb.row(InlineKeyboardButton("🔎 Coin info", callback_data="menu_info"),
           InlineKeyboardButton("💱 Currencies", callback_data="menu_multi"))
    kb.row(InlineKeyboardButton("🛡 Scan CA", callback_data="menu_scan"),
           InlineKeyboardButton("📋 Active alerts", callback_data="list_alerts"))
    kb.row(InlineKeyboardButton("👤 Profile", callback_data="profile"))
    return text, kb

def price_menu():
    text = "💵 <b>Price Check</b>\n\nTap a coin or type a ticker.\nExamples: <code>BTC</code>, <code>PEPE</code>, <code>TAO</code>"
    # 9 coins – 3×3 grid
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
    kb = coin_grid("price", coins)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_price"))
    kb.row(InlineKeyboardButton("⬅️ Back to cockpit", callback_data="back_main"))
    return text, kb

def info_menu():
    text = "🔎 <b>Coin Info</b>\n\nPick a coin for rank, ATH/ATL, supply, market cap, and volume."
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
    kb = coin_grid("info", coins)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_info"))
    kb.row(InlineKeyboardButton("⬅️ Back to cockpit", callback_data="back_main"))
    return text, kb

def multi_menu():
    text = "💱 <b>Currencies</b>\n\nSee a coin across multiple currencies."
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "LINK"]  # 7 coins (will have a 3‑3‑1 layout, acceptable)
    kb = coin_grid("multi", coins)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_multi"))
    kb.row(InlineKeyboardButton("⬅️ Back to cockpit", callback_data="back_main"))
    return text, kb

def alerts_menu():
    text = "🔔 <b>Alert Traps</b>\n\nDrop a trigger and let the bot watch the tape."
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC > 100k", callback_data="setalert_BTC_>_100000"),
           InlineKeyboardButton("BTC < 80k", callback_data="setalert_BTC_<_80000"))
    kb.row(InlineKeyboardButton("ETH > 4k", callback_data="setalert_ETH_>_4000"),
           InlineKeyboardButton("ETH < 2k", callback_data="setalert_ETH_<_2000"))
    kb.row(InlineKeyboardButton("SOL > 200", callback_data="setalert_SOL_>_200"),
           InlineKeyboardButton("SOL < 100", callback_data="setalert_SOL_<_100"))
    kb.row(InlineKeyboardButton("✏️ Custom alert", callback_data="custom_alert"))
    kb.row(InlineKeyboardButton("📋 Active alerts", callback_data="list_alerts"))
    kb.row(InlineKeyboardButton("⬅️ Back to cockpit", callback_data="back_main"))
    return text, kb

def scan_menu():
    text = "🛡 <b>Contract Scanner</b>\n\nPaste contract address (ETH/BSC/Solana)"
    return text, back_button()

# =========================
# COOLDOWN
# =========================
cooldown = {}
cooldown_lock = threading.RLock()

def cooldown_ok(uid):
    with cooldown_lock:
        now = time.time()
        if uid in cooldown and now - cooldown[uid] < COOLDOWN_SECONDS:
            return False
        cooldown[uid] = now
    return True

# =========================
# ADMIN COMMANDS
# =========================
@bot.message_handler(commands=["stats"])
def stats_cmd(m):
    if not is_admin(m.from_user.id):
        return
    users         = db_query("SELECT COUNT(*) FROM profiles", fetch_one=True)[0]
    interactions  = db_query("SELECT COUNT(*) FROM analytics", fetch_one=True)[0]
    active_alerts = db_query("SELECT COUNT(*) FROM alerts WHERE active=1", fetch_one=True)[0]
    total_alerts  = db_query("SELECT COUNT(*) FROM alerts", fetch_one=True)[0]
    with tickers_lock:
        live_count = len(active_tickers)
    text = (f"📊 <b>Bot Stats</b>\n\n"
            f"👥 Users: <b>{users}</b>\n"
            f"💬 Interactions: <b>{interactions}</b>\n"
            f"🔔 Active alerts: <b>{active_alerts}</b>\n"
            f"📦 Total alerts: <b>{total_alerts}</b>\n"
            f"📡 Live tickers: <b>{live_count}</b>")
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["users"])
def users_cmd(m):
    if not is_admin(m.from_user.id):
        return
    rows = db_query(
        "SELECT user_id, username, first_name, total_interactions, alerts_set, alerts_triggered "
        "FROM profiles ORDER BY total_interactions DESC LIMIT 50",
        fetch_all=True,
    )
    if not rows:
        send_and_track(m.chat.id, "No users yet.", back_button())
        return
    lines = []
    for uid, username, first_name, interactions, alerts_set, alerts_triggered in rows:
        display_name = f"{first_name or '?'} (@{username or 'no username'})"
        lines.append(f"<code>{uid}</code> — {display_name}\n   💬 {interactions} | 🔔 {alerts_set} | ⚡ {alerts_triggered}")
    total = db_query("SELECT COUNT(*) FROM profiles", fetch_one=True)[0]
    if total > 50:
        lines.append(f"…and {total - 50} more")
    send_and_track(m.chat.id, "👥 <b>Users</b>\n\n" + "\n".join(lines), back_button())

@bot.message_handler(commands=["announce"])
def announce_cmd(m):
    if not is_admin(m.from_user.id):
        return
    msg_text = m.text.partition(" ")[2].strip()
    if not msg_text:
        send_and_track(m.chat.id, "Usage: <code>/announce your message</code>", back_button())
        return
    sent, failed = broadcast_all(f"📢 <b>Announcement</b>\n\n{h(msg_text)}", skip_uid=m.from_user.id)
    send_and_track(m.chat.id, f"📢 Sent to <b>{sent}</b> users. Failed: <b>{failed}</b>", back_button())

@bot.message_handler(commands=["clear_alerts"])
def clear_alerts_cmd(m):
    if not is_admin(m.from_user.id):
        return
    db_query("UPDATE alerts SET active=0 WHERE active=1")
    rebuild_ws()
    send_and_track(m.chat.id, "✅ All active alerts have been cleared.", back_button())

@bot.message_handler(commands=["maintenance"])
def maintenance_cmd(m):
    if not is_admin(m.from_user.id):
        return
    args = m.text.split()
    if len(args) < 2:
        active, msg = get_maintenance()
        status = "🔴 ON" if active else "🟢 OFF"
        send_and_track(
            m.chat.id,
            f"🔧 <b>Maintenance Mode</b>\n\nStatus: <b>{status}</b>\n"
            f"Message: <i>{h(msg) or '(none)'}</i>\n\n"
            f"Usage:\n<code>/maintenance on Your message here</code>\n<code>/maintenance off</code>",
            back_button(),
        )
        return

    action = args[1].lower()

    if action == "on":
        custom_msg = " ".join(args[2:]) if len(args) > 2 else ""
        set_maintenance(True, custom_msg)
        broadcast_text = (
            "🔧 <b>Maintenance started</b>\n\n"
            + (h(custom_msg) if custom_msg else "Bot is under maintenance. We'll be back shortly.")
        )
        sent, _ = broadcast_all(broadcast_text, skip_uid=m.from_user.id)
        send_and_track(
            m.chat.id,
            f"🔧 Maintenance mode <b>ON</b>. Notified <b>{sent}</b> users.\n\n"
            f"<i>{h(custom_msg) or '(default message)'}</i>",
            back_button(),
        )
        log.info(f"Maintenance ON by admin {m.from_user.id}: {custom_msg!r}")

    elif action == "off":
        set_maintenance(False, "")
        sent, _ = broadcast_all(
            "✅ <b>Maintenance ended</b>\n\nBot is back online. All features available.",
            skip_uid=m.from_user.id,
        )
        send_and_track(m.chat.id, f"✅ Maintenance <b>OFF</b>. Notified <b>{sent}</b> users.", back_button())
        log.info(f"Maintenance OFF by admin {m.from_user.id}")

    elif action == "status":
        active, msg = get_maintenance()
        status = "🔴 ON" if active else "🟢 OFF"
        send_and_track(m.chat.id, f"🔧 Maintenance: <b>{status}</b>\nMessage: <i>{h(msg) or '(none)'}</i>", back_button())

    else:
        send_and_track(m.chat.id, "🚫 Use <code>on</code>, <code>off</code>, or <code>status</code>.", back_button())

# =========================
# START / HELP
# =========================
@bot.message_handler(commands=["start", "help"])
def start(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    log_interaction(m.from_user.id, m.from_user.username, m.from_user.first_name, "/start")
    text, kb = main_menu()
    send_and_track(m.chat.id, text, kb)

# =========================
# CALLBACKS
# =========================
waiting = {}
wait_lock = threading.RLock()

def render_alert_list(chat_id):
    active = get_active_alerts(chat_id)
    if not active:
        return "📋 <b>Active Alerts</b>\n\nNothing active right now.", back_button()
    text = "📋 <b>Active Alerts</b>\n\n"
    kb = InlineKeyboardMarkup()
    for a in active:
        arrow = "▲" if a["direction"] == ">" else "▼"
        text += f"#{a['id']} <b>{h(a['coin'])}</b> {arrow} <b>${a['target']:,.2f}</b>\n"
        kb.row(InlineKeyboardButton(f"❌ Cancel #{a['id']}", callback_data=f"cancel_{a['id']}"))
    kb.row(InlineKeyboardButton("⬅️ Back to cockpit", callback_data="back_main"))
    return text, kb

@bot.callback_query_handler(func=lambda call: True)
def cb(call):
    uid = call.from_user.id
    cid = call.message.chat.id
    data = call.data
    wait_key = (cid, uid)

    if not cooldown_ok(uid):
        bot.answer_callback_query(call.id, "Easy there.")
        return
    bot.answer_callback_query(call.id)

    if maintenance_block(uid, cid):
        return

    log_interaction(uid, call.from_user.username, call.from_user.first_name, f"cb:{data[:40]}")

    try:
        if data == "back_main":
            with wait_lock:
                waiting.pop(wait_key, None)
            text, kb = main_menu()
            send_and_track(cid, text, kb)

        elif data == "menu_price":
            with wait_lock:
                waiting[wait_key] = "price"
            text, kb = price_menu()
            send_and_track(cid, text, kb)

        elif data == "search_price":
            with wait_lock:
                waiting[wait_key] = "price"
            send_and_track(cid, "🧠 <b>Coin Lookup</b>\n\nType any ticker.\nExamples: <code>BTC</code>, <code>PEPE</code>, <code>TAO</code>", back_button())

        elif data == "menu_info":
            with wait_lock:
                waiting[wait_key] = "info"
            text, kb = info_menu()
            send_and_track(cid, text, kb)

        elif data == "search_info":
            with wait_lock:
                waiting[wait_key] = "info"
            send_and_track(cid, "🔎 <b>Coin Info Search</b>\n\nType any ticker to pull the full profile.", back_button())

        elif data == "menu_multi":
            with wait_lock:
                waiting[wait_key] = "multi"
            text, kb = multi_menu()
            send_and_track(cid, text, kb)

        elif data == "search_multi":
            with wait_lock:
                waiting[wait_key] = "multi"
            send_and_track(cid, "💱 <b>Currency Search</b>\n\nType any ticker.", back_button())

        elif data == "menu_scan":
            with wait_lock:
                waiting[wait_key] = "scan"
            text, kb = scan_menu()
            send_and_track(cid, text, kb)

        elif data == "menu_alerts":
            with wait_lock:
                waiting[wait_key] = "alert"
            text, kb = alerts_menu()
            send_and_track(cid, text, kb)

        elif data == "custom_alert":
            with wait_lock:
                waiting[wait_key] = "alert"
            send_and_track(cid, "✏️ <b>Custom Alert</b>\n\nFormat: <code>COIN &gt; price</code>\nExample: <code>BTC &gt; 95000</code>", back_button())

        elif data.startswith("setalert_"):
            remainder = data[len("setalert_"):]
            parts = remainder.rsplit("_", 2)
            if len(parts) != 3:
                send_and_track(cid, "🚫 Malformed alert callback.", alerts_menu()[1])
                return
            sym, direction, target_str = parts[0].upper(), parts[1], parts[2]
            try:
                target = float(target_str)
            except:
                send_and_track(cid, "🚫 Invalid target price.", alerts_menu()[1])
                return
            current, _, _ = live_price(sym)
            if current is None:
                send_and_track(cid, f"🚫 <b>{h(sym)}</b> not found.", alerts_menu()[1])
                return
            if (direction == ">" and target > current * 10) or (direction == "<" and target < current / 10):
                send_and_track(cid, "🚫 Target too far from current price. Use a realistic value.", alerts_menu()[1])
                return
            if get_alert_count(uid) >= MAX_ALERTS_PER_USER:
                send_and_track(cid, f"🚫 Max <b>{MAX_ALERTS_PER_USER}</b> alerts reached.", alerts_menu()[1])
                return
            aid = add_alert(uid, cid, sym, target, direction)
            rebuild_ws()
            send_and_track(cid, f"✅ <b>Alert armed</b>\n\n#{aid} — <b>{h(sym)}</b> {h(direction)} <b>${target:,.2f}</b>", alerts_menu()[1])

        elif data == "list_alerts":
            text, kb = render_alert_list(cid)
            send_and_track(cid, text, kb)

        elif data.startswith("cancel_"):
            aid = int(data.split("_", 1)[1])
            deactivate_alert(aid)
            rebuild_ws()
            text, kb = render_alert_list(cid)
            send_and_track(cid, text, kb)

        elif data.startswith("price_"):
            sym = data.split("_", 1)[1]
            price, _, source = live_price(sym)
            if price is None:
                send_and_track(cid, f"🚫 <b>{h(sym)}</b> not found.", price_menu()[1])
                return
            sent = send_and_track(cid, f"⏳ Starting live ticker for <b>{h(sym)}</b>...", None)
            start_live_ticker(cid, uid, sym, sent.message_id)

        elif data == "gainers":
            gainers, _ = Binance.top_movers()
            if not gainers:
                send_and_track(cid, "🚫 No data right now.", back_button())
            else:
                text = "🚀 <b>Top Gainers (24h)</b>\n\n"
                for d in gainers:
                    coin = d["symbol"].removesuffix("USDT")
                    text += f"🟢 <b>{h(coin)}</b> — {fmt_price(float(d['lastPrice']))} — ▲ {float(d['priceChangePercent']):.2f}%\n"
                send_and_track(cid, text, back_button())

        elif data == "losers":
            _, losers = Binance.top_movers()
            if not losers:
                send_and_track(cid, "🚫 No data right now.", back_button())
            else:
                text = "📉 <b>Top Losers (24h)</b>\n\n"
                for d in losers:
                    coin = d["symbol"].removesuffix("USDT")
                    text += f"🔴 <b>{h(coin)}</b> — {fmt_price(float(d['lastPrice']))} — ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
                send_and_track(cid, text, back_button())

        elif data.startswith("info_"):
            sym = data.split("_", 1)[1]
            info = CoinGecko.info(sym)
            if not info:
                send_and_track(cid, f"🚫 No info for <b>{h(sym)}</b>.", info_menu()[1])
            else:
                max_supply = info["max_supply"] if info["max_supply"] else "∞"
                text = (f"🔎 <b>{h(info['name'])} ({h(info['symbol'])})</b>\n\n"
                        f"🏆 Rank: <b>#{h(info['rank'])}</b>\n"
                        f"💵 Price: <b>{fmt_price(info['price'])}</b>\n"
                        f"📈 ATH: <b>{fmt_price(info['ath'])}</b> <i>({h(info['ath_date'])})</i>\n"
                        f"📉 ATL: <b>{fmt_price(info['atl'])}</b> <i>({h(info['atl_date'])})</i>\n"
                        f"💹 Market cap: <b>${info['market_cap']:,.0f}</b>\n"
                        f"📊 Volume: <b>${info['volume']:,.0f}</b>\n"
                        f"🪙 Supply: <b>{info['supply']:,.0f}</b> / <b>{h(max_supply)}</b>")
                send_and_track(cid, text, info_menu()[1])

        elif data.startswith("multi_"):
            sym = data.split("_", 1)[1]
            prices = CoinGecko.multi_price(sym)
            if not prices:
                send_and_track(cid, f"🚫 No data for <b>{h(sym)}</b>.", multi_menu()[1])
            else:
                order = [
                    ("usd", "🇺🇸 USD"), ("eur", "🇪🇺 EUR"), ("gbp", "🇬🇧 GBP"),
                    ("jpy", "🇯🇵 JPY"), ("cny", "🇨🇳 CNY"), ("aed", "🇦🇪 AED"),
                    ("try", "🇹🇷 TRY"), ("inr", "🇮🇳 INR"), ("krw", "🇰🇷 KRW"),
                    ("cad", "🇨🇦 CAD"), ("aud", "🇦🇺 AUD"),
                ]
                text = f"💱 <b>{h(sym)} — Currencies</b>\n\n"
                for key, flag in order:
                    val = prices.get(key)
                    if val is not None:
                        text += f"{flag}: <b>{h(fmt_currency_value(key, val))}</b>\n"
                send_and_track(cid, text, multi_menu()[1])

        elif data == "profile":
            p = get_profile(uid)
            text = (f"👤 <b>Profile</b>\n\n"
                    f"💬 Interactions: <b>{p['total_interactions']}</b>\n"
                    f"🔔 Alerts set: <b>{p['alerts_set']}</b>\n"
                    f"⚡ Alerts triggered: <b>{p['alerts_triggered']}</b>")
            send_and_track(cid, text, back_button())

    except Exception as e:
        log.error(f"Callback error: {e}", exc_info=True)
        send_and_track(cid, "⚠️ Something broke.", back_button())

# =========================
# TEXT INPUT
# =========================
@bot.message_handler(func=lambda m: True)
def text_handler(message):
    if not message.text:
        return
    cid = message.chat.id
    uid = message.from_user.id
    wait_key = (cid, uid)

    if not cooldown_ok(uid):
        return
    if maintenance_block(uid, cid):
        return

    with wait_lock:
        if wait_key not in waiting:
            return
        mode = waiting.pop(wait_key)

    text = message.text.strip()[:MAX_TEXT_LEN]
    if not text:
        return
    try:
        bot.delete_message(cid, message.message_id)
    except:
        pass
    log_interaction(uid, message.from_user.username, message.from_user.first_name, f"text:{mode}", text)

    try:
        if mode == "price":
            symbol = text.upper()
            price, _, source = live_price(symbol)
            if price is None:
                send_and_track(cid, f"🚫 <b>{h(symbol)}</b> not found.", price_menu()[1])
                return
            sent = send_and_track(cid, f"⏳ Starting live ticker for <b>{h(symbol)}</b>...", None)
            start_live_ticker(cid, uid, symbol, sent.message_id)

        elif mode == "info":
            info = CoinGecko.info(text.upper())
            if not info:
                send_and_track(cid, f"🚫 No info for <b>{h(text)}</b>.", info_menu()[1])
            else:
                max_supply = info["max_supply"] if info["max_supply"] else "∞"
                out = (f"🔎 <b>{h(info['name'])} ({h(info['symbol'])})</b>\n\n"
                       f"🏆 Rank: <b>#{h(info['rank'])}</b>\n"
                       f"💵 Price: <b>{fmt_price(info['price'])}</b>\n"
                       f"📈 ATH: <b>{fmt_price(info['ath'])}</b> <i>({h(info['ath_date'])})</i>\n"
                       f"📉 ATL: <b>{fmt_price(info['atl'])}</b> <i>({h(info['atl_date'])})</i>\n"
                       f"💹 Market cap: <b>${info['market_cap']:,.0f}</b>\n"
                       f"📊 Volume: <b>${info['volume']:,.0f}</b>\n"
                       f"🪙 Supply: <b>{info['supply']:,.0f}</b> / <b>{h(max_supply)}</b>")
                send_and_track(cid, out, info_menu()[1])

        elif mode == "multi":
            prices = CoinGecko.multi_price(text.upper())
            if not prices:
                send_and_track(cid, f"🚫 No data for <b>{h(text)}</b>.", multi_menu()[1])
            else:
                order = [
                    ("usd", "🇺🇸 USD"), ("eur", "🇪🇺 EUR"), ("gbp", "🇬🇧 GBP"),
                    ("jpy", "🇯🇵 JPY"), ("cny", "🇨🇳 CNY"), ("aed", "🇦🇪 AED"),
                    ("try", "🇹🇷 TRY"), ("inr", "🇮🇳 INR"), ("krw", "🇰🇷 KRW"),
                    ("cad", "🇨🇦 CAD"), ("aud", "🇦🇺 AUD"),
                ]
                out = f"💱 <b>{h(text.upper())} — Currencies</b>\n\n"
                for key, flag in order:
                    val = prices.get(key)
                    if val is not None:
                        out += f"{flag}: <b>{h(fmt_currency_value(key, val))}</b>\n"
                send_and_track(cid, out, multi_menu()[1])

        elif mode == "scan":
            result, err = ContractScanner.scan(text)
            if err:
                send_and_track(cid, f"🚫 {h(err)}", back_button())
            else:
                def flag(v):
                    if v == "1": return "⚠️ Yes"
                    if v == "0": return "✅ No"
                    return "❓ Unknown"
                token_name   = result.get("token_name") or "Unknown"
                token_symbol = result.get("token_symbol") or "?"
                out = (f"🛡 <b>CA Scan</b>\n\n"
                       f"📛 Name: <b>{h(token_name)} ({h(token_symbol)})</b>\n"
                       f"🍯 Honeypot: <b>{flag(result.get('is_honeypot', '?'))}</b>\n"
                       f"🖨 Mintable: <b>{flag(result.get('is_mintable', '?'))}</b>\n"
                       f"🔁 Proxy: <b>{flag(result.get('is_proxy', '?'))}</b>\n"
                       f"📂 Open source: <b>{flag(result.get('is_open_source', '?'))}</b>\n"
                       f"💸 Buy tax: <b>{h(result.get('buy_tax', '?'))}%</b>\n"
                       f"💸 Sell tax: <b>{h(result.get('sell_tax', '?'))}%</b>\n"
                       f"👥 Holders: <b>{h(result.get('holder_count', '?'))}</b>")
                send_and_track(cid, out, back_button())

        elif mode == "alert":
            parts = text.split()
            if len(parts) != 3:
                send_and_track(cid, "🚫 Format: <code>BTC &gt; 100000</code>", alerts_menu()[1])
                return
            symbol, direction, target_str = parts[0].upper(), parts[1], parts[2]
            if direction not in [">", "<"]:
                send_and_track(cid, "🚫 Use <code>&gt;</code> or <code>&lt;</code> only.", alerts_menu()[1])
                return
            try:
                target = float(target_str)
            except:
                send_and_track(cid, "🚫 Invalid target price.", alerts_menu()[1])
                return
            current, _, _ = live_price(symbol)
            if current is None:
                send_and_track(cid, f"🚫 <b>{h(symbol)}</b> not found.", alerts_menu()[1])
                return
            if (direction == ">" and target > current * 10) or (direction == "<" and target < current / 10):
                send_and_track(cid, "🚫 Target too far from current price. Use a realistic value.", alerts_menu()[1])
                return
            if get_alert_count(uid) >= MAX_ALERTS_PER_USER:
                send_and_track(cid, f"🚫 Max <b>{MAX_ALERTS_PER_USER}</b> alerts reached.", alerts_menu()[1])
                return
            aid = add_alert(uid, cid, symbol, target, direction)
            rebuild_ws()
            send_and_track(cid, f"✅ <b>Alert armed</b>\n\n#{aid} — <b>{h(symbol)}</b> {h(direction)} <b>${target:,.2f}</b>", alerts_menu()[1])

    except Exception as e:
        log.error(f"Text handler error: {e}", exc_info=True)
        send_and_track(cid, "⚠️ Something broke.", back_button())

# =========================
# SHUTDOWN
# =========================
def stop(sig, frame):
    ws_restart.set()
    with tickers_lock:
        for ev in active_tickers.values():
            ev.set()
    try:
        bot.stop_polling()
    except:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

# =========================
# BOOT
# =========================
log.info("🚀 Persona Bot started – 9‑coin grid, live ticker, maintenance mode")
bot.delete_webhook()
bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
