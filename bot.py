import asyncio
import collections
import os
import random
from dataclasses import dataclass
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

IDLE_TIMEOUT = 300      # segundos sem tocar antes de sair do canal
MAX_PLAYLIST = 50       # máximo de músicas por playlist
COLOR = 0x5865F2

SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "skip_download": True,
    "source_address": "0.0.0.0",
}
STREAM_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "source_address": "0.0.0.0",
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# Cookies do YouTube (opcional): ajudam a passar do bloqueio "Sign in to confirm you're not a bot".
# No Render, crie um Secret File chamado cookies.txt (fica em /etc/secrets/cookies.txt).
COOKIES_SRC = os.getenv("COOKIES_FILE", "/etc/secrets/cookies.txt")
if os.path.exists(COOKIES_SRC):
    try:
        import shutil
        import tempfile
        cookies_copy = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        shutil.copy(COOKIES_SRC, cookies_copy)  # cópia gravável, o original é somente leitura
        SEARCH_OPTS["cookiefile"] = cookies_copy
        STREAM_OPTS["cookiefile"] = cookies_copy
        print("Cookies do YouTube carregados.")
    except Exception as exc:
        print(f"Não consegui carregar os cookies: {exc}")
else:
    pasta = os.path.dirname(COOKIES_SRC) or "."
    conteudo = os.listdir(pasta) if os.path.isdir(pasta) else "pasta inexistente"
    print(f"Cookies NÃO encontrados em {COOKIES_SRC}. Conteúdo de {pasta}: {conteudo}")


@dataclass
class Track:
    title: str
    url: str
    duration: Optional[int]
    requester: str


def fmt_time(seconds: Optional[int]) -> str:
    if not seconds:
        return "ao vivo"
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def search_tracks(query: str, requester: str) -> list:
    is_url = query.startswith(("http://", "https://"))
    opts = dict(SEARCH_OPTS)
    if is_url and "/playlist" in query:
        opts["playlistend"] = MAX_PLAYLIST
    else:
        opts["noplaylist"] = True
    target = query if is_url else f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        return []
    entries = info.get("entries") or [info]
    tracks = []
    for e in entries:
        if not e:
            continue
        url = e.get("webpage_url") or e.get("url")
        if not url and e.get("id"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        if not url:
            continue
        dur = e.get("duration")
        tracks.append(Track(e.get("title") or "Sem título", url,
                            int(dur) if dur else None, requester))
    return tracks


def _resolve(target: str) -> dict:
    with yt_dlp.YoutubeDL(STREAM_OPTS) as ydl:
        info = ydl.extract_info(target, download=False)
    if "entries" in info:
        info = info["entries"][0]
    return {"url": info["url"], "thumbnail": info.get("thumbnail")}


def resolve_stream(url: str, title: Optional[str] = None) -> dict:
    """Tenta o YouTube; se for bloqueado, procura a mesma música no SoundCloud."""
    try:
        return _resolve(url)
    except Exception as first_error:
        if title:
            try:
                return _resolve(f"scsearch1:{title}")
            except Exception:
                pass
        raise first_error


players = {}


class GuildPlayer:
    def __init__(self, bot, guild, channel):
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.queue = collections.deque()
        self.current: Optional[Track] = None
        self.repeat = "off"  # off | track | queue
        self.volume = 0.5
        self.skipped = False
        self.thumb = None
        self.aloop = asyncio.get_running_loop()
        self.next_event = asyncio.Event()
        self.wakeup = asyncio.Event()
        self.task = asyncio.create_task(self.run())

    def add(self, tracks):
        self.queue.extend(tracks)
        self.wakeup.set()

    def _after(self, err):
        if err:
            print(f"[player] erro: {err}")
        self.aloop.call_soon_threadsafe(self.next_event.set)

    async def say(self, content=None, **kwargs):
        if self.channel:
            try:
                await self.channel.send(content, **kwargs)
            except discord.HTTPException:
                pass

    def now_embed(self, track: Track) -> discord.Embed:
        e = discord.Embed(title="🎶 Tocando agora", description=f"**[{track.title}]({track.url})**", color=COLOR)
        e.add_field(name="Duração", value=fmt_time(track.duration))
        e.add_field(name="Pedido por", value=track.requester)
        e.add_field(name="Repetir", value={"off": "Desligado", "track": "Música", "queue": "Fila"}[self.repeat])
        if self.thumb:
            e.set_thumbnail(url=self.thumb)
        return e

    async def run(self):
        while True:
            if not self.queue:
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    await self.say("👋 Saí do canal por inatividade.")
                    await self.destroy()
                    return
                continue

            track = self.queue.popleft()
            vc = self.guild.voice_client
            if vc is None:
                await self.destroy()
                return

            self.current = track
            self.skipped = False
            try:
                info = await asyncio.to_thread(resolve_stream, track.url, track.title)
            except Exception as exc:
                self.current = None
                await self.say(f"⚠️ Pulei **{track.title}**: não consegui carregar (`{str(exc)[:150]}`)")
                continue

            if self.skipped:
                self.current = None
                continue

            vc = self.guild.voice_client
            if vc is None:
                await self.destroy()
                return

            self.thumb = info.get("thumbnail")
            self.next_event.clear()
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTS), volume=self.volume
            )
            try:
                vc.play(source, after=self._after)
            except Exception as exc:
                self.current = None
                await self.say(f"⚠️ Erro ao tocar **{track.title}**: `{str(exc)[:150]}`")
                continue

            await self.say(embed=self.now_embed(track), view=Controls())
            await self.next_event.wait()

            self.current = None
            if self.repeat == "track" and not self.skipped:
                self.queue.appendleft(track)
            elif self.repeat == "queue":
                self.queue.append(track)

    async def destroy(self, disconnect=True):
        players.pop(self.guild.id, None)
        self.queue.clear()
        if self.task is not asyncio.current_task():
            self.task.cancel()
        vc = self.guild.voice_client
        if vc and disconnect:
            try:
                vc.stop()
                await vc.disconnect()
            except Exception:
                pass


def get_player(guild, channel) -> GuildPlayer:
    p = players.get(guild.id)
    if p is None:
        p = GuildPlayer(bot, guild, channel)
        players[guild.id] = p
    p.channel = channel
    return p


def user_channel(interaction):
    voice = getattr(interaction.user, "voice", None)
    return voice.channel if voice else None


async def ensure_voice(interaction):
    """Entra no canal de voz de quem chamou. Usar depois de interaction.response.defer()."""
    channel = user_channel(interaction)
    if channel is None:
        await interaction.followup.send("❌ Entre em um canal de voz primeiro.")
        return None
    vc = interaction.guild.voice_client
    try:
        if vc is None:
            vc = await channel.connect(self_deaf=True)
        elif vc.channel != channel:
            await vc.move_to(channel)
    except Exception as exc:
        await interaction.followup.send(f"❌ Não consegui entrar no canal: `{str(exc)[:150]}`")
        return None
    return vc


class Controls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc is None or players.get(interaction.guild_id) is None or user_channel(interaction) != vc.channel:
            await interaction.response.send_message("❌ Entre no mesmo canal de voz do bot.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⏯ Pausar/Retomar", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            msg = "▶️ Retomado."
        elif vc.is_playing():
            vc.pause()
            msg = "⏸️ Pausado."
        else:
            msg = "Nada tocando."
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="⏭ Pular", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = players[interaction.guild_id]
        vc = interaction.guild.voice_client
        p.skipped = True
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await interaction.response.send_message("⏭️ Pulada.", ephemeral=True)

    @discord.ui.button(label="🔁 Repetir", style=discord.ButtonStyle.secondary)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = players[interaction.guild_id]
        order = ["off", "track", "queue"]
        p.repeat = order[(order.index(p.repeat) + 1) % 3]
        names = {"off": "desligado", "track": "música", "queue": "fila"}
        await interaction.response.send_message(f"🔁 Repetir: {names[p.repeat]}", ephemeral=True)

    @discord.ui.button(label="🔀 Misturar", style=discord.ButtonStyle.secondary)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = players[interaction.guild_id]
        items = list(p.queue)
        random.shuffle(items)
        p.queue = collections.deque(items)
        await interaction.response.send_message("🔀 Fila misturada.", ephemeral=True)

    @discord.ui.button(label="⏹ Parar", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = players[interaction.guild_id]
        vc = interaction.guild.voice_client
        p.queue.clear()
        p.repeat = "off"
        p.skipped = True
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await interaction.response.send_message("⏹️ Parado e fila limpa.", ephemeral=True)


class Tree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Use os comandos dentro de um servidor.", ephemeral=True)
            return False
        return True


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(), tree_cls=Tree)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = MusicBot()


@bot.event
async def on_ready():
    print(f"Online como {bot.user} (id {bot.user.id})")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))


@bot.event
async def on_voice_state_update(member, before, after):
    # bot foi desconectado manualmente
    if member.id == bot.user.id and before.channel and after.channel is None:
        p = players.get(member.guild.id)
        if p:
            await p.destroy(disconnect=False)
        return
    # saiu do canal do bot: se ficar sozinho por 60s, vai embora
    vc = member.guild.voice_client
    if vc and before.channel == vc.channel and after.channel != vc.channel:
        await asyncio.sleep(60)
        vc = member.guild.voice_client
        if vc and vc.channel and not [m for m in vc.channel.members if not m.bot]:
            p = players.get(member.guild.id)
            if p:
                await p.destroy()
            else:
                await vc.disconnect()


# ----------------------------- comandos -----------------------------

@bot.tree.command(name="play", description="Toca uma música (nome ou link) ou playlist do YouTube")
@app_commands.describe(query="Nome da música ou link")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    try:
        tracks = await asyncio.to_thread(search_tracks, query, interaction.user.display_name)
    except Exception as exc:
        await interaction.followup.send(f"❌ Não consegui buscar: `{str(exc)[:200]}`")
        return
    if not tracks:
        await interaction.followup.send("❌ Nada encontrado.")
        return
    player = get_player(interaction.guild, interaction.channel)
    player.add(tracks)
    if len(tracks) == 1:
        t = tracks[0]
        await interaction.followup.send(f"➕ Adicionada: **{t.title}** ({fmt_time(t.duration)})")
    else:
        await interaction.followup.send(f"➕ **{len(tracks)}** músicas adicionadas à fila.")


@bot.tree.command(name="teste", description="Toca um som de teste (bipe) para checar o áudio")
async def teste(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc is None:
        return
    if vc.is_playing() or vc.is_paused():
        await interaction.followup.send("Já tem algo tocando. Use /stop e tente de novo.")
        return
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio("sine=frequency=440:duration=4", before_options="-f lavfi", options="-vn"),
        volume=0.5,
    )
    vc.play(source)
    await interaction.followup.send("🔔 Tocando um bipe por 4 segundos. Você ouviu?")


@bot.tree.command(name="pause", description="Pausa a música")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Pausado.")
    else:
        await interaction.response.send_message("Nada tocando.", ephemeral=True)


@bot.tree.command(name="resume", description="Retoma a música")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Retomado.")
    else:
        await interaction.response.send_message("Nada pausado.", ephemeral=True)


@bot.tree.command(name="skip", description="Pula a música atual")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    p = players.get(interaction.guild_id)
    if not vc or not p or not p.current:
        await interaction.response.send_message("Nada tocando.", ephemeral=True)
        return
    p.skipped = True
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    await interaction.response.send_message("⏭️ Pulada.")


@bot.tree.command(name="stop", description="Para a música e limpa a fila")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    p = players.get(interaction.guild_id)
    if not vc or not p:
        await interaction.response.send_message("Nada tocando.", ephemeral=True)
        return
    p.queue.clear()
    p.repeat = "off"
    p.skipped = True
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    await interaction.response.send_message("⏹️ Parado e fila limpa.")


@bot.tree.command(name="queue", description="Mostra a fila")
async def queue_cmd(interaction: discord.Interaction):
    p = players.get(interaction.guild_id)
    if not p or (not p.current and not p.queue):
        await interaction.response.send_message("A fila está vazia.", ephemeral=True)
        return
    lines = []
    if p.current:
        lines.append(f"**Tocando:** {p.current.title} ({fmt_time(p.current.duration)})")
    for i, t in enumerate(list(p.queue)[:10], 1):
        lines.append(f"`{i}.` {t.title} ({fmt_time(t.duration)})")
    if len(p.queue) > 10:
        lines.append(f"… e mais {len(p.queue) - 10}")
    total = sum(t.duration or 0 for t in p.queue)
    embed = discord.Embed(title="📜 Fila", description="\n".join(lines)[:4000], color=COLOR)
    embed.set_footer(text=f"{len(p.queue)} na fila • {fmt_time(total)} restantes")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Mostra a música atual")
async def nowplaying(interaction: discord.Interaction):
    p = players.get(interaction.guild_id)
    if not p or not p.current:
        await interaction.response.send_message("Nada tocando.", ephemeral=True)
        return
    await interaction.response.send_message(embed=p.now_embed(p.current), view=Controls())


@bot.tree.command(name="volume", description="Ajusta o volume (1-100)")
@app_commands.describe(level="Volume de 1 a 100")
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 1, 100]):
    p = players.get(interaction.guild_id)
    if not p:
        await interaction.response.send_message("Nada tocando.", ephemeral=True)
        return
    p.volume = level / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p.volume
    await interaction.response.send_message(f"🔊 Volume: {level}%")


@bot.tree.command(name="loop", description="Define o modo de repetição")
@app_commands.choices(mode=[
    app_commands.Choice(name="Desligado", value="off"),
    app_commands.Choice(name="Música", value="track"),
    app_commands.Choice(name="Fila", value="queue"),
])
async def loop_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    p = players.get(interaction.guild_id)
    if not p:
        await interaction.response.send_message("Nada tocando.", ephemeral=True)
        return
    p.repeat = mode.value
    await interaction.response.send_message(f"🔁 Repetir: **{mode.name}**")


@bot.tree.command(name="shuffle", description="Mistura a fila")
async def shuffle(interaction: discord.Interaction):
    p = players.get(interaction.guild_id)
    if not p or len(p.queue) < 2:
        await interaction.response.send_message("Fila pequena demais para misturar.", ephemeral=True)
        return
    items = list(p.queue)
    random.shuffle(items)
    p.queue = collections.deque(items)
    await interaction.response.send_message("🔀 Fila misturada.")


@bot.tree.command(name="remove", description="Remove uma música da fila pela posição")
@app_commands.describe(position="Posição na fila (veja /queue)")
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1, 1000]):
    p = players.get(interaction.guild_id)
    if not p or position > len(p.queue):
        await interaction.response.send_message("❌ Posição inválida.", ephemeral=True)
        return
    t = p.queue[position - 1]
    del p.queue[position - 1]
    await interaction.response.send_message(f"🗑️ Removida: **{t.title}**")


@bot.tree.command(name="clear", description="Limpa a fila (mantém a música atual)")
async def clear(interaction: discord.Interaction):
    p = players.get(interaction.guild_id)
    if not p or not p.queue:
        await interaction.response.send_message("A fila já está vazia.", ephemeral=True)
        return
    p.queue.clear()
    await interaction.response.send_message("🧹 Fila limpa.")


@bot.tree.command(name="leave", description="Faz o bot sair do canal de voz")
async def leave(interaction: discord.Interaction):
    p = players.get(interaction.guild_id)
    vc = interaction.guild.voice_client
    if p:
        await p.destroy()
    elif vc:
        await vc.disconnect()
    else:
        await interaction.response.send_message("Não estou em nenhum canal.", ephemeral=True)
        return
    await interaction.response.send_message("👋 Até mais!")


def start_keepalive():
    """Servidor web mínimo para hospedagens que exigem uma porta aberta (variável PORT)."""
    port = os.getenv("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina DISCORD_TOKEN no arquivo .env")
    start_keepalive()
    bot.run(TOKEN)
