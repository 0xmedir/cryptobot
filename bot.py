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

# ─────────────────────────────
# PRICE (BINANCE)
# ─────────────────────────────
def get_price(symbol):
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}USDT",
            timeout=10
        )
        data = r.json()

        if "lastPrice" in data:
            return float(data["lastPrice"]), float(data["priceChangePercent"])
    except:
        pass
    return None, None


# ─────────────────────────────
# TOP MOVERS
# ─────────────────────────────
def get_top_movers():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
        data = r.json()

        filtered = [
            d for d in data
            if d["symbol"].endswith("USDT")
            and float(d["quoteVolume"]) > 1_000_000
        ]

        sorted_data = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=True)

        return sorted_data[:5], sorted_data[-5:][::-1]
    except:
        return None, None


# ─────────────────────────────
# KEYBOARDS
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
# MESSAGE HANDLER SAFETY
# ─────────────────────────────
def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except:
        pass


def update_main(cid, text, markup):
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


# ─────────────────────────────
# START
# ─────────────────────────────
@bot.message_handler(commands=["start", "help"])
def start(msg):
    waiting_for.pop(msg.chat.id, None)
    update_main(
        msg.chat.id,
        "🤖 *Persona Crypto Bot*\n\nChoose an option:",
        main_menu()
    )


# ─────────────────────────────
# TEXT INPUT
# ─────────────────────────────
@bot.message_handler(func=lambda m: True)
def handle_text(msg):
    cid = msg.chat.id
    text = msg.text.strip()

    safe_delete(cid, msg.message_id)

    if cid not in waiting_for:
        update_main(cid, "🤖 Menu:", main_menu())
        return

    mode = waiting_for.pop(cid)

    # ── PRICE ──
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

    # ── ALERT ──
    elif mode == "alert":
        try:
            parts = text.split()
            coin = parts[0].upper()
            direction = parts[1]
            target = float(parts[2])

            aid = alert_id_counter[0]
            alert_id_counter[0] += 1

            alerts.append({
                "id": aid,
                "chat_id": cid,
                "coin": coin,
                "direction": direction,
                "target": target,
                "active": True
            })

            update_main(
                cid,
                f"🔔 Alert #{aid} set!\n{coin} {direction} {target}",
                main_menu()
            )

        except:
            update_main(cid, "❌ Format: BTC > 70000", main_menu())


# ─────────────────────────────
# CALLBACKS
# ─────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    cid = call.message.chat.id
    data = call.data

    if data == "back_main":
        update_main(cid, "🤖 Menu:", main_menu())

    elif data == "menu_price":
        waiting_for[cid] = "price"
        update_main(cid, "Send coin symbol (BTC, ETH...)", back_button())

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


# ─────────────────────────────
# ALERT CHECKER
# ─────────────────────────────
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
                        f"🔔 ALERT #{a['id']}\n{a['coin']} hit {a['target']}\nNow: {p}"
                    )

            time.sleep(30)

        except:
            time.sleep(5)


threading.Thread(target=check_alerts, daemon=True).start()


# ─────────────────────────────
print("🚀 Bot running...")
bot.infinity_polling()
