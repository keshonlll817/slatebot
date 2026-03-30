
import discord
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

ALLOWED_CHANNELS = [
    1471792196582637728,
    1474078126630768822,
    1479241150996152340
]

FOUR_PLUS_CHANNEL  = 1443356395935240302
TOTALS_CHANNEL     = 1446203029916356649
TEST_CHANNEL       = 1471792196582637728
REMINDER_CHANNEL   = 1442010467345236160
CONFIRMATION_CHANNEL = 1452410545016930335

EST = ZoneInfo("America/New_York")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

last_slate_messages = []

# ==============================
# STATE
# ==============================
scheduled_tasks = {}
BOT_DISABLED = False

# ==============================
# UTIL
# ==============================

def format_units(u):
    if u == 1: return "1U"
    if u == 1.25: return "1.25U"
    if u == 1.5: return "1.5U"
    if u == 1.75: return "1.75U"
    if u == 2: return "2U"
    if u == 2.5: return "2.5U"
    if u == 3: return "3U"
    return f"{u}U"

def convert_league(name):
    name=name.lower()
    if "elite" in name: return "ELITE"
    if "setka" in name: return "SETKA"
    if "czech" in name: return "CZECH"
    if "cup" in name: return "CUP"
    return name.upper()

def parse_time(est_time):
    dt=datetime.strptime(est_time,"%m/%d %I:%M %p")
    est=dt.strftime("%I:%M %p")
    pst_dt=dt.replace(hour=(dt.hour-3)%24)
    pst=pst_dt.strftime("%I:%M %p")
    return est,pst

async def send_long_message(channel,text):
    chunks=[]
    while len(text)>2000:
        split_index=text.rfind("\n",0,2000)
        if split_index==-1:
            split_index=2000
        chunks.append(text[:split_index])
        text=text[split_index:]
    chunks.append(text)
    messages=[]
    for chunk in chunks:
        msg=await channel.send(chunk.strip())
        messages.append(msg)
    return messages

# ==============================
# REMINDER ENGINE
# ==============================

def make_play_key(league, p1, p2, time_str):
    return f"{league}|{p1}|{p2}|{time_str}"

def build_reminder_text(league, p1, p2, wins, total, tier, label):
    if tier=="nuke": emoji=" ☢️"
    elif tier=="caution": emoji=" ⚠️"
    else: emoji=""
    return f"@TT Official\n{league} – {p1} vs {p2}{emoji} ({wins}/{total}) | {label}"

def parse_play_line_for_reminder(line):
    # Ignore graded lines
    if "✅" in line or "❌" in line or "🧼" in line:
        return None

    line=re.sub(r'\s+',' ',line).strip()
    if "vs" not in line or "@" not in line or "EST" not in line:
        return None

    ll=line.lower()
    if "elite" in ll: league="ELITE"
    elif "setka" in ll: league="SETKA"
    elif "czech" in ll: league="CZECH"
    elif "cup" in ll: league="CUP"
    else: league="OTHER"

    if "☢️" in line: tier="nuke"
    elif "⚠️" in line: tier="caution"
    else: tier="normal"

    tm=re.search(r'@\s*(\d{1,2}:\d{2})\s*([AaPp][Mm])',line)
    if not tm: return None
    time_str=tm.group(1)+" "+tm.group(2).upper()

    rm=re.search(r'\((\d+)/(\d+)\)',line)
    if not rm: return None
    wins=int(rm.group(1)); total=int(rm.group(2))

    body=re.sub(r'^[A-Z]+\s*[–\-]\s*','',line).strip()
    body=body.replace("☢️","").replace("⚠️","")
    vm=re.search(r'^(.+?)\s+vs\s+(.+?)(?:\s+[\d\.]+U|\s+@|\s*\()',body,re.I)
    if not vm: return None
    p1=vm.group(1).strip(); p2=vm.group(2).strip()

    now=datetime.now(EST)
    naive=datetime.strptime(f"{now.year}/{now.month}/{now.day} {time_str}","%Y/%m/%d %I:%M %p")
    game_dt=naive.replace(tzinfo=EST)

    # TIME FIX: ignore too-old games (no rollover)
    GRACE_MINUTES = 90
    if game_dt < now - timedelta(minutes=GRACE_MINUTES):
        return None

    return {
        "league":league,"p1":p1,"p2":p2,"wins":wins,"total":total,
        "tier":tier,"game_dt":game_dt,"time_str":time_str
    }

async def _send_reminder_at(fire_dt,text):
    delay=(fire_dt-datetime.now(EST)).total_seconds()
    if delay>0:
        await asyncio.sleep(delay)

    # If locked, route reminders to TEST channel; otherwise normal channel
    ch = client.get_channel(TEST_CHANNEL if BOT_DISABLED else REMINDER_CHANNEL)
    if ch:
        await ch.send(text)

def schedule_reminders_for_play(play):
    key=make_play_key(play["league"],play["p1"],play["p2"],play["time_str"])
    if key in scheduled_tasks:
        return {"scheduled":[]}

    now=datetime.now(EST)
    tasks=[]; scheduled=[]
    for fire_dt,label in [
        (play["game_dt"]-timedelta(minutes=5),"STARTING SOON"),
        (play["game_dt"],"STARTING NOW")
    ]:
        if fire_dt<=now: continue
        txt=build_reminder_text(
            play["league"],play["p1"],play["p2"],
            play["wins"],play["total"],play["tier"],label
        )
        t=asyncio.ensure_future(_send_reminder_at(fire_dt,txt))
        tasks.append(t); scheduled.append((label,fire_dt))

    if tasks:
        scheduled_tasks[key]=tasks
    return {"play":play,"scheduled":scheduled}

def cancel_reminders_for_key(key):
    tasks=scheduled_tasks.pop(key,[])
    for t in tasks:
        t.cancel()

def extract_play_keys_from_text(text):
    keys=set()
    for line in text.split("\n"):
        p=parse_play_line_for_reminder(line)
        if p:
            keys.add(make_play_key(p["league"],p["p1"],p["p2"],p["time_str"]))
    return keys

async def schedule_reminders_from_text(text):
    results=[]
    for line in text.split("\n"):
        play=parse_play_line_for_reminder(line)
        if play:
            r=schedule_reminders_for_play(play)
            if r["scheduled"]:
                results.append(r)
    return results

async def send_reminder_confirmation(results):
    ch=client.get_channel(TEST_CHANNEL if BOT_DISABLED else CONFIRMATION_CHANNEL)
    if not ch or not results:
        return
    tier_tag={"nuke":" ☢️","caution":" ⚠️","normal":""}
    lines=["⏰ **REMINDERS SET** ━━━━━━━━━━━━━━━━━━"]
    for r in results:
        p=r["play"]
        lines.append(f"{p['league']} – {p['p1']} vs {p['p2']}{tier_tag[p['tier']]}")
    lines.append(f"\n**{len(results)} play(s) queued.**")
    await ch.send("\n".join(lines))

# ==============================
# EVENTS
# ==============================

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

@client.event
async def on_message(message):
    global BOT_DISABLED, last_slate_messages

    if message.author.bot:
        return

    content=message.content.lower().strip()

    # Disable/enable command (test channel only)
    if content.startswith("!disable"):
        if message.channel.id != TEST_CHANNEL:
            return
        if "true" in content:
            BOT_DISABLED = True
            await message.channel.send("🔒 Bot locked for testing.")
        elif "false" in content:
            BOT_DISABLED = False
            await message.channel.send("✅ Bot unlocked.")
        return

    # If locked, block main channels but still allow TEST channel
    if BOT_DISABLED and message.channel.id != TEST_CHANNEL:
        await message.channel.send("🔒 Bot is currently locked (testing). Ask Dark to unlock or try again later.")
        return

    # ==============================
    # COMMANDS
    # ==============================

    if content == "ping":
        await message.channel.send("pong")
        return

    # Reminders list with countdown
    if content == "!reminders":
        if not scheduled_tasks:
            await message.channel.send("⏰ No reminders.")
            return

        now_est = datetime.now(EST)
        lines=[f"⏰ **ACTIVE REMINDERS ({len(scheduled_tasks)} play(s))** ━━━━━━━━━━━━━━━━━━"]

        for key in scheduled_tasks.keys():
            parts=key.split("|")
            if len(parts)==4:
                league,p1,p2,time_str = parts

                # reconstruct datetime for countdown
                try:
                    game_dt = datetime.strptime(
                        f"{now_est.year}/{now_est.month}/{now_est.day} {time_str}",
                        "%Y/%m/%d %I:%M %p"
                    ).replace(tzinfo=EST)

                    # if slightly past today, assume next day (for late-night runs)
                    if game_dt < now_est:
                        game_dt += timedelta(days=1)

                    delta = game_dt - now_est
                    total_sec = int(delta.total_seconds())
                    hours = total_sec // 3600
                    minutes = (total_sec % 3600) // 60
                    time_left = f"{hours}h {minutes}m"
                except:
                    time_left = "unknown"

                lines.append(f"{league} – {p1} vs {p2} @ {time_str} EST\n→ in {time_left}")

        await send_long_message(message.channel,"\n".join(lines))
        return

    # ==============================
    # CSV SLATE ENGINE (unchanged)
    # ==============================

    if message.channel.id not in ALLOWED_CHANNELS:
        return

    if not message.attachments:
        return

    attachment = message.attachments[0]

    if not attachment.filename.endswith(".csv"):
        return

    file_bytes = await attachment.read()
    decoded = file_bytes.decode("utf-8")

    reader = csv.DictReader(io.StringIO(decoded))

    four_plus={}
    totals={}

    for row in reader:

        league=convert_league(row["League"])
        p1=row["Player 1"]
        p2=row["Player 2"]
        play=row["Play"]
        history=row["History"]
        est_time=row["Time (Eastern)"]

        est,pst=parse_time(est_time)

        if "4+" in play:

            match=re.search(r"\((\d+)/(\d+)\)",history)
            if not match:
                continue

            losses=int(match.group(1))
            total=int(match.group(2))
            wins=total-losses
            pct=wins/total

            tier="normal"

            if total>=40 and pct>=0.91:
                tier="nuke"
            elif wins<=22:
                tier="caution"

            key=f"{league}{p1}{p2}{est}"
            four_plus[key]=(league,p1,p2,est,pst,wins,total,tier)

        elif "Over/Under" in history:

            match=re.search(r"\((\d+)/(\d+)\)",history)
            if not match:
                continue

            wins=int(match.group(1))
            total=int(match.group(2))
            pct=wins/total

            if total>=30:
                if pct>=.95: units=2.5
                elif pct>=.91: units=2
                elif pct>=.86: units=1.5
                elif pct>=.81: units=1.25
                else: units=1
            else:
                if pct>=.95: units=2
                elif pct>=.91: units=1.75
                elif pct>=.86: units=1.5
                elif pct>=.81: units=1.25
                else: units=1

            key=f"{league}{p1}{p2}{est}{play}"
            totals[key]=(league,p1,p2,play,units,est,pst,wins,total)

    # delete previous slate messages
    for msg in last_slate_messages:
        try:
            await msg.delete()
        except:
            pass

    last_slate_messages=[]

    await message.delete()

    # send new slate
    msg1=await message.channel.send("🏓 **4+ PLAYS** 🏓")
    last_slate_messages.append(msg1)

    if four_plus:
        text=""
        for v in four_plus.values():
            league,p1,p2,est,pst,wins,total,tier=v
            emoji=""
            if tier=="nuke": emoji=" ☢️"
            elif tier=="caution": emoji=" ⚠️"
            text+=f"{league} – {p1} vs {p2} @ {est} EST / {pst} PST ({wins}/{total}){emoji}\n\n"

        sent_msgs=await send_long_message(message.channel,text.strip())
        last_slate_messages.extend(sent_msgs)

    msg3=await message.channel.send("🏓 **TOTAL PLAYS** 🏓")
    last_slate_messages.append(msg3)

    if totals:
        text=""
        for v in totals.values():
            league,p1,p2,play,units,est,pst,wins,total=v
            text+=f"{league} – {p1} vs {p2} {play} {format_units(units)} @ {est} EST / {pst} PST ({wins}/{total})\n\n"

        sent_msgs=await send_long_message(message.channel,text.strip())
        last_slate_messages.extend(sent_msgs)

    # After posting slate, schedule reminders from THIS message content only
    results = await schedule_reminders_from_text(message.content)
    await send_reminder_confirmation(results)

client.run(TOKEN)
