import telebot
import requests
import threading
import time

BOT_TOKEN = "PASTE_YOUR_TOKEN_HERE"
bot = telebot.TeleBot(BOT_TOKEN)

alerts = []
alert_id_counter = [1]

COINGECKO_IDS = {
    "btc": "bitcoin", "eth": "ethereum", "bnb": "binancecoin",
    "sol": "solana", "xrp": "ripple", "ada": "cardano",
    "doge": "dogecoin", "dot": "polkadot", "matic": "matic-network",
    "avax": "avalanche-2", "link": "chainlink", "ltc": "litecoin",
    "uni": "uniswap", "atom": "cosmos", "near": "near",
    "apt": "aptos", "arb": "arbitrum", "op": "optimism",
    "tia": "celestia", "sui": "sui", "trx": "tron",
    "ton": "the-open-network", "shib": "shiba-inu",
}

def get_coin_id(symbol):
    return COINGECKO_IDS.get(symbol.lower(), symbol.lower())

def get_price(symbol):
    coin_id = get_coin_id(symbol)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=10
        )
        data = r.json()
        if coin_id in data:
            return data[coin_id]["usd"], data[coin_id].get("usd_24h_change", 0)
    except:
        pass
    return None, None

@bot.message_handler(commands=['start', 'help'])
def start(msg):
    bot.reply_to(msg,
        "🤖 *CryptoBot*\n\n"
        "*💰 Price*\n"
        "`/price BTC` — live price + 24h change\n\n"
        "*🔔 Alerts*\n"
        "`/alert BTC > 70000`\n"
        "`/alert ETH < 3000`\n"
        "`/alerts` — list active alerts\n"
        "`/delalert <id>` — delete an alert",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['price'])
def price(msg):
    parts = msg.text.split()
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: `/price BTC`", parse_mode="Markdown")
        return
    symbol = parts[1].upper()
    p, change = get_price(symbol)
    if p is None:
        bot.reply_to(msg, f"❌ Couldn't find *{symbol}*. Check the symbol.", parse_mode="Markdown")
        return
    arrow = "🟢 ▲" if change >= 0 else "🔴 ▼"
    bot.reply_to(msg,
        f"*{symbol}*\n💵 ${p:,.4f}\n{arrow} {abs(change):.2f}% (24h)",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['alert'])
def set_alert(msg):
    parts = msg.text.split()
    if len(parts) < 4 or parts[2] not in ['>', '<']:
        bot.reply_to(msg, "Usage: `/alert BTC > 70000`", parse_mode="Markdown")
        return
    symbol = parts[1].upper()
    try:
        target = float(parts[3])
    except:
        bot.reply_to(msg, "❌ Invalid price.")
        return
    aid = alert_id_counter[0]
    alert_id_counter[0] += 1
    alerts.append({"id": aid, "chat_id": msg.chat.id, "coin": symbol, "target": target, "direction": parts[2], "active": True})
    label = "rises above" if parts[2] == ">" else "drops below"
    bot.reply_to(msg, f"🔔 Alert #{aid}: notify when *{symbol}* {label} *${target:,.2f}*", parse_mode="Markdown")

@bot.message_handler(commands=['alerts'])
def list_alerts(msg):
    user_alerts = [a for a in alerts if a['chat_id'] == msg.chat.id and a['active']]
    if not user_alerts:
        bot.reply_to(msg, "No active alerts.")
        return
    text = "🔔 *Active alerts:*\n"
    for a in user_alerts:
        label = "▲ above" if a['direction'] == '>' else "▼ below"
        text += f"  #{a['id']} — {a['coin']} {label} ${a['target']:,.2f}\n"
    bot.reply_to(msg, text, parse_mode="Markdown")

@bot.message_handler(commands=['delalert'])
def del_alert(msg):
    parts = msg.text.split()
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: `/delalert <id>`", parse_mode="Markdown")
        return
    try:
        aid = int(parts[1])
    except:
        bot.reply_to(msg, "❌ Invalid ID.")
        return
    for a in alerts:
        if a['id'] == aid and a['chat_id'] == msg.chat.id:
            a['active'] = False
            bot.reply_to(msg, f"✅ Alert #{aid} deleted.")
            return
    bot.reply_to(msg, f"❌ Alert #{aid} not found.")

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
                bot.send_message(a['chat_id'],
                    f"🔔 *Alert #{a['id']} triggered!*\n"
                    f"*{coin}* has {label} *${a['target']:,.2f}*\n"
                    f"Current price: *${p:,.4f}*",
                    parse_mode="Markdown"
                )
        time.sleep(60)

threading.Thread(target=check_alerts, daemon=True).start()
print("🚀 CryptoBot running...")
bot.infinity_polling()
