import telebot
import requests
import threading
import time
import os
import json

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ─────────────────────────────
# CONFIG
# ─────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8619003788:AAEszjzsxeKH8dSm8FPtqkPJxCG9Dw3Tne4")
bot = telebot.TeleBot(BOT_TOKEN)

ALERT_FILE = "alerts.json"
CACHE_TTL = 10

alerts = []
alert_id_counter = [1]
waiting_for = {}
main_msg = {}

cache = {}
cache_time = {}
last_msg = {}

# ─────────────────────────────
# STORAGE
# ─────────────────────────────
def save_alerts():
    try:
        with open(ALERT_FILE, "w") as f:
            json.dump(alerts, f)
    except:
        pass

def load_alerts():
    global alerts
    try:
        with open(ALERT_FILE, "r") as f:
            alerts.extend(json.load(f))
    except:
        alerts = []

load_alerts()

# ─────────────────────────────
# UTIL
# ─────────────────────────────
def spam_check(cid):
    now = time.time()
    if cid in last_msg and now - last_msg[cid] < 1:
        return False
    last_msg[cid] = now
    return True

def delete_after(cid, mid, delay=5):
    def _delete():
        time.sleep(delay)
        try:
            bot.delete_message(cid, mid)
        except:
            pass
    threading.Thread(target=_delete, daemon=True).start()

def update_main(cid, text, markup):
    old = main_msg.get(cid)
    sent = bot.send_message(cid, text, parse_mode="Markdown", reply_markup=markup)
    main_msg[cid] = sent.message_id

    if old:
        delete_after(cid, old, 5)

# ─────────────────────────────
# CACHE WRAPPER
# ─────────────────────────────
def cached_get(url, params=None):
    key = url + str(params)
    now = time.time()

    if key in cache and now - cache_time.get(key, 0) < CACHE_TTL:
        return cache[key]

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        cache[key] = data
        cache_time[key] = now
        return data
    except:
        return None

# ─────────────────────────────
# PRICE API
# ─────────────────────────────
def get_price(symbol):
    pair = symbol.upper() + "USDT"
    data = cached_get(f"https://api.binance.com/api/v3/ticker/24hr",
                      {"symbol": pair})

    if data and "lastPrice" in data:
        return float(data["lastPrice"]), float(data["priceChangePercent"])
    return None, None

def get_top_movers():
    data = cached_get("https://api.binance.com/api/v3/ticker/24hr")

    if not data:
        return None, None

    stable = {"USDT","BUSD","USDC","DAI","TUSD","FDUSD"}

    filtered = [
        d for d in data
        if d["symbol"].endswith("USDT")
        and d["symbol"].replace("USDT", "") not in stable
        and float(d["quoteVolume"]) > 1_000_000
    ]

    sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)

    return sorted_data[:5], sorted_data[-5:][::-1]

# ─────────────────────────────
# COIN INFO
# ─────────────────────────────
def get_coin_info(symbol):
    coin_id = symbol.lower()

    data = cached_get(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}",
        {"localization": "false"}
    )

    if not data or "id" not in data:
        return None

    md = data["market_data"]

    return {
        "name": data["name"],
        "symbol": data["symbol"].upper(),
        "rank": data.get("market_cap_rank", "N/A"),
        "price": md["current_price"].get("usd", 0),
        "ath": md["ath"].get("usd", 0),
        "ath_date": md["ath_date"].get("usd", "")[:10],
        "ath_change": md["ath_change_percentage"].get("usd", 0),
        "supply": md.get("circulating_supply", 0),
        "max_supply": md.get("max_supply", None),
        "market_cap": md["market_cap"].get("usd", 0),
        "volume": md["total_volume"].get("usd", 0),
    }

# ─────────────────────────────
# ALERT SYSTEM
# ─────────────────────────────
def check_alerts():
    while True:
        active = [a for a in alerts if a["active"]]
        prices = {}

        for a in active:
            coin = a["coin"]

            if coin not in prices:
                p, _ = get_price(coin)
                prices[coin] = p

            p = prices.get(coin)
            if p is None:
                continue

            if (a["direction"] == ">" and p >= a["target"]) or \
               (a["direction"] == "<" and p <= a["target"]):

                a["active"] = False
                save_alerts()

                bot.send_message(
                    a["chat_id"],
                    f"🔔 Alert #{a['id']} triggered!\n"
                    f"{coin} hit {a['target']}\n"
                    f"Current: {p}",
                    parse_mode="Markdown"
                )

        time.sleep(60)

threading.Thread(target=check_alerts, daemon=True).start()

# ─────────────────────────────
# KEYBOARDS (minimal example)
# ─────────────────────────────
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
    return kb

def back_button():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# ─────────────────────────────
# START
# ─────────────────────────────
@bot.message_handler(commands=["start", "help"])
def start(msg):
    cid = msg.chat.id
    update_main(cid, "🤖 Persona v2 Crypto Bot\nChoose option:", main_menu())

# ─────────────────────────────
# TEXT HANDLER
# ─────────────────────────────
@bot.message_handler(func=lambda m: True)
def handle_text(msg):
    cid = msg.chat.id
    text = msg.text.strip()

    if not spam_check(cid):
        return

    if cid not in waiting_for:
        update_main(cid, "Menu:", main_menu())
        return

    mode = waiting_for.pop(cid)

    if mode == "price":
        p, ch = get_price(text)
        if not p:
            update_main(cid, "Not found", main_menu())
        else:
            update_main(cid, f"{text} = {p} ({ch}%)", main_menu())

# ─────────────────────────────
# CALLBACKS (minimal core)
# ─────────────────────────────
@bot.callback_query_handler(func=lambda c: True)
def cb(call):
    cid = call.message.chat.id
    data = call.data

    if data == "back_main":
        update_main(cid, "Menu:", main_menu())

    elif data == "menu_price":
        waiting_for[cid] = "price"
        update_main(cid, "Send coin symbol", back_button())

    elif data == "gainers":
        g, _ = get_top_movers()
        if not g:
            return
        txt = "🚀 Gainers:\n"
        for d in g:
            txt += f"{d['symbol']} {d['priceChangePercent']}%\n"
        update_main(cid, txt, back_button())

    elif data == "losers":
        _, l = get_top_movers()
        if not l:
            return
        txt = "📉 Losers:\n"
        for d in l:
            txt += f"{d['symbol']} {d['priceChangePercent']}%\n"
        update_main(cid, txt, back_button())

# ─────────────────────────────
print("🚀 Persona v2 running...")
bot.infinity_polling()
