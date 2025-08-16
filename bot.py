import os
import sys
import json
import re
import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands

# ========= 環境変数 =========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # 必須
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# カンマ区切りで複数ギルド可（開発中はギルド同期用に推奨）
GUILD_IDS = [int(x.strip()) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip().isdigit()]

# ========= ログ =========
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# ========= 依存（簡易KV: aiosqlite不要版）=========
# Railway のコンテナFSでも動くよう JSON で簡易保存します
# （将来 Shared Disk に切替える場合もコード変更はこの部分のみ）
DB_PATH = "bot_kv.json"
_db_lock = asyncio.Lock()

def _kv_load() -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _kv_save(data: dict):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, DB_PATH)

async def kv_set(key: str, value: str):
    async with _db_lock:
        data = _kv_load()
        data[key] = value
        _kv_save(data)

async def kv_get(key: str) -> str | None:
    async with _db_lock:
        data = _kv_load()
        return data.get(key)

# ========= 匿名掲示板 構成 =========
# /board reveal を使えるユーザーID（固定指名）
ALLOWED_USER_IDS = {716667546241335328, 440893662701027328}

# KVキー
PANEL_KEY   = "anonboard:panel:{channel_id}"
COUNTER_KEY = "anonboard:counter:{channel_id}"
LOGCHAN_KEY = "anonboard:logchan:{channel_id}"
POSTMAP_KEY = "anonboard:post:{message_id}"  # 投稿MsgID -> 投稿者情報(JSON)

def gkey_panel(chid: int) -> str:   return PANEL_KEY.format(channel_id=chid)
def gkey_counter(chid: int) -> str: return COUNTER_KEY.format(channel_id=chid)
def gkey_logchan(chid: int) -> str: return LOGCHAN_KEY.format(channel_id=chid)
def gkey_postmap(mid: int) -> str:  return POSTMAP_KEY.format(message_id=mid)

# 画像URL抽出
IMAGE_EXT_RE = re.compile(r"\.(?:png|jpg|jpeg|gif|webp)(?:\?.*)?$", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

def is_image_url(url: str) -> bool:
    if IMAGE_EXT_RE.search(url):
        return True
    cdn_like = ("cdn.discordapp.com", "media.discordapp.net", "images-ext", "pbs.twimg.com")
    return any(h in url for h in cdn_like)

def extract_first_image_url(text: str) -> str | None:
    for m in URL_RE.findall(text or ""):
        if is_image_url(m):
            return m
    return None

# ========= Discord 本体 =========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---- 匿名掲示板 UI ----
class PostModal(discord.ui.Modal, title="投稿内容を入力"):
    def __init__(self, channel_id: int, is_anonymous: bool):
        super().__init__(timeout=180)
        self.channel_id = channel_id
        self.is_anonymous = is_anonymous
        self.content = discord.ui.TextInput(
            label="本文", style=discord.TextStyle.paragraph,
            placeholder="ここにメッセージを入力", max_length=2000, required=True
        )
        self.add_item(self.content)
        self.img_url = discord.ui.TextInput(
            label="画像URL（任意）", style=discord.TextStyle.short,
            placeholder="https://...", max_length=500, required=False
        )
        self.add_item(self.img_url)

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.client.get_channel(self.channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("対象チャンネルが見つかりません。", ephemeral=True)

        # 表示名
        if self.is_anonymous:
            counter_s = await kv_get(gkey_counter(self.channel_id))
            counter = int(counter_s) if counter_s and counter_s.isdigit() else 0
            counter += 1
            await kv_set(gkey_counter(self.channel_id), str(counter))
            display_name = f"{counter}"
        else:
            display_name = interaction.user.display_name

        content = self.content.value.strip()
        if not content:
            return await interaction.response.send_message("本文が空です。", ephemeral=True)

        img = (self.img_url.value or "").strip()
        if not img:
            img = extract_first_image_url(content) or ""
        if img and not is_image_url(img):
            img = ""

        embed = discord.Embed(description=content, color=discord.Color.blurple())
        embed.set_footer(text=f"投稿者: {display_name}")
        if img:
            embed.set_image(url=img)

        sent = await channel.send(embed=embed)

        # 投稿マップ保存
        post_info = {
            "guild_id": interaction.guild_id,
            "channel_id": self.channel_id,
            "message_id": sent.id,
            "anonymous": self.is_anonymous,
            "anon_display": display_name if self.is_anonymous else None,
            "author_id": interaction.user.id,
            "author_name": str(interaction.user),        # name#discriminator 表示
            "author_display": interaction.user.display_name,
            "img_url": img or None,
        }
        await kv_set(gkey_postmap(sent.id), json.dumps(post_info, ensure_ascii=False))

        # ログチャンネルへ（任意）
        log_chan_id_s = await kv_get(gkey_logchan(self.channel_id))
        if log_chan_id_s and log_chan_id_s.isdigit():
            log_chan = interaction.client.get_channel(int(log_chan_id_s))
            if isinstance(log_chan, discord.TextChannel):
                le = discord.Embed(
                    title="匿名掲示板 投稿ログ", description=content, color=discord.Color.dark_gray()
                )
                le.add_field(name="匿名？", value="はい" if self.is_anonymous else "いいえ", inline=True)
                le.add_field(name="表示名", value=display_name, inline=True)
                le.add_field(name="実投稿者", value=f"{interaction.user.mention} ({interaction.user.id})", inline=False)
                le.add_field(name="メッセージ", value=f"[ジャンプ]({sent.jump_url})", inline=False)
                if img: le.set_image(url=img)
                await log_chan.send(embed=le)

        await repost_panel(interaction.client, channel.id)  # 最下部固定
        await interaction.response.send_message("投稿しました。", ephemeral=True)

class BoardView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="匿名で投稿", style=discord.ButtonStyle.primary, emoji="🕵️")
    async def post_anon(self, btn: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(PostModal(self.channel_id, is_anonymous=True))

    @discord.ui.button(label="通常で投稿", style=discord.ButtonStyle.secondary, emoji="🗣️")
    async def post_public(self, btn: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(PostModal(self.channel_id, is_anonymous=False))

async def repost_panel(client: commands.Bot, channel_id: int):
    """古いパネルを削除 → 新しいパネルを最下部に再掲してID保存"""
    panel_key = gkey_panel(channel_id)
    panel_id_s = await kv_get(panel_key)
    channel = client.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return

    if panel_id_s and panel_id_s.isdigit():
        try:
            old = await channel.fetch_message(int(panel_id_s))
            await old.delete()
        except Exception:
            pass

    view = BoardView(channel_id)
    msg = await channel.send("**匿名掲示板パネル**\n下のボタンから投稿してください。", view=view)
    await kv_set(panel_key, str(msg.id))

# ---- スラッシュグループ ----
board_group = app_commands.Group(name="board", description="匿名掲示板の設定/操作")

# ギルド同期を明示
def guild_deco(func):
    if GUILD_IDS:
        return app_commands.guilds(*[discord.Object(id=g) for g in GUILD_IDS])(func)
    return func

@board_group.command(name="setup", description="このチャンネル（または指定先）に掲示板パネルを設置")
@guild_deco
@app_commands.describe(
    channel="掲示板にするテキストチャンネル（未指定ならこのチャンネル）",
    reset_counter="匿名連番を0から再開",
    log_channel="投稿ログ送信先（任意）"
)
async def board_setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    reset_counter: bool = False,
    log_channel: discord.TextChannel | None = None
):
    if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("権限がありません。（チャンネル管理 or 管理者）", ephemeral=True)
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)

    if reset_counter:
        await kv_set(gkey_counter(target.id), "0")
    if log_channel:
        await kv_set(gkey_logchan(target.id), str(log_channel.id))

    await repost_panel(interaction.client, target.id)
    txt = f"掲示板パネルを設置しました：{target.mention}\n"
    if log_channel: txt += f"投稿ログ：{log_channel.mention}\n"
    if reset_counter: txt += "匿名連番をリセットしました。"
    await interaction.response.send_message(txt, ephemeral=True)

@board_group.command(name="setlog", description="掲示板の投稿ログ先を設定")
@guild_deco
@app_commands.describe(board_channel="掲示板チャンネル（未指定なら実行場所）", log_channel="ログ送信先")
async def board_setlog(
    interaction: discord.Interaction,
    board_channel: discord.TextChannel | None = None,
    log_channel: discord.TextChannel | None = None
):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("権限がありません。（サーバー管理 or 管理者）", ephemeral=True)
    target = board_channel or interaction.channel
    if not isinstance(target, discord.TextChannel) or not log_channel:
        return await interaction.response.send_message("対象/ログ先はテキストチャンネルを指定してください。", ephemeral=True)
    await kv_set(gkey_logchan(target.id), str(log_channel.id))
    await interaction.response.send_message(f"{target.mention} の投稿ログ先を {log_channel.mention} に設定しました。", ephemeral=True)

@board_group.command(name="reset_counter", description="匿名連番を0にリセット")
@guild_deco
@app_commands.describe(channel="対象チャンネル（未指定なら実行場所）")
async def board_reset_counter(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("権限がありません。", ephemeral=True)
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
    await kv_set(gkey_counter(target.id), "0")
    await interaction.response.send_message(f"匿名連番をリセットしました：{target.mention}", ephemeral=True)

@board_group.command(name="panel", description="パネルを最下部に再掲")
@guild_deco
@app_commands.describe(channel="対象チャンネル（未指定なら実行場所）")
async def board_panel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("権限がありません。", ephemeral=True)
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
    await repost_panel(interaction.client, target.id)
    await interaction.response.send_message(f"パネルを再掲しました：{target.mention}", ephemeral=True)

@board_group.command(name="reveal", description="匿名投稿の実投稿者を照会（指定ユーザー限定）")
@guild_deco
@app_commands.describe(message="対象メッセージ（リンク or 直指定）")
async def board_reveal(interaction: discord.Interaction, message: discord.Message):
    if interaction.user.id not in ALLOWED_USER_IDS:
        return await interaction.response.send_message("このコマンドを使う権限がありません。", ephemeral=True)
    data_s = await kv_get(gkey_postmap(message.id))
    if not data_s:
        return await interaction.response.send_message("記録がありません（匿名掲示板の投稿ではない可能性）。", ephemeral=True)
    info = json.loads(data_s)
    desc = (
        f"**匿名？** {'はい' if info.get('anonymous') else 'いいえ'}\n"
        f"**匿名表示名**: {info.get('anon_display') or '-'}\n"
        f"**実投稿者**: <@{info.get('author_id')}> (`{info.get('author_name')}` / 表示名: `{info.get('author_display')}`)\n"
        f"**メッセージ**: {message.jump_url}"
    )
    await interaction.response.send_message(desc, ephemeral=True)

# ---- /ping ----
@tree.command(name="ping", description="生存確認")
@guild_deco
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🏓", ephemeral=True)

# ---- ready ----
@bot.event
async def on_ready():
    user_info = "(user: None)" if bot.user is None else f"{bot.user} (ID: {bot.user.id})"
    log.info(f"Logged in as {user_info}")
    try:
        # グループ登録（単一ファイルなのでここで追加）
        if board_group not in tree.get_commands():
            tree.add_command(board_group)

        # ギルド同期（即時反映）
        if GUILD_IDS:
            for gid in GUILD_IDS:
                await tree.sync(guild=discord.Object(id=gid))
                log.info(f"Synced commands to guild {gid}")
        else:
            await tree.sync()
            log.info("Synced global commands")
    except Exception as e:
        log.exception("Command sync failed: %s", e)

# ---- main ----
def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN が未設定です（Railway Variables で設定してください）")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
