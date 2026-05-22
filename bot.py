import telebot
import requests
import threading
import time
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import os
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8619003788:AAEszjzsxeKH8dSm8FPtqkPJxCG9Dw3Tne4")

alerts = []
alert_id_counter = [1]
waiting_for = {}
main_msg = {}

# ── API ───────────────────────────────────────────────
def get_price(symbol):
    pair = symbol.upper() + "USDT"
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/24hr?symbol={pair}",
            timeout=10
        )
        data = r.json()
        if "lastPrice" in data:
            return float(data["lastPrice"]), float(data["priceChangePercent"])
    except:
        pass
    return None, None

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

def get_coin_info(symbol):
    cg_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
        "SOL": "solana", "XRP": "ripple", "DOGE": "dogecoin",
        "TON": "the-open-network", "AVAX": "avalanche-2", "ARB": "arbitrum",
        "ADA": "cardano", "DOT": "polkadot", "LINK": "chainlink",
        "MATIC": "matic-network", "UNI": "uniswap", "ATOM": "cosmos",
        "NEAR": "near", "APT": "aptos", "SUI": "sui",
        "TRX": "tron", "SHIB": "shiba-inu", "LTC": "litecoin",
        "OP": "optimism", "INJ": "injective-protocol", "TIA": "celestia",
    }
    coin_id = cg_map.get(symbol.upper(), symbol.lower())
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            params={"localization": "false", "tickers": "false", "community_data": "false"},
            timeout=10
        )
        data = r.json()
        if "id" in data:
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

def get_multi_price(symbol):
    cg_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
        "SOL": "solana", "XRP": "ripple", "DOGE": "dogecoin",
        "TON": "the-open-network", "AVAX": "avalanche-2", "ARB": "arbitrum",
        "ADA": "cardano", "DOT": "polkadot", "LINK": "chainlink",
        "MATIC": "matic-network", "UNI": "uniswap", "ATOM": "cosmos",
        "NEAR": "near", "APT": "aptos", "SUI": "sui",
        "TRX": "tron", "SHIB": "shiba-inu", "LTC": "litecoin",
        "OP": "optimism", "INJ": "injective-protocol", "TIA": "celestia",
    }
    coin_id = cg_map.get(symbol.upper(), symbol.lower())
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": "usd,eur,gbp,jpy,cny,aed,try",
                "include_24hr_change": "true"
            },
            timeout=10
        )
        data = r.json()
        if coin_id in data:
            return data[coin_id]
    except:
        pass
    return None

def scan_ca(address):
    try:
        # Detect chain by address format
        if address.startswith("0x") and len(address) == 42:
            # Try ETH first
            r = requests.get(
                f"https://api.gopluslabs.io/api/v1/token_security/1?contract_addresses={address}",
                timeout=10
            )
            data = r.json()
            result = data.get("result", {}).get(address.lower(), {})
            if not result:
                # Try BSC
                r = requests.get(
                    f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={address}",
                    timeout=10
                )
                data = r.json()
                result = data.get("result", {}).get(address.lower(), {})
            return result
        else:
            # Solana
            r = requests.get(
                f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={address}",
                timeout=10
            )
            data = r.json()
            return data.get("result", {}).get(address, {})
    except:
        pass
    return None

# ── Keyboards ─────────────────────────────────────────
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
    kb.row(
        InlineKeyboardButton("BTC", callback_data="price_BTC"),
        InlineKeyboardButton("ETH", callback_data="price_ETH"),
        InlineKeyboardButton("BNB", callback_data="price_BNB")
    )
    kb.row(
        InlineKeyboardButton("SOL", callback_data="price_SOL"),
        InlineKeyboardButton("XRP", callback_data="price_XRP"),
        InlineKeyboardButton("DOGE", callback_data="price_DOGE")
    )
    kb.row(
        InlineKeyboardButton("TON", callback_data="price_TON"),
        InlineKeyboardButton("AVAX", callback_data="price_AVAX"),
        InlineKeyboardButton("ARB", callback_data="price_ARB")
    )
    kb.row(
        InlineKeyboardButton("ADA", callback_data="price_ADA"),
        InlineKeyboardButton("DOT", callback_data="price_DOT"),
        InlineKeyboardButton("LINK", callback_data="price_LINK")
    )
    kb.row(
        InlineKeyboardButton("MATIC", callback_data="price_MATIC"),
        InlineKeyboardButton("UNI", callback_data="price_UNI"),
        InlineKeyboardButton("ATOM", callback_data="price_ATOM")
    )
    kb.row(
        InlineKeyboardButton("NEAR", callback_data="price_NEAR"),
        InlineKeyboardButton("APT", callback_data="price_APT"),
        InlineKeyboardButton("SUI", callback_data="price_SUI")
    )
    kb.row(
        InlineKeyboardButton("TRX", callback_data="price_TRX"),
        InlineKeyboardButton("SHIB", callback_data="price_SHIB"),
        InlineKeyboardButton("LTC", callback_data="price_LTC")
    )
    kb.row(
        InlineKeyboardButton("OP", callback_data="price_OP"),
        InlineKeyboardButton("INJ", callback_data="price_INJ"),
        InlineKeyboardButton("TIA", callback_data="price_TIA")
    )
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
    kb.row(
        InlineKeyboardButton("BNB > 700", callback_data="setalert_BNB_>_700"),
        InlineKeyboardButton("BNB < 500", callback_data="setalert_BNB_<_500")
    )
    kb.row(
        InlineKeyboardButton("XRP > 3", callback_data="setalert_XRP_>_3"),
        InlineKeyboardButton("XRP < 1", callback_data="setalert_XRP_<_1")
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

# ── Core ──────────────────────────────────────────────
def delete_after(cid, mid, delay=5):
    def _delete():
        time.sleep(delay)
        try:
            bot.delete_message(cid, mid)
        except:
            pass
    threading.Thread(target=_delete, daemon=True).start()

def update_main(cid, text, markup):
    old_mid = main_msg.get(cid)
    sent = bot.send_message(cid, text, parse_mode="Markdown", reply_markup=markup)
    main_msg[cid] = sent.message_id
    if old_mid:
        delete_after(cid, old_mid, delay=5)

# ── /start ────────────────────────────────────────────
@bot.message_handler(commands=['start', 'help'])
def start(msg):
    waiting_for.pop(msg.chat.id, None)
    try:
        bot.delete_message(msg.chat.id, msg.message_id)
    except:
        pass
    update_main(msg.chat.id,
        "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
        main_menu()
    )

# ── Free text handler ─────────────────────────────────
@bot.message_handler(func=lambda msg: True)
def handle_text(msg):
    cid = msg.chat.id
    text = msg.text.strip()
    try:
        bot.delete_message(cid, msg.message_id)
    except:
        pass

    if cid in waiting_for:
        mode = waiting_for.pop(cid)

        if mode == "price":
            symbol = text.upper()
            p, change = get_price(symbol)
            if p is None:
                update_main(cid, f"❌ *{symbol}* not found on Binance.", price_menu())
            else:
                arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
                update_main(cid,
                    f"*{symbol}*\n💵 ${p:,.4f}\n{arrow} {abs(change):.2f}% (24h)",
                    price_menu()
                )

        elif mode == "info":
            symbol = text.upper()
            info = get_coin_info(symbol)
            if not info:
                update_main(cid, f"❌ *{symbol}* not found.", info_coins_menu())
            else:
                supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
                max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
                update_main(cid,
                    f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                    f"🏆 Rank: #{info['rank']}\n"
                    f"💵 Price: ${info['price']:,.4f}\n"
                    f"📈 ATH: ${info['ath']:,.4f} ({info['ath_date']})\n"
                    f"📉 From ATH: {info['ath_change']:.2f}%\n"
                    f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                    f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                    f"🔄 Supply: {supply_str} / {max_str}",
                    info_coins_menu()
                )

        elif mode == "multi":
            symbol = text.upper()
            prices = get_multi_price(symbol)
            if not prices:
                update_main(cid, f"❌ *{symbol}* not found.", multi_coins_menu())
            else:
                flags = {"usd": "🇺🇸", "eur": "🇪🇺", "gbp": "🇬🇧", "jpy": "🇯🇵", "cny": "🇨🇳", "aed": "🇦🇪", "try": "🇹🇷"}
                symbols = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cny": "¥", "aed": "د.إ", "try": "₺"}
                text_out = f"💱 *{symbol} Price*\n\n"
                for cur, flag in flags.items():
                    p = prices.get(cur)
                    if p:
                        text_out += f"{flag} {symbols[cur]}{p:,.4f}\n"
                update_main(cid, text_out, multi_coins_menu())

        elif mode == "scan":
            result = scan_ca(text)
            if not result:
                update_main(cid,
                    "❌ Contract not found or unsupported chain.\nSupports ETH, BSC, Solana.",
                    back_button()
                )
            else:
                name = result.get("token_name", "Unknown")
                symbol = result.get("token_symbol", "?")
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

                update_main(cid,
                    f"🛡 *CA Scan: {name} ({symbol})*\n\n"
                    f"🍯 Honeypot: {flag(hp)}\n"
                    f"🖨 Mintable: {flag(mint)}\n"
                    f"🔁 Proxy: {flag(proxy)}\n"
                    f"📂 Open Source: {flag(open_source)}\n"
                    f"💸 Buy Tax: {buy_tax}%\n"
                    f"💸 Sell Tax: {sell_tax}%\n"
                    f"👥 Holders: {holders}",
                    back_button()
                )

        elif mode == "alert":
            parts = text.split()
            if len(parts) < 3 or parts[1] not in ['>', '<']:
                update_main(cid, "❌ Wrong format.\nUse: `BTC > 70000`", alerts_menu())
            else:
                symbol = parts[0].upper()
                direction = parts[1]
                try:
                    target = float(parts[2])
                    aid = alert_id_counter[0]
                    alert_id_counter[0] += 1
                    alerts.append({
                        "id": aid, "chat_id": cid,
                        "coin": symbol, "target": target,
                        "direction": direction, "active": True
                    })
                    label = "rises above" if direction == ">" else "drops below"
                    update_main(cid,
                        f"🔔 Alert #{aid} set!\n*{symbol}* {label} *${target:,.2f}*",
                        alerts_menu()
                    )
                except:
                    update_main(cid, "❌ Invalid price value.", alerts_menu())
    else:
        update_main(cid,
            "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
            main_menu()
        )

# ── Callbacks ─────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data
    cid = call.message.chat.id
    mid = call.message.message_id
    main_msg[cid] = mid

    if data == "back_main":
        waiting_for.pop(cid, None)
        update_main(cid,
            "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
            main_menu()
        )

    elif data == "menu_price":
        update_main(cid, "💰 *Select a coin or search any symbol:*", price_menu())

    elif data == "search_coin":
        waiting_for[cid] = "price"
        update_main(cid,
            "🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`, `SEI`, `ORDI`",
            back_button()
        )

    elif data.startswith("price_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol}...")
        p, change = get_price(symbol)
        if p is None:
            update_main(cid, f"❌ Couldn't fetch *{symbol}*.", price_menu())
        else:
            arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
            update_main(cid,
                f"*{symbol}*\n💵 ${p:,.4f}\n{arrow} {abs(change):.2f}% (24h)",
                price_menu()
            )

    elif data == "menu_alerts":
        update_main(cid, "🔔 *Quick Alerts* — tap to set or create custom:", alerts_menu())

    elif data == "custom_alert":
        waiting_for[cid] = "alert"
        update_main(cid,
            "✏️ *Type your custom alert:*\n"
            "Format: `COIN > price` or `COIN < price`\n\n"
            "Examples:\n`BTC > 95000`\n`PEPE < 0.00001`\n`WIF > 5`",
            back_button()
        )

    elif data.startswith("setalert_"):
        _, symbol, direction, target_str = data.split("_")
        target = float(target_str)
        aid = alert_id_counter[0]
        alert_id_counter[0] += 1
        alerts.append({
            "id": aid, "chat_id": cid,
            "coin": symbol, "target": target,
            "direction": direction, "active": True
        })
        label = "rises above" if direction == ">" else "drops below"
        bot.answer_callback_query(call.id, "✅ Alert set!")
        update_main(cid,
            f"🔔 Alert #{aid} set!\n*{symbol}* {label} *${target:,.2f}*\n\nSet another:",
            alerts_menu()
        )

    elif data == "list_alerts":
        user_alerts = [a for a in alerts if a['chat_id'] == cid and a['active']]
        if not user_alerts:
            bot.answer_callback_query(call.id, "No active alerts.")
            update_main(cid, "📋 No active alerts.\n\nSet one below:", alerts_menu())
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
        update_main(cid, text, kb)

    elif data.startswith("cancel_"):
        aid = int(data.split("_")[1])
        for a in alerts:
            if a['id'] == aid and a['chat_id'] == cid:
                a['active'] = False
                bot.answer_callback_query(call.id, f"✅ Alert #{aid} cancelled.")
                break
        user_alerts = [a for a in alerts if a['chat_id'] == cid and a['active']]
        if not user_alerts:
            update_main(cid, "📋 No more active alerts.", back_button())
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
            update_main(cid, text, kb)

    elif data == "gainers":
        bot.answer_callback_query(call.id, "Fetching top gainers...")
        g, _ = get_top_movers()
        if not g:
            update_main(cid, "❌ Failed to fetch.", back_button())
        else:
            text = "🚀 *Top 5 Gainers (24h)*\n\n"
            for d in g:
                coin = d["symbol"].replace("USDT", "")
                text += f"🟢 *{coin}* — ${float(d['lastPrice']):,.4f} ▲ {float(d['priceChangePercent']):.2f}%\n"
            update_main(cid, text, back_button())

    elif data == "losers":
        bot.answer_callback_query(call.id, "Fetching top losers...")
        _, l = get_top_movers()
        if not l:
            update_main(cid, "❌ Failed to fetch.", back_button())
        else:
            text = "📉 *Top 5 Losers (24h)*\n\n"
            for d in l:
                coin = d["symbol"].replace("USDT", "")
                text += f"🔴 *{coin}* — ${float(d['lastPrice']):,.4f} ▼ {abs(float(d['priceChangePercent'])):.2f}%\n"
            update_main(cid, text, back_button())

    elif data == "menu_info":
        update_main(cid, "🔎 *Coin Info — Select or search:*", info_coins_menu())

    elif data == "search_info":
        waiting_for[cid] = "info"
        update_main(cid,
            "🔍 *Type any coin symbol:*\nExample: `PEPE`, `WIF`, `INJ`, `TIA`",
            back_button()
        )

    elif data.startswith("info_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol} info...")
        info = get_coin_info(symbol)
        if not info:
            update_main(cid, f"❌ Couldn't fetch info for *{symbol}*.", info_coins_menu())
        else:
            supply_str = f"{info['supply']:,.0f}" if info['supply'] else "N/A"
            max_str = f"{info['max_supply']:,.0f}" if info['max_supply'] else "∞"
            update_main(cid,
                f"🔎 *{info['name']} ({info['symbol']})*\n\n"
                f"🏆 Rank: #{info['rank']}\n"
                f"💵 Price: ${info['price']:,.4f}\n"
                f"📈 ATH: ${info['ath']:,.4f} ({info['ath_date']})\n"
                f"📉 From ATH: {info['ath_change']:.2f}%\n"
                f"💰 Market Cap: ${info['market_cap']:,.0f}\n"
                f"📊 Volume 24h: ${info['volume']:,.0f}\n"
                f"🔄 Supply: {supply_str} / {max_str}",
                info_coins_menu()
            )

    elif data == "menu_multi":
        update_main(cid, "💱 *Multi-Currency Price — Select or search:*", multi_coins_menu())

    elif data == "search_multi":
        waiting_for[cid] = "multi"
        update_main(cid,
            "🔍 *Type any coin symbol:*\nExample: `BTC`, `ETH`, `SOL`",
            back_button()
        )

    elif data.startswith("multi_"):
        symbol = data.split("_")[1]
        bot.answer_callback_query(call.id, f"Fetching {symbol} prices...")
        prices = get_multi_price(symbol)
        if not prices:
            update_main(cid, f"❌ Couldn't fetch *{symbol}*.", multi_coins_menu())
        else:
            flags = {"usd": "🇺🇸", "eur": "🇪🇺", "gbp": "🇬🇧", "jpy": "🇯🇵", "cny": "🇨🇳", "aed": "🇦🇪", "try": "🇹🇷"}
            symbols = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥", "cny": "¥", "aed": "د.إ", "try": "₺"}
            text = f"💱 *{symbol} Price*\n\n"
            for cur, flag in flags.items():
                p = prices.get(cur)
                if p:
                    text += f"{flag} {symbols[cur]}{p:,.4f}\n"
            update_main(cid, text, multi_coins_menu())

    elif data == "menu_scan":
        waiting_for[cid] = "scan"
        update_main(cid,
            "🛡 *CA Scanner*\n\nPaste a contract address:\n"
            "✅ Supports ETH, BSC, Solana\n\n"
            "Example:\n`0x1234...abcd`",
            back_button()
        )

# ── Alert checker ─────────────────────────────────────
def check_alerts():
    while True:
        active = [a for a in alerts if a['active']]
        checked = {}
        for a in active:
            coin = a['coin']
            if coin not in checked:
                p, _ = get_price(coin)
                checked[coin] = p
            p = checked.get(coin)
            if p is None:
                continue
            triggered = (a['direction'] == '>' and p >= a['target']) or \
                        (a['direction'] == '<' and p <= a['target'])
            if triggered:
                a['active'] = False
                label = "🚀 risen above" if a['direction'] == '>' else "📉 dropped below"
                sent = bot.send_message(a['chat_id'],
                    f"🔔 *Alert #{a['id']} triggered!*\n"
                    f"*{coin}* has {label} *${a['target']:,.2f}*\n"
                    f"Current price: *${p:,.4f}*",
                    parse_mode="Markdown",
                    reply_markup=main_menu()
                )
                main_msg[a['chat_id']] = sent.message_id
        time.sleep(60)

threading.Thread(target=check_alerts, daemon=True).start()
print("🚀 Persona running...")
bot.infinity_polling()
