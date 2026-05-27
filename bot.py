#!/usr/bin/env python3
"""
Persona Bot – Static Version
- No live updates, no WebSocket, no background loops
- Instant responses for all commands and menus
- Inline menus for price, info, gainers, losers, multi‑currency, alerts, scan, profile
- Admin commands: /users, /broadcast, /maintenance, /stats
- Alerts only checked when using /price (no automatic background checks)
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

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN environment variable not set")
    sys.exit(1)

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "7458428092").split(",") if x.strip()]
MAX_ALERTS_PER_USER = 20
MAX_CA_LENGTH = 100
MAX_HISTORY = 5

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
# REQUEST SESSION
# =========================
session = requests.Session()
retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({"User-Agent": "PersonaBot/Static/1.0"})

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

def delete_user_message(m):
    try:
        bot.delete_message(m.chat.id, m.message_id)
    except Exception:
        pass

def safe_send(chat_id, text, markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
    except ApiTelegramException as e:
        log.error(f"Telegram API error sending to {chat_id}: {e}")
        return None
    except Exception as e:
        log.error(f"Unexpected error sending to {chat_id}: {e}")
        return None

def safe_edit(chat_id, msg_id, text, markup=None):
    try:
        bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=markup)
        return True
    except ApiTelegramException as e:
        if "message is not modified" in str(e):
            return True
        log.warning(f"Failed to edit message {msg_id} in {chat_id}: {e}")
        return False
    except Exception as e:
        log.warning(f"Unexpected edit error: {e}")
        return False

def is_admin(uid):
    return uid in ADMIN_IDS

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
        (int(time.time()), uid, uname or "?", fname or "?", cmd, (det or "")[:200]),
    )
    db_query("UPDATE profiles SET total_interactions = total_interactions + 1 WHERE user_id=?", (uid,))

# =========================
# API CALLS (static)
# =========================
def get_price(symbol):
    try:
        symbol = symbol.upper()
        r = session.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT", timeout=10)
        if r.status_code == 200:
            data = r.json()
            return float(data["lastPrice"]), float(data["priceChangePercent"])
    except Exception as e:
        log.error(f"Price error {symbol}: {e}")
    return None, None

def get_top_movers(limit=10):
    try:
        r = session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
        if r.status_code != 200:
            return [], []
        data = r.json()
        stable = {"USDT", "BUSD", "USDC", "DAI", "FDUSD"}
        filtered = []
        for d in data:
            if d["symbol"].endswith("USDT"):
                coin = d["symbol"].removesuffix("USDT")
                if coin not in stable and float(d.get("quoteVolume", 0)) > 1_000_000:
                    filtered.append(d)
        sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)
        gainers = sorted_data[:limit]
        losers = sorted_data[-limit:][::-1]
        return gainers, losers
    except Exception as e:
        log.error(f"Top movers error: {e}")
        return [], []

def get_coin_info(symbol):
    coin_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
        "SOL": "solana", "XRP": "ripple", "DOGE": "dogecoin",
        "ADA": "cardano", "AVAX": "avalanche-2", "LINK": "chainlink",
    }
    coin_id = coin_map.get(symbol.upper())
    if not coin_id:
        return None
    try:
        r = session.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        md = data.get("market_data", {})
        return {
            "name": data.get("name", symbol),
            "symbol": data.get("symbol", "").upper(),
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
    except Exception as e:
        log.error(f"Coin info error {symbol}: {e}")
        return None

def get_multi_prices(symbol):
    coin_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
        "SOL": "solana", "XRP": "ripple", "ADA": "cardano", "LINK": "chainlink",
    }
    coin_id = coin_map.get(symbol.upper())
    if not coin_id:
        return None
    try:
        r = session.get(
            f"https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd,eur,gbp,jpy,cny,aed,try,inr,krw,cad,aud"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get(coin_id, {})
    except Exception as e:
        log.error(f"Multi price error {symbol}: {e}")
    return None

def scan_contract(address):
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
# ALERTS (no background loop)
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

def check_alerts_for_symbol(uid, cid, symbol, current_price):
    rows = db_query(
        "SELECT id, coin, target, direction FROM alerts WHERE user_id=? AND chat_id=? AND active=1 AND coin=?",
        (uid, cid, symbol.upper()), fetch_all=True
    )
    triggered = []
    for aid, coin, target, direction in rows:
        hit = (direction == ">" and current_price >= target) or (direction == "<" and current_price <= target)
        if hit:
            deactivate_alert(aid)
            triggered.append((coin, direction, target))
            db_query("UPDATE profiles SET alerts_triggered = alerts_triggered + 1 WHERE user_id=?", (uid,))
    return triggered

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
    if now - last < 60:
        return True
    maintenance_spam[cid] = now
    safe_send(cid, "🔧 <b>Bot Under Maintenance</b>\n\n" + (h(msg) if msg else "We'll be back shortly."))
    return True

# =========================
# MESSAGE QUEUE (auto‑delete)
# =========================
msg_queue = {}
q_lock = threading.RLock()

def send_and_track(chat_id, text, markup=None):
    sent = safe_send(chat_id, text, markup)
    if sent is None:
        return None
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
# ADMIN COMMANDS
# =========================
@bot.message_handler(commands=["users"])
def users_cmd(m):
    if not is_admin(m.from_user.id):
        return
    delete_user_message(m)
    rows = db_query("SELECT user_id, username, first_name, join_date FROM profiles ORDER BY join_date DESC", fetch_all=True)
    if not rows:
        safe_send(m.chat.id, "No users found.")
        return
    text = "👥 <b>User List</b>\n\n"
    for uid, uname, fname, joined in rows:
        name_display = f"{fname or '?'}" + (f" (@{uname})" if uname else "")
        text += f"• <code>{uid}</code> – {name_display}\n"
        text += f"  Joined: {time.strftime('%Y-%m-%d', time.localtime(joined))}\n\n"
        if len(text) > 3900:
            safe_send(m.chat.id, text)
            text = ""
    if text:
        safe_send(m.chat.id, text)

@bot.message_handler(commands=["broadcast"])
def broadcast_cmd(m):
    if not is_admin(m.from_user.id):
        return
    delete_user_message(m)
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        safe_send(m.chat.id, "Usage: /broadcast <message>")
        return
    msg = args[1]
    rows = db_query("SELECT DISTINCT user_id FROM profiles", fetch_all=True) or []
    sent = 0
    for (uid,) in rows:
        if safe_send(uid, msg):
            sent += 1
        time.sleep(0.05)
    safe_send(m.chat.id, f"Broadcast sent to {sent} users.")

@bot.message_handler(commands=["maintenance"])
def maintenance_cmd(m):
    if not is_admin(m.from_user.id):
        return
    delete_user_message(m)
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        active, msg = get_maintenance()
        safe_send(m.chat.id, f"Maintenance mode: {'ON' if active else 'OFF'}\nMessage: {msg}")
        return
    sub = args[1].lower()
    if sub == "on":
        set_maintenance(True, "Bot is under maintenance. Please try again later.")
        safe_send(m.chat.id, "✅ Maintenance mode ENABLED. Use /maintenance off to disable.")
    elif sub == "off":
        set_maintenance(False)
        safe_send(m.chat.id, "✅ Maintenance mode DISABLED.")
    else:
        safe_send(m.chat.id, "Usage: /maintenance on|off")

@bot.message_handler(commands=["stats"])
def stats_cmd(m):
    if not is_admin(m.from_user.id):
        return
    delete_user_message(m)
    total_users = db_query("SELECT COUNT(*) FROM profiles", fetch_one=True)[0]
    total_alerts = db_query("SELECT COUNT(*) FROM alerts WHERE active=1", fetch_one=True)[0]
    total_triggers = db_query("SELECT SUM(alerts_triggered) FROM profiles", fetch_one=True)[0] or 0
    safe_send(m.chat.id, f"📊 <b>Bot Stats</b>\n\nUsers: {total_users}\nActive alerts: {total_alerts}\nTotal triggers: {total_triggers}")

# =========================
# COMMAND HANDLERS
# =========================
@bot.message_handler(commands=["start", "help"])
def start(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    log_interaction(m.from_user.id, m.from_user.username, m.from_user.first_name, "/start")
    text, kb = main_menu()
    send_and_track(m.chat.id, text, kb)

@bot.message_handler(commands=["cancel"])
def cancel_cmd(m):
    delete_user_message(m)
    cid = m.chat.id
    uid = m.from_user.id
    with wait_lock:
        had_state = waiting.pop((cid, uid), None)
    if had_state:
        send_and_track(cid, "❌ Cancelled.", back_button())
    else:
        send_and_track(cid, "Nothing to cancel.", back_button())

@bot.message_handler(commands=["price"])
def price_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /price <symbol>", back_button())
        return
    symbol = args[1].upper()
    price, change = get_price(symbol)
    if price is None:
        text = f"❌ Could not fetch price for {symbol}"
    else:
        text = f"💰 <b>{symbol}</b>\n{fmt_price(price)} ({change:+.2f}%)"
        # Check alerts for this symbol
        triggered = check_alerts_for_symbol(m.from_user.id, m.chat.id, symbol, price)
        for coin, direction, target in triggered:
            text += f"\n\n🚨 Alert triggered: {coin} {direction} {target:,.2f}"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["info"])
def info_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /info <symbol>", back_button())
        return
    symbol = args[1].upper()
    info = get_coin_info(symbol)
    if not info:
        text = f"❌ No info found for {symbol}"
    else:
        text = (
            f"🔎 <b>{info['name']} ({info['symbol']})</b>\n"
            f"🏆 Rank: #{info['rank']}\n"
            f"💰 Price: {fmt_price(info['price'])}\n"
            f"📈 ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
            f"📉 ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
            f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
            f"📊 24h Volume: {fmt_price(info['volume'])}\n"
            f"💎 Circulating Supply: {info['supply']:,.0f}"
        )
        if info['max_supply']:
            text += f"\n🔒 Max Supply: {info['max_supply']:,.0f}"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["gainers"])
def gainers_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    g, _ = get_top_movers(10)
    if not g:
        text = "❌ No gainers data available."
    else:
        text = "📈 <b>Top 10 Gainers (24h)</b>\n\n"
        for d in g:
            coin = d["symbol"].removesuffix("USDT")
            text += f"🟢 <b>{h(coin)}</b>  ▲ {float(d['priceChangePercent']):.2f}%  —  {fmt_price(float(d['lastPrice']))}\n"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["losers"])
def losers_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    _, l = get_top_movers(10)
    if not l:
        text = "❌ No losers data available."
    else:
        text = "📉 <b>Top 10 Losers (24h)</b>\n\n"
        for d in l:
            coin = d["symbol"].removesuffix("USDT")
            text += f"🔴 <b>{h(coin)}</b>  ▼ {abs(float(d['priceChangePercent'])):.2f}%  —  {fmt_price(float(d['lastPrice']))}\n"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["multi"])
def multi_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /multi <symbol>", back_button())
        return
    symbol = args[1].upper()
    prices = get_multi_prices(symbol)
    if not prices:
        text = f"❌ Failed to fetch multi‑currency data for {symbol}"
    else:
        order = [
            ("usd", "🇺🇸 USD"), ("eur", "🇪🇺 EUR"), ("gbp", "🇬🇧 GBP"),
            ("jpy", "🇯🇵 JPY"), ("cny", "🇨🇳 CNY"), ("aed", "🇦🇪 AED"),
            ("try", "🇹🇷 TRY"), ("inr", "🇮🇳 INR"), ("krw", "🇰🇷 KRW"),
            ("cad", "🇨🇦 CAD"), ("aud", "🇦🇺 AUD"),
        ]
        text = f"💱 <b>{symbol} – Currencies</b>\n\n"
        for code, flag in order:
            val = prices.get(code)
            if val is not None:
                text += f"{flag}: <b>{h(fmt_currency_value(code, val))}</b>\n"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["alert"])
def alert_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    args = m.text.split()
    if len(args) != 4:
        send_and_track(m.chat.id, "Usage: /alert BTC > 50000   or   /alert BTC < 40000", back_button())
        return
    coin = args[1].upper()
    direction = args[2]
    try:
        target = float(args[3])
    except ValueError:
        send_and_track(m.chat.id, "Invalid target number.", back_button())
        return
    if direction not in (">", "<"):
        send_and_track(m.chat.id, "Direction must be > or <", back_button())
        return
    aid, err = add_alert(m.from_user.id, m.chat.id, coin, target, direction)
    if err:
        send_and_track(m.chat.id, err, back_button())
    else:
        send_and_track(m.chat.id, f"✅ Alert set for {coin} {direction} {target:,.2f}", back_button())

@bot.message_handler(commands=["myalerts"])
def myalerts_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    rows = db_query("SELECT id, coin, target, direction FROM alerts WHERE user_id=? AND chat_id=? AND active=1", (m.from_user.id, m.chat.id), fetch_all=True)
    if not rows:
        send_and_track(m.chat.id, "🔕 You have no active alerts.", back_button())
        return
    text = "🔔 <b>Your active alerts</b>\n\n"
    for aid, coin, target, direction in rows:
        text += f"• {coin} {direction} {target:,.2f}  (ID: {aid})\n"
    text += "\nUse /cancelalert <ID> to remove."
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["cancelalert"])
def cancelalert_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    args = m.text.split()
    if len(args) != 2:
        send_and_track(m.chat.id, "Usage: /cancelalert <alert_id>", back_button())
        return
    try:
        aid = int(args[1])
    except ValueError:
        send_and_track(m.chat.id, "Invalid alert ID.", back_button())
        return
    row = db_query("SELECT user_id FROM alerts WHERE id=?", (aid,), fetch_one=True)
    if not row or row[0] != m.from_user.id:
        send_and_track(m.chat.id, "Not your alert or not found.", back_button())
        return
    deactivate_alert(aid)
    send_and_track(m.chat.id, "✅ Alert cancelled.", back_button())

@bot.message_handler(commands=["scan"])
def scan_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /scan <contract_address>", back_button())
        return
    address = args[1]
    result, err = scan_contract(address)
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

@bot.message_handler(commands=["profile"])
def profile_command(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    p = get_profile(m.from_user.id)
    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"User ID: {p['user_id']}\n"
        f"Joined: {time.strftime('%Y-%m-%d', time.localtime(p['join_date']))}\n"
        f"Alerts set: {p['alerts_set']}\n"
        f"Alerts triggered: {p['alerts_triggered']}\n"
        f"Active alerts: {get_alert_count(m.from_user.id)}"
    )
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["ping"])
def ping_cmd(m):
    if maintenance_block(m.from_user.id, m.chat.id):
        return
    delete_user_message(m)
    safe_send(m.chat.id, "🏓 Pong! Bot is alive.", back_button())

# =========================
# INLINE MENUS
# =========================
def main_menu():
    text = "⚡ <b>PERSONA</b>\n\nFast crypto tools: prices, alerts, intel."
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("💰 Price check", callback_data="menu_price"),
           InlineKeyboardButton("🔔 Alert traps", callback_data="menu_alerts"))
    kb.row(InlineKeyboardButton("📈 Gainers (top 10)", callback_data="menu_gainers"),
           InlineKeyboardButton("📉 Losers (top 10)", callback_data="menu_losers"))
    kb.row(InlineKeyboardButton("🔎 Coin info", callback_data="menu_info"),
           InlineKeyboardButton("💱 Currencies", callback_data="menu_multi"))
    kb.row(InlineKeyboardButton("🛡 Scan CA", callback_data="menu_scan"),
           InlineKeyboardButton("📋 My alerts", callback_data="list_alerts"))
    kb.row(InlineKeyboardButton("👤 Profile", callback_data="profile"))
    return text, kb

def price_menu():
    text = "💵 <b>Price Check</b>\n\nTap a coin to see current price."
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
    kb = InlineKeyboardMarkup()
    row = []
    for coin in coins:
        row.append(InlineKeyboardButton(coin, callback_data=f"price_{coin}"))
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_price"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def info_menu():
    text = "🔎 <b>Coin Info</b>\n\nPick a coin for market data."
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
    kb = InlineKeyboardMarkup()
    row = []
    for coin in coins:
        row.append(InlineKeyboardButton(coin, callback_data=f"info_{coin}"))
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_info"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def multi_menu():
    text = "💱 <b>Currencies</b>\n\nSelect a coin for multi‑currency rates."
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "LINK"]
    kb = InlineKeyboardMarkup()
    row = []
    for coin in coins:
        row.append(InlineKeyboardButton(coin, callback_data=f"multi_{coin}"))
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_multi"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def gainers_menu():
    text = "📈 <b>Top 10 Gainers (24h)</b>\n\nFetching latest data..."
    return text, back_button()

def losers_menu():
    text = "📉 <b>Top 10 Losers (24h)</b>\n\nFetching latest data..."
    return text, back_button()

def alerts_menu():
    text = "🔔 <b>Alert Traps</b>\n\nSet a price alert."
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC > 100k", callback_data="setalert_BTC_>_100000"),
           InlineKeyboardButton("BTC < 80k", callback_data="setalert_BTC_<_80000"))
    kb.row(InlineKeyboardButton("ETH > 4k", callback_data="setalert_ETH_>_4000"),
           InlineKeyboardButton("ETH < 2k", callback_data="setalert_ETH_<_2000"))
    kb.row(InlineKeyboardButton("SOL > 200", callback_data="setalert_SOL_>_200"),
           InlineKeyboardButton("SOL < 100", callback_data="setalert_SOL_<_100"))
    kb.row(InlineKeyboardButton("✏️ Custom alert", callback_data="custom_alert"))
    kb.row(InlineKeyboardButton("📋 My alerts", callback_data="list_alerts"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def scan_menu():
    text = "🛡 <b>Contract Scanner</b>\n\nSend a contract address (EVM or Solana).\n\nUse /scan <address>"
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
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_price")
def menu_price_cb(call):
    text, kb = price_menu()
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_info")
def menu_info_cb(call):
    text, kb = info_menu()
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_multi")
def menu_multi_cb(call):
    text, kb = multi_menu()
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_gainers")
def menu_gainers_cb(call):
    g, _ = get_top_movers(10)
    if not g:
        text = "❌ No gainers data available."
    else:
        text = "📈 <b>Top 10 Gainers (24h)</b>\n\n"
        for d in g:
            coin = d["symbol"].removesuffix("USDT")
            text += f"🟢 <b>{h(coin)}</b>  ▲ {float(d['priceChangePercent']):.2f}%  —  {fmt_price(float(d['lastPrice']))}\n"
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_losers")
def menu_losers_cb(call):
    _, l = get_top_movers(10)
    if not l:
        text = "❌ No losers data available."
    else:
        text = "📉 <b>Top 10 Losers (24h)</b>\n\n"
        for d in l:
            coin = d["symbol"].removesuffix("USDT")
            text += f"🔴 <b>{h(coin)}</b>  ▼ {abs(float(d['priceChangePercent'])):.2f}%  —  {fmt_price(float(d['lastPrice']))}\n"
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_alerts")
def menu_alerts_cb(call):
    text, kb = alerts_menu()
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_scan")
def menu_scan_cb(call):
    text, kb = scan_menu()
    safe_edit(call.message.chat.id, call.message.message_id, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "profile")
def profile_cb(call):
    uid = call.from_user.id
    p = get_profile(uid)
    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"User ID: {p['user_id']}\n"
        f"Joined: {time.strftime('%Y-%m-%d', time.localtime(p['join_date']))}\n"
        f"Alerts set: {p['alerts_set']}\n"
        f"Alerts triggered: {p['alerts_triggered']}\n"
        f"Active alerts: {get_alert_count(uid)}"
    )
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("price_"))
def price_cb(call):
    symbol = call.data.split("_")[1]
    price, change = get_price(symbol)
    if price is None:
        text = f"❌ Could not fetch price for {symbol}"
    else:
        text = f"💰 <b>{symbol}</b>\n{fmt_price(price)} ({change:+.2f}%)"
        # Check alerts for this symbol
        triggered = check_alerts_for_symbol(call.from_user.id, call.message.chat.id, symbol, price)
        for coin, direction, target in triggered:
            text += f"\n\n🚨 Alert triggered: {coin} {direction} {target:,.2f}"
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("info_"))
def info_cb(call):
    symbol = call.data.split("_")[1]
    info = get_coin_info(symbol)
    if not info:
        text = f"❌ No info found for {symbol}"
    else:
        text = (
            f"🔎 <b>{info['name']} ({info['symbol']})</b>\n"
            f"🏆 Rank: #{info['rank']}\n"
            f"💰 Price: {fmt_price(info['price'])}\n"
            f"📈 ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
            f"📉 ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
            f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
            f"📊 24h Volume: {fmt_price(info['volume'])}\n"
            f"💎 Circulating Supply: {info['supply']:,.0f}"
        )
        if info['max_supply']:
            text += f"\n🔒 Max Supply: {info['max_supply']:,.0f}"
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("multi_"))
def multi_cb(call):
    symbol = call.data.split("_")[1]
    prices = get_multi_prices(symbol)
    if not prices:
        text = f"❌ Failed to fetch multi‑currency data for {symbol}"
    else:
        order = [
            ("usd", "🇺🇸 USD"), ("eur", "🇪🇺 EUR"), ("gbp", "🇬🇧 GBP"),
            ("jpy", "🇯🇵 JPY"), ("cny", "🇨🇳 CNY"), ("aed", "🇦🇪 AED"),
            ("try", "🇹🇷 TRY"), ("inr", "🇮🇳 INR"), ("krw", "🇰🇷 KRW"),
            ("cad", "🇨🇦 CAD"), ("aud", "🇦🇺 AUD"),
        ]
        text = f"💱 <b>{symbol} – Currencies</b>\n\n"
        for code, flag in order:
            val = prices.get(code)
            if val is not None:
                text += f"{flag}: <b>{h(fmt_currency_value(code, val))}</b>\n"
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("setalert_"))
def set_alert_preset_cb(call):
    parts = call.data.split("_")
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "Invalid alert format")
        return
    coin = parts[1]
    direction = parts[2]
    try:
        target = float(parts[3])
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid target")
        return
    uid = call.from_user.id
    cid = call.message.chat.id
    aid, err = add_alert(uid, cid, coin, target, direction)
    if err:
        text = err
    else:
        text = f"✅ Alert set for {coin} {direction} {target:,.2f}"
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "custom_alert")
def custom_alert_cb(call):
    cid = call.message.chat.id
    uid = call.from_user.id
    with wait_lock:
        waiting[(cid, uid)] = "custom_alert"
    text = "✏️ Send alert in format: <code>COIN > 12345</code> or <code>COIN < 12345</code>\nExample: <code>BTC > 70000</code>\n\nSend /cancel to abort."
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "list_alerts")
def list_alerts_cb(call):
    uid = call.from_user.id
    cid = call.message.chat.id
    rows = db_query("SELECT id, coin, target, direction FROM alerts WHERE user_id=? AND chat_id=? AND active=1", (uid, cid), fetch_all=True)
    if not rows:
        text = "🔕 You have no active alerts."
    else:
        text = "🔔 <b>Your active alerts</b>\n\n"
        for aid, coin, target, direction in rows:
            text += f"• {coin} {direction} {target:,.2f}  (ID: {aid})\n"
        text += "\nUse /cancelalert <ID> to remove."
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("search_"))
def search_cb(call):
    mode = call.data.split("_")[1]  # price, info, multi
    cid = call.message.chat.id
    uid = call.from_user.id
    with wait_lock:
        waiting[(cid, uid)] = f"search_{mode}"
    text = "🔍 Send the coin symbol (e.g., BTC, PEPE).\nSend /cancel to abort."
    safe_edit(call.message.chat.id, call.message.message_id, text, back_button())
    bot.answer_callback_query(call.id)

# =========================
# TEXT HANDLER (custom alert and search)
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

    delete_user_message(m)

    if state == "custom_alert":
        pattern = r"^(\w+)\s*([<>])\s*([\d.]+)$"
        match = re.match(pattern, m.text.strip().upper())
        if not match:
            send_and_track(cid, "❌ Invalid format. Use: COIN > 12345", back_button())
            return
        coin, direction, target_str = match.groups()
        try:
            target = float(target_str)
        except ValueError:
            send_and_track(cid, "❌ Invalid number.", back_button())
            return
        aid, err = add_alert(uid, cid, coin, target, direction)
        if err:
            send_and_track(cid, err, back_button())
        else:
            send_and_track(cid, f"✅ Alert set for {coin} {direction} {target:,.2f}", back_button())
        # Go back to alerts menu
        text, kb = alerts_menu()
        send_and_track(cid, text, kb)
        return

    if state.startswith("search_"):
        mode = state.split("_")[1]
        symbol = m.text.strip().upper()
        if mode == "price":
            price, change = get_price(symbol)
            if price is None:
                text = f"❌ Could not fetch price for {symbol}"
            else:
                text = f"💰 <b>{symbol}</b>\n{fmt_price(price)} ({change:+.2f}%)"
                triggered = check_alerts_for_symbol(uid, cid, symbol, price)
                for coin, direction, target in triggered:
                    text += f"\n\n🚨 Alert triggered: {coin} {direction} {target:,.2f}"
        elif mode == "info":
            info = get_coin_info(symbol)
            if not info:
                text = f"❌ No info found for {symbol}"
            else:
                text = (
                    f"🔎 <b>{info['name']} ({info['symbol']})</b>\n"
                    f"🏆 Rank: #{info['rank']}\n"
                    f"💰 Price: {fmt_price(info['price'])}\n"
                    f"📈 ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
                    f"📉 ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
                    f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
                    f"📊 24h Volume: {fmt_price(info['volume'])}\n"
                    f"💎 Circulating Supply: {info['supply']:,.0f}"
                )
                if info['max_supply']:
                    text += f"\n🔒 Max Supply: {info['max_supply']:,.0f}"
        elif mode == "multi":
            prices = get_multi_prices(symbol)
            if not prices:
                text = f"❌ Failed to fetch multi‑currency data for {symbol}"
            else:
                order = [
                    ("usd", "🇺🇸 USD"), ("eur", "🇪🇺 EUR"), ("gbp", "🇬🇧 GBP"),
                    ("jpy", "🇯🇵 JPY"), ("cny", "🇨🇳 CNY"), ("aed", "🇦🇪 AED"),
                    ("try", "🇹🇷 TRY"), ("inr", "🇮🇳 INR"), ("krw", "🇰🇷 KRW"),
                    ("cad", "🇨🇦 CAD"), ("aud", "🇦🇺 AUD"),
                ]
                text = f"💱 <b>{symbol} – Currencies</b>\n\n"
                for code, flag in order:
                    val = prices.get(code)
                    if val is not None:
                        text += f"{flag}: <b>{h(fmt_currency_value(code, val))}</b>\n"
        else:
            return
        send_and_track(cid, text, back_button())

# =========================
# SHUTDOWN
# =========================
def stop(sig, frame):
    log.info("Shutting down...")
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
log.info("🚀 Persona Bot started – static version, no live updates, instant responses")
bot.delete_webhook()
time.sleep(1)
bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
