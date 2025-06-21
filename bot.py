import os
import discord
from discord.ext import commands
import asyncio
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError
import concurrent.futures
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
guild_states = {}

class GuildAudioState:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.queue_titles = []
        self.play_next = asyncio.Event()
        self.voice_client = None
        self.audio_task = None
        self.lock = asyncio.Lock()

    async def audio_player_loop(self, ctx):
        while True:
            self.play_next.clear()
            try:
                song = await self.queue.get()
                async with self.lock:
                    if self.queue_titles:
                        self.queue_titles.pop(0)
            except asyncio.CancelledError:
                return

            if not self.voice_client or not self.voice_client.is_connected():
                try:
                    self.voice_client = await ctx.author.voice.channel.connect()
                    await ctx.send("🔄 Reconnected to voice.")
                except Exception as e:
                    await ctx.send("❌ Disconnected and unable to reconnect.")
                    return

            try:
                source = discord.FFmpegPCMAudio(
                    song['url'],
                    before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    options="-vn"
                )
            except Exception:
                await ctx.send(f"❌ Error playing: **{song['title']}**")
                continue

            try:
                self.voice_client.play(
                    source,
                    after=lambda e: self.play_next.set()
                )
                await ctx.send(f"🎶 Now playing: **{song['title']}**")
            except discord.ClientException:
                await ctx.send("⚠️ Playback error - skipping track")
                continue

            await self.play_next.wait()

    def cleanup(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.queue_titles.clear()
        if self.voice_client and self.voice_client.is_connected():
            asyncio.create_task(self.voice_client.disconnect(force=True))
        self.voice_client = None
        if self.audio_task and not self.audio_task.done():
            self.audio_task.cancel()
        self.audio_task = None


def get_state(guild_id):
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildAudioState()
    return guild_states[guild_id]


async def search_youtube(query):
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'skip_download': True,
        'default_search': 'ytsearch',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if 'entries' in info:
                info = info['entries'][0]
            return {
                'title': info.get('title'),
                'url': info.get('url'),
                'webpage_url': info.get('webpage_url'),
            }
    except (DownloadError, ExtractorError) as e:
        err_msg = str(e).lower()
        if "sign in" in err_msg or "age" in err_msg:
            return {'error': "🔞 Video is age-restricted."}
        if "drm" in err_msg:
            return {'error': "🔒 DRM-protected video cannot be played."}
        return {'error': "❌ Could not retrieve video. Try another query."}
    except Exception:
        return {'error': "❗ Unexpected error occurred."}


async def ensure_voice(ctx):
    state = get_state(ctx.guild.id)

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ You must be in a voice channel first!")
        return False

    user_channel = ctx.author.voice.channel

    if state.voice_client and state.voice_client.channel == user_channel:
        return True

    if state.voice_client and state.voice_client.channel != user_channel:
        await ctx.send("⚠️ I'm in another voice channel. Use `!leave` first.")
        return False

    try:
        state.voice_client = await user_channel.connect()
        return True
    except discord.ClientException as e:
        print(f"Connection error: {e}")
        await ctx.send("❌ Failed to join voice channel")
        return False

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id != bot.user.id:
        return
    state = get_state(member.guild.id)
    state.voice_client = member.guild.voice_client
    if before.channel and not after.channel:
        state.cleanup()


@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(title="🎵 Bot Commands", color=discord.Color.blurple())
    embed.add_field(name="!play <query>", value="Search and play audio from YouTube", inline=False)
    embed.add_field(name="!pause", value="Pause the current track", inline=True)
    embed.add_field(name="!resume", value="Resume paused track", inline=True)
    embed.add_field(name="!skip", value="Skip current track", inline=True)
    embed.add_field(name="!stop", value="Stop and clear queue", inline=True)
    embed.add_field(name="!queue", value="Show current queue", inline=True)
    embed.add_field(name="!join", value="Force bot to join voice", inline=True)
    embed.add_field(name="!leave", value="Force bot to disconnect", inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def play(ctx, *, query: str):
    if not await ensure_voice(ctx):
        return
    state = get_state(ctx.guild.id)
    song = await search_youtube(query)
    if not song:
        await ctx.send("❌ Unknown error occurred.")
        return
    if song.get("error"):
        await ctx.send(song["error"])
        return
    async with state.lock:
        await state.queue.put(song)
        state.queue_titles.append(song['title'])
    if state.audio_task is None or state.audio_task.done():
        state.audio_task = asyncio.create_task(state.audio_player_loop(ctx))
    else:
        await ctx.send(f"➕ Added to queue: **{song['title']}**")


@bot.command()
async def skip(ctx):
    state = get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.stop()
        await ctx.send("⏭️ Skipped current track")
    else:
        await ctx.send("⚠️ Nothing is playing")


@bot.command()
async def pause(ctx):
    state = get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.pause()
        await ctx.send("⏸️ Paused")
    else:
        await ctx.send("⚠️ Nothing is playing")


@bot.command()
async def resume(ctx):
    state = get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_paused():
        state.voice_client.resume()
        await ctx.send("▶️ Resumed")
    else:
        await ctx.send("⚠️ Not paused")


@bot.command()
async def stop(ctx):
    state = get_state(ctx.guild.id)
    state.cleanup()
    await ctx.send("🛑 Stopped and cleared queue")


@bot.command()
async def queue(ctx):
    state = get_state(ctx.guild.id)
    if not state.queue_titles:
        await ctx.send("📭 Queue is empty")
        return

    queue_list = state.queue_titles[:10]
    message = "\n".join(f"{idx+1}. {title}" for idx, title in enumerate(queue_list))

    if len(state.queue_titles) > 10:
        message += f"\n...and {len(state.queue_titles) - 10} more"

    await ctx.send(f"📜 Current queue:\n{message}")


@bot.command(name="join")
async def join(ctx):
    if await ensure_voice(ctx):
        await ctx.send(f"✅ Joined {ctx.author.voice.channel}!")


@bot.command(name="leave")
async def leave(ctx):
    if ctx.voice_client:
        state = get_state(ctx.guild.id)
        state.cleanup()
        await ctx.send("👋 Left voice channel")
    else:
        await ctx.send("⚠️ Not in a voice channel")


bot.run(os.getenv("DISCORD_TOKEN"))
