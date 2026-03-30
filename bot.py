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
REMINDER_CHANNEL   = 1442010467345236160   # #tabletennis-chat
CONFIRMATION_CHANNEL = 1452410545016930335  # reminders confirmation channel

EST = ZoneInfo("America/New_York")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

last_slate_messages = []

# ==============================
# REMINDER STATE
# ==============================
# Maps a unique play key -> list of asyncio.Task objects (SOON + NOW)
# Key format: "{league}|{p1}|{p2}|{game_iso}"
scheduled_tasks = {}

# BOT CONTROL
BOT_DISABLED = False


# ==============================
# UTIL FUNCTIONS
# ==============================

def format_units(u):
    if u == 1:    return "1U"
    if u == 1.25: return "1.25U"
    if u == 1.5:  return "1.5U"
    if u == 1.75: return "1.75U"
    if u == 2:    return "2U"
    if u == 2.5:  return "2.5U"
    if u == 3:    return "3U"
    return f"{u}U"

def convert_league(name):
    name = name.lower()
    if "elite" in name: return "ELITE"
    if "setka" in name: return "SETKA"
    if "czech" in name: return "CZECH"
    if "cup"   in name: return "CUP"
    return name.upper()

def parse_time(est_time):
    dt     = datetime.strptime(est_time, "%m/%d %I:%M %p")
    est    = dt.strftime("%I:%M %p")
    pst_dt = dt.replace(hour=(dt.hour - 3) % 24)
    pst    = pst_dt.strftime("%I:%M %p")
    return est, pst

async def send_long_message(channel, text):
    chunks = []
    while len(text) > 2000:
        split_index = text.rfind("\n", 0, 2000)
        if split_index == -1:
            split_index = 2000
        chunks.append(text[:split_index])
        text = text[split_index:]
    chunks.append(text)
    messages = []
    for chunk in chunks:
        msg = await channel.send(chunk.strip())
        messages.append(msg)
    return messages


# ==============================
# REMINDER ENGINE
# ==============================

def make_play_key(league, p1, p2, time_str):
    """Unique key for a play used to track and cancel its reminder tasks."""
    return f"{league}|{p1}|{p2}|{time_str}"


def build_reminder_text(league, p1, p2, wins, total, tier, label):
    """
    Build the reminder message matching the format:
    LEAGUE – P1 vs P2 EMOJI (wins/total) | LABEL
    """
    if   tier == "nuke":    emoji = " ☢️"
    elif tier == "caution": emoji = " ⚠️"
    else:                   emoji = ""
    return f"@TT Official\n{league} – {p1} vs {p2}{emoji} ({wins}/{total}) | {label}"


def parse_play_line_for_reminder(line):

    # Ignore graded plays
    if "✅" in line or "❌" in line or "🧼" in line:
        return None
    """
    Parse a slate line from the 4+ or totals channel into reminder components.

    Expected 4+ format:
        LEAGUE – P1 vs P2 @ HH:MM AM/PM EST / HH:MM AM/PM PST (wins/total) [emoji]

    Expected totals format:
        LEAGUE – P1 vs P2 PLAY XU @ HH:MM AM/PM EST / HH:MM AM/PM PST (wins/total)

    Returns a dict with keys: league, p1, p2, wins, total, tier, game_dt
    Returns None if line cannot be parsed into a valid play.
    """
    line = re.sub(r'\s+', ' ', line).strip()

    # Must contain "vs" and "@ ... EST"
    if "vs" not in line or "@" not in line or "EST" not in line:
        return None

    # League
    ll = line.lower()
    if   "elite" in ll: league = "ELITE"
    elif "setka" in ll: league = "SETKA"
    elif "czech" in ll: league = "CZECH"
    elif "cup"   in ll: league = "CUP"
    else:               league = "OTHER"

    # Tier
    if   "☢️" in line: tier = "nuke"
    elif "⚠️" in line: tier = "caution"
    else:              tier = "normal"

    # EST time — grab the time immediately after "@" and before "EST"
    # Handles formats: "12:05 PM EST", "12:05pm est", "12:05PM EST"
    time_match = re.search(r'@\s*(\d{1,2}:\d{2})\s*([AaPp][Mm])\s*[Ee][Ss][Tt]', line)
    if not time_match:
        return None
    # Normalise: always "HH:MM AM" with a space before AM/PM
    time_str = time_match.group(1).strip() + " " + time_match.group(2).strip().upper()

    # Record (wins/total)
    record_match = re.search(r'\((\d+)/(\d+)\)', line)
    if not record_match:
        return None
    wins  = int(record_match.group(1))
    total = int(record_match.group(2))

    # Player names — strip league prefix, emojis, then grab "P1 vs P2"
    body = re.sub(r'^[A-Z]+\s*[–\-]\s*', '', line).strip()
    body = body.replace("☢️", "").replace("⚠️", "")
    vs_match = re.search(r'^(.+?)\s+vs\s+(.+?)(?:\s+[\d\.]+U|\s+@|\s*\()', body, re.IGNORECASE)
    if not vs_match:
        return None
    p1 = vs_match.group(1).strip()
    p2 = vs_match.group(2).strip()

    # Build timezone-aware datetime for the game in EST
    now_est = datetime.now(EST)
    try:
        naive_game = datetime.strptime(
            f"{now_est.year}/{now_est.month}/{now_est.day} {time_str}", "%Y/%m/%d %I:%M %p"
        )
    except ValueError:
        return None

    game_dt = naive_game.replace(tzinfo=EST)

    # If the time is more than 2 hours in the past, assume it belongs to tomorrow.
    # We use 2 hours instead of minutes to handle slates posted after midnight
    # where early-AM games (e.g. 12:00 AM, 1:00 AM) may have just passed but
    # later games in the same slate (e.g. 9:30 PM) are still today.
    GRACE_MINUTES = 90
    if game_dt < now_est - timedelta(minutes=GRACE_MINUTES):
        return None

    return {
        "league":  league,
        "p1":      p1,
        "p2":      p2,
        "wins":    wins,
        "total":   total,
        "tier":    tier,
        "game_dt": game_dt,
        "time_str": time_str
    }


async def _send_reminder_at(fire_dt, text):
    """Sleep until fire_dt (EST-aware), then post to REMINDER_CHANNEL."""
    now_est = datetime.now(EST)
    delay   = (fire_dt - now_est).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    if BOT_DISABLED:
        ch = client.get_channel(TEST_CHANNEL)
    else:
        ch = client.get_channel(REMINDER_CHANNEL)
    if ch:
        await ch.send(text)


def schedule_reminders_for_play(play, guild_id):
    """
    Schedule STARTING SOON (-5 min) and STARTING NOW tasks for a play.
    Skips if already scheduled or if fire time is in the past.
    Returns a result dict describing what was scheduled (for confirmation messages).
    """
    league  = play["league"]
    p1      = play["p1"]
    p2      = play["p2"]
    wins    = play["wins"]
    total   = play["total"]
    tier    = play["tier"]
    game_dt = play["game_dt"]

    key     = make_play_key(league, p1, p2, play["time_str"])
    now_est = datetime.now(EST)

    # Already scheduled — skip
    if guild_id not in scheduled_tasks:
        scheduled_tasks[guild_id] = {}

    if key in scheduled_tasks[guild_id]:
        return {"key": key, "status": "already_scheduled", "scheduled": []}

    soon_dt    = game_dt - timedelta(minutes=5)
    tasks      = []
    scheduled  = []  # list of (label, fire_dt) that were actually scheduled

    for fire_dt, label in [(soon_dt, "STARTING SOON"), (game_dt, "STARTING NOW")]:
        if fire_dt <= now_est:
            continue  # already past, skip
        text = build_reminder_text(league, p1, p2, wins, total, tier, label)
        task = asyncio.ensure_future(_send_reminder_at(fire_dt, text))
        tasks.append(task)
        scheduled.append((label, fire_dt))

    if tasks:
        scheduled_tasks[guild_id][key] = tasks
        print(f"[REMINDERS] Scheduled {len(tasks)} task(s) for: {key}")

    return {"key": key, "status": "scheduled", "play": play, "scheduled": scheduled}


def cancel_reminders_for_key(key):
    """Cancel any pending reminder tasks for the given play key."""
    tasks = scheduled_tasks.pop(key, [])
    for t in tasks:
        t.cancel()
    if tasks:
        print(f"[REMINDERS] Cancelled reminders for: {key}")


def extract_play_keys_from_text(text):
    """
    Parse all lines in a text block and return the set of play keys
    that would be (or were) scheduled.  Used to diff edits.
    """
    keys = set()
    for raw_line in text.split("\n"):
        play = parse_play_line_for_reminder(raw_line)
        if play:
            keys.add(make_play_key(
                play["league"], play["p1"], play["p2"], play["game_dt"]
            ))
    return keys


async def schedule_reminders_from_text(text, guild_id):
    """
    Parse every line of a text block and schedule reminders for valid plays.
    Treats the entire block as one slate day: the earliest parseable time
    anchors the calendar date, and all other times are assigned to the same
    date (rolling to the next day only if they are before the anchor).
    Returns a list of result dicts (one per successfully scheduled play).
    """
    now_est = datetime.now(EST)

    # ── Pass 1: collect all (line, time_str) pairs ──
    raw_plays = []
    for raw_line in text.split("\n"):
        line = re.sub(r'\s+', ' ', raw_line).strip()
        if "vs" not in line or "@" not in line:
            continue
        if not re.search(r'est', line, re.IGNORECASE):
            continue
        tm = re.search(r'@\s*(\d{1,2}:\d{2})\s*([AaPp][Mm])\s*[Ee][Ss][Tt]', line)
        if not tm:
            continue
        record_match = re.search(r'\((\d+)/(\d+)\)', line)
        if not record_match:
            continue
        time_str = tm.group(1).strip() + " " + tm.group(2).strip().upper()
        raw_plays.append((line, time_str))

    if not raw_plays:
        return []

    # ── Pass 2: determine the anchor date ──
    # Build all candidate datetimes for today and pick the one closest to now
    # that is still in the future (or least in the past within 2 hours).
    # This anchors the whole slate to the correct calendar date.
    def to_minutes(ts):
        try:
            dt = datetime.strptime(ts, "%I:%M %p")
            return dt.hour * 60 + dt.minute
        except:
            return None

    # Find the earliest time in the slate (by minutes since midnight)
    # That anchors us to the slate's "start of day"
    all_mins = [to_minutes(ts) for _, ts in raw_plays]
    all_mins = [m for m in all_mins if m is not None]
    slate_start_mins = min(all_mins) if all_mins else 0  # e.g. 0 = midnight

    # Determine what date the slate start belongs to.
    # If slate starts at midnight (0-120 mins) and current time is past that
    # by up to 23 hours, it's still today's slate.
    slate_start_today = now_est.replace(
        hour=slate_start_mins // 60,
        minute=slate_start_mins % 60,
        second=0, microsecond=0
    )
    # If the slate start was more than 23 hours ago, anchor to tomorrow
    if slate_start_today < now_est - timedelta(hours=23):
        anchor_date = (now_est + timedelta(days=1)).date()
    else:
        anchor_date = now_est.date() if slate_start_today <= now_est else now_est.date()

    # ── Pass 3: assign each play to the correct datetime ──
    results = []
    for line, time_str in raw_plays:
        try:
            naive = datetime.strptime(
                f"{anchor_date.year}/{anchor_date.month}/{anchor_date.day} {time_str}",
                "%Y/%m/%d %I:%M %p"
            )
        except ValueError:
            continue

        game_dt = naive.replace(tzinfo=EST)

        # If this time is before the slate start on anchor_date, it belongs
        # to the next calendar day (e.g. a 12:30 AM game after a 9 PM game)
        slate_start_dt = datetime(
            anchor_date.year, anchor_date.month, anchor_date.day,
            slate_start_mins // 60, slate_start_mins % 60, tzinfo=EST
        )
        if game_dt < slate_start_dt:
            game_dt += timedelta(days=1)

        # Re-parse the full play with the corrected game_dt
        play = parse_play_line_for_reminder(line)
        if not play:
            continue
        play["game_dt"] = game_dt  # override with batch-corrected datetime

        result = schedule_reminders_for_play(play, guild_id)
        if result["scheduled"]:
            results.append(result)

    return results


async def send_reminder_confirmation(results, override_channel=None):
    """
    Post a confirmation message listing every play that had reminders scheduled.
    Sends to CONFIRMATION_CHANNEL by default, or override_channel if provided
    (used when posting from the test channel).
    """
    if not results:
        return

    ch = override_channel if override_channel else client.get_channel(CONFIRMATION_CHANNEL)
    if not ch:
        return

    tier_tag = {"nuke": " ☢️", "caution": " ⚠️", "normal": ""}

    lines = ["⏰ **REMINDERS SET** ━━━━━━━━━━━━━━━━━━"]
    for r in results:
        play = r["play"]
        tag  = tier_tag.get(play["tier"], "")
        lines.append(f"{play['league']} – {play['p1']} vs {play['p2']}{tag}")

    lines.append(f"\n**{len(results)} play(s) queued.**")
    await ch.send("\n".join(lines))


async def reschedule_from_channel(channel, lookback_hours=20):
    """
    On startup: read recent messages from a channel (both bot and human posts)
    and reschedule any reminders whose game time is still in the future.
    """
    cutoff  = datetime.now(EST) - timedelta(hours=lookback_hours)
    async for msg in channel.history(limit=300):
        msg_time = msg.created_at.astimezone(EST)
        if msg_time < cutoff:
            break
        await schedule_reminders_from_text(msg.content)


# ==============================
# RECAP PARSERS
# ==============================

async def parse_four_plus(channel, start, end, limit=None, verify=False):

    wins=losses=washes=0
    normal_w=normal_l=0
    nuke_w=nuke_l=0
    caution_w=caution_l=0

    league_stats={}

    seen=set()

    detected_plays=[]
    ignored_lines=[]
    duplicate_lines=[]

    async for msg in channel.history(limit=limit):

        msg_time=msg.created_at.astimezone(EST)

        if start and not(start<=msg_time<end):
            continue

        for raw_line in msg.content.split("\n"):

            line = re.sub(r'\s+', ' ', raw_line).strip()
            line = line.replace(")❌", ") ❌").replace(")✅", ") ✅")

            if not line:
                continue

            if "vs" not in line and " v " not in line:
                continue

            if "U @" in line or "U@" in line:
                continue

            if line in seen:
                if verify:
                    duplicate_lines.append(line)
                continue

            seen.add(line)

            has_result = "✅" in line or "❌" in line or "🧼" in line

            if not has_result:
                if verify:
                    ignored_lines.append(line)
                continue

            line_lower=line.lower()

            if "elite" in line_lower:
                league="ELITE"
            elif "setka" in line_lower:
                league="SETKA"
            elif "czech" in line_lower:
                league="CZECH"
            elif "cup" in line_lower:
                league="CUP"
            else:
                league="OTHER"

            if league not in league_stats:
                league_stats[league]={"w":0,"l":0,"u":0}

            is_nuke="☢️" in line
            is_caution="⚠️" in line

            # Extract clean player names for verify mode
            if verify:
                vs_match = re.search(r'([A-Za-z\u00C0-\u024F\'\-]+)\s+vs\s+([A-Za-z\u00C0-\u024F\'\-]+)', line, re.IGNORECASE)
                if not vs_match:
                    vs_match = re.search(r'([A-Za-z\u00C0-\u024F\'\-]+)\s+v\s+([A-Za-z\u00C0-\u024F\'\-]+)', line, re.IGNORECASE)
                p1_clean = vs_match.group(1).strip() if vs_match else "?"
                p2_clean = vs_match.group(2).strip() if vs_match else "?"
            else:
                p1_clean = ""
                p2_clean = ""

            if "🧼" in line:
                washes+=1
                if verify:
                    detected_plays.append((league, p1_clean, p2_clean, "WASH", False, False))
                continue

            if "✅" in line:

                wins+=1
                league_stats[league]["w"]+=1

                if is_nuke:
                    nuke_w+=1
                    league_stats[league]["u"]+=2.2
                elif is_caution:
                    caution_w+=1
                    league_stats[league]["u"]+=0.55
                else:
                    normal_w+=1
                    league_stats[league]["u"]+=1.1

                if verify:
                    detected_plays.append((league, p1_clean, p2_clean, "WIN", is_nuke, is_caution))

            elif "❌" in line:

                losses+=1
                league_stats[league]["l"]+=1

                if is_nuke:
                    nuke_l+=1
                    league_stats[league]["u"]-=6
                elif is_caution:
                    caution_l+=1
                    league_stats[league]["u"]-=1.5
                else:
                    normal_l+=1
                    league_stats[league]["u"]-=3

                if verify:
                    detected_plays.append((league, p1_clean, p2_clean, "LOSS", is_nuke, is_caution))

    if verify:
        return wins,losses,washes,normal_w,normal_l,caution_w,caution_l,nuke_w,nuke_l,league_stats,detected_plays,ignored_lines,duplicate_lines

    return wins,losses,washes,normal_w,normal_l,caution_w,caution_l,nuke_w,nuke_l,league_stats


async def parse_totals(channel, start, end, limit=None):

    wins=losses=0
    units=0

    seen=set()

    async for msg in channel.history(limit=limit):

        msg_time=msg.created_at.astimezone(EST)

        if start and not(start<=msg_time<end):
            continue

        for raw_line in msg.content.split("\n"):

            line = re.sub(r'\s+', ' ', raw_line).strip()
            line = line.replace(")❌", ") ❌").replace(")✅", ") ✅")

            if not line:
                continue

            if "vs" not in line and " v " not in line:
                continue

            if line in seen:
                continue

            seen.add(line)

            has_result = "✅" in line or "❌" in line or "🪝" in line

            if not has_result:
                continue

            unit_match=re.search(r'(\d+(\.\d+)?)U',line,re.IGNORECASE)

            if not unit_match:
                continue

            stake=float(unit_match.group(1))

            if "✅" in line:
                wins+=1
                units+=stake/1.2

            elif "❌" in line or "🪝" in line:
                losses+=1
                units-=stake

    return wins,losses,units


# ==============================
# STARTUP
# ==============================

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    # Reschedule any reminders still in the future from today's slate
    four_ch   = client.get_channel(FOUR_PLUS_CHANNEL)
    totals_ch = client.get_channel(TOTALS_CHANNEL)

    if four_ch:
        await reschedule_from_channel(four_ch)
        print("[REMINDERS] Rescheduled from 4+ channel.")

    if totals_ch:
        await reschedule_from_channel(totals_ch)
        print("[REMINDERS] Rescheduled from totals channel.")


# ==============================
# MESSAGE EDIT HANDLER
# ==============================

@client.event
async def on_message_edit(before, after):
    """
    When a message in 4+/totals is edited:
    - Find play lines that were REMOVED from the message
    - Cancel their reminders
    - Find play lines that were ADDED to the message
    - Schedule reminders for them
    """
    if after.channel.id not in (FOUR_PLUS_CHANNEL, TOTALS_CHANNEL, TEST_CHANNEL):
        return

    keys_before = extract_play_keys_from_text(before.content)
    keys_after  = extract_play_keys_from_text(after.content)

    # Cancel reminders for removed plays
    for key in keys_before - keys_after:
        cancel_reminders_for_key(key)

    # Schedule reminders for newly added plays
    for raw_line in after.content.split("\n"):
        play = parse_play_line_for_reminder(raw_line)
        if play:
            key = make_play_key(play["league"], play["p1"], play["p2"], play["time_str"])
            if key not in keys_before:
                schedule_reminders_for_play(play, after.guild.id)


# ==============================
# MESSAGE DELETE HANDLER
# ==============================

@client.event
async def on_message_delete(message):
    """
    When a message in 4+/totals is deleted:
    - Cancel all reminder tasks for every play in that message.
    """
    if message.channel.id not in (FOUR_PLUS_CHANNEL, TOTALS_CHANNEL, TEST_CHANNEL):
        return

    keys = extract_play_keys_from_text(message.content)
    for key in keys:
        cancel_reminders_for_key(key)


# ==============================
# MESSAGE HANDLER
# ==============================

@client.event
async def on_message(message):

    global last_slate_messages

    # ── Bot or human posts in 4+, totals, or test channel — schedule reminders ──
    # This runs BEFORE the bot check so bot-posted slates also get picked up
    if message.channel.id in (FOUR_PLUS_CHANNEL, TOTALS_CHANNEL, TEST_CHANNEL):
        results = await schedule_reminders_from_text(message.content, message.guild.id)
        if results:
            conf_ch = message.channel if message.channel.id == TEST_CHANNEL else None
            await send_reminder_confirmation(results, override_channel=conf_ch)

    if message.author.bot:
        return

    content=message.content.lower().strip()

    global BOT_DISABLED

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

    if BOT_DISABLED and message.channel.id != TEST_CHANNEL:
        await message.channel.send("🔒 Bot is currently locked (testing). Ask Dark to unlock or try again later.")
        return


# ==============================
# RECAP COMMANDS
# ==============================

    if content.startswith("!recap"):

        now=datetime.now(EST)

        if "test" in content:
            start=None
            end=None
            limit=50
            title=f"TEST RECAP — {now.strftime('%b')} {now.day} (EST)"

        elif "today" in content:
            start=now.replace(hour=0,minute=0,second=0,microsecond=0)
            end=now
            title=f"TODAY RECAP — {now.strftime('%b')} {now.day} (EST)"
            limit=None

        elif "lifetime" in content:
            start=None
            end=None
            title="LIFETIME RECAP"
            limit=None

        elif "yesterday" in content:
            start=(now-timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            end=start+timedelta(days=1)
            title=f"DAILY RECAP — {start.strftime('%b')} {start.day} (EST)"
            limit=None

        elif "last week" in content or "lastweek" in content.replace(" ",""):
            days_since_monday=now.weekday()
            this_monday=now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=days_since_monday)
            start=this_monday-timedelta(days=7)
            end=this_monday
            title=f"LAST WEEK RECAP — {start.strftime('%b %-d')} → {end.strftime('%b %-d')} (EST)"
            limit=None

        elif "weekly" in content:
            days_since_monday=now.weekday()
            start=now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=days_since_monday)
            end=start+timedelta(days=7)
            title=f"WEEKLY RECAP — {start.strftime('%b %-d')} → {end.strftime('%b %-d')} (EST)"
            limit=None

        elif "monthly" in content:
            start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
            end=now
            title=f"MONTHLY RECAP — {now.strftime('%b %Y')}"
            limit=None

        elif "verify" in content:

            if message.channel.id==TEST_CHANNEL:
                four_channel=message.channel
            else:
                four_channel=client.get_channel(FOUR_PLUS_CHANNEL)

            result=await parse_four_plus(four_channel,None,None,limit=50,verify=True)
            fw,fl,fwash,nw,nl,cw,cl,kw,kl,league_stats,detected_plays,ignored_lines,duplicate_lines=result

            now_v=datetime.now(EST)
            four_units_v=( (nw*1.1)-(nl*3) + (cw*0.55)-(cl*1.5) + (kw*2.2)-(kl*6) )

            # -- SECTION 1: HEADER --
            verify_out=f"🔍 **RECAP VERIFY — {now_v.strftime('%b')} {now_v.day} (EST)** ━━━━━━━━━━━━━━━━━━\n\n"

            # -- SECTION 2: SUMMARY --
            total_counted=fw+fl+fwash
            total_parsed=total_counted+len(ignored_lines)
            verify_out+=f"📊 **SUMMARY**\n"
            verify_out+=f"Total Parsed: {total_parsed}  |  Counted: {total_counted}  |  Ignored: {len(ignored_lines)}  |  Duplicates Skipped: {len(duplicate_lines)}\n\n"

            # -- SECTION 3: COUNTED PLAYS --
            verify_out+=f"✅ **COUNTED PLAYS**\n"

            display_plays=detected_plays[:40]

            for i,(lg,p1_c,p2_c,outcome,is_nuke,is_caution) in enumerate(display_plays,1):
                tag=""
                if outcome!="WASH":
                    if is_nuke: tag=" ☢️"
                    elif is_caution: tag=" ⚠️"
                verify_out+=f"{i}. {lg} — {p1_c} vs {p2_c} → **{outcome}**{tag}\n"

            if len(detected_plays)>40:
                verify_out+=f"_(... {len(detected_plays)-40} more plays not shown)_\n"

            # -- SECTION 4: IGNORED --
            verify_out+=f"\n❌ **IGNORED (Missing Results)**\n"

            if ignored_lines:
                for ln in ignored_lines:
                    vs_m=re.search(r'([A-Za-z\u00C0-\u024F\'\-]+)\s+vs\s+([A-Za-z\u00C0-\u024F\'\-]+)',ln,re.IGNORECASE)
                    if not vs_m:
                        vs_m=re.search(r'([A-Za-z\u00C0-\u024F\'\-]+)\s+v\s+([A-Za-z\u00C0-\u024F\'\-]+)',ln,re.IGNORECASE)
                    if vs_m:
                        verify_out+=f"• {vs_m.group(1).strip()} vs {vs_m.group(2).strip()}\n"
                    else:
                        verify_out+=f"• {ln}\n"
            else:
                verify_out+="None — all lines had results.\n"

            # -- SECTION 5: DUPLICATES (only if any) --
            if duplicate_lines:
                verify_out+=f"\n⚠️ **DUPLICATES SKIPPED**\n"
                for ln in duplicate_lines:
                    vs_m=re.search(r'([A-Za-z\u00C0-\u024F\'\-]+)\s+vs\s+([A-Za-z\u00C0-\u024F\'\-]+)',ln,re.IGNORECASE)
                    if not vs_m:
                        vs_m=re.search(r'([A-Za-z\u00C0-\u024F\'\-]+)\s+v\s+([A-Za-z\u00C0-\u024F\'\-]+)',ln,re.IGNORECASE)
                    if vs_m:
                        verify_out+=f"• {vs_m.group(1).strip()} vs {vs_m.group(2).strip()}\n"
                    else:
                        verify_out+=f"• {ln}\n"

            # -- SECTION 6: VERIFIED RESULT --
            verify_out+=f"\n━━━━━━━━━━━━━━━━━━ 📊 **VERIFIED RESULT**\n"
            verify_out+=f"Record: {fw}-{fl}"
            if fwash>0:
                verify_out+=f" ({fwash} Wash)"
            verify_out+=f"\nNormal {nw}-{nl}  ⚠️ {cw}-{cl}  ☢️ {kw}-{kl}"
            verify_out+=f"\nUnits: {four_units_v:+.2f}U\n"

            await send_long_message(message.channel, verify_out)
            return

        else:
            return

        if message.channel.id==TEST_CHANNEL:
            four_channel=message.channel
            totals_channel=message.channel
        else:
            four_channel=client.get_channel(FOUR_PLUS_CHANNEL)
            totals_channel=client.get_channel(TOTALS_CHANNEL)

        fw,fl,fwash,nw,nl,cw,cl,kw,kl,league_stats=await parse_four_plus(four_channel,start,end,limit)
        tw,tl,tunits=await parse_totals(totals_channel,start,end,limit)

        four_units=( (nw*1.1)-(nl*3) + (cw*0.55)-(cl*1.5) + (kw*2.2)-(kl*6) )

        recap=f"📊 **{title}**\n\n"

        recap+="🏓 **4+ PLAYS**\n"

        if fw+fl+fwash==0:
            recap+="No plays graded.\n\n"
        else:
            recap+=f"Record: {fw}-{fl}"

            if fwash>0:
                recap+=f" ({fwash} Wash)"

            recap+=f"\nUnits: {four_units:+.2f}U\n\n"

            recap+=f"Normal {nw}-{nl}\n"
            recap+=f"⚠️ {cw}-{cl}\n"
            recap+=f"☢️ {kw}-{kl}\n\n"

        recap+="🏓 **TOTAL PLAYS**\n"

        if tw+tl==0:
            recap+="No plays graded."
        else:
            recap+=f"Record: {tw}-{tl}\n"
            recap+=f"Units: {tunits:+.2f}U"

        await message.channel.send(recap)

        sorted_leagues = sorted(league_stats.items(), key=lambda x: x[1]["u"], reverse=True)

        league_msg="🏓 **LEAGUE BREAKDOWN**\n━━━━━━━━━━━━━━━━━━\n\n"

        for i,(lg,data) in enumerate(sorted_leagues):

            if i==0:
                icon="🔥"
            elif i==1:
                icon="🟢"
            elif i==2:
                icon="🟡"
            else:
                icon="🔻"

            league_msg+=f"{icon} {lg}\nRecord: {data['w']}-{data['l']}\nUnits: {data['u']:+.2f}U\n\n"

        await message.channel.send(league_msg)

        return


# ==============================
# BASIC COMMANDS
# ==============================

    if message.channel.id not in ALLOWED_CHANNELS:
        return

    if content=="ping":
        await message.channel.send("pong")
        return

    if content=="!reminders":
        if not scheduled_tasks:
            await message.channel.send("⏰ No reminders currently scheduled.")
            return

        lines = [f"⏰ **ACTIVE REMINDERS** ({len(scheduled_tasks)} play(s)) ━━━━━━━━━━━━━━━━━━"]

        for key in sorted(scheduled_tasks.keys()):
            # key format: "LEAGUE|P1|P2|game_dt_iso"
            parts = key.split("|")
            if len(parts) < 4:
                continue
            league_k, p1_k, p2_k, game_iso = parts[0], parts[1], parts[2], parts[3]
            try:
                game_dt_k = datetime.fromisoformat(game_iso)
            except ValueError:
                continue
            lines.append(f"{league_k} – {p1_k} vs {p2_k} @ {game_dt_k.strftime('%I:%M %p')} EST")

        await send_long_message(message.channel, "\n".join(lines))
        return

    if content=="!help" or content=="!commands":
        help_msg=(
            "🏓 **SLATEBOT COMMANDS** 🏓\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "📂 **SLATE**\n"
            "Upload a `.csv` file in an allowed channel to post the day's slate.\n"
            "The bot will delete the previous slate and post a fresh one automatically.\n"
            "\n"
            "📊 **RECAP COMMANDS**\n"
            "`!recap today` — Recap from midnight to now\n"
            "`!recap yesterday` — Full recap for yesterday\n"
            "`!recap weekly` — This week Mon → Mon\n"
            "`!recap last week` — Last full week Mon → Mon\n"
            "`!recap monthly` — This month so far\n"
            "`!recap lifetime` — All-time recap\n"
            "`!recap test` — Test recap (last 50 msgs)\n"
            "\n"
            "🔍 **VERIFY**\n"
            "`!recap verify` — Full audit of last 50 plays in the 4+ channel.\n"
            "Shows counted plays, ignored lines, duplicates, and verified result.\n"
            "\n"
            "🎮 **OTHER**\n"
            "`ping` — Check if bot is online (responds with `pong`)\n"
            "`!reminders` — Show all currently active/pending reminders\n"
            "`!help` or `!commands` — Show this menu\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 **PLAY TIERS (4+ Channel)**\n"
            "Normal — Standard play\n"
            "⚠️ Caution — Lower confidence play\n"
            "☢️ Nuke — Highest confidence play\n"
            "🧼 Wash — No result counted"
        )
        await message.channel.send(help_msg)
        return


# ==============================
# CSV SLATE ENGINE
# ==============================

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


# DELETE PREVIOUS SLATE FIRST
    for msg in last_slate_messages:
        try:
            await msg.delete()
        except:
            pass

    last_slate_messages=[]

    await message.delete()

# SEND NEW SLATE

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

        # Confirmation: show which reminders were scheduled for 4+ plays
        results_4plus = await schedule_reminders_from_text(text, message.guild.id)
        if results_4plus:
            conf_ch = message.channel if message.channel.id == TEST_CHANNEL else None
            await send_reminder_confirmation(results_4plus, override_channel=conf_ch)

    msg3=await message.channel.send("🏓 **TOTAL PLAYS** 🏓")
    last_slate_messages.append(msg3)

    if totals:

        text=""

        for v in totals.values():

            league,p1,p2,play,units,est,pst,wins,total=v

            text+=f"{league} – {p1} vs {p2} {play} {format_units(units)} @ {est} EST / {pst} PST ({wins}/{total})\n\n"

        sent_msgs=await send_long_message(message.channel,text.strip())
        last_slate_messages.extend(sent_msgs)

        # Confirmation: show which reminders were scheduled for totals plays
        results_totals = await schedule_reminders_from_text(text, message.guild.id)
        if results_totals:
            conf_ch = message.channel if message.channel.id == TEST_CHANNEL else None
            await send_reminder_confirmation(results_totals, override_channel=conf_ch)

client.run(TOKEN)
