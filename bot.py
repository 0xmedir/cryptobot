import telebot
import requests
import threading
import time
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ─────────────────────────────────────────────
# TOKEN (IMPORTANT FIX: force safe fallback)
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN or BOT_TOKEN == "8619003788:AAEszjzsxeKH8dSm8FPtqkPJxCG9Dw3Tne4":
    print("❌ BOT_TOKEN missing! Bot will not respond.")
    exit()

bot = telebot.TeleBot(BOT_TOKEN)

alerts = []
alert_id_counter = [1]
waiting_for = {}
main_msg = {}


# ─────────────────────────────────────────────
# SAFE REQUEST (prevents silent crash)
# ─────────────────────────────────────────────
def safe_get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=10)
        return r.json()
    except Exception as e:
        print("API ERROR:", e)
        return None


# ─────────────────────────────────────────────
# PRICE
# ─────────────────────────────────────────────
def get_price(symbol):
    try:
        data = safe_get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": symbol.upper() + "USDT"}
        )

        if data and "lastPrice" in data:
            return float(data["lastPrice"]), float(data["priceChangePercent"])

    except Exception as e:
        print("PRICE ERROR:", e)

    return None, None


# ─────────────────────────────────────────────
# TOP MOVERS
# ─────────────────────────────────────────────
def get_top_movers():
    data = safe_get("https://api.binance.com/api/v3/ticker/24hr")

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
        print("MOVERS ERROR:", e)
        return None, None


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
    return kb


def back_button():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb


# ─────────────────────────────────────────────
# SAFE DELETE (prevents silent crash)
# ─────────────────────────────────────────────
def safe_delete(cid, mid):
    try:
        bot.delete_message(cid, mid)
    except Exception as e:
        print("DELETE ERROR:", e)


# ─────────────────────────────────────────────
# UPDATE MESSAGE
# ─────────────────────────────────────────────
def update_main(cid, text, markup):
    try:
        old = main_msg.get(cid)

        sent = bot.send_message(
            cid,
            text,
            parse_mode="Markdown",
            reply_markup=markup
        )

        main_msg[cid] = sent.message_id

        if old:
            threading.Thread(
                target=lambda: (time.sleep(5), safe_delete(cid, old)),
                daemon=True
            ).start()

    except Exception as e:
        print("UPDATE ERROR:", e)


# ─────────────────────────────────────────────
# START (CRITICAL FIX: make sure it always responds)
# ─────────────────────────────────────────────
@bot.message_handler(commands=["start", "help"])
def start(msg):
    try:
        waiting_for.pop(msg.chat.id, None)

        update_main(
            msg.chat.id,
            "🤖 *Persona* — your crypto assistant\n\nChoose an option:",
            main_menu()
        )

    except Exception as e:
        print("START ERROR:", e)


# ─────────────────────────────────────────────
# TEXT HANDLER (SAFE)
# ─────────────────────────────────────────────
@bot.message_handler(func=lambda msg: True)
def handle_text(msg):
    try:
        cid = msg.chat.id
        text = msg.text.strip()

        safe_delete(cid, msg.message_id)

        if cid not in waiting_for:
            update_main(cid, "🤖 Menu:", main_menu())
            return

        mode = waiting_for.pop(cid)

        if mode == "price":
            p, ch = get_price(text)

            if p is None:
                update_main(cid, f"❌ {text} not found", main_menu())
            else:
                arrow = "🟢 ▲" if ch >= 0 else "🔴 ▼"
                update_main(
                    cid,
                    f"*{text.upper()}*\n💵 ${p:,.4f}\n{arrow} {abs(ch):.2f}%",
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

            except:
                update_main(cid, "❌ Format: BTC > 70000", main_menu())

    except Exception as e:
        print("TEXT ERROR:", e)


# ─────────────────────────────────────────────
# CALLBACKS (SAFE WRAPPER)
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    try:
        cid = call.message.chat.id
        data = call.data

        if data == "back_main":
            update_main(cid, "🤖 Menu:", main_menu())

        elif data == "menu_price":
            waiting_for[cid] = "price"
            update_main(cid, "Send coin symbol", back_button())

        elif data == "menu_alerts":
            waiting_for[cid] = "alert"
            update_main(cid, "Format: BTC > 70000", back_button())

        elif data == "gainers":
            g, _ = get_top_movers()
            if not g:
                return

            text = "🚀 Gainers:\n\n"
            for d in g:
                text += f"{d['symbol']} ▲ {d['priceChangePercent']}%\n"

            update_main(cid, text, back_button())

        elif data == "losers":
            _, l = get_top_movers()
            if not l:
                return

            text = "📉 Losers:\n\n"
            for d in l:
                text += f"{d['symbol']} ▼ {d['priceChangePercent']}%\n"

            update_main(cid, text, back_button())

    except Exception as e:
        print("CALLBACK ERROR:", e)


# ─────────────────────────────────────────────
# ALERT ENGINE (SAFE)
# ─────────────────────────────────────────────
def check_alerts():
    while True:
        try:
            for a in alerts:
                if not a["active"]:
                    continue

                p, _ = get_price(a["coin"])
                if p is None:
                    continue

                triggered = (
                    (a["direction"] == ">" and p >= a["target"]) or
                    (a["direction"] == "<" and p <= a["target"])
                )

                if triggered:
                    a["active"] = False

                    bot.send_message(
                        a["chat_id"],
                        f"🔔 ALERT #{a['id']}\n{a['coin']} → {p}"
                    )

            time.sleep(60)

        except Exception as e:
            print("ALERT ERROR:", e)
            time.sleep(5)


threading.Thread(target=check_alerts, daemon=True).start()


# ─────────────────────────────────────────────
print("🚀 Persona running...")
bot.infinity_polling(skip_pending=True)
