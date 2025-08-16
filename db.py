import aiosqlite
import asyncio

DB_PATH = "bot.db"

INIT_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 機能ごとのテーブルは必要に応じて追加してください
"""

_db_lock = asyncio.Lock()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(INIT_SQL)
        await db.commit()

async def kv_set(key: str, value: str):
    async with _db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("REPLACE INTO kv(key, value) VALUES(?,?)", (key, value))
            await db.commit()

async def kv_get(key: str) -> str | None:
    async with _db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT value FROM kv WHERE key = ?", (key,))
            row = await cur.fetchone()
            return row[0] if row else None
