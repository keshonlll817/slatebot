
# ================= SLATEBOT FINAL DEPLOYMENT =================
# FULL SYSTEM: CSV + RECAP + ANALYTICS + FIXED REMINDERS

import discord
from discord.ext import commands
import csv
import io
import re
import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise ValueError("No TOKEN found.")

EST = ZoneInfo("America/New_York")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

ROLE_NAME = "TT Official"

# ================= REMINDER STATE =================
scheduled_tasks = {}      # {guild: {message_id: {key: task}}}
active_keys = {}          # {guild: set(keys)}

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

# ================= PARSER =================

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

                stats = ""
                emoji = ""

                if "(" in line and ")" in line:
                    stats = line.split("(")[-1].split(")")[0]

                if "☢️" in line:
                    emoji = "☢️"
                elif "⚠️" in line:
                    emoji = "⚠️"

                plays.append({
                    "league": league.strip(),
                    "p1": p1.strip(),
                    "p2": p2.strip(),
                    "time": time_part.strip(),
                    "stats": stats,
                    "emoji": emoji
                })
            except:
                continue
    return plays

# ================= REMINDER ENGINE =================

def schedule_message_plays(message, plays):
    guild_id = message.guild.id
    message_id = message.id

    if guild_id not in scheduled_tasks:
        scheduled_tasks[guild_id] = {}

    if guild_id not in active_keys:
        active_keys[guild_id] = set()

    if message_id in scheduled_tasks[guild_id]:
        for key, task in scheduled_tasks[guild_id][message_id].items():
            task.cancel()
            active_keys[guild_id].discard(key)

    scheduled_tasks[guild_id][message_id] = {}

    added = 0

    for play in plays:
        key = make_key(play["league"], play["p1"], play["p2"], play["time"])

        if key in active_keys[guild_id]:
            continue

        async def reminder(p=play):
            game_time = parse_time(p["time"])
            now = datetime.now(EST)

            stats = f" ({p['stats']})" if p["stats"] else ""
            emoji = f" {p['emoji']}" if p["emoji"] else ""

            pre_delay = (game_time - timedelta(minutes=5) - now).total_seconds()

            if pre_delay > 0:
                await asyncio.sleep(pre_delay)
                await send_ping(message.channel,
                    f"{p['league']} – {p['p1']} vs {p['p2']}{stats}{emoji} | STARTING SOON"
                )

            now2 = datetime.now(EST)
            start_delay = (game_time - now2).total_seconds()

            if start_delay > 0:
                await asyncio.sleep(start_delay)

            await send_ping(message.channel,
                f"{p['league']} – {p['p1']} vs {p['p2']}{stats}{emoji} | STARTING NOW"
            )

        task = asyncio.create_task(reminder(play))

        scheduled_tasks[guild_id][message_id][key] = task
        active_keys[guild_id].add(key)
        added += 1

    return added

# ================= EVENTS =================

@bot.event
async def on_ready():
    print(f"✅ FINAL BOT ONLINE: {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content

    if "vs" in content and "@" in content:
        plays = extract_plays(content)
        added = schedule_message_plays(message, plays)
        await message.channel.send(f"⏰ REMINDERS SET ({added} plays)")
        return

    if content == "!reminders":
        guild_id = message.guild.id

        if guild_id not in scheduled_tasks:
            await message.channel.send("No reminders.")
            return

        total = sum(len(m) for m in scheduled_tasks[guild_id].values())

        lines = [f"ACTIVE REMINDERS ({total})"]

        for msg_tasks in scheduled_tasks[guild_id].values():
            for key in msg_tasks:
                try:
                    l, p1, p2, t = key.split("|")
                    dt = parse_time(t)
                    delta = dt - datetime.now(EST)

                    h = int(delta.total_seconds() // 3600)
                    m = int((delta.total_seconds() % 3600) // 60)

                    lines.append(f"{l} – {p1} vs {p2} @ {t} → {h}h {m}m")
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
        added = schedule_message_plays(after, plays)
        await after.channel.send(f"🔄 UPDATED ({added})")

# ================= RUN =================

bot.run(TOKEN)
