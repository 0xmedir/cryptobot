import telebot
import requests
import threading
import time
import json
import os
from functools import wraps
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import websocket
import _thread

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8619003788:AAEszjzsxeKH8dSm8FPtqkPJxCG9Dw3Tne4")
bot = telebot.TeleBot(BOT_TOKEN)

ALERTS_FILE = "alerts.json"
PRICE_CACHE = {}
CACHE_TTL = 10

# ========== MESSAGE CLEANUP (keeps last 3 messages) ==========
user_msg_history = {}  # chat_id -> list of message_ids
MAX_HISTORY = 3

def cleanup_old_messages(chat_id):
    if chat_id not in user_msg_history:
        return
    history = user_msg_history[chat_id]
    while len(history) > MAX_HISTORY:
        old_id = history.pop(0)
        try:
            bot.delete_message(chat_id, old_id)
        except:
            pass

def safe_send(chat_id, text, parse_mode=None, reply_markup=None):
    """Send a message and keep last MAX_HISTORY messages."""
    sent = bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    if chat_id not in user_msg_history:
        user_msg_history[chat_id] = []
    user_msg_history[chat_id].append(sent.message_id)
    cleanup_old_messages(chat_id)
    return sent

def safe_edit(chat_id, message_id, text, parse_mode=None, reply_markup=None):
    """Edit a message; if fails, send new and clean up."""
    try:
        bot.edit_message_text(text, chat_id, message_id, parse_mode=parse_mode, reply_markup=reply_markup)
        # Keep same ID in history (do not add new)
    except:
        # Fallback: send new and clean
        safe_send(chat_id, text, parse_mode, reply_markup)

# ========== ALERTS PERSISTENCE ==========
def load_alerts():
    if os.path.exists(ALERTS_FILE):
        with open(ALERTS_FILE, "r") as f:
            return json.load(f)
    return []

def save_alerts():
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)

alerts = load_alerts()
alert_id_counter = max((a["id"] for a in alerts), default=0) + 1 if alerts else 1

# ========== RETRY DECORATOR ==========
def retry(max_retries=3, delay=1, backoff=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(delay * (backoff ** attempt))
            return None
        return wrapper
    return decorator

# ========== API HELPERS ==========
@retry(max_retries=2)
def get_price(symbol):
    pair = symbol.upper() + "USDT"
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={pair}", timeout=10)
        data = r.json()
        if "lastPrice" in data:
            return float(data["lastPrice"]), float(data["priceChangePercent"])
    except:
        pass
    return None, None

@retry(max_retries=2)
def get_top_movers():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
        data = r.json()
        stable = {"USDT", "BUSD", "USDC", "DAI", "TUSD", "FDUSD"}
        filtered = [
            d for d in data
            if d["symbol"].endswith("USDT")
            and d["symbol"].replace("USDT", "") not in stable
            and float(d["quoteVolume"]) > 1_000_000
        ]
        sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)
        return sorted_data[:5], sorted_data[-5:][::-1]
    except:
        return None, None

@retry(max_retries=2)
def get_coin_info(symbol):
    try:
        search_url = f"https://api.coingecko.com/api/v3/search?query={symbol.lower()}"
        r = requests.get(search_url, timeout=10)
        if r.status_code == 200:
            search_data = r.json()
            coins = search_data.get("coins", [])
            if not coins:
                return None
            coin_id = coins[0]["id"]
        else:
            return None
    except:
        return None

    try:
        detail_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        params = {"localization": "false", "tickers": "false", "community_data": "false"}
        r = requests.get(detail_url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
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
    except:
        pass
    return None

@retry(max_retries=2)
def get_multi_price(symbol):
    try:
        search_url = f"https://api.coingecko.com/api/v3/search?query={symbol.lower()}"
        r = requests.get(search_url, timeout=10)
        if r.status_code == 200:
            search_data = r.json()
            coins = search_data.get("coins", [])
            if not coins:
                return None
            coin_id = coins[0]["id"]
        else:
            return None
    except:
        return None

    try:
        price_url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": coin_id,
            "vs_currencies": "usd,eur,gbp,jpy,cny,aed,try",
            "include_24hr_change": "true"
        }
        r = requests.get(price_url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get(coin_id)
    except:
        pass
    return None

@retry(max_retries=2)
def scan_ca(address):
    try:
        if address.startswith("0x") and len(address) == 42:
            url = f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={address}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                result = data.get("result", {}).get(address.lower(), {})
                if result:
                    return result
            url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={address}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                return data.get("result", {}).get(address.lower(), {})
        else:
            url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                return data.get("result", {}).get(address, {})
    except:
        pass
    return None

# ========== WEBSOCKET REAL-TIME PRICES ==========
def on_ws_message(ws, message):
    data = json.loads(message)
    if "data" in data:
        ticker = data["data"]
        symbol = ticker["s"].replace("USDT", "")
        price = float(ticker["c"])
        PRICE_CACHE[symbol] = {"price": price, "timestamp": time.time()}

def on_ws_error(ws, error):
    print(f"WebSocket error: {error}")

def on_ws_close(ws, close_status_code, close_msg):
    print("WebSocket closed, reconnecting...")
    time.sleep(5)
    start_websocket()

def on_ws_open(ws):
    print("WebSocket connected")

def start_websocket():
    active_symbols = {a["coin"] for a in alerts if a["active"]}
    if not active_symbols:
        time.sleep(30)
        start_websocket()
        return
    streams = [f"{sym.lower()}usdt@ticker" for sym in active_symbols]
    stream_url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
    ws = websocket.WebSocketApp(stream_url,
                                on_open=on_ws_open,
                                on_message=on_ws_message,
                                on_error=on_ws_error,
                                on_close=on_ws_close)
    ws.run_forever()

def run_websocket_thread():
    while True:
        try:
            start_websocket()
        except Exception as e:
            print(f"WebSocket thread error: {e}")
            time.sleep(10)

threading.Thread(target=run_websocket_thread, daemon=True).start()

# ========== ALERT CHECKER ==========
def check_alerts():
    while True:
        now = time.time()
        triggered = []
        for a in alerts:
            if not a["active"]:
                continue
            cache_entry = PRICE_CACHE.get(a["coin"])
            if not cache_entry or (now - cache_entry["timestamp"]) > CACHE_TTL:
                price, _ = get_price(a["coin"])
                if price is None:
                    continue
                PRICE_CACHE[a["coin"]] = {"price": price, "timestamp": now}
            else:
                price = cache_entry["price"]

            target = a["target"]
            if (a["direction"] == ">" and price >= target) or (a["direction"] == "<" and price <= target):
                triggered.append(a)
                a["active"] = False

        if triggered:
            save_alerts()
            for a in triggered:
                label = "🚀 risen above" if a["direction"] == ">" else "📉 dropped below"
                try:
                    # Alert messages are sent as new messages (not tracked by cleanup)
                    bot.send_message(
                        a["chat_id"],
                        f"🔔 *Alert #{a['id']} triggered!*\n"
                        f"*{a['coin']}* has {label} *${a['target']:,.2f}*\n"
                        f"Current price: *${PRICE_CACHE[a['coin']]['price']:,.4f}*",
                        parse_mode="Markdown",
                        reply_markup=main_menu()
                    )
                except Exception as e:
                    print(f"Failed to send alert: {e}")
        time.sleep(5)

threading.Thread(target=check_alerts, daemon=True).start()

# ========== KEYBOARDS ==========
def main_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("💰 Price", callback_data="menu_price"),
           InlineKeyboardButton("🔔 Alerts", callback_data="menu_alerts"))
    kb.row(InlineKeyboardButton("🚀 Gainers", callback_data="gainers"),
           InlineKeyboardButton("📉 Losers", callback_data="losers"))
    kb.row(InlineKeyboardButton("🔎 Coin Info", callback_data="menu_info"),
           InlineKeyboardButton("💱 Multi Price", callback_data="menu_multi"))
    kb.row(InlineKeyboardButton("🛡 Scan CA", callback_data="menu_scan"),
           InlineKeyboardButton("📋 My Alerts", callback_data="list_alerts"))
    return kb

def price_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC", callback_data="price_BTC"),
           InlineKeyboardButton("ETH", callback_data="price_ETH"),
           InlineKeyboardButton("BNB", callback_data="price_BNB"))
    kb.row(InlineKeyboardButton("SOL", callback_data="price_SOL"),
           InlineKeyboardButton("XRP", callback_data="price_XRP"),
           InlineKeyboardButton("DOGE", callback_data="price_DOGE"))
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_coin"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def alerts_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC > 100k", callback_data="setalert_BTC_>_100000"),
           InlineKeyboardButton("BTC < 80k", callback_data="setalert_BTC_<_80000"))
    kb.row(InlineKeyboardButton("ETH > 4k", callback_data="setalert_ETH_>_4000"),
           InlineKeyboardButton("ETH < 2k", callback_data="setalert_ETH_<_2000"))
    kb.row(InlineKeyboardButton("SOL > 200", callback_data="setalert_SOL_>_200"),
           InlineKeyboardButton("SOL < 100", callback_data="setalert_SOL_<_100"))
    kb.row(InlineKeyboardButton("✏️ Custom alert", callback_data="custom_alert"))
    kb.row(InlineKeyboardButton("📋 My Alerts", callback_data="list_alerts"),
           InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def back_button():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main"))
    return kb

def info_coins_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC", callback_data="info_BTC"),
           InlineKeyboardButton("ETH", callback_data="info_ETH"),
           InlineKeyboardButton("BNB", callback_data="info_BNB"))
    kb.row(InlineKeyboardButton("SOL", callback_data="info_SOL"),
           InlineKeyboardButton("XRP", callback_data="info_XRP"),
           InlineKeyboardButton("ADA", callback_data="info_ADA"))
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_info"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def multi_coins_menu():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("BTC", callback_data="multi_BTC"),
           InlineKeyboardButton("ETH", callback_data="multi_ETH"),
           InlineKeyboardButton("BNB", callback_data="multi_BNB"))
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_multi"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# ========== STREAMING PRICE ==========
def stream_price_update(chat_id, symbol):
    sent = bot.send_message(chat_id, f"⏳ Fetching {symbol} price...", parse_mode="Markdown")
    # Track this message
    if chat_id not in user_msg_history:
        user_msg_history[chat_id] = []
    user_msg_history[chat_id].append(sent.message_id)
    cleanup_old_messages(chat_id)

    price, change = get_price(symbol)
    if price is None:
        safe_edit(chat_id, sent.message_id, f"❌ Could not fetch {symbol}. Please try again.")
    else:
        arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
        safe_edit(chat_id, sent.message_id,
                  f"*{symbol}*\n💵 ${price:,.4f}\n{arrow} {abs(change):.2f}% (24h)",
                  parse_mode="Markdown",
                  reply_markup=price_menu())

# ========== HANDLERS ==========
waiting_for = {}

@bot.message_handler(commands=['start', 'help'])
def start(msg):
    safe_send(msg.chat.id,
              "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
              parse_mode="Markdown",
              reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data
    cid = call.message.chat.id
    mid = call.message.message_id

    if data == "back_main":
        waiting_for.pop(cid, None)
        safe_edit(cid, mid,
                  "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
                  parse_mode="Markdown",
                  reply_markup=main_menu())
        bot.answer_callback_query(call.id)

    elif data == "menu_price":
        safe_edit(cid, mid, "💰 *Select a coin or search:*",
                  parse_mode="Markdown", reply_markup=price_menu())
        bot.answer_callback_query(call.id)

    elif data == "search_coin":
        safe_edit(cid, mid,
                  "🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`, `SEI`",
                  parse_mode="Markdown",
                  reply_markup=back_button())
        waiting_for[cid] = "price"
        bot.answer_callback_query(call.id)

    elif data.startswith("price_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol}...")
        stream_price_update(cid, symbol)

    elif data == "menu_alerts":
        safe_edit(cid, mid, "🔔 *Quick Alerts* — tap to set or create custom:",
                  parse_mode="Markdown", reply_markup=alerts_menu())
        bot.answer_callback_query(call.id)

    elif data == "custom_alert":
        safe_edit(cid, mid,
                  "✏️ *Type your custom alert:*\nFormat: `COIN > price` or `COIN < price`\n\nExamples:\n`BTC > 95000`\n`PEPE < 0.00001`",
                  parse_mode="Markdown", reply_markup=back_button())
        waiting_for[cid] = "alert"
        bot.answer_callback_query(call.id)

    elif data.startswith("setalert_"):
        parts = data.split("_")
        symbol = parts[1]
        direction = parts[2]
        target = float(parts[3])
        global alert_id_counter
        aid = alert_id_counter
        alert_id_counter += 1
        alerts.append({
            "id": aid, "chat_id": cid,
            "coin": symbol, "target": target,
            "direction": direction, "active": True
        })
        save_alerts()
        label = "rises above" if direction == ">" else "drops below"
        bot.answer_callback_query(call.id, "✅ Alert set!")
        safe_edit(cid, mid,
                  f"🔔 Alert #{aid} set!\n*{symbol}* {label} *${target:,.2f}*",
                  parse_mode="Markdown", reply_markup=alerts_menu())

    elif data == "list_alerts":
        user_alerts = [a for a in alerts if a['chat_id'] == cid and a['active']]
        if not user_alerts:
            bot.answer_callback_query(call.id, "No active alerts.")
            safe_edit(cid, mid, "📋 No active alerts.\n\nSet one below:",
                      reply_markup=alerts_menu())
            return
        text = "📋 *Active Alerts:*\n\n"
        kb = InlineKeyboardMarkup()
        for a in user_alerts:
            label = "▲" if a['direction'] == '>' else "▼"
            text += f"#{a['id']} — {a['coin']} {label} ${a['target']:,.2f}\n"
            kb.row(InlineKeyboardButton(
                f"❌ Cancel #{a['id']} ({a['coin']} {label} ${a['target']:,.0f})",
                callback_data=f"cancel_{a['id']}"
            ))
        kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
        safe_edit(cid, mid, text, parse_mode="Markdown", reply_markup=kb)
        bot.answer_callback_query(call.id)

    elif data.startswith("cancel_"):
        aid = int(data.split("_")[1])
        for a in alerts:
            if a['id'] == aid and a['chat_id'] == cid:
                a['active'] = False
                save_alerts()
                bot.answer_callback_query(call.id, f"✅ Alert #{aid} cancelled.")
                break
        # Refresh list
        user_alerts = [a for a in alerts if a['chat_id'] == cid and a['active']]
        if not user_alerts:
            safe_edit(cid, mid, "📋 No more active alerts.", reply_markup=back_button())
        else:
            text = "📋 *Active Alerts:*\n\n"
            kb = InlineKeyboardMarkup()
            for a in user_alerts:
                label = "▲" if a['direction'] == '>' else "▼"
                text += f"#{a['id']} — {a['coin']} {label} ${a['target']:,.2f}\n"
                kb.row(InlineKeyboardButton(
                    f"❌ Cancel #{a['id']} ({a['coin']} {label} ${a['target']:,.0f})",
                    callback_data=f"cancel_{a['id']}"
                ))
            kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            safe_edit(cid, mid, text, parse_mode="Markdown", reply_markup=kb)

    elif data == "gainers":
        bot.answer_callback_query(call.id, "Fetching top gainers...")
        g, _ = get_top_movers()
        if not g:
            safe_edit(cid, mid, "❌ Failed to fetch.", reply_markup=back_button())
        else:
            text = "🚀 *Top 5 Gainers (24h)*\n\n"
            for d in g:
                coin = d["symbol"].replace("USDT", "")
                text += f"🟢 *{coin}* — ${float(d['lastPrice']):,.4f} ▲ {float(d['priceChangePercent']):.2f}%\n"
            safe_edit(cid, mid, text, parse_mode="Markdown", reply_markup=back_button())

    elif data == "losers":
        bot.answer_callback_query(call.id, "Fetching top losers...")
        _, l = get_top_movers()
        if not l:
            safe_edit(cid, mid, "❌ Failed to fetch.", reply_markup=back_button())
        else:
            text = "📉 *Top 5 Losers (24h)*\n\n"
            for d in l:
                coin = d["symbol"].replace("USDT", "")
                text += f"🔴 *{coin}* — ${float(d['lastPrice']):,.4f} ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
            safe_edit(cid, mid, text, parse_mode="Markdown", reply_markup=back_button())

    elif data == "menu_info":
        safe_edit(cid, mid, "🔎 *Coin Info — Select or search:*",
                  parse_mode="Markdown", reply_markup=info_coins_menu())
        bot.answer_callback_query(call.id)

    elif data == "search_info":
        safe_edit(cid, mid, "🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`",
                  parse_mode="Markdown", reply_markup=back_button())
        waiting_for[cid] = "info"
        bot.answer_callback_query(call.id)

    elif data.startswith("info_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol} info...")
        info = get_coin_info(symbol)
        if not info:
            safe_edit(cid, mid, f"❌ Couldn't fetch info for *{symbol}*.",
                      parse_mode="Markdown", reply_markup=info_coins_menu())
        else:
            supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
            max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
            safe_edit(cid, mid,
                      f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                      f"🏆 Rank: #{info['rank']}\n"
                      f"💵 Price: ${info['price']:,.4f}\n"
                      f"📈 ATH: ${info['ath']:,.4f} ({info['ath_date']})\n"
                      f"📉 From ATH: {info['ath_change']:.2f}%\n"
                      f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                      f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                      f"🔄 Supply: {supply_str} / {max_str}",
                      parse_mode="Markdown", reply_markup=info_coins_menu())

    elif data == "menu_multi":
        safe_edit(cid, mid, "💱 *Multi-Currency Price — Select or search:*",
                  parse_mode="Markdown", reply_markup=multi_coins_menu())
        bot.answer_callback_query(call.id)

    elif data == "search_multi":
        safe_edit(cid, mid, "🔍 *Type any coin symbol:*\nExample: `BTC`, `ETH`",
                  parse_mode="Markdown", reply_markup=back_button())
        waiting_for[cid] = "multi"
        bot.answer_callback_query(call.id)

    elif data.startswith("multi_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol} prices...")
        prices = get_multi_price(symbol)
        if not prices:
            safe_edit(cid, mid, f"❌ Couldn't fetch *{symbol}*.",
                      parse_mode="Markdown", reply_markup=multi_coins_menu())
        else:
            flags = {"usd": "🇺🇸", "eur": "🇪🇺", "gbp": "🇬🇧", "jpy": "🇯🇵", "cny": "🇨🇳", "aed": "🇦🇪", "try": "🇹🇷"}
            symbols_map = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cny": "¥", "aed": "د.إ", "try": "₺"}
            text = f"💱 *{symbol} Price*\n\n"
            for cur, flag in flags.items():
                p = prices.get(cur)
                if p:
                    text += f"{flag} {symbols_map[cur]}{p:,.4f}\n"
            safe_edit(cid, mid, text, parse_mode="Markdown", reply_markup=multi_coins_menu())

    elif data == "menu_scan":
        safe_edit(cid, mid,
                  "🛡 *CA Scanner*\n\nPaste a contract address:\n✅ Supports ETH, BSC, Solana\n\nExample:\n`0x1234...abcd`",
                  parse_mode="Markdown", reply_markup=back_button())
        waiting_for[cid] = "scan"
        bot.answer_callback_query(call.id)

# ========== TEXT HANDLER ==========
@bot.message_handler(func=lambda msg: True)
def handle_text(msg):
    cid = msg.chat.id
    text = msg.text.strip()
    if cid not in waiting_for:
        return
    mode = waiting_for.pop(cid)

    if mode == "price":
        stream_price_update(cid, text.upper())
    elif mode == "info":
        symbol = text.upper()
        info = get_coin_info(symbol)
        if not info:
            safe_send(cid, f"❌ *{symbol}* not found.", parse_mode="Markdown", reply_markup=info_coins_menu())
        else:
            supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
            max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
            safe_send(cid,
                f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                f"🏆 Rank: #{info['rank']}\n"
                f"💵 Price: ${info['price']:,.4f}\n"
                f"📈 ATH: ${info['ath']:,.4f} ({info['ath_date']})\n"
                f"📉 From ATH: {info['ath_change']:.2f}%\n"
                f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                f"🔄 Supply: {supply_str} / {max_str}",
                parse_mode="Markdown", reply_markup=info_coins_menu())
    elif mode == "multi":
        symbol = text.upper()
        prices = get_multi_price(symbol)
        if not prices:
            safe_send(cid, f"❌ *{symbol}* not found.", parse_mode="Markdown", reply_markup=multi_coins_menu())
        else:
            flags = {"usd": "🇺🇸", "eur": "🇪🇺", "gbp": "🇬🇧", "jpy": "🇯🇵", "cny": "🇨🇳", "aed": "🇦🇪", "try": "🇹🇷"}
            symbols_map = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cny": "¥", "aed": "د.إ", "try": "₺"}
            text_out = f"💱 *{symbol} Price*\n\n"
            for cur, flag in flags.items():
                p = prices.get(cur)
                if p:
                    text_out += f"{flag} {symbols_map[cur]}{p:,.4f}\n"
            safe_send(cid, text_out, parse_mode="Markdown", reply_markup=multi_coins_menu())
    elif mode == "scan":
        result = scan_ca(text)
        if not result:
            safe_send(cid, "❌ Contract not found or unsupported chain.\nSupports ETH, BSC, Solana.",
                      reply_markup=back_button())
        else:
            name = result.get("token_name", "Unknown")
            sym = result.get("token_symbol", "?")
            hp = result.get("is_honeypot", "?")
            mint = result.get("is_mintable", "?")
            proxy = result.get("is_proxy", "?")
            buy_tax = result.get("buy_tax", "?")
            sell_tax = result.get("sell_tax", "?")
            open_source = result.get("is_open_source", "?")
            holders = result.get("holder_count", "?")

            def flag(v):
                if v == "1": return "⚠️ Yes"
                if v == "0": return "✅ No"
                return "❓ Unknown"

            safe_send(cid,
                f"🛡 *CA Scan: {name} ({sym})*\n\n"
                f"🍯 Honeypot: {flag(hp)}\n"
                f"🖨 Mintable: {flag(mint)}\n"
                f"🔁 Proxy: {flag(proxy)}\n"
                f"📂 Open Source: {flag(open_source)}\n"
                f"💸 Buy Tax: {buy_tax}%\n"
                f"💸 Sell Tax: {sell_tax}%\n"
                f"👥 Holders: {holders}",
                parse_mode="Markdown", reply_markup=back_button())
    elif mode == "alert":
        parts = text.split()
        if len(parts) < 3 or parts[1] not in ['>', '<']:
            safe_send(cid, "❌ Wrong format.\nUse: `BTC > 70000`", parse_mode="Markdown", reply_markup=alerts_menu())
            return
        symbol = parts[0].upper()
        direction = parts[1]
        try:
            target = float(parts[2])
            global alert_id_counter
            aid = alert_id_counter
            alert_id_counter += 1
            alerts.append({
                "id": aid, "chat_id": cid,
                "coin": symbol, "target": target,
                "direction": direction, "active": True
            })
            save_alerts()
            label = "rises above" if direction == ">" else "drops below"
            safe_send(cid,
                f"🔔 Alert #{aid} set!\n*{symbol}* {label} *${target:,.2f}*",
                parse_mode="Markdown", reply_markup=alerts_menu())
        except:
            safe_send(cid, "❌ Invalid price value.", reply_markup=alerts_menu())

# ========== BOT START ==========
print("🚀 Persona upgraded bot running with message cleanup (max 3 messages)...")
bot.infinity_polling()
