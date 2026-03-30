
# FINAL STABLE SLATE BOT (patched)

import discord
from discord.ext import commands
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

EST = datetime.now().astimezone().tzinfo

scheduled_tasks = {}  # {guild_id: {key: [tasks]}}

ROLE_NAME = "TT Official"

def make_play_key(league, p1, p2, time_str):
    return f"{league}|{p1}|{p2}|{time_str}"

def parse_time(time_str):
    now = datetime.now()
    dt = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %I:%M %p")
    if dt < now:
        dt += timedelta(days=1)
    return dt

def find_role(guild):
    for r in guild.roles:
        if r.name.strip().lower() == ROLE_NAME.lower():
            return r
    return None

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

def schedule_play(play, guild_id):
    key = make_play_key(play["league"], play["p1"], play["p2"], play["time"])

    if guild_id not in scheduled_tasks:
        scheduled_tasks[guild_id] = {}

    if key in scheduled_tasks[guild_id]:
        return

    dt = parse_time(play["time"])
    delay = (dt - datetime.now()).total_seconds()

    async def reminder(channel):
        await send_ping(channel, f"{play['league']} – {play['p1']} vs {play['p2']} | STARTING SOON")

    async def task_wrapper(channel):
        await asyncio.sleep(delay)
        await reminder(channel)

    return key

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content

    if content.startswith("SETKA") or "vs" in content:
        plays = extract_plays(content)

        guild_id = message.guild.id

        if guild_id in scheduled_tasks:
            scheduled_tasks[guild_id].clear()

        for play in plays:
            key = schedule_play(play, guild_id)
            if key:
                scheduled_tasks[guild_id][key] = True

        await message.channel.send(f"⏰ REMINDERS SET ({len(plays)} plays)")

    if content == "!reminders":
        guild_id = message.guild.id

        if guild_id not in scheduled_tasks or not scheduled_tasks[guild_id]:
            await message.channel.send("⏰ No reminders currently scheduled.")
            return

        now = datetime.now()

        lines = [f"⏰ ACTIVE REMINDERS ({len(scheduled_tasks[guild_id])})"]

        for key in scheduled_tasks[guild_id]:
            try:
                league, p1, p2, time_str = key.split("|")
                dt = parse_time(time_str)

                delta = dt - now
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)

                lines.append(f"{league} – {p1} vs {p2} @ {time_str} → {h}h {m}m")
            except:
                continue

        await message.channel.send("\n".join(lines))

    await bot.process_commands(message)

bot.run("YOUR_TOKEN_HERE")
