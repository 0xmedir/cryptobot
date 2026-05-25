#!/usr/bin/env python3
"""
Persona Bot – Ultimate Edition (fully fixed)
- Whale alerts removed
- Etherscan V2 with env API key (wallet checker removed)
- All menu handlers implemented
- WebSocket auto-reconnect
- Admin commands

FIXES APPLIED:
  1. text_handler now skips commands so /price /info /live /scan /start /help work
  2. scan_command correctly reads GoPlusLabs top-level risk fields
  3. init_db() migration changes are committed immediately
  4. alert_command now opens the alerts menu instead of referencing /custom_alert
  5. /price /info /live /scan all check maintenance_block()
  6. Wallet checker feature removed completely
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
from telebot.apihelper import ApiTelegramException
import websocket

# =========================
# CONFIG (environment)
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN environment variable not set")
    sys.exit(1)

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
if not ETHERSCAN_API_KEY:
    print("❌ ETHERSCAN_API_KEY environment variable not set")
    sys.exit(1)

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "7458428092").split(",") if x.strip()]
COOLDOWN_SECONDS = 2
MAX_ALERTS_PER_USER = 20
MAX_CA_LENGTH = 100
MAX_TEXT_LEN = 200
MAX_HISTORY = 3
MAX_LIVE_TICKERS = 50
MAINTENANCE_SPAM_COOLDOWN = 60

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
                conn.commit()
            elif "username" not in columns:
                log.info("Adding username and first_name columns to profiles.")
                conn.execute("ALTER TABLE profiles ADD COLUMN username TEXT")
                conn.execute("ALTER TABLE profiles ADD COLUMN first_name TEXT")
                conn.commit()
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
multi_cache = TTLCache(10)

# =========================
# REQUEST SESSION
# =========================
session = requests.Session()
retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({"User-Agent": "PersonaBot/17.0"})

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
maintenance_spam = {}
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
    now = time.time()
    last = maintenance_spam.get(cid, 0)
    if now - last < MAINTENANCE_SPAM_COOLDOWN:
        return True
    maintenance_spam[cid] = now
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
        time.sleep(0.05)
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
# SERVICES (Binance, CoinGecko, Scanner)
# =========================
class Binance:
    @staticmethod
    def price(symbol):
        try:
            symbol = symbol.upper()
            r = session.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT", timeout=10)
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
    def top_movers(limit=10):
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
            gainers = sorted_data[:limit]
            losers = sorted_data[-limit:][::-1]
            result = (gainers, losers)
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
        cached = multi_cache.get(f"multi_{symbol}")
        if cached:
            return cached
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
            data = r.json().get(coin_id)
            if data:
                multi_cache.set(f"multi_{symbol}", data, ttl=10)
            return data
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
    current = get_alert_count(user_id)
    if current >= MAX_ALERTS_PER_USER:
        return None, f"❌ You already have {current} active alerts (max {MAX_ALERTS_PER_USER})."
    alert_id = db_query(
        "INSERT INTO alerts(user_id, chat_id, coin, target, direction, created_at) VALUES(?,?,?,?,?,?)",
        (user_id, chat_id, coin.upper(), target, direction, int(time.time())),
    )
    db_query("UPDATE profiles SET alerts_set = alerts_set + 1 WHERE user_id=?", (user_id,))
    return alert_id, None

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
# WEBSOCKET (auto-reconnect)
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
                except Exception as e:
                    log.warning(f"WS on_message error: {e}")
            def on_error(_ws, err):
                log.warning(f"WS error: {err}")
                rebuild_ws()
            def on_close(_ws, close_status_code, close_msg):
                log.info("WS closed, will reconnect")
                rebuild_ws()
            app = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
            worker = threading.Thread(target=app.run_forever, kwargs={"ping_interval": 30, "ping_timeout": 10}, daemon=True)
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
# MESSAGE QUEUE
# =========================
msg_queue = {}
q_lock = threading.RLock()

def send_and_track(chat_id, text, markup=None):
    sent = bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
    with q_lock:
        if chat_id not in msg_queue:
            msg_queue[chat_id] = []
        msg_queue[chat_id].append(sent.message_id)
        while len(msg_queue[chat_id]) > MAX_HISTORY:
            old = msg_queue[chat_id].pop(0)
            try:
                bot.delete_message(chat_id, old)
            except Exception as e:
                log.warning(f"Failed to delete message {old}: {e}")
    return sent

def back_button():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# =========================
# LIVE TICKERS FOR SINGLE COIN
# =========================
tickers = {}
tickers_lock = threading.RLock()
next_ticker_id = 0

def create_ticker(chat_id, symbol, message_id):
    global next_ticker_id
    key = (chat_id, next_ticker_id)
    next_ticker_id += 1
    stop_event = threading.Event()
    with tickers_lock:
        if len(tickers) >= MAX_LIVE_TICKERS:
            bot.send_message(chat_id, "🚫 Too many active tickers. Please wait.")
            return None
        tickers[key] = stop_event
    thread = threading.Thread(target=_ticker_worker, args=(chat_id, symbol, message_id, stop_event, key), daemon=True)
    thread.start()
    return key

def _ticker_worker(chat_id, symbol, msg_id, stop_event, key):
    test_price, _ = Binance.price(symbol)
    source_is_binance = test_price is not None
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
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=back_button())
        except ApiTelegramException as e:
            if "message is not modified" not in str(e):
                log.warning(f"Ticker edit fatal ({key}): {e}")
                break
        except Exception as e:
            log.warning(f"Ticker edit error ({key}): {e}")
        stop_event.wait(timeout=3)
    with tickers_lock:
        if key in tickers:
            del tickers[key]

def start_ticker(chat_id, symbol):
    sent = send_and_track(chat_id, f"⏳ Starting live ticker for {symbol}...", back_button())
    create_ticker(chat_id, symbol, sent.message_id)

# =========================
# LIVE GAINERS/LOSERS
# =========================
live_gainers_active = {}
live_gainers_lock = threading.RLock()

def _live_gainers_worker(chat_id, msg_id):
    while True:
        with live_gainers_lock:
            if chat_id not in live_gainers_active or not live_gainers_active[chat_id].get("running", False):
                break
        gainers, losers = Binance.top_movers()
        if not gainers:
            text = "❌ Failed to fetch data. Will retry..."
        else:
            text = "🚀 <b>Live Gainers/Losers (Top 10)</b>\n\n"
            text += "📈 <b>Top 10 Gainers</b>\n"
            for d in gainers:
                coin = d["symbol"].removesuffix("USDT")
                text += f"🟢 {coin}  ▲ {float(d['priceChangePercent']):.2f}%\n"
            text += "\n📉 <b>Top 10 Losers</b>\n"
            for d in losers:
                coin = d["symbol"].removesuffix("USDT")
                text += f"🔴 {coin}  ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=back_button())
        except Exception as e:
            log.warning(f"Live gainers edit error: {e}")
        for _ in range(10):
            with live_gainers_lock:
                if chat_id not in live_gainers_active or not live_gainers_active[chat_id].get("running", False):
                    break
            time.sleep(1)
    with live_gainers_lock:
        live_gainers_active.pop(chat_id, None)

# =========================
# LIVE MULTI-CURRENCY
# =========================
multi_tickers = {}
multi_tickers_lock = threading.RLock()

def _live_multi_worker(chat_id, symbol, msg_id):
    order = [
        ("usd", "🇺🇸 USD"), ("eur", "🇪🇺 EUR"), ("gbp", "🇬🇧 GBP"),
        ("jpy", "🇯🇵 JPY"), ("cny", "🇨🇳 CNY"), ("aed", "🇦🇪 AED"),
        ("try", "🇹🇷 TRY"), ("inr", "🇮🇳 INR"), ("krw", "🇰🇷 KRW"),
        ("cad", "🇨🇦 CAD"), ("aud", "🇦🇺 AUD"),
    ]
    while True:
        with multi_tickers_lock:
            if chat_id not in multi_tickers or not multi_tickers[chat_id].get("running", False):
                break
            current_symbol = multi_tickers[chat_id]["symbol"]
        prices = CoinGecko.multi_price(current_symbol)
        if not prices:
            text = f"❌ Failed to fetch data for {current_symbol}"
        else:
            text = f"💱 <b>{current_symbol} – Currencies (live)</b>\n\n"
            for key, flag in order:
                val = prices.get(key)
                if val is not None:
                    text += f"{flag}: <b>{h(fmt_currency_value(key, val))}</b>\n"
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=back_button())
        except Exception as e:
            log.warning(f"Live multi edit error: {e}")
        for _ in range(3):
            with multi_tickers_lock:
                if chat_id not in multi_tickers or not multi_tickers[chat_id].get("running", False):
                    break
            time.sleep(1)
    with multi_tickers_lock:
        multi_tickers.pop(chat_id, None)

# =========================
# ADMIN COMMANDS
# =========================
@bot.message_handler(commands=["broadcast"])
def broadcast_cmd(m):
    if not is_admin(m.from_user.id):
        return
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(m, "Usage: /broadcast <message>")
        return
    msg = args[1]
    sent, failed = broadcast_all(msg, skip_uid=m.from_user.id)
    bot.reply_to(m, f"Broadcast sent to {sent} users. Failed: {failed}")

@bot.message_handler(commands=["maintenance"])
def maintenance_cmd(m):
    if not is_admin(m.from_user.id):
        return
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        active, msg = get_maintenance()
        bot.reply_to(m, f"Maintenance mode: {'ON' if active else 'OFF'}\nMessage: {msg}")
        return
    sub = args[1].lower()
    if sub == "on":
        set_maintenance(True, "Bot is under maintenance. Please try again later.")
        bot.reply_to(m, "✅ Maintenance mode ENABLED. Use /maintenance off to disable.")
    elif sub == "off":
        set_maintenance(False)
        bot.reply_to(m, "✅ Maintenance mode DISABLED.")
    else:
        bot.reply_to(m, "Usage: /maintenance on|off")

@bot.message_handler(commands=["stats"])
def stats_cmd(m):
    if not is_admin(m.from_user.id):
        return
    total_users = db_query("SELECT COUNT(*) FROM profiles", fetch_one=True)[0]
    total_alerts = db_query("SELECT COUNT(*) FROM alerts WHERE active=1", fetch_one=True)[0]
    total_triggers = db_query("SELECT SUM(alerts_triggered) FROM profiles", fetch_one=True)[0] or 0
    bot.reply_to(m, f"📊 <b>Bot Stats</b>\n\nUsers: {total_users}\nActive alerts: {total_alerts}\nTotal triggers: {total_triggers}", parse_mode="HTML")

# =========================
# COMMAND HANDLERS
# =========================
@bot.message_handler(commands=["start", "help"])
def start(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    log_interaction(m.from_user.id, m.from_user.username, m.from_user.first_name, "/start")
    text, kb = main_menu()
    send_and_track(m.chat.id, text, kb)

@bot.message_handler(commands=["price"])
def price_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /price <symbol>", back_button())
        return
    symbol = args[1].upper()
    price, change, src = live_price(symbol)
    if price is None:
        text = f"❌ Could not fetch price for {symbol}"
    else:
        change_str = f" ({change:+.2f}%)" if change is not None else ""
        text = f"💰 <b>{symbol}</b>\n{fmt_price(price)}{change_str}\n<i>Source: {src}</i>"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["info"])
def info_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /info <symbol>", back_button())
        return
    symbol = args[1].upper()
    info = CoinGecko.info(symbol)
    if not info:
        text = f"❌ No info found for {symbol}"
    else:
        text = (
            f"🔎 <b>{info['name']} ({info['symbol']})</b>\n\n"
            f"📊 Rank: #{info['rank']}\n"
            f"💰 Price: {fmt_price(info['price'])}\n"
            f"📈 ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
            f"📉 ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
            f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
            f"📊 24h Volume: {fmt_price(info['volume'])}\n"
            f"🔄 Circulating Supply: {info['supply']:,.0f}\n"
        )
        if info['max_supply']:
            text += f"🔝 Max Supply: {info['max_supply']:,.0f}"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["live"])
def live_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /live <symbol>", back_button())
        return
    symbol = args[1].upper()
    start_ticker(m.chat.id, symbol)

@bot.message_handler(commands=["alert"])
def alert_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    text, kb = alerts_menu()
    send_and_track(m.chat.id, text, kb)

@bot.message_handler(commands=["scan"])
def scan_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /scan <contract_address>", back_button())
        return
    address = args[1]
    result, err = ContractScanner.scan(address)
    if err:
        send_and_track(m.chat.id, f"❌ {err}", back_button())
        return

    text = "🛡 <b>Contract Security</b>\n\n"
    text += f"<code>{address[:20]}...{address[-10:]}</code>\n\n"

    RISK_FLAGS = {
        "is_honeypot":    ("🍯 Honeypot",          "1"),
        "is_mintable":    ("🖨 Mintable",           "1"),
        "is_proxy":       ("🔀 Proxy contract",     "1"),
        "is_blacklisted": ("⛔ Blacklist",          "1"),
        "is_open_source": ("📄 Not open source",    "0"),
    }
    NUMERIC_FLAGS = {
        "buy_tax":  "🛒 Buy tax",
        "sell_tax": "💸 Sell tax",
    }

    flagged = []
    for field, (label, bad_val) in RISK_FLAGS.items():
        val = str(result.get(field, ""))
        if val == bad_val:
            flagged.append(f"⚠️ {label}: YES")

    for field, label in NUMERIC_FLAGS.items():
        val = result.get(field)
        try:
            pct = float(val) * 100
            if pct > 0:
                flagged.append(f"⚠️ {label}: {pct:.1f}%")
        except (TypeError, ValueError):
            pass

    if flagged:
        text += "<b>Risk flags:</b>\n" + "\n".join(flagged) + "\n"
    else:
        text += "✅ No high-risk flags detected.\n"

    text += f"\n🔗 <a href='https://gopluslabs.io/'>GoPlusLabs</a>"
    send_and_track(m.chat.id, text, back_button())

# =========================
# UI / MENUS
# =========================
def main_menu():
    text = "⚡ <b>PERSONA</b>\n\nFast crypto tools: prices, alerts, intel."
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("💰 Price check", callback_data="menu_price"),
           InlineKeyboardButton("🔔 Alert traps", callback_data="menu_alerts"))
    kb.row(InlineKeyboardButton("📊 Gainers/Losers", callback_data="menu_gainers_losers"),
           InlineKeyboardButton("🔎 Coin info", callback_data="menu_info"))
    kb.row(InlineKeyboardButton("💱 Currencies", callback_data="menu_multi"),
           InlineKeyboardButton("🛡 Scan CA", callback_data="menu_scan"))
    kb.row(InlineKeyboardButton("📋 Active alerts", callback_data="list_alerts"),
           InlineKeyboardButton("👤 Profile", callback_data="profile"))
    return text, kb

def gainers_losers_menu():
    text = "📊 <b>Gainers & Losers (Top 10)</b>\n\nChoose mode:"
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("🔄 Live (updates every 10s)", callback_data="live_gainers"))
    kb.row(InlineKeyboardButton("📈 Static top 10", callback_data="gainers"))
    kb.row(InlineKeyboardButton("📉 Static top 10 losers", callback_data="losers"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def price_menu():
    text = "💵 <b>Price Check</b>\n\nTap a coin or type a ticker.\nExamples: <code>BTC</code>, <code>PEPE</code>, <code>TAO</code>"
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
    kb = coin_grid("price", coins)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_price"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def info_menu():
    text = "🔎 <b>Coin Info</b>\n\nPick a coin for rank, ATH/ATL, supply, market cap, and volume."
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
    kb = coin_grid("info", coins)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_info"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def multi_menu():
    text = "💱 <b>Currencies</b>\n\nSelect a coin to see live multi‑currency prices (updates every 3s)."
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "LINK"]
    kb = InlineKeyboardMarkup()
    row = []
    for coin in coins:
        row.append(InlineKeyboardButton(coin, callback_data=f"multi_live_{coin}"))
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_multi"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
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
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def scan_menu():
    text = "🛡 <b>Contract Scanner</b>\n\nPaste contract address (ETH/BSC/Solana)"
    return text, back_button()

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

# =========================
# CALLBACK HANDLERS
# =========================
waiting = {}
wait_lock = threading.RLock()

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main_cb(call):
    cid = call.message.chat.id
    with wait_lock:
        waiting.pop((cid, call.from_user.id), None)
    text, kb = main_menu()
    send_and_track(cid, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_price")
def menu_price_cb(call):
    text, kb = price_menu()
    send_and_track(call.message.chat.id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_info")
def menu_info_cb(call):
    text, kb = info_menu()
    send_and_track(call.message.chat.id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_multi")
def menu_multi_cb(call):
    text, kb = multi_menu()
    send_and_track(call.message.chat.id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_alerts")
def menu_alerts_cb(call):
    text, kb = alerts_menu()
    send_and_track(call.message.chat.id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_gainers_losers")
def menu_gainers_losers_cb(call):
    text, kb = gainers_losers_menu()
    send_and_track(call.message.chat.id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_scan")
def menu_scan_cb(call):
    text, kb = scan_menu()
    send_and_track(call.message.chat.id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "profile")
def profile_cb(call):
    uid = call.from_user.id
    p = get_profile(uid)
    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"User ID: {p['user_id']}\n"
        f"Joined: {time.strftime('%Y-%m-%d', time.localtime(p['join_date']))}\n"
        f"Interactions: {p['total_interactions']}\n"
        f"Alerts set: {p['alerts_set']}\n"
        f"Alerts triggered: {p['alerts_triggered']}\n"
        f"Active alerts: {get_alert_count(uid)}"
    )
    send_and_track(call.message.chat.id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("price_"))
def price_cb(call):
    symbol = call.data.split("_", 1)[1]
    price, change, src = live_price(symbol)
    if price is None:
        text = f"❌ Could not fetch price for {symbol}"
    else:
        change_str = f" ({change:+.2f}%)" if change is not None else ""
        text = f"💰 <b>{symbol}</b>\n{fmt_price(price)}{change_str}\n<i>Source: {src}</i>"
    send_and_track(call.message.chat.id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("info_"))
def info_cb(call):
    symbol = call.data.split("_", 1)[1]
    info = CoinGecko.info(symbol)
    if not info:
        text = f"❌ No info found for {symbol}"
    else:
        text = (
            f"🔎 <b>{info['name']} ({info['symbol']})</b>\n\n"
            f"📊 Rank: #{info['rank']}\n"
            f"💰 Price: {fmt_price(info['price'])}\n"
            f"📈 ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
            f"📉 ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
            f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
            f"📊 24h Volume: {fmt_price(info['volume'])}\n"
            f"🔄 Circulating Supply: {info['supply']:,.0f}\n"
        )
        if info['max_supply']:
            text += f"🔝 Max Supply: {info['max_supply']:,.0f}"
    send_and_track(call.message.chat.id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("setalert_"))
def set_alert_preset_cb(call):
    parts = call.data.split("_")
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "Invalid alert format")
        return
    coin = parts[1]
    direction = parts[2]
    target_str = parts[3]
    try:
        target = float(target_str)
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid target value")
        return
    uid = call.from_user.id
    cid = call.message.chat.id
    alert_id, err = add_alert(uid, cid, coin, target, direction)
    if err:
        send_and_track(cid, err, back_button())
    else:
        send_and_track(cid, f"✅ Alert set for {coin} {direction} {target:,.2f}\nYou will be notified when triggered.", back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "custom_alert")
def custom_alert_cb(call):
    cid = call.message.chat.id
    uid = call.from_user.id
    with wait_lock:
        waiting[(cid, uid)] = "custom_alert"
    send_and_track(cid, "✏️ Send alert in format: <code>COIN > 12345</code> or <code>COIN < 12345</code>\nExample: <code>BTC > 70000</code>\n\nSend /cancel to abort.", back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "list_alerts")
def list_alerts_cb(call):
    cid = call.message.chat.id
    uid = call.from_user.id
    alerts = get_active_alerts(chat_id=cid)
    user_alerts = [a for a in alerts if a["user_id"] == uid]
    if not user_alerts:
        send_and_track(cid, "🔕 You have no active alerts.", back_button())
    else:
        text = "🔔 <b>Your active alerts</b>\n\n"
        for a in user_alerts:
            text += f"• {a['coin']} {a['direction']} {a['target']:,.2f}\n"
        text += "\nTap an alert to cancel it:"
        kb = InlineKeyboardMarkup()
        for a in user_alerts:
            kb.row(InlineKeyboardButton(f"❌ {a['coin']} {a['direction']} {a['target']:,.2f}", callback_data=f"cancel_alert_{a['id']}"))
        kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
        send_and_track(cid, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_alert_"))
def cancel_alert_cb(call):
    alert_id = int(call.data.split("_")[-1])
    deactivate_alert(alert_id)
    send_and_track(call.message.chat.id, "✅ Alert cancelled.", back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "live_gainers")
def live_gainers_cb(call):
    cid = call.message.chat.id
    with live_gainers_lock:
        if cid in live_gainers_active and live_gainers_active[cid].get("running", False):
            live_gainers_active[cid]["running"] = False
            send_and_track(cid, "⏹️ Live gainers/losers stopped.", back_button())
            bot.answer_callback_query(call.id)
            return
        text = "🚀 <b>Live Gainers/Losers (updates every 10s, Top 10)</b>\n\nLoading..."
        sent = send_and_track(cid, text, back_button())
        live_gainers_active[cid] = {"message_id": sent.message_id, "running": True}
        threading.Thread(target=_live_gainers_worker, args=(cid, sent.message_id), daemon=True).start()
        bot.answer_callback_query(call.id, "Started live gainers/losers")

@bot.callback_query_handler(func=lambda call: call.data == "gainers")
def static_gainers_cb(call):
    g, _ = Binance.top_movers()
    if not g:
        send_and_track(call.message.chat.id, "🚫 No data right now.", back_button())
    else:
        text = "🚀 <b>Top 10 Gainers (24h)</b>\n\n"
        for d in g:
            coin = d["symbol"].removesuffix("USDT")
            text += f"🟢 <b>{h(coin)}</b> — {fmt_price(float(d['lastPrice']))} — ▲ {float(d['priceChangePercent']):.2f}%\n"
        send_and_track(call.message.chat.id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "losers")
def static_losers_cb(call):
    _, l = Binance.top_movers()
    if not l:
        send_and_track(call.message.chat.id, "🚫 No data right now.", back_button())
    else:
        text = "📉 <b>Top 10 Losers (24h)</b>\n\n"
        for d in l:
            coin = d["symbol"].removesuffix("USDT")
            text += f"🔴 <b>{h(coin)}</b> — {fmt_price(float(d['lastPrice']))} — ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
        send_and_track(call.message.chat.id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("multi_live_"))
def multi_live_cb(call):
    symbol = call.data.split("_", 2)[2]
    cid = call.message.chat.id
    with multi_tickers_lock:
        if cid in multi_tickers and multi_tickers[cid].get("running", False):
            multi_tickers[cid]["running"] = False
            send_and_track(cid, f"⏹️ Stopped live multi‑currency for {symbol}.", back_button())
            bot.answer_callback_query(call.id)
            return
        text = f"💱 <b>{symbol} – Currencies (live)</b>\n\nLoading..."
        sent = send_and_track(cid, text, back_button())
        multi_tickers[cid] = {"symbol": symbol, "message_id": sent.message_id, "running": True}
        threading.Thread(target=_live_multi_worker, args=(cid, symbol, sent.message_id), daemon=True).start()
        bot.answer_callback_query(call.id, f"Live multi‑currency for {symbol} started")

@bot.callback_query_handler(func=lambda call: call.data.startswith("search_"))
def search_cb(call):
    mode = call.data.split("_", 1)[1]
    cid = call.message.chat.id
    uid = call.from_user.id
    with wait_lock:
        waiting[(cid, uid)] = f"search_{mode}"
    send_and_track(cid, f"🔍 Send the coin symbol (e.g., BTC, PEPE).\nSend /cancel to abort.", back_button())
    bot.answer_callback_query(call.id)

# =========================
# TEXT HANDLER (catch-all)
# =========================
@bot.message_handler(func=lambda m: True)
def text_handler(m):
    if m.text and m.text.startswith("/"):
        return
    cid = m.chat.id
    uid = m.from_user.id
    with wait_lock:
        state = waiting.pop((cid, uid), None)
    if not state:
        return

    if m.text.lower() == "/cancel":
        send_and_track(cid, "❌ Cancelled.", back_button())
        return

    if state == "custom_alert":
        pattern = r"^(\w+)\s*([<>])\s*([\d.]+)$"
        match = re.match(pattern, m.text.strip().upper())
        if not match:
            send_and_track(cid, "❌ Invalid format. Use: <code>COIN > 12345</code> or <code>COIN < 12345</code>", back_button())
            return
        coin, direction, target_str = match.groups()
        try:
            target = float(target_str)
        except ValueError:
            send_and_track(cid, "❌ Invalid number.", back_button())
            return
        alert_id, err = add_alert(uid, cid, coin, target, direction)
        if err:
            send_and_track(cid, err, back_button())
        else:
            send_and_track(cid, f"✅ Alert set for {coin} {direction} {target:,.2f}\nYou will be notified when triggered.", back_button())
        return

    if state.startswith("search_"):
        mode = state.split("_", 1)[1]
        symbol = m.text.strip().upper()
        if mode == "price":
            price, change, src = live_price(symbol)
            if price is None:
                text = f"❌ Could not fetch price for {symbol}"
            else:
                change_str = f" ({change:+.2f}%)" if change is not None else ""
                text = f"💰 <b>{symbol}</b>\n{fmt_price(price)}{change_str}\n<i>Source: {src}</i>"
            send_and_track(cid, text, back_button())
        elif mode == "info":
            info = CoinGecko.info(symbol)
            if not info:
                text = f"❌ No info found for {symbol}"
            else:
                text = (
                    f"🔎 <b>{info['name']} ({info['symbol']})</b>\n\n"
                    f"📊 Rank: #{info['rank']}\n"
                    f"💰 Price: {fmt_price(info['price'])}\n"
                    f"📈 ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
                    f"📉 ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
                    f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
                    f"📊 24h Volume: {fmt_price(info['volume'])}\n"
                    f"🔄 Circulating Supply: {info['supply']:,.0f}\n"
                )
                if info['max_supply']:
                    text += f"🔝 Max Supply: {info['max_supply']:,.0f}"
            send_and_track(cid, text, back_button())
        elif mode == "multi":
            start_live_multi(cid, symbol)
        return

def start_live_multi(chat_id, symbol):
    with multi_tickers_lock:
        if chat_id in multi_tickers and multi_tickers[chat_id].get("running", False):
            multi_tickers[chat_id]["running"] = False
            send_and_track(chat_id, f"⏹️ Stopped live multi‑currency for {symbol}.", back_button())
            return
        text = f"💱 <b>{symbol} – Currencies (live)</b>\n\nLoading..."
        sent = send_and_track(chat_id, text, back_button())
        multi_tickers[chat_id] = {"symbol": symbol, "message_id": sent.message_id, "running": True}
        threading.Thread(target=_live_multi_worker, args=(chat_id, symbol, sent.message_id), daemon=True).start()

# =========================
# SHUTDOWN
# =========================
def stop(sig, frame):
    log.info("Shutting down...")
    ws_restart.set()
    with tickers_lock:
        for key in list(tickers.keys()):
            tickers[key].set()
    with live_gainers_lock:
        for chat_id in list(live_gainers_active.keys()):
            live_gainers_active[chat_id]["running"] = False
    with multi_tickers_lock:
        for chat_id in list(multi_tickers.keys()):
            multi_tickers[chat_id]["running"] = False
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
log.info("🚀 Persona Bot started – Ultimate Edition (wallet checker removed)")
bot.delete_webhook()
time.sleep(1)
bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
