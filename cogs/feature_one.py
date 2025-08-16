import re
import json
import discord
from discord.ext import commands
from discord import app_commands

from config import GUILD_IDS
from db import kv_get, kv_set

# ====== 設定：/board reveal を使えるユーザーID ======
ALLOWED_USER_IDS = {716667546241335328, 440893662701027328}

# ====== KVキー ======
PANEL_KEY    = "anonboard:panel:{channel_id}"
COUNTER_KEY  = "anonboard:counter:{channel_id}"
LOGCHAN_KEY  = "anonboard:logchan:{channel_id}"
POSTMAP_KEY  = "anonboard:post:{message_id}"  # 投稿メッセージID -> 投稿者情報(JSON)

def gkey_panel(channel_id: int) -> str:
    return PANEL_KEY.format(channel_id=channel_id)

def gkey_counter(channel_id: int) -> str:
    return COUNTER_KEY.format(channel_id=channel_id)

def gkey_logchan(channel_id: int) -> str:
    return LOGCHAN_KEY.format(channel_id=channel_id)

def gkey_postmap(message_id: int) -> str:
    return POSTMAP_KEY.format(message_id=message_id)

# ====== 画像URL抽出まわり ======
IMAGE_EXT_RE = re.compile(r"\.(?:png|jpg|jpeg|gif|webp)(?:\?.*)?$", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

def is_image_url(url: str) -> bool:
    # 拡張子 or Discord系CDN等（簡易判定）
    if IMAGE_EXT_RE.search(url):
        return True
    cdn_like = ("cdn.discordapp.com", "media.discordapp.net", "images-ext", "pbs.twimg.com")
    return any(host in url for host in cdn_like)

def extract_first_image_url(text: str) -> str | None:
    for m in URL_RE.findall(text or ""):
        if is_image_url(m):
            return m
    return None

# ====== モーダル ======
class PostModal(discord.ui.Modal, title="投稿内容を入力"):
    def __init__(self, channel_id: int, is_anonymous: bool):
        super().__init__(timeout=180)
        self.channel_id = channel_id
        self.is_anonymous = is_anonymous

        self.content = discord.ui.TextInput(
            label="本文",
            style=discord.TextStyle.paragraph,
            placeholder="ここにメッセージを入力",
            max_length=2000,
            required=True
        )
        self.add_item(self.content)

        self.img_url = discord.ui.TextInput(
            label="画像URL（任意）",
            style=discord.TextStyle.short,
            placeholder="https://...（画像リンク）",
            max_length=500,
            required=False
        )
        self.add_item(self.img_url)

    async def on_submit(self, interaction: discord.Interaction):
        # 対象チャンネル取得
        channel = interaction.client.get_channel(self.channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("対象チャンネルが見つかりません。設定をやり直してください。", ephemeral=True)
            return

        # 表示名の決定（匿名 → 連番）
        if self.is_anonymous:
            counter_s = await kv_get(gkey_counter(self.channel_id))
            counter = int(counter_s) if counter_s and counter_s.isdigit() else 0
            counter += 1
            await kv_set(gkey_counter(self.channel_id), str(counter))
            display_name = f"{counter}"
        else:
            display_name = interaction.user.display_name

        # 本文
        content = self.content.value.strip()
        if not content:
            await interaction.response.send_message("本文が空です。", ephemeral=True)
            return

        # 画像URL（優先：フォーム、次点：本文から抽出）
        img = (self.img_url.value or "").strip()
        if not img:
            img = extract_first_image_url(content) or ""
        if img and not is_image_url(img):
            # 画像っぽくないURLは画像としては載せない（本文内のURLはそのまま）
            img = ""

        # 埋め込みを送信
        embed = discord.Embed(description=content, color=discord.Color.blurple())
        embed.set_footer(text=f"投稿者: {display_name}")
        if img:
            embed.set_image(url=img)

        sent = await channel.send(embed=embed)

        # 投稿マッピングを保存（/board reveal 用）
        post_info = {
            "guild_id": interaction.guild_id,
            "channel_id": self.channel_id,
            "message_id": sent.id,
            "anonymous": self.is_anonymous,
            "anon_display": display_name if self.is_anonymous else None,
            "author_id": interaction.user.id,
            "author_name": str(interaction.user),           # name#discriminator（表示形式）
            "author_display": interaction.user.display_name,
            "img_url": img or None,
        }
        await kv_set(gkey_postmap(sent.id), json.dumps(post_info, ensure_ascii=False))

        # ログチャンネルが設定されていれば詳細を送る（モデレーター閲覧用）
        log_chan_id_s = await kv_get(gkey_logchan(self.channel_id))
        if log_chan_id_s and log_chan_id_s.isdigit():
            log_chan = interaction.client.get_channel(int(log_chan_id_s))
            if isinstance(log_chan, discord.TextChannel):
                le = discord.Embed(
                    title="匿名掲示板 投稿ログ",
                    description=content,
                    color=discord.Color.dark_gray()
                )
                le.add_field(name="匿名？", value="はい" if self.is_anonymous else "いいえ", inline=True)
                le.add_field(name="表示名", value=display_name, inline=True)
                le.add_field(name="実投稿者", value=f"{interaction.user.mention} ({interaction.user.id})", inline=False)
                le.add_field(name="メッセージ", value=f"[ジャンプ]({sent.jump_url})", inline=False)
                if img:
                    le.set_image(url=img)
                await log_chan.send(embed=le)

        # パネルを再掲（＝最下部へ）
        await repost_panel(interaction.client, channel.id)

        await interaction.response.send_message("投稿しました。", ephemeral=True)

# ====== ビュー（ボタンUI） ======
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

# ====== パネルの再掲（最下部固定化） ======
async def repost_panel(bot: commands.Bot, channel_id: int):
    """古いパネルを削除し、最新のパネルを最下部に再投稿してIDを保存。"""
    panel_key = gkey_panel(channel_id)
    panel_id_s = await kv_get(panel_key)

    channel = bot.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return

    # 古いパネルメッセージ削除
    if panel_id_s and panel_id_s.isdigit():
        try:
            old_msg = await channel.fetch_message(int(panel_id_s))
            await old_msg.delete()
        except Exception:
            pass

    # 新しいパネル送信
    view = BoardView(channel_id)
    msg = await channel.send(
        content="**匿名掲示板パネル**\n下のボタンから投稿してください。",
        view=view
    )
    await kv_set(panel_key, str(msg.id))

# ====== Cog本体 ======
class FeatureOne(commands.Cog):
    """匿名掲示板（画像URL対応／ログ／指定ユーザー限定の投稿者照会／最下部固定パネル）"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(
        name="board",
        description="匿名掲示板の設定/操作",
        guild_ids=GUILD_IDS or None
    )

    @group.command(name="setup", description="このチャンネルまたは指定チャンネルに匿名掲示板パネルを設置します")
    @app_commands.describe(
        channel="掲示板を設置するテキストチャンネル（未指定なら実行した場所）",
        reset_counter="匿名連番カウンタを0から再開するか",
        log_channel="投稿ログを送るチャンネル（モデレーター向け）"
    )
    async def setup_board(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reset_counter: bool = False,
        log_channel: discord.TextChannel | None = None
    ):
        # 権限チェック：管理者 or チャンネル管理（設置操作のみ）
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message("権限がありません。（チャンネル管理 or 管理者）", ephemeral=True)

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)

        if reset_counter:
            await kv_set(gkey_counter(target.id), "0")

        if log_channel:
            await kv_set(gkey_logchan(target.id), str(log_channel.id))

        await repost_panel(self.bot, target.id)
        await interaction.response.send_message(
            f"掲示板パネルを設置しました：{target.mention}\n"
            + (f"投稿ログチャンネル：{log_channel.mention}\n" if log_channel else "")
            + ("匿名連番をリセットしました。" if reset_counter else ""),
            ephemeral=True
        )

    @group.command(name="setlog", description="投稿ログを送るチャンネルを設定します（モデレーター向け）")
    @app_commands.describe(
        board_channel="対象の掲示板チャンネル（未指定なら実行場所）",
        log_channel="ログ送信先テキストチャンネル"
    )
    async def setlog(
        self,
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

    @group.command(name="reset_counter", description="匿名連番カウンタを0にリセットします")
    @app_commands.describe(channel="対象チャンネル（未指定なら実行した場所）")
    async def reset_counter(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message("権限がありません。", ephemeral=True)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
        await kv_set(gkey_counter(target.id), "0")
        await interaction.response.send_message(f"匿名連番をリセットしました：{target.mention}", ephemeral=True)

    @group.command(name="panel", description="パネルを手動で再掲します（最下部に移動）")
    @app_commands.describe(channel="対象チャンネル（未指定なら実行した場所）")
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message("権限がありません。", ephemeral=True)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
        await repost_panel(self.bot, target.id)
        await interaction.response.send_message(f"パネルを再掲しました：{target.mention}", ephemeral=True)

    @group.command(name="reveal", description="匿名投稿の実投稿者を照会（指定ユーザー限定）")
    @app_commands.describe(message="対象メッセージ（リンク or 直指定）")
    async def reveal(self, interaction: discord.Interaction, message: discord.Message):
        # ✅ ユーザーID制御：許可されたIDのみ実行可能
        if interaction.user.id not in ALLOWED_USER_IDS:
            return await interaction.response.send_message("このコマンドを使う権限がありません。", ephemeral=True)

        data_s = await kv_get(gkey_postmap(message.id))
        if not data_s:
            return await interaction.response.send_message("このメッセージは匿名掲示板の投稿として記録がありません。", ephemeral=True)

        info = json.loads(data_s)
        author_id = info.get("author_id")
        author_name = info.get("author_name")
        author_display = info.get("author_display")
        anon_flag = info.get("anonymous")
        anon_disp = info.get("anon_display")

        desc = (
            f"**匿名？** {'はい' if anon_flag else 'いいえ'}\n"
            f"**匿名表示名**: {anon_disp or '-'}\n"
            f"**実投稿者**: <@{author_id}> (`{author_name}` / 表示名: `{author_display}`)\n"
            f"**メッセージ**: {message.jump_url}"
        )
        await interaction.response.send_message(desc, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(FeatureOne(bot))
    # スラッシュグループ登録
    if FeatureOne.group not in bot.tree.get_commands():
        bot.tree.add_command(FeatureOne.group)
