#!/usr/bin/env python3
"""
Persona Bot – Full Features (Stable)
- Inline menus: price, info, gainers, losers, multi-currency, contract scanner
- Keeps last 5 messages (user + bot + inline buttons)
- Contract scanner (GoPlusLabs) with timeout
- No database, no alerts, no WebSocket
- Works on Termux and Railway
"""

import telebot
import requests
import threading
import time
import os
import sys
import re
import logging
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    print("❌ BOT_TOKEN not set")
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("PersonaBot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
os.makedirs("data", exist_ok=True)

# =========================
# REQUEST SESSION
# =========================
session = requests.Session()
session.headers.update({"User-Agent": "PersonaBot/5.0"})

# =========================
# HELPERS
# =========================
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

# =========================
# MESSAGE QUEUE – keep last 5 messages
# =========================
message_queues = {}
queue_lock = threading.RLock()
MAX_CHAT_MESSAGES = 5

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
            except Exception as e:
                log.debug(f"Could not delete message {oldest}: {e}")

def send_and_track(chat_id, text, markup=None):
    sent = safe_send(chat_id, text, markup)
    if sent:
        track_message(chat_id, sent.message_id)
    return sent

def clear_old_message(chat_id, msg_id):
    try:
        bot.delete_message(chat_id, msg_id)
    except:
        pass

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
    except:
        pass
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
            "market_cap": md.get("market_cap", {}).get("usd", 0),
            "volume": md.get("total_volume", {}).get("usd", 0),
            "supply": md.get("circulating_supply", 0)
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

# =========================
# CONTRACT SCANNER (GoPlusLabs)
# =========================
def scan_contract(address):
    if not address:
        return None, "Empty address"
    addr = re.sub(r"\s+", "", address)
    if len(addr) > 100:
        return None, "Address too long"
    addr_lower = addr.lower()
    # EVM (Ethereum/BSC)
    if re.match(r"^0x[a-f0-9]{40}$", addr_lower):
        for chain in [1, 56]:  # 1 = Ethereum, 56 = BSC
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
        return None, "Contract not found (GoPlusLabs)."
    else:
        # Solana (optional)
        sol_addr = re.sub(r"[^a-zA-Z0-9]", "", addr)
        if len(sol_addr) < 32 or len(sol_addr) > 44:
            return None, "Invalid Solana address."
        url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={sol_addr}"
        try:
            r = session.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                result = data.get("result", {}).get(sol_addr, {})
                if result:
                    return result, None
            return None, "Solana contract not found."
        except Exception as e:
            return None, f"Scanner error: {str(e)[:50]}"

# =========================
# INLINE MENUS
# =========================
def main_menu():
    text = "⚡ <b>PERSONA</b>\n\nFast crypto tools: prices, alerts, intel."
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("💰 Price check", callback_data="menu_price"),
           InlineKeyboardButton("🔎 Coin info", callback_data="menu_info"))
    kb.row(InlineKeyboardButton("📈 Gainers (top 10)", callback_data="menu_gainers"),
           InlineKeyboardButton("📉 Losers (top 10)", callback_data="menu_losers"))
    kb.row(InlineKeyboardButton("💱 Currencies", callback_data="menu_multi"),
           InlineKeyboardButton("🛡 Scan CA", callback_data="menu_scan"))
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
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return text, kb

def scan_menu():
    text = "🛡 <b>Contract Scanner</b>\n\nSend a contract address (EVM or Solana).\n\nUse /scan <address>"
    return text, back_button()

# =========================
# CALLBACK HANDLERS
# =========================
@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    text, kb = main_menu()
    send_and_track(cid, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_price")
def menu_price_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    text, kb = price_menu()
    send_and_track(cid, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_info")
def menu_info_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    text, kb = info_menu()
    send_and_track(cid, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_multi")
def menu_multi_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    text, kb = multi_menu()
    send_and_track(cid, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_gainers")
def menu_gainers_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    g, _ = get_top_movers(10)
    if not g:
        text = "❌ No gainers data available."
    else:
        text = "📈 <b>Top 10 Gainers (24h)</b>\n\n"
        for d in g:
            coin = d["symbol"].removesuffix("USDT")
            text += f"🟢 <b>{coin}</b>  ▲ {float(d['priceChangePercent']):.2f}%  —  {fmt_price(float(d['lastPrice']))}\n"
    send_and_track(cid, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_losers")
def menu_losers_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    _, l = get_top_movers(10)
    if not l:
        text = "❌ No losers data available."
    else:
        text = "📉 <b>Top 10 Losers (24h)</b>\n\n"
        for d in l:
            coin = d["symbol"].removesuffix("USDT")
            text += f"🔴 <b>{coin}</b>  ▼ {abs(float(d['priceChangePercent'])):.2f}%  —  {fmt_price(float(d['lastPrice']))}\n"
    send_and_track(cid, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_scan")
def menu_scan_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    text, kb = scan_menu()
    send_and_track(cid, text, kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("price_"))
def price_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    symbol = call.data.split("_")[1]
    price, change = get_price(symbol)
    if price is None:
        text = f"❌ Could not fetch price for {symbol}"
    else:
        text = f"💰 <b>{symbol}</b>\n{fmt_price(price)} ({change:+.2f}%)"
    send_and_track(cid, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("info_"))
def info_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    symbol = call.data.split("_")[1]
    info = get_coin_info(symbol)
    if not info:
        text = f"❌ No info found for {symbol}"
    else:
        text = (f"🔎 <b>{info['name']} ({info['symbol']})</b>\n"
                f"🏆 Rank: #{info['rank']}\n"
                f"💰 Price: {fmt_price(info['price'])}\n"
                f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
                f"📊 24h Volume: {fmt_price(info['volume'])}\n"
                f"💎 Circulating Supply: {info['supply']:,.0f}")
    send_and_track(cid, text, back_button())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("multi_"))
def multi_cb(call):
    cid = call.message.chat.id
    clear_old_message(cid, call.message.message_id)
    symbol = call.data.split("_")[1]
    prices = get_multi_prices(symbol)
    if not prices:
        text = f"❌ Failed to fetch multi‑currency data for {symbol}"
    else:
        order = [("usd","USD"),("eur","EUR"),("gbp","GBP"),("jpy","JPY"),("cny","CNY"),("aed","AED"),("try","TRY"),("inr","INR"),("krw","KRW"),("cad","CAD"),("aud","AUD")]
        text = f"💱 <b>{symbol} – Currencies</b>\n\n"
        for code, name in order:
            val = prices.get(code)
            if val is not None:
                text += f"{name}: {fmt_currency_value(code, val)}\n"
    send_and_track(cid, text, back_button())
    bot.answer_callback_query(call.id)

# =========================
# SCAN COMMAND (slash)
# =========================
@bot.message_handler(commands=["scan"])
def cmd_scan(m):
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
    risk_map = {"is_honeypot": "🍯 Honeypot", "is_mintable": "🖨 Mintable", "is_proxy": "🔀 Proxy", "is_blacklisted": "⛔ Blacklist"}
    flags = []
    for f, label in risk_map.items():
        if str(result.get(f, "")) == "1":
            flags.append(f"⚠️ {label}: YES")
    buy_tax = result.get("buy_tax")
    if buy_tax:
        try:
            pct = float(buy_tax) * 100
            if pct > 0:
                flags.append(f"⚠️ Buy tax: {pct:.1f}%")
        except:
            pass
    if flags:
        text += "<b>Risk flags:</b>\n" + "\n".join(flags) + "\n"
    else:
        text += "✅ No high‑risk flags detected.\n"
    text += "\n🔗 <a href='https://gopluslabs.io/'>GoPlusLabs</a>"
    send_and_track(m.chat.id, text, back_button())

# =========================
# OTHER SLASH COMMANDS (fallback)
# =========================
@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    track_message(m.chat.id, m.message_id)
    text, kb = main_menu()
    send_and_track(m.chat.id, text, kb)

@bot.message_handler(commands=["price"])
def cmd_price(m):
    track_message(m.chat.id, m.message_id)
    args = m.text.split()
    if len(args) < 2:
        send_and_track(m.chat.id, "Usage: /price <symbol>", back_button())
        return
    sym = args[1].upper()
    price, change = get_price(sym)
    if price is None:
        send_and_track(m.chat.id, f"❌ Could not fetch price for {sym}", back_button())
    else:
        send_and_track(m.chat.id, f"💰 {sym}: {fmt_price(price)} ({change:+.2f}%)", back_button())

@bot.message_handler(commands=["info"])
def cmd_info(m):
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
            f"🏦 Market Cap: {fmt_price(info['market_cap'])}\n"
            f"📊 24h Volume: {fmt_price(info['volume'])}\n"
            f"💎 Circulating Supply: {info['supply']:,.0f}")
    send_and_track(m.chat.id, text, back_button())

@bot.message_handler(commands=["gainers"])
def cmd_gainers(m):
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
def cmd_losers(m):
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
def cmd_multi(m):
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

# =========================
# SHUTDOWN HANDLER
# =========================
def stop(sig, frame):
    log.info("Shutting down...")
    try:
        bot.stop_polling()
    except:
        pass
    sys.exit(0)

import signal
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

# =========================
# MAIN – POLLING (NO CONFLICT)
# =========================
if __name__ == "__main__":
    # Remove webhook to avoid 409 conflict
    try:
        bot.remove_webhook()
        print("Webhook removed.")
    except Exception as e:
        print(f"Remove webhook failed: {e}")
    time.sleep(2)

    print("🚀 Bot started – full features (price, info, gainers, losers, multi-currency, contract scanner, keep last 5 messages)")
    bot.polling(none_stop=True, interval=0, timeout=20)
