import telebot
import requests
import threading
import time
import json
import os
import websocket
import random
import signal
import sys
import re
import csv
from datetime import datetime
from functools import wraps
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN environment variable not set. Exiting.")
    sys.exit(1)

ADMIN_IDS = [7458428092]

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
ALERTS_FILE = "alerts.json"
CACHE_TTL = 10
MAX_HISTORY = 3
COOLDOWN_SECONDS = 2
MAX_ALERTS_PER_USER = 20
MAX_CA_LENGTH = 100
ANALYTICS_FILE = "analytics.csv"
COIN_INFO_CACHE_TTL = 3600
MULTI_PRICE_CACHE_TTL = 60

PRICE_CACHE = {}
MULTI_PRICE_CACHE = {}
waiting_for = {}
user_msg_queue = {}
cooldowns = {}
ws_restart_required = False
lock = threading.Lock()
alert_counter_lock = threading.Lock()
active_ws = None
shutdown_flag = False

# ================= STARTUP =================
os.makedirs("data", exist_ok=True)

# ================= USER TRACKING =================
def init_analytics():
    if not os.path.exists(ANALYTICS_FILE):
        with open(ANALYTICS_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Timestamp', 'User ID', 'Username', 'First Name', 'Command', 'Details'])

def log_interaction(user_id, username, first_name, command, details=""):
    try:
        with open(ANALYTICS_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([datetime.now().isoformat(), user_id, username or "?", first_name or "?", command, details])
    except Exception as e:
        print(f"Log error: {e}")

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ================= LOGGING =================
def log(msg, level="INFO"):
    print(f"[{level}] {time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}")

def atomic_save(data, filename):
    tmp = filename + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, filename)
        return True
    except Exception as e:
        log(f"Atomic save failed: {e}", "ERROR")
        return False

# ================= ALERT STORAGE =================
def load_alerts():
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        log(f"Load alerts error: {e}", "ERROR")
    return []

alerts = load_alerts()
alert_id_counter = max((a["id"] for a in alerts), default=0) + 1 if alerts else 1

def save_alerts():
    with lock:
        atomic_save(alerts, ALERTS_FILE)

def cleanup_inactive_alerts():
    now = time.time()
    cutoff = now - 30 * 86400
    global alerts
    with lock:
        original_count = len(alerts)
        alerts = [a for a in alerts if a.get("active", True) or a.get("timestamp", now) > cutoff]
        if len(alerts) != original_count:
            save_alerts()
            log(f"Cleaned up {original_count - len(alerts)} old inactive alerts")

threading.Thread(target=cleanup_inactive_alerts, daemon=True).start()

# ================= MESSAGE QUEUE =================
def cleanup_old_messages(chat_id):
    if chat_id not in user_msg_queue:
        return
    q = user_msg_queue[chat_id]
    while len(q) > MAX_HISTORY:
        old_id = q.pop(0)
        try:
            bot.delete_message(chat_id, old_id)
        except Exception as e:
            log(f"Failed to delete message {old_id}: {e}", "WARNING")

def send_and_track(chat_id, text, reply_markup=None):
    sent = bot.send_message(chat_id, text, reply_markup=reply_markup)
    if chat_id not in user_msg_queue:
        user_msg_queue[chat_id] = []
    user_msg_queue[chat_id].append(sent.message_id)
    cleanup_old_messages(chat_id)
    return sent

# ================= COOLDOWN & RETRY =================
def cooldown_ok(user_id):
    now = time.time()
    if user_id in cooldowns and now - cooldowns[user_id] < COOLDOWN_SECONDS:
        return False
    cooldowns[user_id] = now
    return True

def retry_with_backoff(max_retries=3, base_delay=1):
    def decorator(f):
        @wraps(f)
        def wrapper(*a, **k):
            for attempt in range(max_retries):
                try:
                    return f(*a, **k)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    log(f"Retry {f.__name__} attempt {attempt+1} in {delay:.2f}s: {e}", "WARNING")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

# ================= SESSION =================
session = requests.Session()
session.headers.update({"User-Agent": "PersonaBot/2.0 (crypto assistant)"})

# ================= API =================
@retry_with_backoff(max_retries=2)
def get_price(symbol):
    pair = symbol.upper() + "USDT"
    try:
        r = session.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={pair}", timeout=10)
        data = r.json()
        if "lastPrice" not in data:
            return None, None
        return float(data["lastPrice"]), float(data.get("priceChangePercent", 0))
    except Exception as e:
        log(f"get_price error for {symbol}: {e}", "ERROR")
        return None, None

@retry_with_backoff(max_retries=2)
def get_top_movers():
    try:
        r = session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
        data = r.json()
        stable = {"USDT", "BUSD", "USDC", "DAI", "FDUSD"}
        filtered = [
            d for d in data
            if d["symbol"].endswith("USDT")
            and d["symbol"].replace("USDT", "") not in stable
            and float(d.get("quoteVolume", 0)) > 1_000_000
            and "priceChangePercent" in d
        ]
        sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)
        return sorted_data[:5], sorted_data[-5:][::-1]
    except Exception as e:
        log(f"get_top_movers error: {e}", "ERROR")
        return None, None

coin_info_cache = {}

@retry_with_backoff(max_retries=3, base_delay=2)
def get_coin_info(symbol):
    symbol_up = symbol.upper()
    now = time.time()
    if symbol_up in coin_info_cache:
        cached = coin_info_cache[symbol_up]
        if now - cached["timestamp"] < COIN_INFO_CACHE_TTL:
            return cached["data"]

    cg_id_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
        "SOL": "solana", "XRP": "ripple", "DOGE": "dogecoin",
        "ADA": "cardano", "AVAX": "avalanche-2", "LINK": "chainlink",
        "MATIC": "matic-network", "UNI": "uniswap", "ATOM": "cosmos",
        "NEAR": "near", "APT": "aptos", "SUI": "sui", "LTC": "litecoin",
        "SHIB": "shiba-inu", "TON": "the-open-network", "ARB": "arbitrum",
        "OP": "optimism", "INJ": "injective-protocol", "TIA": "celestia",
        "PEPE": "pepe", "WIF": "dogwifcoin", "SEI": "sei-network",
        "TRX": "tron", "DOT": "polkadot",
    }
    coin_id = cg_id_map.get(symbol_up)
    if not coin_id:
        try:
            r = session.get(f"https://api.coingecko.com/api/v3/search?query={symbol.lower()}", timeout=10)
            if r.status_code == 200:
                coins = r.json().get("coins", [])
                coin_id = coins[0]["id"] if coins else None
            if not coin_id:
                return None
        except Exception as e:
            log(f"Coin search error: {e}", "ERROR")
            return None

    try:
        r = session.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            params={"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false", "sparkline": "false"},
            timeout=15
        )
        if r.status_code == 429:
            log("CoinGecko rate limit", "WARNING")
            time.sleep(30)
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        md = data["market_data"]
        result = {
            "name": data["name"],
            "symbol": data["symbol"].upper(),
            "rank": data.get("market_cap_rank", "N/A"),
            "price": md.get("current_price", {}).get("usd", 0),
            "ath": md.get("ath", {}).get("usd", 0),
            "ath_date": md.get("ath_date", {}).get("usd", "")[:10],
            "ath_change": md.get("ath_change_percentage", {}).get("usd", 0),
            "atl": md.get("atl", {}).get("usd", 0),
            "atl_date": md.get("atl_date", {}).get("usd", "")[:10],
            "atl_change": md.get("atl_change_percentage", {}).get("usd", 0),
            "supply": md.get("circulating_supply", 0),
            "max_supply": md.get("max_supply"),
            "market_cap": md.get("market_cap", {}).get("usd", 0),
            "volume": md.get("total_volume", {}).get("usd", 0),
        }
        coin_info_cache[symbol_up] = {"data": result, "timestamp": now}
        return result
    except Exception as e:
        log(f"Coin info error for {symbol_up}: {e}", "ERROR")
        return None

@retry_with_backoff(max_retries=2)
def get_multi_price(symbol):
    symbol_up = symbol.upper()
    now = time.time()
    if symbol_up in MULTI_PRICE_CACHE:
        cached = MULTI_PRICE_CACHE[symbol_up]
        if now - cached["timestamp"] < MULTI_PRICE_CACHE_TTL:
            return cached["data"]
    try:
        r = session.get(f"https://api.coingecko.com/api/v3/search?query={symbol.lower()}", timeout=10)
        if r.status_code != 200:
            return None
        coins = r.json().get("coins", [])
        if not coins:
            return None
        coin_id = coins[0]["id"]
        r2 = session.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd,eur,gbp,jpy,cny,aed,try,etb", "include_24hr_change": "true"},
            timeout=10
        )
        if r2.status_code == 200:
            data = r2.json().get(coin_id)
            if data:
                MULTI_PRICE_CACHE[symbol_up] = {"data": data, "timestamp": now}
            return data
    except Exception as e:
        log(f"Multi price error: {e}", "ERROR")
    return None

@retry_with_backoff(max_retries=2)
def scan_ca(address):
    if len(address) > MAX_CA_LENGTH:
        return None
    address = re.sub(r'[^a-zA-Z0-9]', '', address)
    try:
        if address.startswith("0x") and len(address) == 42:
            r = session.get(f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={address}", timeout=10)
            if r.status_code == 200:
                res = r.json().get("result", {}).get(address.lower(), {})
                if res:
                    return res
            r = session.get(f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={address}", timeout=10)
            if r.status_code == 200:
                return r.json().get("result", {}).get(address.lower(), {})
        else:
            r = session.get(f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}", timeout=10)
            if r.status_code == 200:
                return r.json().get("result", {}).get(address, {})
    except Exception as e:
        log(f"CA scan error: {e}", "ERROR")
    return None

# ================= PRICE FORMATTING =================
def format_price(price):
    if price is None:
        return "N/A"
    if price >= 1:
        return f"${price:,.4f}"
    elif price >= 0.0001:
        return f"${price:,.6f}"
    elif price >= 0.000001:
        return f"${price:,.8f}"
    else:
        return f"${price:.10f}"

# ================= WEBSOCKET =================
def on_ws_message(ws, msg):
    try:
        data = json.loads(msg)
        if "data" not in data:
            return
        ticker = data["data"]
        symbol = ticker["s"].replace("USDT", "")
        price = float(ticker["c"])
        with lock:
            PRICE_CACHE[symbol] = {"price": price, "timestamp": time.time()}
    except Exception as e:
        log(f"WS message error: {e}", "ERROR")

def on_ws_error(ws, err):
    log(f"WS error: {err}", "ERROR")

def on_ws_close(ws, *args):
    log("WS closed", "WARNING")

def on_ws_open(ws):
    log("WS connected")

def websocket_loop():
    global active_ws, ws_restart_required, shutdown_flag
    while not shutdown_flag:
        try:
            with lock:
                active_symbols = {a["coin"] for a in alerts if a.get("active", False)}
            if not active_symbols:
                time.sleep(10)
                continue
            streams = [f"{s.lower()}usdt@ticker" for s in active_symbols]
            url = "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)
            active_ws = websocket.WebSocketApp(
                url,
                on_open=on_ws_open,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close
            )
            wst = threading.Thread(target=active_ws.run_forever, daemon=True)
            wst.start()
            while wst.is_alive() and not shutdown_flag:
                if ws_restart_required:
                    log("Restarting WS due to alert change")
                    ws_restart_required = False
                    active_ws.close()
                    break
                time.sleep(1)
        except Exception as e:
            log(f"Websocket loop error: {e}", "ERROR")
        time.sleep(5)

threading.Thread(target=websocket_loop, daemon=True).start()

# ================= ALERT CHECKER =================
def check_alerts():
    last_triggered = {}
    while not shutdown_flag:
        try:
            triggered = []
            with lock:
                for a in alerts:
                    if not a.get("active", False):
                        continue
                    sym = a["coin"]
                    cache = PRICE_CACHE.get(sym)
                    if not cache or time.time() - cache["timestamp"] > CACHE_TTL:
                        price, _ = get_price(sym)
                        if price is None:
                            continue
                        PRICE_CACHE[sym] = {"price": price, "timestamp": time.time()}
                    else:
                        price = cache["price"]
                    key = (a["chat_id"], a["id"])
                    if key in last_triggered and time.time() - last_triggered[key] < 300:
                        continue
                    if (a["direction"] == ">" and price >= a["target"]) or \
                       (a["direction"] == "<" and price <= a["target"]):
                        a["active"] = False
                        triggered.append((a, price))
                        last_triggered[key] = time.time()
            if triggered:
                save_alerts()
                for a, price in triggered:
                    label = "🚀 risen above" if a["direction"] == ">" else "📉 dropped below"
                    send_and_track(
                        a["chat_id"],
                        f"🔔 *Alert #{a['id']} triggered!*\n\n"
                        f"*{a['coin']}* has {label} *${a['target']:,.2f}*\n"
                        f"Current price: *{format_price(price)}*",
                        reply_markup=main_menu()
                    )
        except Exception as e:
            log(f"Alert check error: {e}", "ERROR")
        time.sleep(5)

threading.Thread(target=check_alerts, daemon=True).start()

# ================= MEMORY CLEANER =================
def memory_cleaner():
    while not shutdown_flag:
        try:
            with lock:
                if len(PRICE_CACHE) > 500:
                    PRICE_CACHE.clear()
            if len(cooldowns) > 1000:
                cooldowns.clear()
            if len(waiting_for) > 1000:
                waiting_for.clear()
            if len(MULTI_PRICE_CACHE) > 200:
                MULTI_PRICE_CACHE.clear()
            for cid in list(user_msg_queue.keys()):
                if len(user_msg_queue.get(cid, [])) > MAX_HISTORY:
                    cleanup_old_messages(cid)
        except Exception as e:
            log(f"Memory cleaner error: {e}", "ERROR")
        time.sleep(600)

threading.Thread(target=memory_cleaner, daemon=True).start()

# ================= KEYBOARDS =================
def main_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("💰 Price", callback_data="menu_price"), InlineKeyboardButton("🔔 Alerts", callback_data="menu_alerts"))
    kb.row(InlineKeyboardButton("🚀 Gainers", callback_data="gainers"), InlineKeyboardButton("📉 Losers", callback_data="losers"))
    kb.row(InlineKeyboardButton("🔎 Coin Info", callback_data="menu_info"), InlineKeyboardButton("💱 Multi Price", callback_data="menu_multi"))
    kb.row(InlineKeyboardButton("🛡 Scan CA", callback_data="menu_scan"), InlineKeyboardButton("📋 My Alerts", callback_data="list_alerts"))
    return kb

def price_menu():
    kb = InlineKeyboardMarkup()
    coins = ["BTC","ETH","BNB","SOL","XRP","DOGE","ADA","AVAX","LINK","MATIC","UNI","ATOM","NEAR","APT","SUI","LTC","SHIB","TRX","TON","ARB","OP","INJ","TIA","DOT"]
    row = []
    for i, coin in enumerate(coins, 1):
        row.append(InlineKeyboardButton(coin, callback_data=f"price_{coin}"))
        if i % 3 == 0:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_coin"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def alerts_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC > 100k", callback_data="setalert_BTC_>_100000"), InlineKeyboardButton("BTC < 80k", callback_data="setalert_BTC_<_80000"))
    kb.row(InlineKeyboardButton("ETH > 4k", callback_data="setalert_ETH_>_4000"), InlineKeyboardButton("ETH < 2k", callback_data="setalert_ETH_<_2000"))
    kb.row(InlineKeyboardButton("SOL > 200", callback_data="setalert_SOL_>_200"), InlineKeyboardButton("SOL < 100", callback_data="setalert_SOL_<_100"))
    kb.row(InlineKeyboardButton("BNB > 700", callback_data="setalert_BNB_>_700"), InlineKeyboardButton("BNB < 500", callback_data="setalert_BNB_<_500"))
    kb.row(InlineKeyboardButton("XRP > 3", callback_data="setalert_XRP_>_3"), InlineKeyboardButton("XRP < 1", callback_data="setalert_XRP_<_1"))
    kb.row(InlineKeyboardButton("✏️ Custom alert", callback_data="custom_alert"))
    kb.row(InlineKeyboardButton("📋 My Alerts", callback_data="list_alerts"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def back_button():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main"))
    return kb

def info_coins_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC", callback_data="info_BTC"), InlineKeyboardButton("ETH", callback_data="info_ETH"), InlineKeyboardButton("BNB", callback_data="info_BNB"))
    kb.row(InlineKeyboardButton("SOL", callback_data="info_SOL"), InlineKeyboardButton("XRP", callback_data="info_XRP"), InlineKeyboardButton("ADA", callback_data="info_ADA"))
    kb.row(InlineKeyboardButton("DOGE", callback_data="info_DOGE"), InlineKeyboardButton("AVAX", callback_data="info_AVAX"), InlineKeyboardButton("LINK", callback_data="info_LINK"))
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_info"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def multi_coins_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC", callback_data="multi_BTC"), InlineKeyboardButton("ETH", callback_data="multi_ETH"), InlineKeyboardButton("BNB", callback_data="multi_BNB"))
    kb.row(InlineKeyboardButton("SOL", callback_data="multi_SOL"), InlineKeyboardButton("XRP", callback_data="multi_XRP"), InlineKeyboardButton("ADA", callback_data="multi_ADA"))
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_multi"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# ================= ADMIN COMMANDS =================
@bot.message_handler(commands=["stats"])
def stats_command(msg):
    if not is_admin(msg.from_user.id):
        send_and_track(msg.chat.id, "⛔ Admin only.")
        return
    log_interaction(msg.from_user.id, msg.from_user.username, msg.from_user.first_name, "/stats")
    try:
        if not os.path.exists(ANALYTICS_FILE):
            send_and_track(msg.chat.id, "No analytics data yet.")
            return
        with open(ANALYTICS_FILE, 'r') as f:
            rows = list(csv.reader(f))
        if len(rows) <= 1:
            send_and_track(msg.chat.id, "No user interactions logged.")
            return
        total = len(rows) - 1
        unique = len(set(r[1] for r in rows[1:]))
        cmd_counts = {}
        for r in rows[1:]:
            cmd_counts[r[4]] = cmd_counts.get(r[4], 0) + 1
        most_used = max(cmd_counts.items(), key=lambda x: x[1]) if cmd_counts else ("None", 0)
        active_alerts = len([a for a in alerts if a.get("active", False)])
        send_and_track(msg.chat.id,
            f"📊 *Bot Analytics*\n\n"
            f"👥 Unique users: {unique}\n"
            f"🔄 Total interactions: {total}\n"
            f"🔔 Active alerts: {active_alerts}\n"
            f"🔥 Most used: `{most_used[0]}` ({most_used[1]} times)"
        )
    except Exception as e:
        send_and_track(msg.chat.id, f"Error: {e}")

@bot.message_handler(commands=["users"])
def users_command(msg):
    if not is_admin(msg.from_user.id):
        send_and_track(msg.chat.id, "⛔ Admin only.")
        return
    log_interaction(msg.from_user.id, msg.from_user.username, msg.from_user.first_name, "/users")
    try:
        if not os.path.exists(ANALYTICS_FILE):
            send_and_track(msg.chat.id, "No users yet.")
            return
        with open(ANALYTICS_FILE, 'r') as f:
            rows = list(csv.reader(f))
        users = {}
        for r in rows[1:]:
            uid = r[1]
            if uid not in users:
                users[uid] = (r[2], r[3])
        if not users:
            send_and_track(msg.chat.id, "No users.")
            return
        user_list = "\n".join([f"`{uid}` — {name} (@{uname})" for uid, (uname, name) in list(users.items())[:20]])
        if len(users) > 20:
            user_list += f"\n... and {len(users)-20} more"
        send_and_track(msg.chat.id, f"👥 *Users ({len(users)} total)*\n\n{user_list}")
    except Exception as e:
        send_and_track(msg.chat.id, f"Error: {e}")

# ================= HANDLERS =================
@bot.message_handler(commands=["start", "help"])
def start(msg):
    log_interaction(msg.from_user.id, msg.from_user.username, msg.from_user.first_name, "/start")
    send_and_track(msg.chat.id,
        "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
        reply_markup=main_menu()
    )

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global alert_id_counter, ws_restart_required
    cid = call.message.chat.id
    data = call.data
    user_id = call.from_user.id

    if not cooldown_ok(user_id):
        bot.answer_callback_query(call.id, "⏳ Slow down")
        return

    bot.answer_callback_query(call.id)
    log_interaction(user_id, call.from_user.username, call.from_user.first_name, f"callback:{data[:50]}")

    try:
        if data == "back_main":
            waiting_for.pop(cid, None)
            send_and_track(cid, "🤖 *Persona* — your crypto assistant\n\nChoose an option:", reply_markup=main_menu())

        elif data == "menu_price":
            send_and_track(cid, "💰 *Select a coin or search:*", reply_markup=price_menu())

        elif data == "search_coin":
            waiting_for[cid] = "price"
            send_and_track(cid, "🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`", reply_markup=back_button())

        elif data == "menu_alerts":
            send_and_track(cid, "🔔 *Quick Alerts* — tap to set or create custom:", reply_markup=alerts_menu())

        elif data == "custom_alert":
            waiting_for[cid] = "alert"
            send_and_track(cid, "✏️ *Format:* `COIN > price` or `COIN < price`\nExample: `BTC > 95000`", reply_markup=back_button())

        elif data.startswith("setalert_"):
            parts = data.split("_")
            if len(parts) != 4:
                send_and_track(cid, "❌ Invalid alert.", reply_markup=alerts_menu())
                return
            _, sym, dir, t = parts
            try:
                target = float(t)
            except ValueError:
                send_and_track(cid, "❌ Invalid price.", reply_markup=alerts_menu())
                return
            with alert_counter_lock:
                with lock:
                    user_alerts = [a for a in alerts if a["chat_id"] == cid and a.get("active", True)]
                    if len(user_alerts) >= MAX_ALERTS_PER_USER:
                        send_and_track(cid, f"❌ Max {MAX_ALERTS_PER_USER} active alerts allowed.", reply_markup=alerts_menu())
                        return
                    cur = alert_id_counter
                    alert_id_counter += 1
                    alerts.append({
                        "id": cur, "chat_id": cid, "coin": sym.upper(),
                        "target": target, "direction": dir,
                        "active": True, "timestamp": time.time()
                    })
            save_alerts()
            ws_restart_required = True
            send_and_track(cid, f"✅ Alert #{cur} set!\n*{sym.upper()}* {dir} *${target:,.2f}*", reply_markup=alerts_menu())

        elif data == "list_alerts":
            active = [a for a in alerts if a.get("chat_id") == cid and a.get("active", True)]
            if not active:
                send_and_track(cid, "📋 No active alerts.", reply_markup=alerts_menu())
                return
            text = "📋 *Active Alerts:*\n\n"
            kb = InlineKeyboardMarkup()
            for a in active:
                label = "▲" if a['direction'] == '>' else "▼"
                text += f"#{a['id']} — {a['coin']} {label} ${a['target']:,.2f}\n"
                kb.row(InlineKeyboardButton(f"❌ Cancel #{a['id']}", callback_data=f"cancel_{a['id']}"))
            kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            send_and_track(cid, text, reply_markup=kb)

        elif data.startswith("cancel_"):
            aid = int(data.split("_")[1])
            with lock:
                for a in alerts:
                    if a.get('id') == aid and a.get('chat_id') == cid:
                        a['active'] = False
                        break
            save_alerts()
            ws_restart_required = True
            active = [a for a in alerts if a.get("chat_id") == cid and a.get("active", True)]
            if not active:
                send_and_track(cid, "📋 No more active alerts.", reply_markup=back_button())
            else:
                text = "📋 *Active Alerts:*\n\n"
                kb = InlineKeyboardMarkup()
                for a in active:
                    label = "▲" if a['direction'] == '>' else "▼"
                    text += f"#{a['id']} — {a['coin']} {label} ${a['target']:,.2f}\n"
                    kb.row(InlineKeyboardButton(f"❌ Cancel #{a['id']}", callback_data=f"cancel_{a['id']}"))
                kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
                send_and_track(cid, text, reply_markup=kb)

        elif data.startswith("price_"):
            sym = data.split("_")[1]
            p, ch = get_price(sym)
            if p is None:
                send_and_track(cid, f"❌ Could not fetch *{sym}*.", reply_markup=price_menu())
            else:
                arrow = "🟢 ▲" if ch >= 0 else "🔴 ▼"
                send_and_track(cid, f"*{sym}*\n💵 {format_price(p)}\n{arrow} {abs(ch):.2f}% (24h)", reply_markup=price_menu())

        elif data == "gainers":
            g, _ = get_top_movers()
            if not g:
                send_and_track(cid, "❌ Failed to fetch gainers.", reply_markup=back_button())
            else:
                text = "🚀 *Top 5 Gainers (24h)*\n\n"
                for d in g:
                    coin = d["symbol"].replace("USDT", "")
                    text += f"🟢 *{coin}* — {format_price(float(d['lastPrice']))} ▲ {float(d['priceChangePercent']):.2f}%\n"
                send_and_track(cid, text, reply_markup=back_button())

        elif data == "losers":
            _, l = get_top_movers()
            if not l:
                send_and_track(cid, "❌ Failed to fetch losers.", reply_markup=back_button())
            else:
                text = "📉 *Top 5 Losers (24h)*\n\n"
                for d in l:
                    coin = d["symbol"].replace("USDT", "")
                    text += f"🔴 *{coin}* — {format_price(float(d['lastPrice']))} ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
                send_and_track(cid, text, reply_markup=back_button())

        elif data == "menu_info":
            send_and_track(cid, "🔎 *Coin Info — Select or search:*", reply_markup=info_coins_menu())

        elif data == "search_info":
            waiting_for[cid] = "info"
            send_and_track(cid, "🔍 *Type any coin symbol:*\nExample: `PEPE`", reply_markup=back_button())

        elif data.startswith("info_"):
            sym = data.split("_")[1]
            info = get_coin_info(sym)
            if not info:
                send_and_track(cid, f"❌ Couldn't fetch info for *{sym}*.", reply_markup=info_coins_menu())
            else:
                supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
                max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
                send_and_track(cid,
                    f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                    f"🏆 Rank: #{info['rank']}\n"
                    f"💵 Price: {format_price(info['price'])}\n"
                    f"📈 ATH: {format_price(info['ath'])} ({info['ath_date']})\n"
                    f"📉 From ATH: {info['ath_change']:.2f}%\n"
                    f"📉 ATL: {format_price(info['atl'])} ({info['atl_date']})\n"
                    f"📈 From ATL: {info['atl_change']:.2f}%\n"
                    f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                    f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                    f"🔄 Supply: {supply_str} / {max_str}",
                    reply_markup=info_coins_menu()
                )

        elif data == "menu_multi":
            send_and_track(cid, "💱 *Multi-Currency Price — Select or search:*", reply_markup=multi_coins_menu())

        elif data == "search_multi":
            waiting_for[cid] = "multi"
            send_and_track(cid, "🔍 *Type any coin symbol:*\nExample: `BTC`", reply_markup=back_button())

        elif data.startswith("multi_"):
            sym = data.split("_")[1]
            prices = get_multi_price(sym)
            if not prices:
                send_and_track(cid, f"❌ Couldn't fetch *{sym}*.", reply_markup=multi_coins_menu())
            else:
                flags = {
                    "usd": "🇺🇸 $", "eur": "🇪🇺 €", "gbp": "🇬🇧 £",
                    "jpy": "🇯🇵 ¥", "cny": "🇨🇳 ¥", "aed": "🇦🇪 د.إ",
                    "try": "🇹🇷 ₺", "etb": "🇪🇹 Br"
                }
                text = f"💱 *{sym} Price*\n\n"
                for cur, flag in flags.items():
                    p = prices.get(cur)
                    if p:
                        text += f"{flag} {format_price(p)}\n"
                send_and_track(cid, text, reply_markup=multi_coins_menu())

        elif data == "menu_scan":
            waiting_for[cid] = "scan"
            send_and_track(cid, "🛡 *CA Scanner*\n\nPaste contract address (ETH, BSC, Solana):", reply_markup=back_button())

    except Exception as e:
        log(f"Callback error: {e}", "ERROR")
        try:
            send_and_track(cid, "⚠️ An error occurred. Please try again.", reply_markup=main_menu())
        except:
            pass

flags = {
                    "usd": "🇺🇸 $", "eur": "🇪🇺 €", "gbp": "🇬🇧 £",
                    "jpy": "🇯🇵 ¥", "cny": "🇨🇳 ¥", "aed": "🇦🇪 د.إ",
                    "try": "🇹🇷 ₺", "etb": "🇪🇹 Br"
                }# ================= TEXT INPUT =================
@bot.message_handler(func=lambda msg: True)
def text_input(msg):
    global alert_id_counter, ws_restart_required
    cid = msg.chat.id
    user_id = msg.from_user.id
    if cid not in waiting_for:
        return
    mode = waiting_for.pop(cid)
    text = msg.text.strip()
    log_interaction(user_id, msg.from_user.username, msg.from_user.first_name, f"text:{mode}", text[:100])

    if not text:
        send_and_track(cid, "❌ Empty input.", reply_markup=back_button())
        return

    try:
        if mode == "price":
            p, ch = get_price(text.upper())
            if p is None:
                send_and_track(cid, f"❌ Could not fetch *{text.upper()}*.", reply_markup=price_menu())
            else:
                arrow = "🟢 ▲" if ch >= 0 else "🔴 ▼"
                send_and_track(cid, f"*{text.upper()}*\n💵 {format_price(p)}\n{arrow} {abs(ch):.2f}% (24h)", reply_markup=price_menu())

        elif mode == "info":
            info = get_coin_info(text.upper())
            if not info:
                send_and_track(cid, f"❌ *{text.upper()}* not found.", reply_markup=info_coins_menu())
            else:
                supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
                max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
                send_and_track(cid,
                    f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                    f"🏆 Rank: #{info['rank']}\n"
                    f"💵 Price: {format_price(info['price'])}\n"
                    f"📈 ATH: {format_price(info['ath'])} ({info['ath_date']})\n"
                    f"📉 From ATH: {info['ath_change']:.2f}%\n"
                    f"📉 ATL: {format_price(info['atl'])} ({info['atl_date']})\n"
                    f"📈 From ATL: {info['atl_change']:.2f}%\n"
                    f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                    f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                    f"🔄 Supply: {supply_str} / {max_str}",
                    reply_markup=info_coins_menu()
                )

        elif mode == "multi":
            prices = get_multi_price(text.upper())
            if not prices:
                send_and_track(cid, f"❌ *{text.upper()}* not found.", reply_markup=multi_coins_menu())
            else:
                flags =
                    {"usd": "🇺🇸 $", "eur": "🇪🇺 €", "gbp": "🇬🇧 £",
                    "jpy": "🇯🇵 ¥", "cny": "🇨🇳 ¥", "aed": "🇦🇪 د.إ",
                    "try": "🇹🇷 ₺"}

                out = f"💱 *{text.upper()} Price*\n\n"
                for cur, flag in flags.items():
                    p = prices.get(cur)
                    if p:
                        out += f"{flag} {format_price(p)}\n"
                send_and_track(cid, out, reply_markup=multi_coins_menu())

        elif mode == "scan":
            if len(text) > MAX_CA_LENGTH:
                send_and_track(cid, f"❌ Address too long (max {MAX_CA_LENGTH} chars).", reply_markup=back_button())
                return
            res = scan_ca(text)
            if not res:
                send_and_track(cid, "❌ Contract not found or unsupported chain.", reply_markup=back_button())
            else:
                def flag(v):
                    if v == "1": return "⚠️ Yes"
                    if v == "0": return "✅ No"
                    return "❓ Unknown"
                send_and_track(cid,
                    f"🛡 *CA Scan: {res.get('token_name','Unknown')} ({res.get('token_symbol','?')})*\n\n"
                    f"🍯 Honeypot: {flag(res.get('is_honeypot','?'))}\n"
                    f"🖨 Mintable: {flag(res.get('is_mintable','?'))}\n"
                    f"🔁 Proxy: {flag(res.get('is_proxy','?'))}\n"
                    f"📂 Open Source: {flag(res.get('is_open_source','?'))}\n"
                    f"💸 Buy Tax: {res.get('buy_tax','?')}%\n"
                    f"💸 Sell Tax: {res.get('sell_tax','?')}%\n"
                    f"👥 Holders: {res.get('holder_count','?')}",
                    reply_markup=back_button()
                )

        elif mode == "alert":
            parts = text.split()
            if len(parts) != 3 or parts[1] not in ('>', '<'):
                send_and_track(cid, "❌ Wrong format. Use: `BTC > 70000`", reply_markup=alerts_menu())
                return
            sym = parts[0].upper()
            dir = parts[1]
            try:
                target = float(parts[2])
            except ValueError:
                send_and_track(cid, "❌ Invalid price value.", reply_markup=alerts_menu())
                return
            with alert_counter_lock:
                with lock:
                    user_alerts = [a for a in alerts if a["chat_id"] == cid and a.get("active", True)]
                    if len(user_alerts) >= MAX_ALERTS_PER_USER:
                        send_and_track(cid, f"❌ Max {MAX_ALERTS_PER_USER} active alerts allowed.", reply_markup=alerts_menu())
                        return
                    cur = alert_id_counter
                    alert_id_counter += 1
                    alerts.append({
                        "id": cur, "chat_id": cid, "coin": sym,
                        "target": target, "direction": dir,
                        "active": True, "timestamp": time.time()
                    })
            save_alerts()
            ws_restart_required = True
            send_and_track(cid, f"🔔 Alert #{cur} set!\n*{sym}* {dir} *${target:,.2f}*", reply_markup=alerts_menu())

    except Exception as e:
        log(f"Text handler error: {e}", "ERROR")
        send_and_track(cid, "⚠️ An error occurred.", reply_markup=main_menu())

# ================= GRACEFUL SHUTDOWN =================
def signal_handler(sig, frame):
    global shutdown_flag
    log("Shutting down gracefully...")
    shutdown_flag = True
    if active_ws:
        active_ws.close()
    bot.stop_polling()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ================= START =================
init_analytics()
log("🚀 Persona — Production ready")
bot.delete_webhook()
while not shutdown_flag:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        log(f"Polling error: {e}", "ERROR")
        time.sleep(5)
