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
from datetime import datetime
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import websocket

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN missing")
    sys.exit(1)

ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "7458428092").split(",")]
COOLDOWN_SECONDS = 2
MAX_ALERTS_PER_USER = 20
MAX_CA_LENGTH = 100
MAX_TEXT_LEN = 200
MAX_HISTORY = 3

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)
log = logging.getLogger("PersonaBot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
os.makedirs("data", exist_ok=True)

# ================= DATABASE =================
db_path = "data/persona.db"
db_lock = threading.RLock()
conn = sqlite3.connect(db_path, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=ON")

def db_query(query, params=(), fetch_one=False, fetch_all=False):
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

def init_db():
    db_query("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            streak INTEGER DEFAULT 0,
            last_active INTEGER,
            total_interactions INTEGER DEFAULT 0,
            alerts_set INTEGER DEFAULT 0,
            alerts_triggered INTEGER DEFAULT 0
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
init_db()

# ================= CACHES =================
class TTLCache:
    def __init__(self, ttl=60):
        self.ttl = ttl
        self.data = {}
        self.lock = threading.RLock()
    def set(self, k, v):
        with self.lock:
            self.data[k] = (v, time.time())
    def get(self, k):
        with self.lock:
            if k not in self.data:
                return None
            v, t = self.data[k]
            if time.time() - t > self.ttl:
                return None
            return v
price_cache = TTLCache(10)
coin_cache = TTLCache(3600)

# ================= REQUESTS SESSION =================
session = requests.Session()
retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429,500,502,503,504])
adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({"User-Agent": "PersonaBot/7.0"})

# ================= SERVICES =================
class Binance:
    @staticmethod
    def price(symbol):
        try:
            r = session.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}USDT", timeout=10)
            if r.status_code != 200:
                return None, None
            data = r.json()
            return float(data["lastPrice"]), float(data["priceChangePercent"])
        except:
            return None, None

    @staticmethod
    def top_movers():
        try:
            r = session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
            if r.status_code != 200:
                return [], []
            data = r.json()
            stable = {"USDT","BUSD","USDC","DAI","FDUSD"}
            filtered = [d for d in data if d["symbol"].endswith("USDT") and d["symbol"].replace("USDT","") not in stable and float(d.get("quoteVolume",0))>1_000_000]
            sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)
            return sorted_data[:5], sorted_data[-5:][::-1]
        except:
            return [], []

class CoinGecko:
    COIN_MAP = {
        "BTC":"bitcoin","ETH":"ethereum","BNB":"binancecoin","SOL":"solana","XRP":"ripple",
        "DOGE":"dogecoin","ADA":"cardano","AVAX":"avalanche-2","LINK":"chainlink","MATIC":"matic-network",
        "UNI":"uniswap","ATOM":"cosmos","NEAR":"near","APT":"aptos","SUI":"sui","LTC":"litecoin",
        "SHIB":"shiba-inu","PEPE":"pepe","WIF":"dogwifcoin","SEI":"sei-network","TRX":"tron","DOT":"polkadot"
    }
    @staticmethod
    def info(symbol):
        sym_up = symbol.upper()
        cached = coin_cache.get(sym_up)
        if cached:
            return cached
        coin_id = CoinGecko.COIN_MAP.get(sym_up)
        if not coin_id:
            try:
                r = session.get(f"https://api.coingecko.com/api/v3/search?query={symbol.lower()}", timeout=10)
                if r.status_code != 200:
                    return None
                coins = r.json().get("coins", [])
                if not coins:
                    return None
                coin_id = coins[0]["id"]
            except:
                return None
        try:
            r = session.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}", params={"localization":"false","tickers":"false","community_data":"false","developer_data":"false","sparkline":"false"}, timeout=15)
            if r.status_code != 200:
                return None
            data = r.json()
            md = data.get("market_data", {})
            result = {
                "name": data.get("name","?"),
                "symbol": data.get("symbol","?").upper(),
                "rank": data.get("market_cap_rank","N/A"),
                "price": md.get("current_price",{}).get("usd",0),
                "ath": md.get("ath",{}).get("usd",0),
                "ath_date": md.get("ath_date",{}).get("usd","")[:10],
                "atl": md.get("atl",{}).get("usd",0),
                "atl_date": md.get("atl_date",{}).get("usd","")[:10],
                "market_cap": md.get("market_cap",{}).get("usd",0),
                "volume": md.get("total_volume",{}).get("usd",0),
                "supply": md.get("circulating_supply",0),
                "max_supply": md.get("max_supply")
            }
            coin_cache.set(sym_up, result)
            return result
        except:
            return None

    @staticmethod
    def multi_price(symbol):
        sym_up = symbol.upper()
        coin_id = CoinGecko.COIN_MAP.get(sym_up)
        if not coin_id:
            try:
                r = session.get(f"https://api.coingecko.com/api/v3/search?query={symbol.lower()}", timeout=10)
                if r.status_code != 200:
                    return None
                coins = r.json().get("coins", [])
                if not coins:
                    return None
                coin_id = coins[0]["id"]
            except:
                return None
        try:
            r = session.get("https://api.coingecko.com/api/v3/simple/price", params={"ids":coin_id, "vs_currencies":"usd,eur,gbp,jpy,cny,aed,try"}, timeout=10)
            if r.status_code == 200:
                return r.json().get(coin_id)
        except:
            return None

class ContractScanner:
    @staticmethod
    def scan(address):
        if not address:
            return None, "Empty address"
        addr = re.sub(r'\s+', '', address)
        if len(addr) > MAX_CA_LENGTH:
            return None, "Address too long"
        addr_lower = addr.lower()
        if re.match(r'^0x[a-f0-9]{40}$', addr_lower):
            for chain in [1, 56]:
                for attempt in range(2):
                    try:
                        url = f"https://api.gopluslabs.io/api/v1/token_security/{chain}?contract_addresses={addr_lower}"
                        r = session.get(url, timeout=15)
                        if r.status_code == 200:
                            data = r.json()
                            result = data.get("result", {}).get(addr_lower, {})
                            if result:
                                return result, None
                        time.sleep(0.5)
                    except Exception as e:
                        log.error(f"GoPlus chain {chain}: {e}")
            return None, "Contract not found on Ethereum/BSC"
        else:
            sol_addr = re.sub(r'[^a-zA-Z0-9]', '', addr)
            if len(sol_addr) < 32 or len(sol_addr) > 44:
                return None, "Invalid Solana address length"
            for attempt in range(2):
                try:
                    url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={sol_addr}"
                    r = session.get(url, timeout=15)
                    if r.status_code == 200:
                        data = r.json()
                        result = data.get("result", {}).get(sol_addr, {})
                        if result:
                            return result, None
                    time.sleep(0.5)
                except Exception as e:
                    log.error(f"GoPlus Solana: {e}")
            return None, "Solana token not found"

# ================= USER PROFILES =================
def get_profile(user_id):
    row = db_query("SELECT * FROM profiles WHERE user_id=?", (user_id,), fetch_one=True)
    if not row:
        now = int(time.time())
        db_query("INSERT INTO profiles(user_id,join_date,last_active) VALUES(?,?,?)", (user_id, now, now))
        return {"user_id":user_id, "join_date":now, "streak":0, "last_active":now, "total_interactions":0, "alerts_set":0, "alerts_triggered":0}
    return {"user_id":row[0], "join_date":row[1], "streak":row[2], "last_active":row[3], "total_interactions":row[4], "alerts_set":row[5], "alerts_triggered":row[6]}

def update_profile(user_id, **kw):
    for k, v in kw.items():
        db_query(f"UPDATE profiles SET {k}=? WHERE user_id=?", (v, user_id))

def update_streak(user_id):
    p = get_profile(user_id)
    today = datetime.now().date()
    last = datetime.fromtimestamp(p["last_active"]).date()
    if last == today:
        return
    new_streak = p["streak"]+1 if (today - last).days == 1 else 0
    update_profile(user_id, streak=new_streak, last_active=int(time.time()), total_interactions=p["total_interactions"]+1)

def log_interaction(uid, uname, fname, cmd, det=""):
    db_query("INSERT INTO analytics(timestamp,user_id,username,first_name,command,details) VALUES(?,?,?,?,?,?)",
             (int(time.time()), uid, uname or "?", fname or "?", cmd, det[:200]))
    update_streak(uid)

def is_admin(uid):
    return uid in ADMIN_IDS

# ================= ALERTS =================
def add_alert(chat_id, coin, target, direction):
    aid = db_query("INSERT INTO alerts(chat_id,coin,target,direction,created_at) VALUES(?,?,?,?,?)",
                   (chat_id, coin.upper(), target, direction, int(time.time())))
    p = get_profile(chat_id)
    update_profile(chat_id, alerts_set=p["alerts_set"]+1)
    return aid

def get_active_alerts(chat_id=None):
    if chat_id:
        rows = db_query("SELECT id,chat_id,coin,target,direction FROM alerts WHERE active=1 AND chat_id=?", (chat_id,), fetch_all=True)
    else:
        rows = db_query("SELECT id,chat_id,coin,target,direction FROM alerts WHERE active=1", fetch_all=True)
    return [{"id":r[0], "chat_id":r[1], "coin":r[2], "target":r[3], "direction":r[4]} for r in rows]

def deactivate_alert(aid):
    db_query("UPDATE alerts SET active=0 WHERE id=?", (aid,))

def get_alert_count(chat_id):
    row = db_query("SELECT COUNT(*) FROM alerts WHERE active=1 AND chat_id=?", (chat_id,), fetch_one=True)
    return row[0] if row else 0

# ================= WEBSOCKET =================
ws_stop = False
ws_lock = threading.RLock()
last_symbols = set()

def rebuild_ws():
    global ws_stop
    with ws_lock:
        ws_stop = True

def ws_loop():
    global last_symbols, ws_stop
    while True:
        alerts = get_active_alerts()
        symbols = {a["coin"] for a in alerts}
        if not symbols:
            time.sleep(5)
            continue
        with ws_lock:
            if symbols == last_symbols and not ws_stop:
                time.sleep(5)
                continue
            last_symbols = symbols
            ws_stop = False
        streams = "/".join([f"{s.lower()}usdt@ticker" for s in list(symbols)[:50]])
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                if "data" in d:
                    t = d["data"]
                    price_cache.set(t["s"].replace("USDT",""), float(t["c"]))
            except:
                pass
        ws = websocket.WebSocketApp(url, on_message=on_msg)
        wst = threading.Thread(target=ws.run_forever, kwargs={"ping_interval":30}, daemon=True)
        wst.start()
        while wst.is_alive():
            with ws_lock:
                if ws_stop:
                    ws.close()
                    break
            time.sleep(1)
        time.sleep(1)

threading.Thread(target=ws_loop, daemon=True).start()

def alert_loop():
    last_trigger = {}
    while True:
        now = time.time()
        alerts = get_active_alerts()
        for a in alerts:
            key = (a["chat_id"], a["id"])
            if key in last_trigger and now - last_trigger[key] < 300:
                continue
            price = price_cache.get(a["coin"])
            if price is None:
                price, _ = Binance.price(a["coin"])
                if price is None:
                    continue
                price_cache.set(a["coin"], price)
            if (a["direction"]==">" and price >= a["target"]) or (a["direction"]=="<" and price <= a["target"]):
                deactivate_alert(a["id"])
                last_trigger[key] = now
                p = get_profile(a["chat_id"])
                update_profile(a["chat_id"], alerts_triggered=p["alerts_triggered"]+1)
                bot.send_message(a["chat_id"], f"🔔 *Alert* {a['coin']} {a['direction']} {a['target']} hit!\nCurrent: {price}")
        time.sleep(5)

threading.Thread(target=alert_loop, daemon=True).start()

# ================= UI HELPERS =================
def sep():
    return "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

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

def escape_md(t):
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', t)

# ================= MESSAGE EDITING (clean UI) =================
def edit_or_send(chat_id, text, markup=None, message_id=None):
    """Edit existing message if message_id given, else send new."""
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
            return message_id
        except Exception as e:
            log.error(f"Edit failed: {e}")
    sent = bot.send_message(chat_id, text, reply_markup=markup)
    return sent.message_id

# ================= KEYBOARDS =================
def main_menu():
    text = f"`{sep()}`\n🤖 **PERSONA**\n`{sep()}`"
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("💰 Price", callback_data="menu_price"),
        InlineKeyboardButton("🔔 Alerts", callback_data="menu_alerts")
    )
    kb.row(
        InlineKeyboardButton("🚀 Gainers", callback_data="gainers"),
        InlineKeyboardButton("📉 Losers", callback_data="losers")
    )
    kb.row(
        InlineKeyboardButton("🔎 Coin Info", callback_data="menu_info"),
        InlineKeyboardButton("💱 Multi", callback_data="menu_multi")
    )
    kb.row(
        InlineKeyboardButton("🛡 Scan", callback_data="menu_scan"),
        InlineKeyboardButton("📋 My Alerts", callback_data="list_alerts")
    )
    kb.row(
        InlineKeyboardButton("📈 Profile", callback_data="profile")
    )
    return text, kb

def price_menu():
    text = f"`{sep()}`\n💰 **Price**\n`{sep()}`"
    kb = InlineKeyboardMarkup()
    coins = ["BTC","ETH","BNB","SOL","XRP","DOGE","ADA","AVAX","LINK","MATIC","UNI","ATOM","NEAR","APT","SUI","LTC","SHIB"]
    row = []
    for i, c in enumerate(coins, 1):
        row.append(InlineKeyboardButton(c, callback_data=f"price_{c}"))
        if i % 3 == 0:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(InlineKeyboardButton("🔍 Search", callback_data="search_coin"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def alerts_menu():
    text = f"`{sep()}`\n🔔 **Alerts**\n`{sep()}`"
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("BTC >100k", callback_data="setalert_BTC_>_100000"),
        InlineKeyboardButton("BTC <80k", callback_data="setalert_BTC_<_80000")
    )
    kb.row(
        InlineKeyboardButton("ETH >4k", callback_data="setalert_ETH_>_4000"),
        InlineKeyboardButton("ETH <2k", callback_data="setalert_ETH_<_2000")
    )
    kb.row(
        InlineKeyboardButton("SOL >200", callback_data="setalert_SOL_>_200"),
        InlineKeyboardButton("SOL <100", callback_data="setalert_SOL_<_100")
    )
    kb.row(InlineKeyboardButton("✏️ Custom", callback_data="custom_alert"))
    kb.row(InlineKeyboardButton("📋 My Alerts", callback_data="list_alerts"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def info_menu():
    text = f"`{sep()}`\n🔎 **Coin Info**\n`{sep()}`"
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("BTC", callback_data="info_BTC"),
        InlineKeyboardButton("ETH", callback_data="info_ETH"),
        InlineKeyboardButton("BNB", callback_data="info_BNB")
    )
    kb.row(
        InlineKeyboardButton("SOL", callback_data="info_SOL"),
        InlineKeyboardButton("XRP", callback_data="info_XRP"),
        InlineKeyboardButton("ADA", callback_data="info_ADA")
    )
    kb.row(
        InlineKeyboardButton("DOGE", callback_data="info_DOGE"),
        InlineKeyboardButton("AVAX", callback_data="info_AVAX"),
        InlineKeyboardButton("LINK", callback_data="info_LINK")
    )
    kb.row(InlineKeyboardButton("🔍 Search", callback_data="search_info"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def multi_menu():
    text = f"`{sep()}`\n💱 **Multi‑Currency**\n`{sep()}`"
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("BTC", callback_data="multi_BTC"),
        InlineKeyboardButton("ETH", callback_data="multi_ETH"),
        InlineKeyboardButton("BNB", callback_data="multi_BNB")
    )
    kb.row(
        InlineKeyboardButton("SOL", callback_data="multi_SOL"),
        InlineKeyboardButton("XRP", callback_data="multi_XRP"),
        InlineKeyboardButton("ADA", callback_data="multi_ADA")
    )
    kb.row(InlineKeyboardButton("🔍 Search", callback_data="search_multi"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def back_button():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# ================= COOLDOWN =================
cooldown = {}
c_lock = threading.RLock()
def cooldown_ok(uid):
    with c_lock:
        now = time.time()
        if uid in cooldown and now - cooldown[uid] < COOLDOWN_SECONDS:
            return False
        cooldown[uid] = now
    return True

# ================= COMMAND HANDLERS =================
@bot.message_handler(commands=["start", "help"])
def start(m):
    log_interaction(m.from_user.id, m.from_user.username, m.from_user.first_name, "/start")
    text, kb = main_menu()
    bot.send_message(m.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["stats"])
def stats(m):
    if not is_admin(m.from_user.id):
        return
    users = db_query("SELECT COUNT(DISTINCT user_id) FROM analytics", fetch_one=True)[0]
    interactions = db_query("SELECT COUNT(*) FROM analytics", fetch_one=True)[0]
    active = db_query("SELECT COUNT(*) FROM alerts WHERE active=1", fetch_one=True)[0]
    bot.send_message(m.chat.id, f"📊 Stats\nUsers: {users}\nInteractions: {interactions}\nActive alerts: {active}", reply_markup=back_button())

# ================= CALLBACKS (with message editing) =================
waiting = {}
wait_lock = threading.RLock()
current_msg = {}  # chat_id -> last message id

@bot.callback_query_handler(func=lambda call: True)
def cb(call):
    uid = call.from_user.id
    cid = call.message.chat.id
    data = call.data
    msg_id = call.message.message_id

    if not cooldown_ok(uid):
        bot.answer_callback_query(call.id, "Slow down")
        return
    bot.answer_callback_query(call.id)
    log_interaction(uid, call.from_user.username, call.from_user.first_name, f"cb:{data[:30]}")

    try:
        if data == "back_main":
            with wait_lock:
                waiting.pop(cid, None)
            text, kb = main_menu()
            new_id = edit_or_send(cid, text, kb, msg_id)
            current_msg[cid] = new_id
        elif data == "menu_price":
            text, kb = price_menu()
            new_id = edit_or_send(cid, text, kb, msg_id)
            current_msg[cid] = new_id
        elif data == "search_coin":
            with wait_lock:
                waiting[cid] = "price"
            text = "🔍 Type coin symbol (e.g., PEPE)"
            new_id = edit_or_send(cid, text, back_button(), msg_id)
            current_msg[cid] = new_id
        elif data == "menu_alerts":
            text, kb = alerts_menu()
            new_id = edit_or_send(cid, text, kb, msg_id)
            current_msg[cid] = new_id
        elif data == "custom_alert":
            with wait_lock:
                waiting[cid] = "alert"
            text = "✏️ Format: COIN > price\nExample: BTC > 95000"
            new_id = edit_or_send(cid, text, back_button(), msg_id)
            current_msg[cid] = new_id
        elif data.startswith("setalert_"):
            parts = data.split("_")
            if len(parts) != 4:
                return
            _, sym, direction, t = parts
            try:
                target = float(t)
            except:
                edit_or_send(cid, "❌ Invalid price", alerts_menu()[1], msg_id)
                return
            p, _ = Binance.price(sym)
            if p is None:
                edit_or_send(cid, f"❌ {sym} not found", alerts_menu()[1], msg_id)
                return
            if get_alert_count(cid) >= MAX_ALERTS_PER_USER:
                edit_or_send(cid, f"❌ Max {MAX_ALERTS_PER_USER} alerts", alerts_menu()[1], msg_id)
                return
            aid = add_alert(cid, sym, target, direction)
            rebuild_ws()
            new_id = edit_or_send(cid, f"✅ Alert #{aid} set!\n{sym} {direction} ${target:,.2f}", alerts_menu()[1], msg_id)
            current_msg[cid] = new_id
        elif data == "list_alerts":
            active = get_active_alerts(cid)
            if not active:
                edit_or_send(cid, "📋 No active alerts", alerts_menu()[1], msg_id)
                return
            text = "📋 *Active Alerts*\n" + sep() + "\n"
            kb = InlineKeyboardMarkup()
            for a in active:
                label = "▲" if a["direction"] == ">" else "▼"
                text += f"#{a['id']} {a['coin']} {label} ${a['target']:,.2f}\n"
                kb.row(InlineKeyboardButton(f"❌ Cancel #{a['id']}", callback_data=f"cancel_{a['id']}"))
            kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            new_id = edit_or_send(cid, text, kb, msg_id)
            current_msg[cid] = new_id
        elif data.startswith("cancel_"):
            aid = int(data.split("_")[1])
            deactivate_alert(aid)
            rebuild_ws()
            active = get_active_alerts(cid)
            if not active:
                new_id = edit_or_send(cid, "📋 No active alerts", back_button(), msg_id)
            else:
                text = "📋 *Active Alerts*\n" + sep() + "\n"
                kb = InlineKeyboardMarkup()
                for a in active:
                    label = "▲" if a["direction"] == ">" else "▼"
                    text += f"#{a['id']} {a['coin']} {label} ${a['target']:,.2f}\n"
                    kb.row(InlineKeyboardButton(f"❌ Cancel #{a['id']}", callback_data=f"cancel_{a['id']}"))
                kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
                new_id = edit_or_send(cid, text, kb, msg_id)
            current_msg[cid] = new_id
        elif data.startswith("price_"):
            sym = data.split("_")[1]
            p, ch = Binance.price(sym)
            if p is None:
                edit_or_send(cid, f"❌ {sym} not found", price_menu()[1], msg_id)
            else:
                arrow = "🟢▲" if ch >= 0 else "🔴▼"
                text = f"*{sym}*\n💵 {fmt_price(p)}\n{arrow} {abs(ch):.2f}%"
                new_id = edit_or_send(cid, text, price_menu()[1], msg_id)
                current_msg[cid] = new_id
        elif data == "gainers":
            g, _ = Binance.top_movers()
            if not g:
                edit_or_send(cid, "❌ No data", back_button(), msg_id)
            else:
                text = "🚀 *Gainers*\n" + sep() + "\n"
                for d in g:
                    coin = d["symbol"].replace("USDT", "")
                    text += f"🟢 {coin} {fmt_price(float(d['lastPrice']))} ▲ {float(d['priceChangePercent']):.2f}%\n"
                new_id = edit_or_send(cid, text, back_button(), msg_id)
                current_msg[cid] = new_id
        elif data == "losers":
            _, l = Binance.top_movers()
            if not l:
                edit_or_send(cid, "❌ No data", back_button(), msg_id)
            else:
                text = "📉 *Losers*\n" + sep() + "\n"
                for d in l:
                    coin = d["symbol"].replace("USDT", "")
                    text += f"🔴 {coin} {fmt_price(float(d['lastPrice']))} ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
                new_id = edit_or_send(cid, text, back_button(), msg_id)
                current_msg[cid] = new_id
        elif data == "menu_info":
            text, kb = info_menu()
            new_id = edit_or_send(cid, text, kb, msg_id)
            current_msg[cid] = new_id
        elif data == "search_info":
            with wait_lock:
                waiting[cid] = "info"
            new_id = edit_or_send(cid, "🔍 Enter coin symbol", back_button(), msg_id)
            current_msg[cid] = new_id
        elif data.startswith("info_"):
            sym = data.split("_")[1]
            info = CoinGecko.info(sym)
            if not info:
                edit_or_send(cid, f"❌ No info for {sym}", info_menu()[1], msg_id)
            else:
                text = (f"🔎 *{info['name']} ({info['symbol']})*\n{sep()}\n"
                        f"Rank: #{info['rank']}\n"
                        f"Price: {fmt_price(info['price'])}\n"
                        f"ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
                        f"ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
                        f"Market Cap: ${info['market_cap']:,.0f}\n"
                        f"Volume: ${info['volume']:,.0f}")
                new_id = edit_or_send(cid, text, info_menu()[1], msg_id)
                current_msg[cid] = new_id
        elif data == "menu_multi":
            text, kb = multi_menu()
            new_id = edit_or_send(cid, text, kb, msg_id)
            current_msg[cid] = new_id
        elif data == "search_multi":
            with wait_lock:
                waiting[cid] = "multi"
            new_id = edit_or_send(cid, "🔍 Enter coin symbol", back_button(), msg_id)
            current_msg[cid] = new_id
        elif data.startswith("multi_"):
            sym = data.split("_")[1]
            prices = CoinGecko.multi_price(sym)
            if not prices:
                edit_or_send(cid, f"❌ No data for {sym}", multi_menu()[1], msg_id)
            else:
                flags = {"usd":"🇺🇸","eur":"🇪🇺","gbp":"🇬🇧","jpy":"🇯🇵","cny":"🇨🇳","aed":"🇦🇪","try":"🇹🇷"}
                text = f"💱 {sym}\n{sep()}\n"
                for cur, flag in flags.items():
                    p = prices.get(cur)
                    if p:
                        text += f"{flag} {fmt_price(p)}\n"
                new_id = edit_or_send(cid, text, multi_menu()[1], msg_id)
                current_msg[cid] = new_id
        elif data == "menu_scan":
            with wait_lock:
                waiting[cid] = "scan"
            new_id = edit_or_send(cid, "🛡 Paste contract address (ETH/BSC/Solana)", back_button(), msg_id)
            current_msg[cid] = new_id
        elif data == "profile":
            p = get_profile(uid)
            text = (f"👤 Profile\n{sep()}\n"
                    f"Streak: {p['streak']} days\n"
                    f"Interactions: {p['total_interactions']}\n"
                    f"Alerts set: {p['alerts_set']}\n"
                    f"Alerts triggered: {p['alerts_triggered']}")
            new_id = edit_or_send(cid, text, back_button(), msg_id)
            current_msg[cid] = new_id
        else:
            pass
    except Exception as e:
        log.error(f"Callback error: {e}", exc_info=True)
        edit_or_send(cid, "⚠️ Error", back_button(), msg_id)

# ================= TEXT HANDLER =================
@bot.message_handler(func=lambda m: True)
def text_input(m):
    cid = m.chat.id
    uid = m.from_user.id
    if not cooldown_ok(uid):
        return
    with wait_lock:
        if cid not in waiting:
            return
        mode = waiting.pop(cid)

    t = m.text.strip()[:MAX_TEXT_LEN]
    if not t:
        return

    # [NEW] Try to delete the user's message (works only in groups where bot is admin)
    try:
        bot.delete_message(cid, m.message_id)
    except Exception:
        pass  # Fails silently in private chats

    log_interaction(uid, m.from_user.username, m.from_user.first_name, f"text:{mode}", t)

    # Get current message ID to edit (the menu message)
    msg_id = current_msg.get(cid)

    try:
        if mode == "price":
            p, ch = Binance.price(t.upper())
            if p is None:
                edit_or_send(cid, f"❌ {escape_md(t)} not found", price_menu()[1], msg_id)
            else:
                arrow = "🟢▲" if ch >= 0 else "🔴▼"
                text = f"*{escape_md(t)}*\n💵 {fmt_price(p)}\n{arrow} {abs(ch):.2f}%"
                new_id = edit_or_send(cid, text, price_menu()[1], msg_id)
                current_msg[cid] = new_id
        elif mode == "info":
            info = CoinGecko.info(t.upper())
            if not info:
                edit_or_send(cid, f"❌ {escape_md(t)} not found", info_menu()[1], msg_id)
            else:
                text = (f"🔎 *{info['name']} ({info['symbol']})*\n{sep()}\n"
                        f"Rank: #{info['rank']}\n"
                        f"Price: {fmt_price(info['price'])}\n"
                        f"ATH: {fmt_price(info['ath'])} ({info['ath_date']})\n"
                        f"ATL: {fmt_price(info['atl'])} ({info['atl_date']})\n"
                        f"Market Cap: ${info['market_cap']:,.0f}\n"
                        f"Volume: ${info['volume']:,.0f}")
                new_id = edit_or_send(cid, text, info_menu()[1], msg_id)
                current_msg[cid] = new_id
        elif mode == "multi":
            prices = CoinGecko.multi_price(t.upper())
            if not prices:
                edit_or_send(cid, f"❌ {escape_md(t)} not found", multi_menu()[1], msg_id)
            else:
                flags = {"usd":"🇺🇸","eur":"🇪🇺","gbp":"🇬🇧","jpy":"🇯🇵","cny":"🇨🇳","aed":"🇦🇪","try":"🇹🇷"}
                text = f"💱 {escape_md(t)}\n{sep()}\n"
                for cur, flag in flags.items():
                    p = prices.get(cur)
                    if p:
                        text += f"{flag} {fmt_price(p)}\n"
                new_id = edit_or_send(cid, text, multi_menu()[1], msg_id)
                current_msg[cid] = new_id
        elif mode == "scan":
            result, err = ContractScanner.scan(t)
            if err:
                edit_or_send(cid, f"❌ {err}", back_button(), msg_id)
            else:
                def flag(v):
                    if v == "1": return "⚠️ Yes"
                    if v == "0": return "✅ No"
                    return "❓ Unknown"
                text = (f"🛡 *CA Scan*\n{sep()}\n"
                        f"🍯 Honeypot: {flag(result.get('is_honeypot','?'))}\n"
                        f"🖨 Mintable: {flag(result.get('is_mintable','?'))}\n"
                        f"🔁 Proxy: {flag(result.get('is_proxy','?'))}\n"
                        f"📂 Open Source: {flag(result.get('is_open_source','?'))}\n"
                        f"💸 Buy Tax: {result.get('buy_tax','?')}%\n"
                        f"💸 Sell Tax: {result.get('sell_tax','?')}%\n"
                        f"👥 Holders: {result.get('holder_count','?')}")
                new_id = edit_or_send(cid, text, back_button(), msg_id)
                current_msg[cid] = new_id
        elif mode == "alert":
            parts = t.split()
            if len(parts) != 3 or parts[1] not in ('>', '<'):
                edit_or_send(cid, "❌ Format: BTC > 70000", alerts_menu()[1], msg_id)
                return
            sym, direction, target_str = parts[0].upper(), parts[1], parts[2]
            try:
                target = float(target_str)
            except:
                edit_or_send(cid, "❌ Invalid price", alerts_menu()[1], msg_id)
                return
            p, _ = Binance.price(sym)
            if p is None:
                edit_or_send(cid, f"❌ {escape_md(sym)} not found", alerts_menu()[1], msg_id)
                return
            if get_alert_count(cid) >= MAX_ALERTS_PER_USER:
                edit_or_send(cid, f"❌ Max {MAX_ALERTS_PER_USER} alerts", alerts_menu()[1], msg_id)
                return
            aid = add_alert(cid, sym, target, direction)
            rebuild_ws()
            new_id = edit_or_send(cid, f"✅ Alert #{aid} set!\n{sym} {direction} ${target:,.2f}", alerts_menu()[1], msg_id)
            current_msg[cid] = new_id
    except Exception as e:
        log.error(f"Text error: {e}", exc_info=True)
        edit_or_send(cid, "⚠️ Error", back_button(), msg_id)

# ================= START =================
def stop(sig, frame):
    global ws_stop
    ws_stop = True
    bot.stop_polling()
    sys.exit(0)

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

log.info("🚀 Persona Bot – Clean UI, Fast, Reliable (with user message deletion attempt)")
bot.delete_webhook()
bot.infinity_polling(timeout=60, long_polling_timeout=60)
