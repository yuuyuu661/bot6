import os
import sys
import json
import re
import asyncio
import logging
import datetime

import discord
from discord.ext import commands
from discord import app_commands

# ========= 環境変数 =========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # 必須（Railway Variables で設定）
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# ギルド即時反映用：複数サーバならカンマ区切りで指定可。未設定なら例のIDを既定値に。
GUILD_IDS = [int(x.strip()) for x in os.getenv("GUILD_IDS", "1398607685158440991").split(",") if x.strip().isdigit()]
PRIMARY_GUILD_ID = GUILD_IDS[0] if GUILD_IDS else 1398607685158440991

# ========= ログ =========
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="(%(asctime)s) [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# ========= 簡易KV(JSON) =========
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

async def kv_del(key: str):
    async with _db_lock:
        data = _kv_load()
        if key in data:
            del data[key]
            _kv_save(data)

async def kv_all() -> dict:
    async with _db_lock:
        return _kv_load()

# ========= 権限/設定 =========
ALLOWED_USER_IDS = {716667546241335328, 440893662701027328}

def is_allowed_user(user: discord.abc.User) -> bool:
    return user.id in ALLOWED_USER_IDS

async def guard_allowed(interaction: discord.Interaction) -> bool:
    if not is_allowed_user(interaction.user):
        await interaction.response.send_message("この操作を行えるのは許可ユーザーだけです。", ephemeral=True)
        return False
    return True

# ========= 掲示板用 KVキー =========
PANEL_KEY    = "anonboard:panel:{channel_id}"
COUNTER_KEY  = "anonboard:counter:{channel_id}"
LOGCHAN_KEY  = "anonboard:logchan:{channel_id}"
POSTMAP_KEY  = "anonboard:post:{message_id}"      # 公開メッセージID -> 投稿者情報(JSON)
PENDING_KEY  = "anonboard:pending:{log_msg_id}"   # 承認待ちログメッセージID -> 申請情報(JSON)
AUTODEL_KEY  = "anonboard:autodel_sec:{channel_id}"  # 送信後◯秒削除（新規のみ）

def gkey_panel(chid: int) -> str:       return PANEL_KEY.format(channel_id=chid)
def gkey_counter(chid: int) -> str:     return COUNTER_KEY.format(channel_id=chid)
def gkey_logchan(chid: int) -> str:     return LOGCHAN_KEY.format(channel_id=chid)
def gkey_postmap(mid: int) -> str:      return POSTMAP_KEY.format(message_id=mid)
def gkey_pending(log_mid: int) -> str:  return PENDING_KEY.format(log_msg_id=log_mid)
def gkey_autodel(chid: int) -> str:     return AUTODEL_KEY.format(channel_id=chid)

# （後方互換）
PENDING_KEY_LEGACY = "anonboard:pending:{message_id}"
def gkey_pending_legacy(log_mid: int) -> str:
    return PENDING_KEY_LEGACY.format(message_id=log_mid)

# ========= 定期掃除（掲示板と無関係） =========
PURGE_KEY = "cleaner:purge:{channel_id}"  # JSON: {"interval": int, "keep_hours": int, "batch_limit": int}
def gkey_purge(chid: int) -> str: return PURGE_KEY.format(channel_id=chid)
_purge_tasks: dict[int, asyncio.Task] = {}

async def _run_purge(channel: discord.TextChannel, interval_sec: int, keep_hours: int, batch_limit: int):
    """掲示板とは無関係の定期掃除。ピン留め以外を削除。"""
    while True:
        try:
            await asyncio.sleep(interval_sec)
            cutoff = discord.utils.utcnow() - datetime.timedelta(hours=keep_hours)

            to_delete_bulk, to_delete_single = [], []
            async for msg in channel.history(limit=1000, oldest_first=False):
                if len(to_delete_bulk) + len(to_delete_single) >= batch_limit:
                    break
                if msg.pinned:
                    continue
                if msg.created_at >= cutoff:
                    continue

                # 14日以内: bulk / 超過: 個別
                if (discord.utils.utcnow() - msg.created_at) <= datetime.timedelta(days=14):
                    to_delete_bulk.append(msg)
                else:
                    to_delete_single.append(msg)

            if to_delete_bulk:
                try:
                    await channel.delete_messages(to_delete_bulk)
                except Exception:
                    for m in to_delete_bulk:
                        try:
                            await m.delete()
                        except Exception:
                            pass

            for m in to_delete_single:
                try:
                    await m.delete()
                except Exception:
                    pass

            if to_delete_bulk or to_delete_single:
                log.info(f"[purge] channel={channel.id} deleted bulk={len(to_delete_bulk)} single={len(to_delete_single)} (<{keep_hours}h)")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception(f"[purge] error in channel {channel.id}: {e}")
            continue

async def start_purge_for_channel(bot: commands.Bot, channel_id: int, interval_sec: int, keep_hours: int, batch_limit: int):
    await stop_purge_for_channel(channel_id)
    ch = bot.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        return
    t = asyncio.create_task(_run_purge(ch, interval_sec, keep_hours, batch_limit))
    _purge_tasks[channel_id] = t

async def stop_purge_for_channel(channel_id: int):
    t = _purge_tasks.pop(channel_id, None)
    if t and not t.done():
        t.cancel()
        try:
            await t
        except Exception:
            pass

# ========= URL/メッセージリンク 解析 =========
IMAGE_EXT_RE = re.compile(r"\.(?:png|jpg|jpeg|gif|webp)(?:\?.*)?$", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

def is_image_url(url: str) -> bool:
    if IMAGE_EXT_RE.search(url):
        return True
    cdn_like = ("cdn.discordapp.com", "media.discordapp.net", "images-ext", "pbs.twimg.com", "imgur.com")
    return any(h in url for h in cdn_like)

def extract_first_image_url(text: str) -> str | None:
    for m in URL_RE.findall(text or ""):
        if is_image_url(m):
            return m
    return None

MSG_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord\.com/channels/(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)"
)

async def fetch_message_from_link(bot: commands.Bot, link: str) -> discord.Message | None:
    m = MSG_LINK_RE.match(link.strip())
    if not m:
        return None
    ch_id = int(m.group("channel_id"))
    msg_id = int(m.group("message_id"))
    ch = bot.get_channel(ch_id)
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return None
    try:
        return await ch.fetch_message(msg_id)
    except Exception:
        return None

# ========= Discord =========
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ========= 匿名掲示板 UI =========
class PostModal(discord.ui.Modal, title="投稿内容を入力"):
    """画像付き: 本文は即時公開・画像はログ承認後に追記。画像なし: 即時公開＋ログ記録。"""
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
            label="画像URL（任意・画像は承認後に反映）", style=discord.TextStyle.short,
            placeholder="https://...", max_length=500, required=False
        )
        self.add_item(self.img_url)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=False)

        board_ch = interaction.client.get_channel(self.channel_id)
        if board_ch is None or not isinstance(board_ch, discord.TextChannel):
            return await interaction.followup.send("対象チャンネルが見つかりません。", ephemeral=True)

        # 表示名（匿名は連番）
        if self.is_anonymous:
            counter_s = await kv_get(gkey_counter(self.channel_id))
            counter = int(counter_s) if counter_s and counter_s.isdigit() else 0
            counter += 1
            await kv_set(gkey_counter(self.channel_id), str(counter))
            display_name = f"{counter}"
        else:
            display_name = interaction.user.display_name  # いまは匿名のみのボタン運用

        content = self.content.value.strip()
        if not content:
            return await interaction.followup.send("本文が空です。", ephemeral=True)

        # 画像URL抽出（承認フローへ）
        img = (self.img_url.value or "").strip()
        if not img:
            img = extract_first_image_url(content) or ""
        img = img.strip()
        has_image = bool(img)

        # 本文だけ公開
        embed = discord.Embed(description=content, color=discord.Color.blurple())
        embed.set_footer(text=f"投稿者: {display_name}")
        published = await board_ch.send(embed=embed)

        # 公開マッピング保存（reveal用）
        post_info = {
            "guild_id": interaction.guild_id,
            "channel_id": self.channel_id,
            "message_id": published.id,
            "anonymous": self.is_anonymous,
            "anon_display": display_name if self.is_anonymous else None,
            "author_id": interaction.user.id,
            "author_name": str(interaction.user),
            "author_display": interaction.user.display_name,
            "img_url": None,
        }
        await kv_set(gkey_postmap(published.id), json.dumps(post_info, ensure_ascii=False))

        # ログ送信（画像なしでも送る）
        log_chan_id_s = await kv_get(gkey_logchan(self.channel_id))
        log_ch = interaction.client.get_channel(int(log_chan_id_s)) if (log_chan_id_s and log_chan_id_s.isdigit()) else None

        if not has_image:
            if isinstance(log_ch, discord.TextChannel):
                le = discord.Embed(title="📝 投稿ログ（画像なし）", description=content, color=discord.Color.dark_gray())
                le.add_field(name="匿名？", value="はい" if self.is_anonymous else "いいえ", inline=True)
                le.add_field(name="表示名", value=display_name, inline=True)
                le.add_field(name="投稿先", value=f"<#{self.channel_id}>", inline=True)
                le.add_field(name="本文メッセージ", value=f"[ジャンプ]({published.jump_url})", inline=False)
                le.add_field(name="送信者", value=f"{interaction.user.mention} ({interaction.user.id})", inline=False)
                await log_ch.send(embed=le)
            await repost_panel(interaction.client, board_ch.id)
            return

        # 画像あり → 承認カード
        if not isinstance(log_ch, discord.TextChannel):
            await interaction.followup.send(
                "画像は承認制ですが、ログチャンネルが未設定のため画像は反映できませんでした（本文は公開済み）。\n"
                "管理者に /board setlog で設定してもらってください。",
                ephemeral=True
            )
            await repost_panel(interaction.client, board_ch.id)
            return

        pending = discord.Embed(title="🕒 画像承認リクエスト", description=content, color=discord.Color.orange())
        pending.add_field(name="匿名？", value="はい" if self.is_anonymous else "いいえ", inline=True)
        pending.add_field(name="表示名", value=display_name, inline=True)
        pending.add_field(name="投稿先", value=f"<#{self.channel_id}>", inline=True)
        pending.add_field(name="本文メッセージ", value=f"[ジャンプ]({published.jump_url})", inline=False)
        pending.add_field(name="送信者", value=f"{interaction.user.mention} ({interaction.user.id})", inline=False)
        pending.set_image(url=img)

        view = ApprovalView()
        log_msg = await log_ch.send(embed=pending, view=view)

        pending_info = {
            "guild_id": interaction.guild_id,
            "board_channel_id": self.channel_id,
            "board_message_id": published.id,
            "log_message_id": log_msg.id,
            "anonymous": self.is_anonymous,
            "anon_display": display_name if self.is_anonymous else None,
            "author_id": interaction.user.id,
            "author_name": str(interaction.user),
            "author_display": interaction.user.display_name,
            "content": content,
            "img_url": img
        }
        await kv_set(gkey_pending(log_msg.id), json.dumps(pending_info, ensure_ascii=False))
        await repost_panel(interaction.client, board_ch.id)

class ApprovalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed_user(interaction.user):
            return await interaction.response.send_message("承認権限がありません。", ephemeral=True)

        pending_s = await kv_get(gkey_pending(interaction.message.id))
        if not pending_s:
            pending_s = await kv_get(gkey_pending_legacy(interaction.message.id))
            if pending_s:
                await kv_set(gkey_pending(interaction.message.id), pending_s)
                await kv_del(gkey_pending_legacy(interaction.message.id))
        if not pending_s:
            return await interaction.response.send_message("承認待ち情報が見つかりません。", ephemeral=True)

        info = json.loads(pending_s)
        board_ch = interaction.client.get_channel(int(info["board_channel_id"]))
        if not isinstance(board_ch, discord.TextChannel):
            return await interaction.response.send_message("投稿先チャンネルが見つかりません。", ephemeral=True)
        try:
            target_msg = await board_ch.fetch_message(int(info["board_message_id"]))
        except Exception:
            return await interaction.response.send_message("本文メッセージが取得できませんでした。", ephemeral=True)

        if target_msg.embeds:
            base = target_msg.embeds[0]
            new_embed = discord.Embed(description=base.description or info["content"], color=discord.Color.blurple())
        else:
            new_embed = discord.Embed(description=info["content"], color=discord.Color.blurple())
        display_name = info["anon_display"] if info["anonymous"] else info["author_display"]
        new_embed.set_footer(text=f"投稿者: {display_name}")
        if info.get("img_url"):
            new_embed.set_image(url=info["img_url"])
        await target_msg.edit(embed=new_embed)

        post_s = await kv_get(gkey_postmap(target_msg.id))
        if post_s:
            post = json.loads(post_s)
            post["img_url"] = info.get("img_url")
            await kv_set(gkey_postmap(target_msg.id), json.dumps(post, ensure_ascii=False))

        new_log_embed = interaction.message.embeds[0]
        new_log_embed.title = "✅ 承認・反映済み"
        new_log_embed.color = discord.Color.green()
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(embed=new_log_embed, view=self)

        await kv_del(gkey_pending(interaction.message.id))
        await kv_del(gkey_pending_legacy(interaction.message.id))
        await interaction.response.send_message("承認して掲示板に画像を反映しました。", ephemeral=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="🛑")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_allowed_user(interaction.user):
            return await interaction.response.send_message("承認権限がありません。", ephemeral=True)

        pending_s = await kv_get(gkey_pending(interaction.message.id))
        if not pending_s:
            pending_s = await kv_get(gkey_pending_legacy(interaction.message.id))
            if pending_s:
                await kv_set(gkey_pending(interaction.message.id), pending_s)
                await kv_del(gkey_pending_legacy(interaction.message.id))
        if not pending_s:
            return await interaction.response.send_message("承認待ち情報が見つかりません。", ephemeral=True)

        new_log_embed = interaction.message.embeds[0]
        new_log_embed.title = "⛔ 実施せず（本文は公開済み）"
        new_log_embed.color = discord.Color.red()
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(embed=new_log_embed, view=self)

        await kv_del(gkey_pending(interaction.message.id))
        await kv_del(gkey_pending_legacy(interaction.message.id))
        await interaction.response.send_message("却下しました（本文は公開済みのまま）。", ephemeral=True)

class BoardView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    # ★ 通常投稿ボタンを削除し、匿名のみ残す
    @discord.ui.button(label="匿名で投稿", style=discord.ButtonStyle.primary, emoji="🕵️")
    async def post_anon(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PostModal(self.channel_id, is_anonymous=True))

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
    msg = await channel.send("**匿名掲示板パネル**\n（匿名のみ）下のボタンから投稿してください。", view=view)
    await kv_set(panel_key, str(msg.id))

# ---- スラッシュグループ（子コマンドに guild 指定は付けない）----
board_group = app_commands.Group(name="board", description="匿名掲示板の設定/操作")

@board_group.command(name="setup", description="このチャンネル（または指定先）に掲示板パネルを設置")
@app_commands.describe(
    channel="掲示板にするテキストチャンネル（未指定ならこのチャンネル）",
    reset_counter="匿名連番を0から再開",
    log_channel="投稿ログ送信先（画像承認用・推奨）"
)
async def board_setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    reset_counter: bool = False,
    log_channel: discord.TextChannel | None = None
):
    if not await guard_allowed(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
    if reset_counter:
        await kv_set(gkey_counter(target.id), "0")
    if log_channel:
        await kv_set(gkey_logchan(target.id), str(log_channel.id))
    await repost_panel(interaction.client, target.id)
    txt = f"掲示板パネルを設置しました：{target.mention}\n"
    if log_channel: txt += f"投稿ログ（承認用）：{log_channel.mention}\n"
    if reset_counter: txt += "匿名連番をリセットしました。"
    await interaction.response.send_message(txt, ephemeral=True)

@board_group.command(name="setlog", description="掲示板の投稿ログ先を設定（画像承認用）")
@app_commands.describe(board_channel="掲示板チャンネル（未指定なら実行場所）", log_channel="ログ送信先")
async def board_setlog(
    interaction: discord.Interaction,
    board_channel: discord.TextChannel | None = None,
    log_channel: discord.TextChannel | None = None
):
    if not await guard_allowed(interaction):
        return
    target = board_channel or interaction.channel
    if not isinstance(target, discord.TextChannel) or not log_channel:
        return await interaction.response.send_message("対象/ログ先はテキストチャンネルを指定してください。", ephemeral=True)
    await kv_set(gkey_logchan(target.id), str(log_channel.id))
    await interaction.response.send_message(f"{target.mention} の投稿ログ先を {log_channel.mention} に設定しました。", ephemeral=True)

@board_group.command(name="reset_counter", description="匿名連番を0にリセット")
@app_commands.describe(channel="対象チャンネル（未指定なら実行場所）")
async def board_reset_counter(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if not await guard_allowed(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
    await kv_set(gkey_counter(target.id), "0")
    await interaction.response.send_message(f"匿名連番をリセットしました：{target.mention}", ephemeral=True)

@board_group.command(name="panel", description="パネルを最下部に再掲")
@app_commands.describe(channel="対象チャンネル（未指定なら実行場所）")
async def board_panel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if not await guard_allowed(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
    await repost_panel(interaction.client, target.id)
    await interaction.response.send_message(f"パネルを再掲しました：{target.mention}", ephemeral=True)

@board_group.command(name="reveal", description="匿名投稿の実投稿者を照会（指定ユーザーのみ）")
@app_commands.describe(message_link="対象メッセージのリンク（右クリック→リンクをコピー）")
async def board_reveal(interaction: discord.Interaction, message_link: str):
    if not await guard_allowed(interaction):
        return
    msg = await fetch_message_from_link(interaction.client, message_link)
    if not msg:
        return await interaction.response.send_message("メッセージリンクが無効です。正しいリンクを指定してください。", ephemeral=True)
    data_s = await kv_get(gkey_postmap(msg.id))
    if not data_s:
        return await interaction.response.send_message("このメッセージの記録が見つかりません。匿名掲示板の投稿ではない可能性があります。", ephemeral=True)
    info = json.loads(data_s)
    desc = (
        f"**匿名？** {'はい' if info.get('anonymous') else 'いいえ'}\n"
        f"**匿名表示名**: {info.get('anon_display') or '-'}\n"
        f"**実投稿者**: <@{info.get('author_id')}> (`{info.get('author_name')}` / 表示名: `{info.get('author_display')}`)\n"
        f"**メッセージ**: {msg.jump_url}"
    )
    await interaction.response.send_message(desc, ephemeral=True)

# ---- 送信後◯秒で削除（新規のみ） ----
@board_group.command(name="autodel_start", description="このチャンネルで新規メッセージを自動削除します")
@app_commands.describe(seconds="削除までの秒数（10〜604800）")
async def board_autodel_start(interaction: discord.Interaction, seconds: app_commands.Range[int, 10, 604800]):
    if not await guard_allowed(interaction):
        return
    await kv_set(gkey_autodel(interaction.channel_id), str(int(seconds)))
    await interaction.response.send_message(
        f"このチャンネルの新規メッセージを **{int(seconds)}秒後** に自動削除します。\n"
        "※ ピン留めと掲示板パネルは削除対象外です。",
        ephemeral=True
    )

@board_group.command(name="autodel_stop", description="このチャンネルの自動削除を停止します")
async def board_autodel_stop(interaction: discord.Interaction):
    if not await guard_allowed(interaction):
        return
    await kv_del(gkey_autodel(interaction.channel_id))
    await interaction.response.send_message("このチャンネルの自動削除を **停止** しました。", ephemeral=True)

# ---- トップレベル：掲示板とは無関係の定期掃除コマンド（ギルド即時反映）----
def guild_only_deco(func):
    return app_commands.guilds(*[discord.Object(id=g) for g in (GUILD_IDS or [PRIMARY_GUILD_ID])])(func)

@tree.command(name="purge_start", description="一定間隔で古い履歴を定期削除（掲示板とは無関係）")
@guild_only_deco
@app_commands.describe(
    interval_seconds="実行間隔（5〜86400秒）",
    keep_hours="保存期間（0〜720時間：これより古いメッセージを削除）",
    batch_limit="1回の最大削除数（10〜1000、既定200）"
)
async def purge_start(
    interaction: discord.Interaction,
    interval_seconds: app_commands.Range[int, 5, 86400],
    keep_hours: app_commands.Range[int, 0, 720],
    batch_limit: app_commands.Range[int, 10, 1000] = 200
):
    # Botの権限チェック
    me = interaction.guild.me if interaction.guild else None
    if not (me and interaction.channel.permissions_for(me).manage_messages):
        return await interaction.response.send_message("ボットに **メッセージの管理** 権限が必要です。", ephemeral=True)

    cfg = {"interval": int(interval_seconds), "keep_hours": int(keep_hours), "batch_limit": int(batch_limit)}
    await kv_set(gkey_purge(interaction.channel_id), json.dumps(cfg, ensure_ascii=False))
    await start_purge_for_channel(interaction.client, interaction.channel_id, cfg["interval"], cfg["keep_hours"], cfg["batch_limit"])
    await interaction.response.send_message(
        f"✅ 定期掃除を開始しました（掲示板とは無関係）。\n"
        f"- 実行間隔: **{cfg['interval']}秒**\n"
        f"- 保存期間: **{cfg['keep_hours']}時間**\n"
        f"- 1回の上限: **{cfg['batch_limit']}件**",
        ephemeral=True
    )

@tree.command(name="purge_stop", description="定期掃除を停止（掲示板とは無関係）")
@guild_only_deco
async def purge_stop(interaction: discord.Interaction):
    await kv_del(gkey_purge(interaction.channel_id))
    await stop_purge_for_channel(interaction.channel_id)
    await interaction.response.send_message("⏹️ 定期掃除を停止しました。", ephemeral=True)

# ---- /ping ----
@tree.command(name="ping", description="生存確認")
@guild_only_deco
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🏓", ephemeral=True)

# ---- on_message: 送信後◯秒削除のスケジュール ----
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if not isinstance(message.channel, discord.TextChannel):
        return
    if message.author is None:
        return

    sec_s = await kv_get(gkey_autodel(message.channel.id))
    if not sec_s:
        return
    try:
        seconds = int(sec_s)
    except Exception:
        return
    if seconds <= 0:
        return

    # ピン留めとパネルは削除対象外
    if getattr(message, "pinned", False):
        return
    panel_id_s = await kv_get(gkey_panel(message.channel.id))
    if panel_id_s and panel_id_s.isdigit() and int(panel_id_s) == message.id:
        return

    async def _delete_later(msg: discord.Message, delay: int):
        try:
            await asyncio.sleep(delay)
            await msg.delete()
        except Exception:
            pass

    asyncio.create_task(_delete_later(message, seconds))

# ---- ready ----
@bot.event
async def on_ready():
    user_info = "(user: None)" if bot.user is None else f"{bot.user} (ID: {bot.user.id})"
    log.info(f"Logged in as {user_info}")
    try:
        # /board グループの登録
        if board_group not in tree.get_commands():
            tree.add_command(board_group)

        # ギルド同期（即時反映）
        for gid in GUILD_IDS:
            await tree.sync(guild=discord.Object(id=gid))
            log.info(f"Synced commands to guild {gid}")
    except Exception as e:
        log.exception("Command sync failed: %s", e)

    # --- 起動時に定期掃除タスクを復元 ---
    try:
        allkv = await kv_all()
        prefix = "cleaner:purge:"
        for k, v in allkv.items():
            if not k.startswith(prefix):
                continue
            try:
                ch_id = int(k.split(":")[-1])
            except Exception:
                continue
            cfg = json.loads(v)
            interval = int(cfg.get("interval", 600))
            keep_hours = int(cfg.get("keep_hours", 24))
            batch_limit = int(cfg.get("batch_limit", 200))
            await start_purge_for_channel(bot, ch_id, interval, keep_hours, batch_limit)
            log.info(f"[purge] restored task for channel={ch_id} interval={interval}s keep={keep_hours}h batch={batch_limit}")
    except Exception as e:
        log.exception("restore purge failed: %s", e)

# ---- main ----
def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN が未設定です（Railway Variables で設定してください）")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
