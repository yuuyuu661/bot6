import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands
from discord import app_commands

from config import DISCORD_TOKEN, LOG_LEVEL, GUILD_IDS
from db import init_db

# ===== ログ設定 =====
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# ===== Intents =====
intents = discord.Intents.default()
intents.message_content = True  # 必要に応じてON
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ===== 起動時 =====
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        # 開発中はギルドスコープに同期（即時反映）
        if GUILD_IDS:
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                await tree.sync(guild=guild)
                log.info(f"Synced commands to guild {gid}")
        else:
            await tree.sync()
            log.info("Synced global commands")
    except Exception as e:
        log.exception("Command sync failed: %s", e)

# ===== ヘルスチェック =====
@tree.command(name="ping", description="生存確認")
@app_commands.guilds(*[discord.Object(id=g) for g in GUILD_IDS]) if GUILD_IDS else (lambda x: x)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🏓", ephemeral=True)

async def load_cogs():
    await bot.load_extension("cogs.feature_one")
    await bot.load_extension("cogs.feature_two")
    await bot.load_extension("cogs.feature_three")

async def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN が .env で未設定です")
        sys.exit(1)
    await init_db()
    await load_cogs()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
