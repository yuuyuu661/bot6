import discord
from discord.ext import commands
from discord import app_commands

# ====== 設定 ======
GUILD_ID = 1398607685158440991  # サーバーID
ALLOWED_USER_IDS = [440893662701027328, 716667546241335328]  # コマンド利用を許可するユーザーID

# ====== Bot設定 ======
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ====== 投稿用モーダル ======
class PostModal(discord.ui.Modal, title="投稿フォーム"):
    def __init__(self, channel_id: int, is_anonymous: bool = False):
        super().__init__()
        self.channel_id = channel_id
        self.is_anonymous = is_anonymous

        self.add_item(
            discord.ui.TextInput(
                label="内容を入力してください",
                style=discord.TextStyle.long,
                placeholder="ここに入力…",
                required=True,
                max_length=2000
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.client.get_channel(self.channel_id)
        content = self.children[0].value

        if self.is_anonymous:
            embed = discord.Embed(
                title="🕵️ 匿名投稿",
                description=content,
                color=discord.Color.blue()
            )
            await channel.send(embed=embed)
            await interaction.response.send_message("✅ 匿名で投稿しました！", ephemeral=True)
        else:
            embed = discord.Embed(
                title=f"💬 {interaction.user.display_name} さんの投稿",
                description=content,
                color=discord.Color.green()
            )
            await channel.send(embed=embed)
            await interaction.response.send_message("✅ 投稿しました！", ephemeral=True)


# ====== ボタン付きパネル ======
class BoardView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    # 匿名投稿ボタン
    @discord.ui.button(label="匿名で投稿", style=discord.ButtonStyle.primary, emoji="🕵️")
    async def anon_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PostModal(self.channel_id, is_anonymous=True))

    # 通常投稿ボタン
    @discord.ui.button(label="通常投稿", style=discord.ButtonStyle.secondary, emoji="💬")
    async def normal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PostModal(self.channel_id, is_anonymous=False))


# ====== コマンド ======
@tree.command(name="board", description="投稿パネルを表示します（特定ユーザーのみ使用可能）")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def board(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ あなたはこのコマンドを使う権限がありません。", ephemeral=True)
        return

    view = BoardView(channel.id)
    await interaction.response.send_message("📌 投稿パネルを設置しました！", view=view)


# ====== 起動 ======
@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Bot connected as {bot.user}")


bot.run("YOUR_BOT_TOKEN")
