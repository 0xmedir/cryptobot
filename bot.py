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
PRICE_CACHE = {}  # symbol -> {"price": float, "timestamp": float}
CACHE_TTL = 10  # seconds

# ========== PERSISTENT ALERTS ==========
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
    """Dynamic coin search via CoinGecko"""
    # First, find coin ID
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

    # Get details
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
    # Dynamic coin ID
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
            # Try ETH
            url = f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={address}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                result = data.get("result", {}).get(address.lower(), {})
                if result:
                    return result
            # Fallback BSC
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
    """Called when a WebSocket message is received."""
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

# Run WebSocket in a background thread
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

# ========== MESSAGE STREAMING ==========
def stream_price_update(chat_id, symbol, original_message_id=None):
    """Sends a 'Fetching...' message then edits it with the price."""
    sent = bot.send_message(chat_id, f"⏳ Fetching {symbol} price...", parse_mode="Markdown")
    price, change = get_price(symbol)
    if price is None:
        bot.edit_message_text(f"❌ Could not fetch {symbol}. Please try again.", chat_id, sent.message_id)
    else:
        arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
        bot.edit_message_text(
            f"*{symbol}*\n💵 ${price:,.4f}\n{arrow} {abs(change):.2f}% (24h)",
            chat_id,
            sent.message_id,
            parse_mode="Markdown",
            reply_markup=price_menu()
        )

# ========== HANDLERS ==========
@bot.message_handler(commands=['start', 'help'])
def start(msg):
    bot.send_message(msg.chat.id,
                     "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
                     parse_mode="Markdown",
                     reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data
    cid = call.message.chat.id
    mid = call.message.message_id

    if data == "back_main":
        bot.edit_message_text(
            "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
            cid, mid,
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        bot.answer_callback_query(call.id)

    elif data == "menu_price":
        bot.edit_message_text("💰 *Select a coin or search:*", cid, mid,
                              parse_mode="Markdown", reply_markup=price_menu())
        bot.answer_callback_query(call.id)

    elif data == "search_coin":
        bot.edit_message_text(
            "🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`, `SEI`",
            cid, mid,
            parse_mode="Markdown",
            reply_markup=back_button()
        )
        bot.answer_callback_query(call.id)
        # Set a temporary state: we'll use a dict to know user is waiting for symbol
        waiting_for[cid] = "price"

    elif data.startswith("price_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol}...")
        # Use streaming
        stream_price_update(cid, symbol)

    elif data == "menu_alerts":
        bot.edit_message_text("🔔 *Quick Alerts* — tap to set or create custom:",
                              cid, mid, parse_mode="Markdown", reply_markup=alerts_menu())
        bot.answer_callback_query(call.id)

    elif data == "custom_alert":
        bot.edit_message_text(
            "✏️ *Type your custom alert:*\nFormat: `COIN > price` or `COIN < price`\n\nExamples:\n`BTC > 95000`\n`PEPE < 0.00001`",
            cid, mid, parse_mode="Markdown", reply_markup=back_button()
        )
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
        bot.edit_message_text(
            f"🔔 Alert #{aid} set!\n*{symbol}* {label} *${target:,.2f}*",
            cid, mid, parse_mode="Markdown", reply_markup=alerts_menu()
        )

    elif data == "list_alerts":
        user_alerts = [a for a in alerts if a['chat_id'] == cid and a['active']]
        if not user_alerts:
            bot.answer_callback_query(call.id, "No active alerts.")
            bot.edit_message_text("📋 No active alerts.\n\nSet one below:",
                                  cid, mid, reply_markup=alerts_menu())
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
        bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=kb)
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
            bot.edit_message_text("📋 No more active alerts.", cid, mid, reply_markup=back_button())
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
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=kb)

    elif data == "gainers":
        bot.answer_callback_query(call.id, "Fetching top gainers...")
        g, _ = get_top_movers()
        if not g:
            bot.edit_message_text("❌ Failed to fetch.", cid, mid, reply_markup=back_button())
        else:
            text = "🚀 *Top 5 Gainers (24h)*\n\n"
            for d in g:
                coin = d["symbol"].replace("USDT", "")
                text += f"🟢 *{coin}* — ${float(d['lastPrice']):,.4f} ▲ {float(d['priceChangePercent']):.2f}%\n"
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=back_button())

    elif data == "losers":
        bot.answer_callback_query(call.id, "Fetching top losers...")
        _, l = get_top_movers()
        if not l:
            bot.edit_message_text("❌ Failed to fetch.", cid, mid, reply_markup=back_button())
        else:
            text = "📉 *Top 5 Losers (24h)*\n\n"
            for d in l:
                coin = d["symbol"].replace("USDT", "")
                text += f"🔴 *{coin}* — ${float(d['lastPrice']):,.4f} ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=back_button())

    elif data == "menu_info":
        bot.edit_message_text("🔎 *Coin Info — Select or search:*", cid, mid,
                              parse_mode="Markdown", reply_markup=info_coins_menu())
        bot.answer_callback_query(call.id)

    elif data == "search_info":
        bot.edit_message_text("🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`",
                              cid, mid, parse_mode="Markdown", reply_markup=back_button())
        waiting_for[cid] = "info"
        bot.answer_callback_query(call.id)

    elif data.startswith("info_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol} info...")
        info = get_coin_info(symbol)
        if not info:
            bot.edit_message_text(f"❌ Couldn't fetch info for *{symbol}*.", cid, mid,
                                  parse_mode="Markdown", reply_markup=info_coins_menu())
        else:
            supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
            max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
            bot.edit_message_text(
                f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                f"🏆 Rank: #{info['rank']}\n"
                f"💵 Price: ${info['price']:,.4f}\n"
                f"📈 ATH: ${info['ath']:,.4f} ({info['ath_date']})\n"
                f"📉 From ATH: {info['ath_change']:.2f}%\n"
                f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                f"🔄 Supply: {supply_str} / {max_str}",
                cid, mid, parse_mode="Markdown", reply_markup=info_coins_menu()
            )

    elif data == "menu_multi":
        bot.edit_message_text("💱 *Multi-Currency Price — Select or search:*", cid, mid,
                              parse_mode="Markdown", reply_markup=multi_coins_menu())
        bot.answer_callback_query(call.id)

    elif data == "search_multi":
        bot.edit_message_text("🔍 *Type any coin symbol:*\nExample: `BTC`, `ETH`",
                              cid, mid, parse_mode="Markdown", reply_markup=back_button())
        waiting_for[cid] = "multi"
        bot.answer_callback_query(call.id)

    elif data.startswith("multi_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol} prices...")
        prices = get_multi_price(symbol)
        if not prices:
            bot.edit_message_text(f"❌ Couldn't fetch *{symbol}*.", cid, mid,
                                  parse_mode="Markdown", reply_markup=multi_coins_menu())
        else:
            flags = {"usd": "🇺🇸", "eur": "🇪🇺", "gbp": "🇬🇧", "jpy": "🇯🇵", "cny": "🇨🇳", "aed": "🇦🇪", "try": "🇹🇷"}
            symbols_map = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cny": "¥", "aed": "د.إ", "try": "₺"}
            text = f"💱 *{symbol} Price*\n\n"
            for cur, flag in flags.items():
                p = prices.get(cur)
                if p:
                    text += f"{flag} {symbols_map[cur]}{p:,.4f}\n"
            bot.edit_message_text(text, cid, mid, parse_mode="Markdown", reply_markup=multi_coins_menu())

    elif data == "menu_scan":
        bot.edit_message_text(
            "🛡 *CA Scanner*\n\nPaste a contract address:\n✅ Supports ETH, BSC, Solana\n\nExample:\n`0x1234...abcd`",
            cid, mid, parse_mode="Markdown", reply_markup=back_button()
        )
        waiting_for[cid] = "scan"
        bot.answer_callback_query(call.id)

# ========== TEXT HANDLER (for free text input) ==========
waiting_for = {}

@bot.message_handler(func=lambda msg: True)
def handle_text(msg):
    cid = msg.chat.id
    text = msg.text.strip()
    if cid not in waiting_for:
        return  # ignore
    mode = waiting_for.pop(cid)

    if mode == "price":
        stream_price_update(cid, text.upper())
    elif mode == "info":
        symbol = text.upper()
        info = get_coin_info(symbol)
        if not info:
            bot.send_message(cid, f"❌ *{symbol}* not found.", parse_mode="Markdown", reply_markup=info_coins_menu())
        else:
            supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
            max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
            bot.send_message(cid,
                f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                f"🏆 Rank: #{info['rank']}\n"
                f"💵 Price: ${info['price']:,.4f}\n"
                f"📈 ATH: ${info['ath']:,.4f} ({info['ath_date']})\n"
                f"📉 From ATH: {info['ath_change']:.2f}%\n"
                f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                f"🔄 Supply: {supply_str} / {max_str}",
                parse_mode="Markdown", reply_markup=info_coins_menu()
            )
    elif mode == "multi":
        symbol = text.upper()
        prices = get_multi_price(symbol)
        if not prices:
            bot.send_message(cid, f"❌ *{symbol}* not found.", parse_mode="Markdown", reply_markup=multi_coins_menu())
        else:
            flags = {"usd": "🇺🇸", "eur": "🇪🇺", "gbp": "🇬🇧", "jpy": "🇯🇵", "cny": "🇨🇳", "aed": "🇦🇪", "try": "🇹🇷"}
            symbols_map = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cny": "¥", "aed": "د.إ", "try": "₺"}
            text_out = f"💱 *{symbol} Price*\n\n"
            for cur, flag in flags.items():
                p = prices.get(cur)
                if p:
                    text_out += f"{flag} {symbols_map[cur]}{p:,.4f}\n"
            bot.send_message(cid, text_out, parse_mode="Markdown", reply_markup=multi_coins_menu())
    elif mode == "scan":
        result = scan_ca(text)
        if not result:
            bot.send_message(cid, "❌ Contract not found or unsupported chain.\nSupports ETH, BSC, Solana.",
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

            bot.send_message(cid,
                f"🛡 *CA Scan: {name} ({sym})*\n\n"
                f"🍯 Honeypot: {flag(hp)}\n"
                f"🖨 Mintable: {flag(mint)}\n"
                f"🔁 Proxy: {flag(proxy)}\n"
                f"📂 Open Source: {flag(open_source)}\n"
                f"💸 Buy Tax: {buy_tax}%\n"
                f"💸 Sell Tax: {sell_tax}%\n"
                f"👥 Holders: {holders}",
                parse_mode="Markdown", reply_markup=back_button()
            )
    elif mode == "alert":
        parts = text.split()
        if len(parts) < 3 or parts[1] not in ['>', '<']:
            bot.send_message(cid, "❌ Wrong format.\nUse: `BTC > 70000`", parse_mode="Markdown", reply_markup=alerts_menu())
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
            bot.send_message(cid,
                f"🔔 Alert #{aid} set!\n*{symbol}* {label} *${target:,.2f}*",
                parse_mode="Markdown", reply_markup=alerts_menu()
            )
        except:
            bot.send_message(cid, "❌ Invalid price value.", reply_markup=alerts_menu())

# ========== BOT START ==========
print("🚀 Persona upgraded bot running...")
bot.infinity_polling()
