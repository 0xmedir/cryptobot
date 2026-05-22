import asyncio
import json
import websockets
from database import get_all_active_alerts, deactivate_alert

prices = {}

async def binance_ws():
    uri = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("✅ WebSocket connected")
                async for msg in ws:
                    data = json.loads(msg)
                    for ticker in data:
                        symbol = ticker["s"]
                        if symbol.endswith("USDT"):
                            coin = symbol.replace("USDT", "")
                            prices[coin] = float(ticker["c"])
        except Exception as e:
            print(f"WS error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)

async def alert_engine(bot):
    while True:
        try:
            alerts = await get_all_active_alerts()
            for alert in alerts:
                aid, chat_id, coin, direction, target = alert
                price = prices.get(coin)
                if price is None:
                    continue
                triggered = (direction == ">" and price >= target) or \
                            (direction == "<" and price <= target)
                if triggered:
                    await deactivate_alert(aid)
                    label = "🚀 risen above" if direction == ">" else "📉 dropped below"
                    bot.send_message(
                        chat_id,
                        f"🔔 *Alert #{aid} triggered!*\n"
                        f"*{coin}* has {label} *${target:,.2f}*\n"
                        f"Current price: *${price:,.4f}*",
                        parse_mode="Markdown"
                    )
        except Exception as e:
            print(f"Alert engine error: {e}")
        await asyncio.sleep(2)

async def run_engines(bot):
    await asyncio.gather(
        binance_ws(),
        alert_engine(bot)
    )
