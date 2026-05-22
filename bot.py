import telebot
import requests
import threading
import time
import json
import os
import websocket

from functools import wraps
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8619003788:AAEszjzsxeKH8dSm8FPtqkPJxCG9Dw3Tne4")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

ALERTS_FILE = "alerts.json"
CACHE_TTL = 10
MAX_HISTORY = 3                 # Keep only last 3 messages total (menus + results)
COOLDOWN_SECONDS = 2

PRICE_CACHE = {}
waiting_for = {}
user_msg_history = {}           # Stores message IDs for each user
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
        with open(ALERTS_FILE, "w") as f:
            json.dump(alerts, f, indent=2)
    except Exception as e:
        print(f"[SAVE ALERTS ERROR] {e}")

# ================= MESSAGE CLEANUP (keep only last 3 messages) =================
def cleanup_messages(chat_id):
    """Delete oldest messages if total > MAX_HISTORY."""
    if chat_id not in user_msg_history:
        return
    history = user_msg_history[chat_id]
    while len(history) > MAX_HISTORY:
        old_id = history.pop(0)
        try:
            bot.delete_message(chat_id, old_id)
        except Exception:
            pass

def send_and_track(chat_id, text, reply_markup=None):
    """Send a message, track its ID, and delete old ones."""
    sent = bot.send_message(chat_id, text, reply_markup=reply_markup)
    if chat_id not in user_msg_history:
        user_msg_history[chat_id] = []
    user_msg_history[chat_id].append(sent.message_id)
    cleanup_messages(chat_id)
    return sent

# ================= UTILITIES =================
def cooldown_ok(user_id):
    now = time.time()
    if user_id in cooldowns:
        if now - cooldowns[user_id] < COOLDOWN_SECONDS:
            return False
    cooldowns[user_id] = now
    return True

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

@retry()
def get_coin_info(symbol):
    try:
        search_url = f"https://api.coingecko.com/api/v3/search?query={symbol.lower()}"
        r = requests.get(search_url, timeout=10)
        if r.status_code != 200:
            return None
        coins = r.json().get("coins", [])
        if not coins:
            return None
        coin_id = coins[0]["id"]
    except:
        return None

    try:
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        params = {"localization": "false", "tickers": "false", "community_data": "false"}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
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
            "max_supply": md.get("max_supply"),
            "market_cap": md["market_cap"].get("usd", 0),
            "volume": md["total_volume"].get("usd", 0),
        }
    except:
        return None

@retry()
def get_multi_price(symbol):
    try:
        search_url = f"https://api.coingecko.com/api/v3/search?query={symbol.lower()}"
        r = requests.get(search_url, timeout=10)
        if r.status_code != 200:
            return None
        coins = r.json().get("coins", [])
        if not coins:
            return None
        coin_id = coins[0]["id"]
    except:
        return None

    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": coin_id,
            "vs_currencies": "usd,eur,gbp,jpy,cny,aed,try",
            "include_24hr_change": "true"
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get(coin_id)
    except:
        return None

@retry()
def scan_ca(address):
    try:
        if address.startswith("0x") and len(address) == 42:
            url = f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={address}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                result = r.json().get("result", {}).get(address.lower(), {})
                if result:
                    return result
            url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={address}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return r.json().get("result", {}).get(address.lower(), {})
        else:
            url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return r.json().get("result", {}).get(address, {})
    except:
        pass
    return None

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
                        # Alert messages are also tracked and cleaned (but they are important)
                        send_and_track(
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

# ================= KEYBOARDS =================
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
        InlineKeyboardButton("🔎 Coin Info", callback_data="menu_info"),
        InlineKeyboardButton("💱 Multi Price", callback_data="menu_multi")
    )
    kb.row(
        InlineKeyboardButton("🛡 Scan CA", callback_data="menu_scan"),
        InlineKeyboardButton("📋 My Alerts", callback_data="list_alerts")
    )
    return kb

def price_menu():
    kb = InlineKeyboardMarkup()
    coins = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "TON", "AVAX", "ARB", "ADA", "DOT", "LINK", "MATIC", "UNI", "ATOM", "NEAR", "APT", "SUI", "TRX", "SHIB", "LTC", "OP", "INJ", "TIA"]
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
    kb.row(
        InlineKeyboardButton("BTC > 100k", callback_data="setalert_BTC_>_100000"),
        InlineKeyboardButton("BTC < 80k", callback_data="setalert_BTC_<_80000")
    )
    kb.row(
        InlineKeyboardButton("ETH > 4k", callback_data="setalert_ETH_>_4000"),
        InlineKeyboardButton("ETH < 2k", callback_data="setalert_ETH_<_2000")
    )
    kb.row(
        InlineKeyboardButton("SOL > 200", callback_data="setalert_SOL_>_200"),
        InlineKeyboardButton("SOL < 100", callback_data="setalert_SOL_<_100")
    )
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
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_info"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def multi_coins_menu():
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
    kb.row(InlineKeyboardButton("🔍 Search any coin", callback_data="search_multi"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# ================= COMMAND HANDLER =================
@bot.message_handler(commands=["start", "help"])
def start(msg):
    send_and_track(msg.chat.id, "🤖 *Persona* — your crypto assistant\n\nChoose an option:", reply_markup=main_menu())

# ================= CALLBACK HANDLER =================
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global alert_id_counter, ws_restart_required
    cid = call.message.chat.id
    data = call.data

    if not cooldown_ok(cid):
        bot.answer_callback_query(call.id, "⏳ Slow down")
        return

    try:
        # ---- Back to main menu (send new menu) ----
        if data == "back_main":
            waiting_for.pop(cid, None)
            send_and_track(cid, "🤖 *Persona* — your crypto assistant\n\nChoose an option:", reply_markup=main_menu())
            bot.answer_callback_query(call.id)

        # ---- Price menu ----
        elif data == "menu_price":
            send_and_track(cid, "💰 *Select a coin or search:*", reply_markup=price_menu())
            bot.answer_callback_query(call.id)

        elif data == "search_coin":
            waiting_for[cid] = "price"
            send_and_track(cid, "🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`, `SEI`", reply_markup=back_button())
            bot.answer_callback_query(call.id)

        # ---- Alerts menu ----
        elif data == "menu_alerts":
            send_and_track(cid, "🔔 *Quick Alerts* — tap to set or create custom:", reply_markup=alerts_menu())
            bot.answer_callback_query(call.id)

        elif data == "custom_alert":
            waiting_for[cid] = "alert"
            send_and_track(cid, "✏️ *Type your custom alert:*\nFormat: `COIN > price` or `COIN < price`\n\nExamples:\n`BTC > 95000`\n`PEPE < 0.00001`", reply_markup=back_button())
            bot.answer_callback_query(call.id)

        # ---- Set alert (preset) ----
        elif data.startswith("setalert_"):
            parts = data.split("_")
            symbol = parts[1]
            direction = parts[2]
            target = float(parts[3])
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
            send_and_track(cid, f"✅ Alert #{current_id} set!\n*{symbol}* {direction} *${target:,.2f}*", reply_markup=alerts_menu())
            bot.answer_callback_query(call.id, "✅ Alert set!")

        # ---- List alerts ----
        elif data == "list_alerts":
            active = [a for a in alerts if a["chat_id"] == cid and a["active"]]
            if not active:
                send_and_track(cid, "📋 No active alerts.\n\nSet one below:", reply_markup=alerts_menu())
                bot.answer_callback_query(call.id, "No active alerts.")
                return
            text = "📋 *Active Alerts:*\n\n"
            kb = InlineKeyboardMarkup()
            for a in active:
                label = "▲" if a['direction'] == '>' else "▼"
                text += f"#{a['id']} — {a['coin']} {label} ${a['target']:,.2f}\n"
                kb.row(InlineKeyboardButton(f"❌ Cancel #{a['id']}", callback_data=f"cancel_{a['id']}"))
            kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            send_and_track(cid, text, reply_markup=kb)
            bot.answer_callback_query(call.id)

        # ---- Cancel alert ----
        elif data.startswith("cancel_"):
            aid = int(data.split("_")[1])
            with lock:
                for a in alerts:
                    if a['id'] == aid and a['chat_id'] == cid:
                        a['active'] = False
                        save_alerts()
                        ws_restart_required = True
                        bot.answer_callback_query(call.id, f"✅ Alert #{aid} cancelled.")
                        break
            # Refresh list
            active = [a for a in alerts if a['chat_id'] == cid and a['active']]
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

        # ---- Price button (send result) ----
        elif data.startswith("price_"):
            symbol = data.split("_")[1]
            bot.answer_callback_query(call.id, f"Fetching {symbol}...")
            price, change = get_price(symbol)
            if price is None:
                send_and_track(cid, f"❌ Could not fetch *{symbol}*.", reply_markup=price_menu())
            else:
                arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
                send_and_track(cid, f"*{symbol}*\n💵 ${price:,.4f}\n{arrow} {abs(change):.2f}% (24h)", reply_markup=price_menu())

        # ---- Gainers / Losers ----
        elif data == "gainers":
            bot.answer_callback_query(call.id, "Fetching top gainers...")
            g, _ = get_top_movers()
            if not g:
                send_and_track(cid, "❌ Failed to fetch.", reply_markup=back_button())
            else:
                text = "🚀 *Top 5 Gainers (24h)*\n\n"
                for d in g:
                    coin = d["symbol"].replace("USDT", "")
                    text += f"🟢 *{coin}* — ${float(d['lastPrice']):,.4f} ▲ {float(d['priceChangePercent']):.2f}%\n"
                send_and_track(cid, text, reply_markup=back_button())

        elif data == "losers":
            bot.answer_callback_query(call.id, "Fetching top losers...")
            _, l = get_top_movers()
            if not l:
                send_and_track(cid, "❌ Failed to fetch.", reply_markup=back_button())
            else:
                text = "📉 *Top 5 Losers (24h)*\n\n"
                for d in l:
                    coin = d["symbol"].replace("USDT", "")
                    text += f"🔴 *{coin}* — ${float(d['lastPrice']):,.4f} ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
                send_and_track(cid, text, reply_markup=back_button())

        # ---- Coin Info menu and button ----
        elif data == "menu_info":
            send_and_track(cid, "🔎 *Coin Info — Select or search:*", reply_markup=info_coins_menu())
            bot.answer_callback_query(call.id)

        elif data == "search_info":
            waiting_for[cid] = "info"
            send_and_track(cid, "🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`", reply_markup=back_button())
            bot.answer_callback_query(call.id)

        elif data.startswith("info_"):
            symbol = data.split("_")[1]
            bot.answer_callback_query(call.id, f"Fetching {symbol} info...")
            info = get_coin_info(symbol)
            if not info:
                send_and_track(cid, f"❌ Couldn't fetch info for *{symbol}*.", reply_markup=info_coins_menu())
            else:
                supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
                max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
                send_and_track(cid,
                    f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                    f"🏆 Rank: #{info['rank']}\n"
                    f"💵 Price: ${info['price']:,.4f}\n"
                    f"📈 ATH: ${info['ath']:,.4f} ({info['ath_date']})\n"
                    f"📉 From ATH: {info['ath_change']:.2f}%\n"
                    f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                    f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                    f"🔄 Supply: {supply_str} / {max_str}",
                    reply_markup=info_coins_menu()
                )

        # ---- Multi Price menu and button ----
        elif data == "menu_multi":
            send_and_track(cid, "💱 *Multi-Currency Price — Select or search:*", reply_markup=multi_coins_menu())
            bot.answer_callback_query(call.id)

        elif data == "search_multi":
            waiting_for[cid] = "multi"
            send_and_track(cid, "🔍 *Type any coin symbol:*\nExample: `BTC`, `ETH`", reply_markup=back_button())
            bot.answer_callback_query(call.id)

        elif data.startswith("multi_"):
            symbol = data.split("_")[1]
            bot.answer_callback_query(call.id, f"Fetching {symbol} prices...")
            prices = get_multi_price(symbol)
            if not prices:
                send_and_track(cid, f"❌ Couldn't fetch *{symbol}*.", reply_markup=multi_coins_menu())
            else:
                flags = {"usd": "🇺🇸 $", "eur": "🇪🇺 €", "gbp": "🇬🇧 £", "jpy": "🇯🇵 ¥", "cny": "🇨🇳 ¥", "aed": "🇦🇪 د.إ", "try": "🇹🇷 ₺"}
                text = f"💱 *{symbol} Price*\n\n"
                for cur, flag in flags.items():
                    p = prices.get(cur)
                    if p:
                        text += f"{flag} {p:,.4f}\n"
                send_and_track(cid, text, reply_markup=multi_coins_menu())

        # ---- Scan CA ----
        elif data == "menu_scan":
            waiting_for[cid] = "scan"
            send_and_track(cid, "🛡 *CA Scanner*\n\nPaste a contract address:\n✅ Supports ETH, BSC, Solana\n\nExample:\n`0x1234...abcd`", reply_markup=back_button())
            bot.answer_callback_query(call.id)

    except Exception as e:
        print(f"[CALLBACK ERROR] {e}")

# ================= TEXT HANDLER (user typed input) =================
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
            symbol = text.upper()
            price, change = get_price(symbol)
            if price is None:
                send_and_track(cid, f"❌ Could not fetch *{symbol}*.", reply_markup=price_menu())
            else:
                arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
                send_and_track(cid, f"*{symbol}*\n💵 ${price:,.4f}\n{arrow} {abs(change):.2f}% (24h)", reply_markup=price_menu())

        elif mode == "info":
            symbol = text.upper()
            info = get_coin_info(symbol)
            if not info:
                send_and_track(cid, f"❌ *{symbol}* not found.", reply_markup=info_coins_menu())
            else:
                supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
                max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
                send_and_track(cid,
                    f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                    f"🏆 Rank: #{info['rank']}\n"
                    f"💵 Price: ${info['price']:,.4f}\n"
                    f"📈 ATH: ${info['ath']:,.4f} ({info['ath_date']})\n"
                    f"📉 From ATH: {info['ath_change']:.2f}%\n"
                    f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                    f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                    f"🔄 Supply: {supply_str} / {max_str}",
                    reply_markup=info_coins_menu()
                )

        elif mode == "multi":
            symbol = text.upper()
            prices = get_multi_price(symbol)
            if not prices:
                send_and_track(cid, f"❌ *{symbol}* not found.", reply_markup=multi_coins_menu())
            else:
                flags = {"usd": "🇺🇸 $", "eur": "🇪🇺 €", "gbp": "🇬🇧 £", "jpy": "🇯🇵 ¥", "cny": "🇨🇳 ¥", "aed": "🇦🇪 د.إ", "try": "🇹🇷 ₺"}
                text_out = f"💱 *{symbol} Price*\n\n"
                for cur, flag in flags.items():
                    p = prices.get(cur)
                    if p:
                        text_out += f"{flag} {p:,.4f}\n"
                send_and_track(cid, text_out, reply_markup=multi_coins_menu())

        elif mode == "scan":
            result = scan_ca(text)
            if not result:
                send_and_track(cid, "❌ Contract not found or unsupported chain.\nSupports ETH, BSC, Solana.", reply_markup=back_button())
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

                send_and_track(cid,
                    f"🛡 *CA Scan: {name} ({sym})*\n\n"
                    f"🍯 Honeypot: {flag(hp)}\n"
                    f"🖨 Mintable: {flag(mint)}\n"
                    f"🔁 Proxy: {flag(proxy)}\n"
                    f"📂 Open Source: {flag(open_source)}\n"
                    f"💸 Buy Tax: {buy_tax}%\n"
                    f"💸 Sell Tax: {sell_tax}%\n"
                    f"👥 Holders: {holders}",
                    reply_markup=back_button()
                )

        elif mode == "alert":
            parts = text.split()
            if len(parts) != 3 or parts[1] not in ['>', '<']:
                send_and_track(cid, "❌ Wrong format.\nUse: `BTC > 70000`", reply_markup=alerts_menu())
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
            send_and_track(cid, f"🔔 Alert #{current_id} set!\n*{symbol}* {direction} *${target:,.2f}*", reply_markup=alerts_menu())

    except Exception as e:
        print(f"[TEXT ERROR] {e}")

# ================= POLLING =================
print("🚀 Persona — Only last 3 messages kept, no editing, each menu has back button")
while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        print(f"[POLLING ERROR] {e}")
        time.sleep(5)
