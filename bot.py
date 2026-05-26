#!/usr/bin/env python3
"""
Persona Bot – Ultra Stable (No live updates, no WebSocket, no hanging)
- All commands respond instantly
- /price, /info, /gainers, /losers, /multi (static)
- Alerts (simple, no live loop – checks on command)
- Contract scanner, profile, admin commands
- No background threads except alert check (lightweight)
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
    print("❌ BOT_TOKEN not set")
    sys.exit(1)

ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "7458428092").split(",") if x.strip()]
MAX_ALERTS_PER_USER = 20
MAX_CA_LENGTH = 100
MAX_HISTORY = 5

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("PersonaBot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)  # threaded=False for stability
os.makedirs("data", exist_ok=True)

# =========================
# DATABASE
# =========================
db_path = "data/persona.db"
db_lock = threading.RLock()
conn = sqlite3.connect(db_path, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")

def db_query(query, params=(), fetch_one=False, fetch_all=False):
    with db_lock:
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        if fetch_one:
            return cur.fetchone()
        if fetch_all:
            return cur.fetchall()
        return cur.lastrowid

def init_db():
    db_query("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        coin TEXT,
        target REAL,
        direction TEXT,
        active INTEGER DEFAULT 1,
        created_at INTEGER
    )""")
    db_query("""CREATE TABLE IF NOT EXISTS profiles (
        user_id INTEGER PRIMARY KEY,
        join_date INTEGER,
        alerts_set INTEGER DEFAULT 0,
        alerts_triggered INTEGER DEFAULT 0,
        username TEXT,
        first_name TEXT
    )""")
    db_query("""CREATE TABLE IF NOT EXISTS maintenance (
        id INTEGER PRIMARY KEY CHECK (id=1),
        active INTEGER DEFAULT 0,
        message TEXT DEFAULT ''
    )""")
    db_query("INSERT OR IGNORE INTO maintenance(id, active, message) VALUES(1,0,'')")

init_db()

# =========================
# HELPERS
# =========================
def h(value):
    return html.escape(str(value) if value else "", quote=False)

def fmt_price(p):
    if p is None: return "N/A"
    if p >= 1: return f"${p:,.4f}"
    if p >= 0.0001: return f"${p:,.6f}"
    if p >= 0.000001: return f"${p:,.8f}"
    return f"${p:.10f}"

def safe_send(chat_id, text, markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        log.error(f"Send error: {e}")
        return None

def delete_user_message(m):
    try:
        bot.delete_message(m.chat.id, m.message_id)
    except:
        pass

def is_admin(uid):
    return uid in ADMIN_IDS

def get_profile(user_id):
    row = db_query("SELECT * FROM profiles WHERE user_id=?", (user_id,), fetch_one=True)
    if not row:
        now = int(time.time())
        db_query("INSERT INTO profiles(user_id, join_date) VALUES(?,?)", (user_id, now))
        row = (user_id, now, 0, 0, None, None)
    return {"user_id": row[0], "join_date": row[1], "alerts_set": row[2], "alerts_triggered": row[3], "username": row[4], "first_name": row[5]}

def update_profile(user_id, **kwargs):
    for k, v in kwargs.items():
        db_query(f"UPDATE profiles SET {k}=? WHERE user_id=?", (v, user_id))

def log_interaction(uid, uname, fname, cmd):
    p = get_profile(uid)
    if p["username"] != uname or p["first_name"] != fname:
        update_profile(uid, username=uname, first_name=fname)
    db_query("UPDATE profiles SET alerts_set=alerts_set WHERE user_id=?", (uid,))  # dummy to keep pattern

# =========================
# API CALLS (static, no cache issues)
# =========================
session = requests.Session()
session.headers.update({"User-Agent": "PersonaBot/2.0"})

def get_price(symbol):
    try:
        r = session.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT", timeout=10)
        if r.status_code == 200:
            data = r.json()
            return float(data["lastPrice"]), float(data["priceChangePercent"])
    except:
        pass
    return None, None

def get_top_movers(limit=10):
    try:
        r = session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
        if r.status_code != 200:
            return [], []
        data = r.json()
        stable = {"USDT","BUSD","USDC","DAI","FDUSD"}
        filtered = [d for d in data if d["symbol"].endswith("USDT") and d["symbol"].removesuffix("USDT") not in stable and float(d.get("quoteVolume",0))>1_000_000]
        sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)
        return sorted_data[:limit], sorted_data[-limit:][::-1]
    except:
        return [], []

def get_coin_info(symbol):
    coin_map = {"BTC":"bitcoin","ETH":"ethereum","BNB":"binancecoin","SOL":"solana","XRP":"ripple","DOGE":"dogecoin","ADA":"cardano","AVAX":"avalanche-2","LINK":"chainlink"}
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
            "price": md.get("current_price", {}).get("usd", 0),
            "market_cap": md.get("market_cap", {}).get("usd", 0),
            "volume": md.get("total_volume", {}).get("usd", 0),
            "ath": md.get("ath", {}).get("usd", 0),
            "atl": md.get("atl", {}).get("usd", 0),
            "supply": md.get("circulating_supply", 0),
            "max_supply": md.get("max_supply")
        }
    except:
        return None

def get_multi_prices(symbol):
    coin_map = {"BTC":"bitcoin","ETH":"ethereum","BNB":"binancecoin","SOL":"solana","XRP":"ripple","ADA":"cardano","LINK":"chainlink"}
    coin_id = coin_map.get(symbol.upper())
    if not coin_id:
        return None
    try:
        r = session.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd,eur,gbp,jpy,cny,aed,try,inr,krw,cad,aud", timeout=10)
        if r.status_code == 200:
            return r.json().get(coin_id, {})
    except:
        pass
    return None

def scan_contract(address):
    # simplified – same as before but without heavy imports
    addr = re.sub(r"\s+", "", address)
    if len(addr) > 100:
        return None, "Too long"
    try:
        if re.match(r"^0x[a-f0-9]{40}$", addr.lower()):
            for chain in [1,56]:
                url = f"https://api.gopluslabs.io/api/v1/token_security/{chain}?contract_addresses={addr.lower()}"
                r = session.get(url, timeout=15)
                if r.status_code == 200:
                    result = r.json().get("result", {}).get(addr.lower(), {})
                    if result:
                        return result, None
            return None, "Not found"
        else:
            return None, "Only EVM addresses supported"
    except Exception as e:
        return None, f"Error: {e}"

# =========================
# ALERTS (simple, no background loop – check on command)
# =========================
def add_alert(uid, cid, coin, target, direction):
    cnt = db_query("SELECT COUNT(*) FROM alerts WHERE user_id=? AND active=1", (uid,), fetch_one=True)[0]
    if cnt >= MAX_ALERTS_PER_USER:
        return None, f"Max {MAX_ALERTS_PER_USER} alerts"
    aid = db_query("INSERT INTO alerts(user_id,chat_id,coin,target,direction,created_at) VALUES(?,?,?,?,?,?)",
                   (uid, cid, coin.upper(), target, direction, int(time.time())))
    db_query("UPDATE profiles SET alerts_set = alerts_set + 1 WHERE user_id=?", (uid,))
    return aid, None

def get_user_alerts(uid, cid=None):
    if cid:
        return db_query("SELECT id,coin,target,direction FROM alerts WHERE user_id=? AND chat_id=? AND active=1", (uid, cid), fetch_all=True)
    return db_query("SELECT id,coin,target,direction FROM alerts WHERE user_id=? AND active=1", (uid,), fetch_all=True)

def deactivate_alert(aid):
    db_query("UPDATE alerts SET active=0 WHERE id=?", (aid,))

def check_alerts_now(uid, cid, symbol, current_price):
    # Called after price command to trigger alerts for that symbol
    rows = db_query("SELECT id,coin,target,direction FROM alerts WHERE user_id=? AND chat_id=? AND active=1 AND coin=?", (uid, cid, symbol.upper()), fetch_all=True)
    triggered = []
    for aid, coin, target, direction in rows:
        if (direction == ">" and current_price >= target) or (direction == "<" and current_price <= target):
            deactivate_alert(aid)
            triggered.append((coin, direction, target))
            db_query("UPDATE profiles SET alerts_triggered = alerts_triggered + 1 WHERE user_id=?", (uid,))
    return triggered

# =========================
# COMMANDS
# =========================
@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    if is_admin(m.from_user.id):
        safe_send(m.chat.id, "⚡ Admin commands:\n/broadcast <msg>\n/maintenance on|off\n/stats\n/users")
    safe_send(m.chat.id, "🤖 <b>Persona Bot (Stable)</b>\n\n/price BTC\n/info BTC\n/gainers\n/losers\n/multi BTC\n/alert BTC > 50000\n/my alerts\n/scan 0x...\n/profile")

@bot.message_handler(commands=["ping"])
def cmd_ping(m):
    safe_send(m.chat.id, "🏓 Pong!")

@bot.message_handler(commands=["price"])
def cmd_price(m):
    args = m.text.split()
    if len(args) < 2:
        safe_send(m.chat.id, "Usage: /price BTC")
        return
    sym = args[1].upper()
    price, change = get_price(sym)
    if price:
        text = f"💰 {sym}: {fmt_price(price)} ({change:+.2f}%)"
        # Check alerts for this user
        triggered = check_alerts_now(m.from_user.id, m.chat.id, sym, price)
        for coin, dir, tgt in triggered:
            text += f"\n\n🚨 Alert triggered: {coin} {dir} {tgt:,.2f}"
        safe_send(m.chat.id, text)
    else:
        safe_send(m.chat.id, f"❌ No price for {sym}")

@bot.message_handler(commands=["info"])
def cmd_info(m):
    args = m.text.split()
    if len(args) < 2:
        safe_send(m.chat.id, "Usage: /info BTC")
        return
    sym = args[1].upper()
    info = get_coin_info(sym)
    if info:
        text = f"🔎 {info['name']} ({sym})\n💰 {fmt_price(info['price'])}\n🏦 Market Cap: {fmt_price(info['market_cap'])}\n📊 24h Vol: {fmt_price(info['volume'])}\n📈 ATH: {fmt_price(info['ath'])}\n📉 ATL: {fmt_price(info['atl'])}\n🔄 Supply: {info['supply']:,.0f}"
        if info['max_supply']:
            text += f" / {info['max_supply']:,.0f}"
        safe_send(m.chat.id, text)
    else:
        safe_send(m.chat.id, f"❌ No info for {sym}")

@bot.message_handler(commands=["gainers"])
def cmd_gainers(m):
    g, _ = get_top_movers(10)
    if not g:
        safe_send(m.chat.id, "No gainers data")
        return
    text = "📈 Top 10 Gainers (24h)\n"
    for d in g:
        coin = d["symbol"].removesuffix("USDT")
        text += f"🟢 {coin} +{float(d['priceChangePercent']):.2f}% – {fmt_price(float(d['lastPrice']))}\n"
    safe_send(m.chat.id, text)

@bot.message_handler(commands=["losers"])
def cmd_losers(m):
    _, l = get_top_movers(10)
    if not l:
        safe_send(m.chat.id, "No losers data")
        return
    text = "📉 Top 10 Losers (24h)\n"
    for d in l:
        coin = d["symbol"].removesuffix("USDT")
        text += f"🔴 {coin} {float(d['priceChangePercent']):.2f}% – {fmt_price(float(d['lastPrice']))}\n"
    safe_send(m.chat.id, text)

@bot.message_handler(commands=["multi"])
def cmd_multi(m):
    args = m.text.split()
    if len(args) < 2:
        safe_send(m.chat.id, "Usage: /multi BTC")
        return
    sym = args[1].upper()
    prices = get_multi_prices(sym)
    if not prices:
        safe_send(m.chat.id, f"❌ No multi‑currency data for {sym}")
        return
    order = [("usd","USD"),("eur","EUR"),("gbp","GBP"),("jpy","JPY"),("cny","CNY"),("aed","AED"),("try","TRY"),("inr","INR"),("krw","KRW"),("cad","CAD"),("aud","AUD")]
    text = f"💱 {sym} multi-currency:\n"
    for code, name in order:
        val = prices.get(code)
        if val:
            if code == "aed":
                text += f"🇦🇪 AED: {val:,.2f}\n"
            elif val >= 1:
                text += f"{name}: {val:,.2f}\n"
            else:
                text += f"{name}: {val:.8f}\n"
    safe_send(m.chat.id, text)

@bot.message_handler(commands=["alert"])
def cmd_alert(m):
    args = m.text.split()
    if len(args) != 4:
        safe_send(m.chat.id, "Usage: /alert BTC > 50000   or   /alert BTC < 40000")
        return
    coin = args[1].upper()
    direction = args[2]
    try:
        target = float(args[3])
    except:
        safe_send(m.chat.id, "Invalid target")
        return
    if direction not in (">", "<"):
        safe_send(m.chat.id, "Direction must be > or <")
        return
    aid, err = add_alert(m.from_user.id, m.chat.id, coin, target, direction)
    if err:
        safe_send(m.chat.id, err)
    else:
        safe_send(m.chat.id, f"✅ Alert set: {coin} {direction} {target:,.2f}")

@bot.message_handler(commands=["myalerts"])
def cmd_myalerts(m):
    alerts = get_user_alerts(m.from_user.id, m.chat.id)
    if not alerts:
        safe_send(m.chat.id, "No active alerts.")
        return
    text = "🔔 Your alerts:\n"
    for aid, coin, target, direction in alerts:
        text += f"• {coin} {direction} {target:,.2f}  [/cancelalert {aid}]\n"
    safe_send(m.chat.id, text)

@bot.message_handler(commands=["cancelalert"])
def cmd_cancelalert(m):
    args = m.text.split()
    if len(args) != 2:
        safe_send(m.chat.id, "Usage: /cancelalert <alert_id>")
        return
    try:
        aid = int(args[1])
    except:
        safe_send(m.chat.id, "Invalid ID")
        return
    # verify ownership
    row = db_query("SELECT user_id FROM alerts WHERE id=?", (aid,), fetch_one=True)
    if not row or row[0] != m.from_user.id:
        safe_send(m.chat.id, "Not your alert")
        return
    deactivate_alert(aid)
    safe_send(m.chat.id, "Alert cancelled.")

@bot.message_handler(commands=["scan"])
def cmd_scan(m):
    args = m.text.split()
    if len(args) < 2:
        safe_send(m.chat.id, "Usage: /scan 0x...")
        return
    address = args[1]
    result, err = scan_contract(address)
    if err:
        safe_send(m.chat.id, f"❌ {err}")
        return
    text = "🛡 Contract scan\n\n"
    risk_flags = []
    for field, label in [("is_honeypot","Honeypot"),("is_mintable","Mintable"),("is_proxy","Proxy"),("is_blacklisted","Blacklist")]:
        if str(result.get(field, "")).lower() == "1":
            risk_flags.append(f"⚠️ {label}: YES")
    buy_tax = result.get("buy_tax", 0)
    if buy_tax:
        try:
            pct = float(buy_tax)*100
            if pct > 0:
                risk_flags.append(f"⚠️ Buy tax: {pct:.1f}%")
        except: pass
    if risk_flags:
        text += "\n".join(risk_flags)
    else:
        text += "✅ No major risks detected"
    safe_send(m.chat.id, text)

@bot.message_handler(commands=["profile"])
def cmd_profile(m):
    p = get_profile(m.from_user.id)
    text = f"👤 Profile\nUser ID: {p['user_id']}\nJoined: {time.strftime('%Y-%m-%d', time.localtime(p['join_date']))}\nAlerts set: {p['alerts_set']}\nAlerts triggered: {p['alerts_triggered']}"
    safe_send(m.chat.id, text)

# =========================
# ADMIN COMMANDS
# =========================
@bot.message_handler(commands=["users"])
def cmd_users(m):
    if not is_admin(m.from_user.id):
        return
    rows = db_query("SELECT user_id, username, first_name, join_date FROM profiles ORDER BY join_date DESC", fetch_all=True)
    if not rows:
        safe_send(m.chat.id, "No users")
        return
    text = "👥 Users:\n"
    for uid, uname, fname, joined in rows:
        name = f"{fname or '?'}" + (f" (@{uname})" if uname else "")
        text += f"• {uid} – {name}\n"
        if len(text) > 3900:
            safe_send(m.chat.id, text)
            text = ""
    if text:
        safe_send(m.chat.id, text)

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(m):
    if not is_admin(m.from_user.id):
        return
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        safe_send(m.chat.id, "Usage: /broadcast <message>")
        return
    msg = args[1]
    users = db_query("SELECT user_id FROM profiles", fetch_all=True)
    sent = 0
    for (uid,) in users:
        if safe_send(uid, msg):
            sent += 1
        time.sleep(0.1)
    safe_send(m.chat.id, f"Broadcast sent to {sent} users")

@bot.message_handler(commands=["maintenance"])
def cmd_maintenance(m):
    if not is_admin(m.from_user.id):
        return
    args = m.text.split()
    if len(args) < 2:
        safe_send(m.chat.id, "Usage: /maintenance on|off")
        return
    if args[1] == "on":
        db_query("UPDATE maintenance SET active=1, message='Bot under maintenance' WHERE id=1")
        safe_send(m.chat.id, "Maintenance ON")
    else:
        db_query("UPDATE maintenance SET active=0 WHERE id=1")
        safe_send(m.chat.id, "Maintenance OFF")

@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    if not is_admin(m.from_user.id):
        return
    total_users = db_query("SELECT COUNT(*) FROM profiles", fetch_one=True)[0]
    total_alerts = db_query("SELECT COUNT(*) FROM alerts WHERE active=1", fetch_one=True)[0]
    total_triggers = db_query("SELECT SUM(alerts_triggered) FROM profiles", fetch_one=True)[0] or 0
    safe_send(m.chat.id, f"Stats\nUsers: {total_users}\nActive alerts: {total_alerts}\nTriggers: {total_triggers}")

# =========================
# RUN
# =========================
print("🚀 Bot started (ultra stable, no live updates)")
bot.infinity_polling(timeout=60, long_polling_timeout=60)
