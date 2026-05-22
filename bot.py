import telebot
import requests
import threading
import time
import json
import os
import websocket

from functools import wraps
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telebot.formatting import escape_markdown

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8619003788:AAEszjzsxeKH8dSm8FPtqkPJxCG9Dw3Tne4")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

ALERTS_FILE = "alerts.json"

CACHE_TTL = 10
MAX_HISTORY = 3
COOLDOWN_SECONDS = 2

PRICE_CACHE = {}
waiting_for = {}
user_msg_history = {}
cooldowns = {}

ws_restart_required = False

lock = threading.Lock()

# ================= ALERT STORAGE =================

def load_alerts():
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"[LOAD ALERTS ERROR] {e}")
    return []

alerts = load_alerts()
alert_id_counter = max((a["id"] for a in alerts), default=0) + 1 if alerts else 1

def save_alerts():
    try:
        tmp = ALERTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(alerts, f, indent=2)
        os.replace(tmp, ALERTS_FILE)
    except Exception as e:
        print(f"[SAVE ALERTS ERROR] {e}")

# ================= UTILITIES =================

def cooldown_ok(user_id):
    now = time.time()
    if user_id in cooldowns:
        if now - cooldowns[user_id] < COOLDOWN_SECONDS:
            return False
    cooldowns[user_id] = now
    return True

def cleanup_old_messages(chat_id):
    if chat_id not in user_msg_history:
        return
    history = user_msg_history[chat_id]
    while len(history) > MAX_HISTORY:
        old_id = history.pop(0)
        try:
            bot.delete_message(chat_id, old_id)
        except Exception:
            pass

def safe_send(chat_id, text, reply_markup=None):
    try:
        sent = bot.send_message(chat_id, text, reply_markup=reply_markup)
        if chat_id not in user_msg_history:
            user_msg_history[chat_id] = []
        user_msg_history[chat_id].append(sent.message_id)
        cleanup_old_messages(chat_id)
        return sent
    except Exception as e:
        print(f"[SEND ERROR] {e}")

def safe_edit(chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" not in str(e):
            print(f"[EDIT ERROR] {e}")
            safe_send(chat_id, text, reply_markup)

def retry(max_retries=3, delay=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    print(f"[RETRY ERROR] {func.__name__}: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(delay)
            return None
        return wrapper
    return decorator

# ================= API =================

@retry()
def get_price(symbol):
    pair = symbol.upper() + "USDT"
    r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={pair}", timeout=10)
    data = r.json()
    if "price" not in data:
        return None, None
    price = float(data["price"])
    r2 = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={pair}", timeout=10)
    data2 = r2.json()
    change = float(data2["priceChangePercent"])
    return price, change

@retry()
def get_top_movers():
    r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
    data = r.json()
    stable = {"USDT", "BUSD", "USDC", "DAI", "FDUSD"}
    filtered = [
        d for d in data
        if d["symbol"].endswith("USDT")
        and d["symbol"].replace("USDT", "") not in stable
        and float(d["quoteVolume"]) > 1_000_000
    ]
    sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)
    return sorted_data[:5], sorted_data[-5:][::-1]

# ================= WEBSOCKET =================

current_ws = None

def on_ws_message(ws, message):
    try:
        data = json.loads(message)
        if "data" not in data:
            return
        ticker = data["data"]
        symbol = ticker["s"].replace("USDT", "")
        price = float(ticker["c"])
        with lock:
            PRICE_CACHE[symbol] = {"price": price, "timestamp": time.time()}
    except Exception as e:
        print(f"[WS MESSAGE ERROR] {e}")

def on_ws_error(ws, error):
    print(f"[WS ERROR] {error}")

def on_ws_close(ws, close_status_code, close_msg):
    print("[WS CLOSED]")

def on_ws_open(ws):
    print("[WS CONNECTED]")

def websocket_loop():
    global current_ws, ws_restart_required
    while True:
        try:
            active_symbols = {a["coin"] for a in alerts if a["active"]}
            if not active_symbols:
                time.sleep(10)
                continue
            streams = [f"{sym.lower()}usdt@ticker" for sym in active_symbols]
            url = "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)
            current_ws = websocket.WebSocketApp(
                url,
                on_open=on_ws_open,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close
            )
            ws_thread = threading.Thread(target=current_ws.run_forever)
            ws_thread.start()
            while ws_thread.is_alive():
                if ws_restart_required:
                    print("[WS RESTARTING]")
                    ws_restart_required = False
                    current_ws.close()
                    break
                time.sleep(1)
        except Exception as e:
            print(f"[WS LOOP ERROR] {e}")
        time.sleep(5)

threading.Thread(target=websocket_loop, daemon=True).start()

# ================= ALERT CHECKER =================

def check_alerts():
    while True:
        try:
            triggered = []
            with lock:
                for a in alerts:
                    if not a["active"]:
                        continue
                    symbol = a["coin"]
                    cache = PRICE_CACHE.get(symbol)
                    if not cache or time.time() - cache["timestamp"] > CACHE_TTL:
                        price, _ = get_price(symbol)
                        if price is None:
                            continue
                        PRICE_CACHE[symbol] = {"price": price, "timestamp": time.time()}
                    else:
                        price = cache["price"]
                    if (a["direction"] == ">" and price >= a["target"]) or (a["direction"] == "<" and price <= a["target"]):
                        a["active"] = False
                        triggered.append((a, price))
            if triggered:
                save_alerts()
                for a, price in triggered:
                    label = "🚀 risen above" if a["direction"] == ">" else "📉 dropped below"
                    try:
                        bot.send_message(
                            a["chat_id"],
                            f"🔔 *Alert #{a['id']} triggered!*\n\n"
                            f"*{a['coin']}* has {label} *${a['target']:,.2f}*\n"
                            f"Current price: *${price:,.4f}*",
                            reply_markup=main_menu()
                        )
                    except Exception as e:
                        print(f"[ALERT SEND ERROR] {e}")
        except Exception as e:
            print(f"[CHECK ALERT ERROR] {e}")
        time.sleep(5)

threading.Thread(target=check_alerts, daemon=True).start()

# ================= MEMORY CLEANER =================

def memory_cleaner():
    while True:
        try:
            if len(PRICE_CACHE) > 500:
                PRICE_CACHE.clear()
            if len(cooldowns) > 1000:
                cooldowns.clear()
            if len(waiting_for) > 1000:
                waiting_for.clear()
        except Exception as e:
            print(f"[MEMORY CLEANER ERROR] {e}")
        time.sleep(600)

threading.Thread(target=memory_cleaner, daemon=True).start()

# ================= MENUS =================

def main_menu():
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
        InlineKeyboardButton("📋 My Alerts", callback_data="list_alerts")
    )
    return kb

def price_menu():
    kb = InlineKeyboardMarkup()
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE"]
    row = []
    for i, coin in enumerate(coins, 1):
        row.append(InlineKeyboardButton(coin, callback_data=f"price_{coin}"))
        if i % 3 == 0:
            kb.row(*row)
            row = []
    kb.row(InlineKeyboardButton("🔍 Search", callback_data="search_coin"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def alerts_menu():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("BTC > 100k", callback_data="setalert_BTC_>_100000"),
        InlineKeyboardButton("BTC < 80k", callback_data="setalert_BTC_<_80000")
    )
    kb.row(InlineKeyboardButton("✏️ Custom Alert", callback_data="custom_alert"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# ================= PRICE DISPLAY =================

def show_price(chat_id, symbol):
    bot.send_chat_action(chat_id, "typing")
    clean_symbol = symbol.upper()
    price, change = get_price(clean_symbol)
    if price is None:
        safe_send(chat_id, f"❌ Could not fetch *{escape_markdown(clean_symbol)}*")
        return
    arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
    safe_send(
        chat_id,
        f"*{escape_markdown(clean_symbol)}*\n"
        f"💵 ${price:,.4f}\n"
        f"{arrow} {abs(change):.2f}% (24h)",
        reply_markup=price_menu()
    )

# ================= START =================

@bot.message_handler(commands=["start", "help"])
def start(msg):
    safe_send(
        msg.chat.id,
        "🤖 *Persona* — crypto assistant\n\nChoose an option:",
        reply_markup=main_menu()
    )

# ================= CALLBACKS =================

@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    global alert_id_counter, ws_restart_required
    cid = call.message.chat.id
    mid = call.message.message_id
    data = call.data

    if not cooldown_ok(cid):
        bot.answer_callback_query(call.id, "⏳ Slow down")
        return

    try:
        if data == "back_main":
            waiting_for.pop(cid, None)
            safe_edit(cid, mid, "🤖 *Persona* — crypto assistant", reply_markup=main_menu())

        elif data == "menu_price":
            safe_edit(cid, mid, "💰 *Select a coin:*", reply_markup=price_menu())

        elif data.startswith("price_"):
            symbol = data.split("_")[1]
            bot.answer_callback_query(call.id, f"Fetching {symbol}...")
            show_price(cid, symbol)

        elif data == "search_coin":
            waiting_for[cid] = "price"
            safe_edit(cid, mid, "🔍 *Type any coin symbol*\n\nExample: `PEPE`", reply_markup=price_menu())

        elif data == "menu_alerts":
            safe_edit(cid, mid, "🔔 *Alerts Menu*", reply_markup=alerts_menu())

        elif data == "custom_alert":
            waiting_for[cid] = "alert"
            safe_edit(cid, mid, "✏️ *Format:*\n`BTC > 100000`", reply_markup=alerts_menu())

        elif data.startswith("setalert_"):
            _, symbol, direction, target_str = data.split("_")
            target = float(target_str)
            with lock:
                alerts.append({
                    "id": alert_id_counter,
                    "chat_id": cid,
                    "coin": symbol,
                    "target": target,
                    "direction": direction,
                    "active": True
                })
                current_id = alert_id_counter
                alert_id_counter += 1
            save_alerts()
            ws_restart_required = True
            safe_edit(cid, mid, f"✅ Alert #{current_id} set\n*{symbol}* {direction} *${target:,.2f}*", reply_markup=alerts_menu())

        elif data == "gainers":
            g, _ = get_top_movers()
            if not g:
                safe_send(cid, "❌ Failed to fetch")
                return
            text = "🚀 *Top Gainers*\n\n"
            for d in g:
                coin = d["symbol"].replace("USDT", "")
                text += f"🟢 *{coin}* {float(d['priceChangePercent']):.2f}%\n"
            safe_send(cid, text)

        elif data == "losers":
            _, l = get_top_movers()
            if not l:
                safe_send(cid, "❌ Failed to fetch")
                return
            text = "📉 *Top Losers*\n\n"
            for d in l:
                coin = d["symbol"].replace("USDT", "")
                text += f"🔴 *{coin}* {float(d['priceChangePercent']):.2f}%\n"
            safe_send(cid, text)

        elif data == "list_alerts":
            active = [a for a in alerts if a["chat_id"] == cid and a["active"]]
            if not active:
                safe_send(cid, "📋 No active alerts")
                return
            text = "📋 *Your Alerts*\n\n"
            for a in active:
                text += f"#{a['id']} {a['coin']} {a['direction']} ${a['target']:,.2f}\n"
            # Add cancel buttons for each alert
            kb = InlineKeyboardMarkup()
            for a in active:
                kb.row(InlineKeyboardButton(f"❌ Cancel #{a['id']}", callback_data=f"cancel_{a['id']}"))
            kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            safe_send(cid, text, reply_markup=kb)

        elif data.startswith("cancel_"):
            alert_id = int(data.split("_")[1])
            with lock:
                for a in alerts:
                    if a["id"] == alert_id and a["chat_id"] == cid:
                        a["active"] = False
                        save_alerts()
                        ws_restart_required = True
                        bot.answer_callback_query(call.id, f"✅ Alert #{alert_id} cancelled")
                        break
            # Refresh list
            active = [a for a in alerts if a["chat_id"] == cid and a["active"]]
            if not active:
                safe_edit(cid, mid, "📋 No active alerts", reply_markup=main_menu())
            else:
                text = "📋 *Your Alerts*\n\n"
                kb = InlineKeyboardMarkup()
                for a in active:
                    text += f"#{a['id']} {a['coin']} {a['direction']} ${a['target']:,.2f}\n"
                    kb.row(InlineKeyboardButton(f"❌ Cancel #{a['id']}", callback_data=f"cancel_{a['id']}"))
                kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
                safe_edit(cid, mid, text, reply_markup=kb)

    except Exception as e:
        print(f"[CALLBACK ERROR] {e}")

# ================= TEXT HANDLER =================

@bot.message_handler(func=lambda msg: True)
def text_handler(msg):
    global alert_id_counter, ws_restart_required
    cid = msg.chat.id
    if cid not in waiting_for:
        return
    mode = waiting_for.pop(cid)
    text = msg.text.strip()
    try:
        if mode == "price":
            show_price(cid, text)
        elif mode == "alert":
            parts = text.split()
            if len(parts) != 3:
                safe_send(cid, "❌ Format:\n`BTC > 100000`")
                return
            symbol = parts[0].upper()
            direction = parts[1]
            target = float(parts[2])
            with lock:
                alerts.append({
                    "id": alert_id_counter,
                    "chat_id": cid,
                    "coin": symbol,
                    "target": target,
                    "direction": direction,
                    "active": True
                })
                current_id = alert_id_counter
                alert_id_counter += 1
            save_alerts()
            ws_restart_required = True
            safe_send(cid, f"✅ Alert #{current_id} set\n*{symbol}* {direction} *${target:,.2f}*", reply_markup=alerts_menu())
    except Exception as e:
        print(f"[TEXT ERROR] {e}")

# ================= POLLING =================

print("🚀 Persona V5 running...")
while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        print(f"[POLLING ERROR] {e}")
        time.sleep(5)
