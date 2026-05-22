import telebot
import requests
import threading
import time
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8619003788:AAEszjzsxeKH8dSm8FPtqkPJxCG9Dw3Tne4")
bot = telebot.TeleBot(BOT_TOKEN)

alerts = []
alert_id_counter = [1]
waiting_for = {}
main_msg = {}

# ─────────────────────────────────────────────
# SAFE HELPERS
# ─────────────────────────────────────────────
def safe_delete(cid, mid):
    try:
        bot.delete_message(cid, mid)
    except Exception as e:
        print("Delete error:", e)


def safe_request(url, **kwargs):
    try:
        r = requests.get(url, timeout=10, **kwargs)
        return r.json()
    except Exception as e:
        print("Request error:", e)
        return None


# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────
def get_price(symbol):
    try:
        pair = symbol.upper() + "USDT"

        data = safe_request(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": pair}
        )

        if data and "lastPrice" in data:
            return float(data["lastPrice"]), float(data["priceChangePercent"])

    except Exception as e:
        print("get_price error:", e)

    return None, None


def get_top_movers():
    data = safe_request("https://api.binance.com/api/v3/ticker/24hr")

    if not data:
        return None, None

    try:
        filtered = [
            d for d in data
            if d["symbol"].endswith("USDT")
            and float(d["quoteVolume"]) > 1_000_000
        ]

        sorted_data = sorted(
            filtered,
            key=lambda x: float(x["priceChangePercent"]),
            reverse=True
        )

        return sorted_data[:5], sorted_data[-5:][::-1]

    except Exception as e:
        print("movers error:", e)
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

    data = safe_request(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "community_data": "false"
        }
    )

    if not data or "market_data" not in data:
        return None

    md = data["market_data"]

    return {
        "name": data.get("name"),
        "symbol": data.get("symbol", "").upper(),
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


def scan_ca(address):
    try:
        if address.startswith("0x") and len(address) == 42:
            r = requests.get(
                f"https://api.gopluslabs.io/api/v1/token_security/1",
                params={"contract_addresses": address},
                timeout=10
            )
            data = r.json()
            result = data.get("result", {}).get(address.lower(), {})

            if not result:
                r = requests.get(
                    f"https://api.gopluslabs.io/api/v1/token_security/56",
                    params={"contract_addresses": address},
                    timeout=10
                )
                data = r.json()
                result = data.get("result", {}).get(address.lower(), {})

            return result

        else:
            r = requests.get(
                f"https://api.gopluslabs.io/api/v1/solana/token_security",
                params={"contract_addresses": address},
                timeout=10
            )
            data = r.json()
            return data.get("result", {}).get(address, {})

    except Exception as e:
        print("scan_ca error:", e)

    return None


# ─────────────────────────────────────────────
# KEYBOARDS (UNCHANGED)
# ─────────────────────────────────────────────
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


# (other keyboards unchanged — keep yours as-is)
# alerts_menu(), info_coins_menu(), multi_coins_menu(), etc.


# ─────────────────────────────────────────────
# MAIN UPDATE
# ─────────────────────────────────────────────
def update_main(cid, text, markup):
    old_mid = main_msg.get(cid)

    sent = bot.send_message(
        cid,
        text,
        parse_mode="Markdown",
        reply_markup=markup
    )

    main_msg[cid] = sent.message_id

    if old_mid:
        threading.Thread(
            target=lambda: (time.sleep(5), safe_delete(cid, old_mid)),
            daemon=True
        ).start()


# ─────────────────────────────────────────────
# START
# ─────────────────────────────────────────────
@bot.message_handler(commands=["start", "help"])
def start(msg):
    waiting_for.pop(msg.chat.id, None)
    update_main(
        msg.chat.id,
        "🤖 *Persona* — your crypto assistant",
        main_menu()
    )


# ─────────────────────────────────────────────
# TEXT HANDLER (UNCHANGED LOGIC)
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda msg: True)
def handle_text(msg):
    cid = msg.chat.id
    text = msg.text.strip()

    safe_delete(cid, msg.message_id)

    if cid not in waiting_for:
        update_main(cid, "Menu:", main_menu())
        return

    mode = waiting_for.pop(cid)

    if mode == "price":
        p, change = get_price(text)

        if p is None:
            update_main(cid, f"❌ {text} not found", main_menu())
        else:
            arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
            update_main(
                cid,
                f"*{text.upper()}*\n💵 ${p:,.4f}\n{arrow} {abs(change):.2f}%",
                main_menu()
            )

    elif mode == "alert":
        try:
            coin, direction, target = text.split()
            target = float(target)

            aid = alert_id_counter[0]
            alert_id_counter[0] += 1

            alerts.append({
                "id": aid,
                "chat_id": cid,
                "coin": coin.upper(),
                "direction": direction,
                "target": target,
                "active": True
            })

            update_main(cid, f"🔔 Alert #{aid} set!", main_menu())

        except Exception as e:
            print("alert error:", e)
            update_main(cid, "❌ Format: BTC > 70000", main_menu())


# ─────────────────────────────────────────────
# ALERT LOOP (FIXED ONLY)
# ─────────────────────────────────────────────
def check_alerts():
    while True:
        try:
            active = [a for a in alerts if a["active"]]
            checked = {}

            for a in active:
                coin = a["coin"]

                if coin not in checked:
                    p, _ = get_price(coin)
                    checked[coin] = p

                p = checked.get(coin)

                if p is None:
                    continue

                if (
                    (a["direction"] == ">" and p >= a["target"]) or
                    (a["direction"] == "<" and p <= a["target"])
                ):
                    a["active"] = False

                    bot.send_message(
                        a["chat_id"],
                        f"🔔 Alert #{a['id']} triggered!\n"
                        f"{coin} → {p}"
                    )

            time.sleep(60)

        except Exception as e:
            print("alert loop error:", e)
            time.sleep(5)


threading.Thread(target=check_alerts, daemon=True).start()


# ─────────────────────────────────────────────
print("🚀 Persona running...")
bot.infinity_polling()
