#!/usr/bin/env python3
"""
Persona Bot – Final Working Version
- Keeps last 5 messages (user + bot + buttons)
- All commands work (price, info, gainers, losers, multi, alerts, scan, profile, admin)
- No live updates, no WebSocket, no Etherscan
- Fixed database schema with migration
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
MAX_CHAT_MESSAGES = 5

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("PersonaBot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
os.makedirs("data", exist_ok=True)

# =========================
# DATABASE (with proper migration)
# =========================
db_path = "data/persona.db"
db_lock = threading.RLock()
conn = sqlite3.connect(db_path, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")

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
    # Create profiles table with all columns
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
    # Add missing columns if table existed without them
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(profiles)")
        columns = [col[1] for col in cur.fetchall()]
        if "username" not in columns:
            conn.execute("ALTER TABLE profiles ADD COLUMN username TEXT")
            conn.commit()
        if "first_name" not in columns:
            conn.execute("ALTER TABLE profiles ADD COLUMN first_name TEXT")
            conn.commit()
    except Exception as e:
        log.warning(f"Migration alter error: {e}")

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
            id INTEGER PRIMARY KEY CHECK (id=1),
            active INTEGER DEFAULT 0,
            message TEXT DEFAULT ''
        )
    """)
    db_query("INSERT OR IGNORE INTO maintenance(id, active, message) VALUES(1,0,'')")

init_db()

# =========================
# REQUEST SESSION
# =========================
session = requests.Session()
retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({"User-Agent": "PersonaBot/1.0"})

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

def fmt_currency_value(code, val):
    if val is None: return "N/A"
    symbols = {"usd":"$","eur":"€","gbp":"£","jpy":"¥","cny":"¥","aed":"د.إ","try":"₺","inr":"₹","krw":"₩","cad":"C$","aud":"A$"}
    sym = symbols.get(code, "")
    if code == "aed": return f"{sym} {val:,.2f}"
    if val >= 1: return f"{sym}{val:,.2f}"
    return f"{sym}{val:.8f}"

def safe_send(chat_id, text, markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        log.error(f"Send error: {e}")
        return None

def is_admin(uid):
    return uid in ADMIN_IDS

def get_profile(user_id):
    row = db_query("SELECT * FROM profiles WHERE user_id=?", (user_id,), fetch_one=True)
    if not row:
        now = int(time.time())
        db_query("INSERT INTO profiles(user_id, join_date) VALUES(?,?)", (user_id, now))
        row = (user_id, now, 0, 0, 0, None, None)  # 7 columns
    return {
        "user_id": row[0],
        "join_date": row[1],
        "total_interactions": row[2],
        "alerts_set": row[3],
        "alerts_triggered": row[4],
        "username": row[5],
        "first_name": row[6]
    }

def update_profile(user_id, **kwargs):
    for k, v in kwargs.items():
        db_query(f"UPDATE profiles SET {k}=? WHERE user_id=?", (v, user_id))

def log_interaction(uid, uname, fname, cmd, det=""):
    p = get_profile(uid)
    if p["username"] != uname or p["first_name"] != fname:
        update_profile(uid, username=uname, first_name=fname)
    db_query("INSERT INTO analytics(timestamp,user_id,username,first_name,command,details) VALUES(?,?,?,?,?,?)",
             (int(time.time()), uid, uname or "?", fname or "?", cmd, (det or "")[:200]))
    db_query("UPDATE profiles SET total_interactions = total_interactions + 1 WHERE user_id=?", (uid,))

# =========================
# MESSAGE QUEUE – keep last 5 messages
# =========================
message_queues = {}
queue_lock = threading.RLock()

def track_message(chat_id, msg_id):
    with queue_lock:
        if chat_id not in message_queues:
            message_queues[chat_id] = []
        queue = message_queues[chat_id]
        queue.append(msg_id)
        while len(queue) > MAX_CHAT_MESSAGES:
            oldest = queue.pop(0)
            try:
                bot.delete_message(chat_id, oldest)
            except:
                pass

def send_and_track(chat_id, text, markup=None):
    sent = safe_send(chat_id, text, markup)
    if sent:
        track_message(chat_id, sent.message_id)
    return sent

def back_button():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# =========================
# API FUNCTIONS
# =========================
def get_price(symbol):
    try:
        r = session.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}USDT", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return float(data["lastPrice"]), float(data["priceChangePercent"])
    except Exception as e:
        log.error(f"Price error: {e}")
    return None, None

def get_top_movers(limit=10):
    try:
        r = session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        if r.status_code != 200:
            return [], []
        data = r.json()
        stable = {"USDT","BUSD","USDC","DAI","FDUSD"}
        filtered = [d for d in data if d["symbol"].endswith("USDT") and d["symbol"].removesuffix("USDT") not in stable and float(d.get("quoteVolume",0)) > 1_000_000]
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
        r = session.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}", timeout=8)
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
        r = session.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd,eur,gbp,jpy,cny,aed,try,inr,krw,cad,aud", timeout=8)
        if r.status_code == 200:
            return r.json().get(coin_id, {})
    except:
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
                try:
                    r = session.get(url, timeout=10)
                    if r.status_code == 200:
                        data = r.json()
                        result = data.get("result", {}).get(addr_lower, {})
                        if result:
                            return result, None
                except Exception as e:
                    log.warning(f"GoPlus chain {chain} error: {e}")
                    continue
            return None, "Contract not found on Ethereum or BSC."
        else:
            sol_addr = re.sub(r"[^a-zA-Z0-9]", "", addr)
            if len(sol_addr) < 32 or len(sol_addr) > 44:
                return None, "Invalid Solana address."
            url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={sol_addr}"
            r = session.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                result = data.get("result", {}).get(sol_addr, {})
                if result:
                    return result, None
            return None, "Solana contract not found."
    except Exception as e:
        log.error(f"Scanner fatal error: {e}")
        return None, "Scanner error: API timeout or unreachable."

# =========================
# ALERTS
# =========================
def add_alert(uid, cid, coin, target, direction):
    cnt = db_query("SELECT COUNT(*) FROM alerts WHERE user_id=? AND active=1", (uid,), fetch_one=True)[0]
    if cnt >= MAX_ALERTS_PER_USER:
        return None, f"Max {MAX_ALERTS_PER_USER} alerts"
    aid = db_query("INSERT INTO alerts(user_id,chat_id,coin,target,direction,created_at) VALUES(?,?,?,?,?,?)",
                   (uid, cid, coin.upper(), target, direction, int(time.time())))
    db_query("UPDATE profiles SET alerts_set = alerts_set + 1 WHERE user_id=?", (uid,))
    return aid, None

def deactivate_alert(aid):
    db_query("UPDATE alerts SET active=0 WHERE id=?", (aid,))

def get_alert_count(uid):
    row = db_query("SELECT COUNT(*) FROM alerts WHERE user_id=? AND active=1", (uid,), fetch_one=True)
    return row[0] if row else 0

def check_alerts_for_symbol(uid, cid, symbol, current_price):
    rows = db_query("SELECT id, coin, target, direction FROM alerts WHERE user_id=? AND chat_id=? AND active=1 AND coin=?", (uid, cid, symbol.upper()), fetch_all=True)
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
    if not row: return False, ""
    return bool(row[0]), row[1] or ""

def set_maintenance(active, message=""):
    db_query("UPDATE maintenance SET active=?, message=? WHERE id=1", (1 if active else 0, message))

def maintenance_block(uid, cid):
    if is_admin(uid): return False
    active, msg = get_maintenance()
    if not active: return False
    now = time.time()
    last = maintenance_spam.get(cid, 0)
    if now - last < 60: return True
    maintenance_spam[cid] = now
    safe_send(cid, f"🔧 <b>Bot Under Maintenance</b>\n\n{msg if msg else 'We will be back shortly.'}")
    return True

# =========================
# COMMAND HANDLERS
# =========================
@bot.message_handler(commands=["start", "help"])
def start(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    log_interaction(m.from_user.id, m.from_user.username, m.from_user.first_name, "/start")
    text = ("⚡ <b>Persona Bot</b>\n\n"
            "/price BTC\n/info BTC\n/gainers\n/losers\n/multi BTC\n"
            "/alert BTC > 50000\n/my alerts\n/scan 0x...\n/profile\n/ping")
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["cancel"])
def cancel_cmd(m):
    track_message(m.chat.id, m.message_id)
    cid = m.chat.id
    uid = m.from_user.id
    with wait_lock:
        had_state = waiting.pop((cid, uid), None)
    if had_state:
        send_and_track(cid, "❌ Cancelled.", back_button())
    else:
        send_and_track(cid, "Nothing to cancel.", back_button())

@bot.message_handler(commands=["ping"])
def ping_cmd(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    send_and_track(m.chat.id, "🏓 Pong! Bot is alive.", back_button())

@bot.message_handler(commands=["price"])
def price_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /price <symbol>", back_button())
        return
    sym = args[1].upper()
    price, change = get_price(sym)
    if price is None:
        send_and_track(m.chat.id, f"❌ No price for {sym}", back_button())
    else:
        send_and_track(m.chat.id, f"💰 {sym}: {fmt_price(price)} ({change:+.2f}%)", back_button())

@bot.message_handler(commands=["info"])
def info_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /info <symbol>", back_button())
        return
    sym = args[1].upper()
    info = get_coin_info(sym)
    if not info:
        send_and_track(m.chat.id, f"❌ No info for {sym}", back_button())
        return
    text = (f"🔎 <b>{info['name']} ({info['symbol']})</b>\n"
            f"🏆 Rank: #{info['rank']}\n"
            f"💰 Price: {fmt_price(info['price'])}\n"
            f"📈 ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
            f"📉 ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
            f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
            f"📊 24h Volume: {fmt_price(info['volume'])}\n"
            f"💎 Circulating Supply: {info['supply']:,.0f}")
    if info['max_supply']:
        text += f"\n🔒 Max Supply: {info['max_supply']:,.0f}"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["gainers"])
def gainers_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    g, _ = get_top_movers(10)
    if not g:
        send_and_track(m.chat.id, "❌ No gainers data", back_button())
        return
    text = "📈 <b>Top 10 Gainers (24h)</b>\n\n"
    for d in g:
        coin = d["symbol"].removesuffix("USDT")
        text += f"🟢 {coin} +{float(d['priceChangePercent']):.2f}% – {fmt_price(float(d['lastPrice']))}\n"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["losers"])
def losers_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    _, l = get_top_movers(10)
    if not l:
        send_and_track(m.chat.id, "❌ No losers data", back_button())
        return
    text = "📉 <b>Top 10 Losers (24h)</b>\n\n"
    for d in l:
        coin = d["symbol"].removesuffix("USDT")
        text += f"🔴 {coin} {float(d['priceChangePercent']):.2f}% – {fmt_price(float(d['lastPrice']))}\n"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["multi"])
def multi_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /multi <symbol>", back_button())
        return
    sym = args[1].upper()
    prices = get_multi_prices(sym)
    if not prices:
        send_and_track(m.chat.id, f"❌ No multi data for {sym}", back_button())
        return
    order = [("usd","USD"),("eur","EUR"),("gbp","GBP"),("jpy","JPY"),("cny","CNY"),("aed","AED"),("try","TRY"),("inr","INR"),("krw","KRW"),("cad","CAD"),("aud","AUD")]
    text = f"💱 <b>{sym} – Currencies</b>\n\n"
    for code, name in order:
        val = prices.get(code)
        if val is not None:
            text += f"{name}: {fmt_currency_value(code, val)}\n"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["alert"])
def alert_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    args = m.text.split()
    if len(args) != 4:
        send_and_track(m.chat.id, "Usage: /alert BTC > 50000", back_button())
        return
    coin = args[1].upper()
    direction = args[2]
    try:
        target = float(args[3])
    except:
        send_and_track(m.chat.id, "Invalid target", back_button())
        return
    if direction not in (">", "<"):
        send_and_track(m.chat.id, "Direction must be > or <", back_button())
        return
    aid, err = add_alert(m.from_user.id, m.chat.id, coin, target, direction)
    if err:
        send_and_track(m.chat.id, err, back_button())
    else:
        send_and_track(m.chat.id, f"✅ Alert set: {coin} {direction} {target:,.2f}", back_button())

@bot.message_handler(commands=["myalerts"])
def myalerts_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    rows = db_query("SELECT id, coin, target, direction FROM alerts WHERE user_id=? AND chat_id=? AND active=1", (m.from_user.id, m.chat.id), fetch_all=True)
    if not rows:
        send_and_track(m.chat.id, "🔕 You have no active alerts.", back_button())
        return
    text = "🔔 <b>Your alerts</b>\n\n"
    for aid, coin, target, direction in rows:
        text += f"• {coin} {direction} {target:,.2f}  (ID: {aid})\n"
    text += "\nUse /cancelalert <ID> to remove."
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["cancelalert"])
def cancelalert_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    args = m.text.split()
    if len(args) != 2:
        send_and_track(m.chat.id, "Usage: /cancelalert <id>", back_button())
        return
    try:
        aid = int(args[1])
    except:
        send_and_track(m.chat.id, "Invalid ID", back_button())
        return
    row = db_query("SELECT user_id FROM alerts WHERE id=?", (aid,), fetch_one=True)
    if not row or row[0] != m.from_user.id:
        send_and_track(m.chat.id, "Not your alert", back_button())
        return
    deactivate_alert(aid)
    send_and_track(m.chat.id, "✅ Alert cancelled.", back_button())

@bot.message_handler(commands=["scan"])
def scan_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
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
    risk_flags = []
    risk_map = {"is_honeypot": "🍯 Honeypot", "is_mintable": "🖨 Mintable", "is_proxy": "🔀 Proxy", "is_blacklisted": "⛔ Blacklist"}
    for f, label in risk_map.items():
        if str(result.get(f, "")) == "1":
            risk_flags.append(f"⚠️ {label}: YES")
    buy_tax = result.get("buy_tax")
    if buy_tax:
        try:
            pct = float(buy_tax) * 100
            if pct > 0:
                risk_flags.append(f"⚠️ Buy tax: {pct:.1f}%")
        except: pass
    if risk_flags:
        text += "<b>Risk flags:</b>\n" + "\n".join(risk_flags) + "\n"
    else:
        text += "✅ No high‑risk flags detected.\n"
    text += "\n🔗 <a href='https://gopluslabs.io/'>GoPlusLabs</a>"
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["profile"])
def profile_command(m):
    if maintenance_block(m.from_user.id, m.chat.id): return
    track_message(m.chat.id, m.message_id)
    p = get_profile(m.from_user.id)
    text = (f"👤 <b>Your Profile</b>\n\n"
            f"User ID: {p['user_id']}\n"
            f"Joined: {time.strftime('%Y-%m-%d', time.localtime(p['join_date']))}\n"
            f"Alerts set: {p['alerts_set']}\n"
            f"Alerts triggered: {p['alerts_triggered']}\n"
            f"Active alerts: {get_alert_count(m.from_user.id)}")
    send_and_track(m.chat.id, text, back_button())

# =========================
# ADMIN COMMANDS
# =========================
@bot.message_handler(commands=["users"])
def users_cmd(m):
    if not is_admin(m.from_user.id): return
    track_message(m.chat.id, m.message_id)
    rows = db_query("SELECT username, first_name, join_date FROM profiles ORDER BY join_date DESC", fetch_all=True)
    if not rows:
        send_and_track(m.chat.id, "No users found.", back_button())
        return
    text = "👥 <b>User List</b>\n\n"
    for uname, fname, joined in rows:
        name = f"{fname or '?'}" + (f" (@{uname})" if uname else "")
        text += f"• {name}\n  Joined: {time.strftime('%Y-%m-%d', time.localtime(joined))}\n\n"
        if len(text) > 3900:
            send_and_track(m.chat.id, text)
            text = ""
    if text:
        send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["broadcast"])
def broadcast_cmd(m):
    if not is_admin(m.from_user.id): return
    track_message(m.chat.id, m.message_id)
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /broadcast <message>", back_button())
        return
    msg = args[1]
    rows = db_query("SELECT DISTINCT user_id FROM profiles", fetch_all=True) or []
    sent = 0
    for (uid,) in rows:
        if safe_send(uid, msg):
            sent += 1
        time.sleep(0.05)
    send_and_track(m.chat.id, f"Broadcast sent to {sent} users.", back_button())

@bot.message_handler(commands=["maintenance"])
def maintenance_cmd(m):
    if not is_admin(m.from_user.id): return
    track_message(m.chat.id, m.message_id)
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        active, msg = get_maintenance()
        send_and_track(m.chat.id, f"Maintenance: {'ON' if active else 'OFF'}\nMsg: {msg}", back_button())
        return
    sub = args[1].lower()
    if sub == "on":
        set_maintenance(True, "Bot is under maintenance.")
        send_and_track(m.chat.id, "✅ Maintenance ENABLED", back_button())
    elif sub == "off":
        set_maintenance(False)
        send_and_track(m.chat.id, "✅ Maintenance DISABLED", back_button())
    else:
        send_and_track(m.chat.id, "Usage: /maintenance on|off", back_button())

@bot.message_handler(commands=["stats"])
def stats_cmd(m):
    if not is_admin(m.from_user.id): return
    track_message(m.chat.id, m.message_id)
    total_users = db_query("SELECT COUNT(*) FROM profiles", fetch_one=True)[0]
    total_alerts = db_query("SELECT COUNT(*) FROM alerts WHERE active=1", fetch_one=True)[0]
    total_triggers = db_query("SELECT SUM(alerts_triggered) FROM profiles", fetch_one=True)[0] or 0
    send_and_track(m.chat.id, f"📊 <b>Stats</b>\n\nUsers: {total_users}\nActive alerts: {total_alerts}\nTriggers: {total_triggers}", back_button())

# =========================
# SIMPLE INLINE MENU (optional)
# =========================
waiting = {}
wait_lock = threading.RLock()

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main_cb(call):
    cid = call.message.chat.id
    with wait_lock:
        waiting.pop((cid, call.from_user.id), None)
    send_and_track(cid, "Main menu", back_button())
    bot.answer_callback_query(call.id)

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
log.info("🚀 Persona Bot started – final version, database schema fixed")
bot.delete_webhook()
time.sleep(1)
bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
