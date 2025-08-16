import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ギルド即時反映（開発中に超便利）
_guild_ids = os.getenv("GUILD_IDS", "")
GUILD_IDS = [int(x.strip()) for x in _guild_ids.split(",") if x.strip().isdigit()]