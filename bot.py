import discord
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
ROLE_NAME = "TT Official"
EST = ZoneInfo("America/New_York")

# ================= BOT =================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# {guild_id: {message_id: {key: task}}}
scheduled_tasks = {}

# ================= HELPERS =================

def make_key(l, p1, p2, t):
    return f"{l}|{p1}|{p2}|{t}"

def parse_time(time_str):
    now = datetime.now(EST)
    dt = datetime.strptime(
        f"{now.strftime('%Y-%m-%d')} {time_str}",
        "%Y-%m-%d %I:%M %p"
    ).replace(tzinfo=EST)

    if dt < now - timedelta(minutes=2):
        dt += timedelta(days=1)

    return dt

def find_role(guild):
    return discord.utils.get(guild.roles, name=ROLE_NAME)

async def send_ping(ch, text):
    role = find_role(ch.guild)
    if role:
        await ch.send(
            f"{role.mention} {text}",
            allowed_mentions=discord.AllowedMentions(roles=True)
        )
    else:
        await ch.send(text)

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

# ================= CORE =================

def schedule_message_plays(message, plays):
    guild_id = message.guild.id
    message_id = message.id

    if guild_id not in scheduled_tasks:
        scheduled_tasks[guild_id] = {}

    # clear ONLY this message
    if message_id in scheduled_tasks[guild_id]:
        for t in scheduled_tasks[guild_id][message_id].values():
            t.cancel()

    scheduled_tasks[guild_id][message_id] = {}

    for play in plays:
        key = make_key(play["league"], play["p1"], play["p2"], play["time"])

        dt = parse_time(play["time"])
        delay = (dt - datetime.now(EST)).total_seconds()

        async def reminder(p=play):
            await asyncio.sleep(delay)
            await send_ping(
                message.channel,
                f"{p['league']} – {p['p1']} vs {p['p2']} | STARTING SOON"
            )

        task = asyncio.create_task(reminder())
        scheduled_tasks[guild_id][message_id][key] = task

# ================= EVENTS =================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content

    if "vs" in content and "@" in content:
        plays = extract_plays(content)
        schedule_message_plays(message, plays)

        await message.channel.send(f"⏰ REMINDERS SET ({len(plays)} plays)")
        return

    if content == "!reminders":
        guild_id = message.guild.id

        if guild_id not in scheduled_tasks:
            await message.channel.send("⏰ No reminders currently scheduled.")
            return

        now = datetime.now(EST)
        total = sum(len(m) for m in scheduled_tasks[guild_id].values())

        if total == 0:
            await message.channel.send("⏰ No reminders currently scheduled.")
            return

        lines = [f"⏰ ACTIVE REMINDERS ({total})"]

        for msg_tasks in scheduled_tasks[guild_id].values():
            for key in msg_tasks:
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

@bot.event
async def on_message_edit(before, after):
    if after.author.bot:
        return

    content = after.content

    if "vs" in content and "@" in content:
        plays = extract_plays(content)
        schedule_message_plays(after, plays)

        await after.channel.send(f"🔄 REMINDERS UPDATED ({len(plays)} plays)")

# ================= RUN =================

if not TOKEN:
    print("❌ TOKEN missing")
else:
    bot.run(TOKEN)
