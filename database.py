import aiosqlite
import asyncio

DB_PATH = "data/persona.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                coin TEXT,
                direction TEXT,
                target REAL,
                active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()

async def register_user(chat_id, username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (chat_id, username) VALUES (?, ?)",
            (chat_id, username)
        )
        await db.commit()

async def add_alert(chat_id, coin, direction, target):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO alerts (chat_id, coin, direction, target) VALUES (?, ?, ?, ?)",
            (chat_id, coin, direction, target)
        )
        await db.commit()
        return cursor.lastrowid

async def get_alerts(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, coin, direction, target FROM alerts WHERE chat_id=? AND active=1",
            (chat_id,)
        )
        return await cursor.fetchall()

async def get_all_active_alerts():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, chat_id, coin, direction, target FROM alerts WHERE active=1"
        )
        return await cursor.fetchall()

async def deactivate_alert(alert_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alerts SET active=0 WHERE id=?",
            (alert_id,)
        )
        await db.commit()
