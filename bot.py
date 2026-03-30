import discord
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

# ================== CONFIG ==================
TOKEN = os.getenv("TOKEN")  # or replace with string
ROLE_NAME = "TT Official"
EST = ZoneInfo("America/New_York")

# ================== BOT SETUP ==================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================== STORAGE ==================
scheduled_tasks = {}  # {guild_id: {key: task}}

# ================== HELPERS ==================

def make_key(league, p1, p2, time_str):
    return f"{league}|{p1}|{p2}|{time_str}"

def parse_time(time_str):
    now = datetime.now(EST)

    dt = datetime.strptime(
        f"{now.strftime('%Y-%m-%d')} {time_str}",
        "%Y-%m-%d %I:%M %p"
    ).replace(tzinfo=EST)

    # Prevent false "next day"
    if dt < now - timedelta(minutes=2):
        dt += timedelta(days=1)

    return dt

def find_role(guild):
    return discord.utils.get(guild.roles, name=ROLE_NAME)

async def send_ping(channel, text):
    role = find_role(channel.guild)

    if role:
        await channel.send(
            f"{role.mention} {text}",
            allowed_mentions=discord.AllowedMentions(roles=True)
        )
    else:
        await channel.send(text)

def extract_plays(text):
    plays = []

    for line in text.splitlines():
        if "@" in line and "vs" in line:
            try:
                parts = line.split("@")
                matchup = parts[0]
                time_part = parts[1].split("EST")[0].strip()

                league, rest = matchup.split("–")
                p1, p2 = rest.split("vs")

                plays.append({
                    "league": league.strip(),
                    "p1": p1.strip(),
                    "p2": p2.strip(),
                    "time": time_part.strip()
                })
            except:
                continue

    return plays

# ================== REMINDERS ==================

def schedule_play(play, channel, guild_id):
    key = make_key(play["league"], play["p1"], play["p2"], play["time"])

    if guild_id not in scheduled_tasks:
        scheduled_tasks[guild_id] = {}

    if key in scheduled_tasks[guild_id]:
        return

    dt = parse_time(play["time"])
    delay = (dt - datetime.now(EST)).total_seconds()

    async def reminder():
        await asyncio.sleep(delay)
        await send_ping(
            channel,
            f"{play['league']} – {play['p1']} vs {play['p2']} | STARTING SOON"
        )

    task = asyncio.create_task(reminder())
    scheduled_tasks[guild_id][key] = task

def clear_guild_tasks(guild_id):
    if guild_id in scheduled_tasks:
        for task in scheduled_tasks[guild_id].values():
            task.cancel()
        scheduled_tasks[guild_id].clear()

# ================== EVENTS ==================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content
    guild_id = message.guild.id

    if "vs" in content and "@" in content:
        plays = extract_plays(content)

        clear_guild_tasks(guild_id)

        for play in plays:
            schedule_play(play, message.channel, guild_id)

        await message.channel.send(f"⏰ REMINDERS SET ({len(plays)} plays)")
        return

    if content == "!reminders":
        if guild_id not in scheduled_tasks or not scheduled_tasks[guild_id]:
            await message.channel.send("⏰ No reminders currently scheduled.")
            return

        now = datetime.now(EST)

        lines = [f"⏰ ACTIVE REMINDERS ({len(scheduled_tasks[guild_id])})"]

        for key in scheduled_tasks[guild_id]:
            try:
                league, p1, p2, time_str = key.split("|")
                dt = parse_time(time_str)

                delta = dt - now
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)

                lines.append(
                    f"{league} – {p1} vs {p2} @ {time_str} → in {h}h {m}m"
                )
            except:
                continue

        await message.channel.send("\n".join(lines))
        return

    await bot.process_commands(message)

# ✅ EDIT HANDLER (NEW FIX)
@bot.event
async def on_message_edit(before, after):
    if after.author.bot:
        return

    content = after.content
    guild_id = after.guild.id

    if "vs" in content and "@" in content:
        plays = extract_plays(content)

        clear_guild_tasks(guild_id)

        for play in plays:
            schedule_play(play, after.channel, guild_id)

        await after.channel.send(f"🔄 REMINDERS UPDATED ({len(plays)} plays)")

# ================== RUN ==================

if not TOKEN:
    print("❌ TOKEN missing")
else:
    bot.run(TOKEN)
