"""
╔══════════════════════════════════════════════════════╗
║         SHADOW BOT · ShadowSeekers Order             ║
║   Objective tracking · Echo management · GAS sync    ║
║   Study sessions · VC tracking · Shadow Grind badge  ║
╚══════════════════════════════════════════════════════╝

COMMANDS (all slash commands):
  /todo add <objective>        — log a new objective
  /todo multiadd <objectives>  — log multiple objectives at once
  /todo done <number(s)>       — mark objective(s) as fulfilled (comma-separated)
  /todo remove <number>        — remove an objective from your dossier
  /todo list                   — view your active dossier
  /todo clear                  — purge your dossier
  /todo date <MM/DD>           — switch active date (default: today)
  /op add <obj#> <op>          — add an op under an objective
  /op multiadd <obj#> <ops>    — add multiple ops (comma-separated)
  /op done <obj#> <op#s>       — complete op(s) under an objective (comma-separated)
  /op remove <obj#> <op#>      — remove an op from an objective
  /op move <obj#s> <target#>   — convert objectives into ops under target
  /study [task]                — start a focus session (detects VC automatically)
  /pomodoro [task]             — start a 25-min pomodoro session
  /endsession                  — end your active session and submit proof
  /sessions                    — view your session history this week
  /echoes                      — reveal your echo count + rank
  /leaderboard                 — top 10 operatives by echo power
  /link <shadow_id> <n>        — bind your identity to a Shadow ID

COMMAND ONLY (HIGH CLEARANCE):
  /approve @operative  — authorize an identity bind request
  /give @operative <amount> — manually channel echoes
  /setbase <number>    — recalibrate the daily echo threshold
  /forceday            — force the midnight echo reckoning
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import asyncio
import aiohttp
from datetime import datetime, time, date
import pytz
import motor.motor_asyncio
import time as time_module
from ai_missions import setup_ai_missions, ai_mission_task

# ── CONFIG ────────────────────────────────────────────────────────
TOKEN        = os.getenv("DISCORD_TOKEN")
GAS_URL      = os.getenv("GAS_URL", "https://script.google.com/macros/s/AKfycbyTadW-WF4vnpaciFv8Qv58ahWSQ7KVmQfxJA75_z5fZN3UEBunnDPAeq_i5jiu35sYjQ/exec")
ADMIN_ROLE   = os.getenv("ADMIN_ROLE", "Admin")
APPROVE_CH   = os.getenv("APPROVE_CHANNEL", "admin-log")
TIMEZONE     = os.getenv("TIMEZONE", "Asia/Kolkata")
EOD_HOUR     = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE   = int(os.getenv("EOD_MINUTE", "55"))
MONGO_URI    = os.getenv("MONGO_URI")

# Study session config
ECHO_PER_HOUR        = 3
MILESTONE_BONUSES    = {3: 2, 5: 3, 7: 5}   # hours -> bonus echoes
MAX_SESSION_HOURS    = 7
DAILY_SESSION_CAP    = 31                     # 7*3 + 2+3+5
FOCUS_LOG_CHANNEL    = os.getenv("FOCUS_LOG_CHANNEL", "focus-log")
POMODORO_MINUTES     = 25

# ── MONGODB SETUP ─────────────────────────────────────────────────
_mongo_client = None
_db = None

def get_db():
    global _mongo_client, _db
    if MONGO_URI and _db is None:
        _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        _db = _mongo_client["shadowbot"]
    return _db

# ── DATA LOAD/SAVE ────────────────────────────────────────────────
DATA_FILE = "data.json"

def _sanitize_members(raw: list) -> list:
    """Ensure every element in the members list is a dict, not a JSON string."""
    result = []
    for m in raw:
        if isinstance(m, str):
            try:
                parsed = json.loads(m)
                if isinstance(parsed, dict):
                    result.append(parsed)
            except Exception:
                pass  # skip malformed entries
        elif isinstance(m, dict):
            result.append(m)
    return result

def _sanitize_sessions(raw: dict) -> dict:
    """Ensure every session value is a dict, not a JSON string (MongoDB serialization bug)."""
    result = {}
    for uid, sess in raw.items():
        if isinstance(sess, str):
            try:
                parsed = json.loads(sess)
                if isinstance(parsed, dict):
                    result[uid] = parsed
            except Exception:
                pass  # drop malformed session
        elif isinstance(sess, dict):
            result[uid] = sess
    return result

async def load_data():
    db = get_db()
    if db is not None:
        doc          = await db["config"].find_one({"_id": "main"}) or {}
        members_doc  = await db["members"].find_one({"_id": "list"}) or {}
        sessions_doc = await db["sessions"].find_one({"_id": "active"}) or {}
        raw_members  = members_doc.get("members", [])
        return {
            "base_echo_rate":       doc.get("base_echo_rate", 10),
            "links":                doc.get("links", {}),
            "pending_links":        doc.get("pending_links", {}),
            "todos":                doc.get("todos", {}),
            "members":              _sanitize_members(raw_members),
            "active_sessions":      _sanitize_sessions(sessions_doc.get("sessions", {})),
            "daily_session_echoes": doc.get("daily_session_echoes", {}),
        }
    else:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        return {
            "base_echo_rate": 10,
            "links": {},
            "pending_links": {},
            "todos": {},
            "members": [],
            "active_sessions": {},
            "daily_session_echoes": {},
        }

async def save_data(data):
    db = get_db()
    if db is not None:
        await db["config"].update_one(
            {"_id": "main"},
            {"$set": {
                "base_echo_rate": data.get("base_echo_rate", 10),
                "links":          data.get("links", {}),
                "pending_links":  data.get("pending_links", {}),
                "todos":          data.get("todos", {}),
                "daily_session_echoes": data.get("daily_session_echoes", {}),
            }},
            upsert=True
        )
        await db["members"].update_one(
            {"_id": "list"},
            {"$set": {"members": data.get("members", [])}},
            upsert=True
        )
        await db["sessions"].update_one(
            {"_id": "active"},
            {"$set": {"sessions": data.get("active_sessions", {})}},
            upsert=True
        )
    else:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)

# ── BOT SETUP ─────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── HELPERS ───────────────────────────────────────────────────────
def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.name == ADMIN_ROLE for r in interaction.user.roles)

def get_shadow_id(user_id: str, data: dict):
    link = data["links"].get(str(user_id))
    if link and link.get("approved"):
        return link["shadow_id"]
    return None

def get_member(shadow_id: str, data: dict):
    return next((m for m in data["members"] if m["shadowId"] == shadow_id), None)

def today_str() -> str:
    tz  = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    return now.strftime("%m/%d")

def get_active_date(uid: str, data: dict) -> str:
    entry = data["todos"].get(uid)
    if isinstance(entry, dict):
        return entry.get("active_date", today_str())
    return today_str()

def get_todos_for_date(uid: str, date_key: str, data: dict) -> list:
    entry = data["todos"].get(uid)
    if isinstance(entry, list):
        data["todos"][uid] = {
            "active_date": today_str(),
            "dates": {today_str(): entry}
        }
        return data["todos"][uid]["dates"].get(date_key, [])
    if isinstance(entry, dict):
        return entry.get("dates", {}).get(date_key, [])
    return []

def set_todos_for_date(uid: str, date_key: str, todos: list, data: dict):
    if not isinstance(data["todos"].get(uid), dict):
        data["todos"][uid] = {"active_date": today_str(), "dates": {}}
    data["todos"][uid].setdefault("dates", {})[date_key] = todos

ECHO_TIERS = [
    {"name": "Initiate",  "min": 0,    "color": 0x6B6B9A},
    {"name": "Seeker",    "min": 500,  "color": 0x7B2FBE},
    {"name": "Phantom",   "min": 1500, "color": 0xA855F7},
    {"name": "Wraith",    "min": 3000, "color": 0xE63946},
    {"name": "Voidborn",  "min": 5000, "color": 0xF0A500},
]

PRIORITY_EMOJI = {
    "p1": "♦️",
    "p2": "🔸",
    "p3": "🏷️",
}
DONE_EMOJI   = "☘️"
UNDONE_EMOJI = "○"

def get_tier(echo_count: int):
    tier = ECHO_TIERS[0]
    for t in ECHO_TIERS:
        if echo_count >= t["min"]:
            tier = t
    return tier

def make_embed(title, description="", color=0x7B2FBE):
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="☽ SHADOWSEEKERS ORDER · DEEP IN THE DARK, I DON'T NEED THE LIGHT")
    return e

def format_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}min"
    elif m > 0:
        return f"{m}min {s}s"
    return f"{s}s"

def make_progress_bar(elapsed_seconds: int, total_seconds: int, width: int = 10) -> str:
    pct = min(elapsed_seconds / total_seconds, 1.0) if total_seconds > 0 else 0
    filled = round(pct * width)
    return "▓" * filled + "░" * (width - filled)

# ── SESSION ECHO CALCULATOR ───────────────────────────────────────
def calculate_session_echoes(duration_seconds: int, daily_earned_so_far: int) -> dict:
    """
    Returns echoes earned, milestones hit, and breakdown.
    3 echoes per completed hour + milestone bonuses.
    Hard cap: DAILY_SESSION_CAP total per day.
    """
    hours_completed = int(duration_seconds // 3600)
    hours_completed = min(hours_completed, MAX_SESSION_HOURS)

    base_echoes = hours_completed * ECHO_PER_HOUR
    bonus_echoes = 0
    milestones_hit = []

    for milestone_hr, bonus in MILESTONE_BONUSES.items():
        if hours_completed >= milestone_hr:
            bonus_echoes += bonus
            milestones_hit.append((milestone_hr, bonus))

    total = base_echoes + bonus_echoes
    # Apply daily cap
    remaining_cap = max(0, DAILY_SESSION_CAP - daily_earned_so_far)
    awarded = min(total, remaining_cap)

    return {
        "hours": hours_completed,
        "base": base_echoes,
        "bonus": bonus_echoes,
        "total": total,
        "awarded": awarded,
        "milestones": milestones_hit,
        "capped": awarded < total,
    }

# ── GAS SYNC ──────────────────────────────────────────────────────
async def pull_from_gas(data: dict):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GAS_URL + "?action=read", allow_redirects=True) as resp:
                text    = await resp.text()
                members = json.loads(text)
                if isinstance(members, list) and members:
                    data["members"] = _sanitize_members(members)
                    await save_data(data)
                    return True
    except Exception as e:
        print(f"[GAS PULL ERROR] {e}")
    return False

async def push_to_gas(data: dict):
    try:
        payload = json.dumps({
            "action": "write",
            "members": [
                {**m, "shadowCardImage": None,
                 "passphrase": data.get("credentials", {}).get(m["shadowId"], "")}
                for m in data["members"]
            ]
        })
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GAS_URL,
                data=payload,
                headers={"Content-Type": "text/plain"}
            ) as resp:
                print(f"[GAS PUSH] Status: {resp.status}")
                return True
    except Exception as e:
        print(f"[GAS PUSH ERROR] {e}")
    return False

async def push_proof_to_gas(session_data: dict) -> bool:
    """Push session proof (image link + metadata) to Google Sheets via GAS."""
    try:
        payload = json.dumps({
            "action": "submitProof",
            "shadowId":    session_data["shadow_id"],
            "codename":    session_data["codename"],
            "task":        session_data["task"],
            "duration":    format_duration(session_data["duration_seconds"]),
            "hours":       session_data["hours"],
            "echoes":      session_data["awarded"],
            "proofLink":   session_data.get("proof_link", ""),
            "sessionType": session_data.get("session_type", "study"),
            "date":        today_str(),
            "timestamp":   datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M"),
        })
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GAS_URL,
                data=payload,
                headers={"Content-Type": "text/plain"}
            ) as resp:
                print(f"[GAS PROOF] Status: {resp.status}")
                return resp.status == 200
    except Exception as e:
        print(f"[GAS PROOF ERROR] {e}")
    return False

async def create_member_on_gas(member: dict) -> bool:
    try:
        payload = json.dumps({
            "action":    "create",
            "shadowId":  member["shadowId"],
            "codename":  member["codename"],
            "discordId": member["discordId"],
            "echoCount": member.get("echoCount", 0),
        })
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GAS_URL,
                data=payload,
                headers={"Content-Type": "text/plain"}
            ) as resp:
                text = await resp.text()
                print(f"[GAS CREATE] Status: {resp.status} | Response: {text[:200]}")
                return resp.status == 200
    except Exception as e:
        print(f"[GAS CREATE ERROR] {e}")
    return False

# ── ACTIVE SESSION TIMER LOOP ─────────────────────────────────────
# Stores: uid -> {task, start_time, session_type, in_vc, channel_id, message_id, guild_id, pomodoro_end}
_session_messages = {}   # uid -> discord.Message (for editing)

@tasks.loop(seconds=20)
async def session_ticker():
    """Every 20 seconds: update live timer embeds + check pomodoro end."""
    data = await load_data()
    now  = time_module.time()

    for uid, sess in list(data["active_sessions"].items()):
        try:
            guild   = bot.get_guild(int(sess["guild_id"]))
            channel = guild.get_channel(int(sess["channel_id"])) if guild else None
            if not channel:
                continue

            elapsed  = int(now - sess["start_time"])
            is_pomo  = sess["session_type"] == "pomodoro"
            pomo_end = sess.get("pomodoro_end")
            timer_total = sess.get("timer_total")

            # Timed session (pomodoro OR study with duration)
            if pomo_end and timer_total:
                remaining = max(0, int(pomo_end - now))
                bar       = make_progress_bar(timer_total - remaining, timer_total)
                time_str  = format_duration(remaining)

                if is_pomo:
                    status = "⏰ POMODORO ENDING SOON" if remaining < 120 else "🍅 POMODORO IN PROGRESS"
                    done_label = "🍅 POMODORO COMPLETE"
                    done_body  = "25 minutes locked in. Use `/endsession` to submit proof and claim your echoes."
                else:
                    mins = timer_total // 60
                    status = "⏰ SESSION ENDING SOON" if remaining < 120 else f"⏱️ TIMED SESSION — {mins}min"
                    done_label = "⏱️ TIMER COMPLETE"
                    done_body  = f"{mins}-minute session done. Use `/endsession` to submit proof and claim echoes."

                if remaining == 0:
                    embed = make_embed(
                        done_label,
                        f"**{sess['task']}**\n\n{done_body}",
                        color=0x10B981
                    )
                else:
                    embed = make_embed(
                        status,
                        f"**{sess['task']}**\n\n"
                        f"`[{bar}]` **{time_str} left**\n"
                        f"Elapsed: {format_duration(elapsed)}\n\n"
                        f"{'🔔 Time is almost up! Use `/endsession` to submit proof.' if remaining < 120 else 'Stay locked in. Use `/endsession` when done.'}",
                        color=0xF0A500 if remaining < 120 else 0xA855F7
                    )
                embed.set_author(name=f"Operative: {sess.get('codename', uid)}")

            else:
                # Open-ended study session (no timer)
                hours_done = elapsed // 3600
                next_milestone = None
                for mhr in sorted(MILESTONE_BONUSES.keys()):
                    if hours_done < mhr:
                        next_milestone = mhr
                        break

                secs_to_next_hr = 3600 - (elapsed % 3600)
                bar = make_progress_bar(elapsed % 3600, 3600)

                milestone_note = ""
                if next_milestone:
                    milestone_note = f"\n🎯 Next milestone: **{next_milestone}h** (+{MILESTONE_BONUSES[next_milestone]} bonus echoes)"
                elif hours_done >= MAX_SESSION_HOURS:
                    milestone_note = "\n🏆 **MAX SESSION REACHED** — Submit proof to claim all echoes."

                vc_note = " · 🎙️ In VC" if sess.get("in_vc") else ""

                embed = make_embed(
                    "☽ FOCUS SESSION IN PROGRESS",
                    f"**{sess['task']}**\n\n"
                    f"`[{bar}]` **{format_duration(elapsed)} elapsed**{vc_note}\n"
                    f"Next hour in: {format_duration(secs_to_next_hr)}\n"
                    f"Echoes so far: **~{hours_done * ECHO_PER_HOUR}**{milestone_note}\n\n"
                    f"Use `/endsession` to submit proof and claim echoes.",
                    color=0x7B2FBE
                )
                embed.set_author(name=f"Operative: {sess.get('codename', uid)}")

            # Edit existing message if we have it
            msg = _session_messages.get(uid)
            if msg:
                try:
                    await msg.edit(embed=embed)
                except:
                    pass

        except Exception as e:
            print(f"[SESSION TICKER ERROR] uid={uid}: {e}")

# ── END OF DAY CALCULATION ────────────────────────────────────────
async def run_end_of_day(guild: discord.Guild, announce=True):
    data    = await load_data()
    base    = data.get("base_echo_rate", 10)
    today   = today_str()
    results = []

    # Reset daily session echo counters
    data["daily_session_echoes"] = {}

    for discord_id, link in data["links"].items():
        if not link.get("approved"):
            continue
        shadow_id = link["shadow_id"]
        todos     = get_todos_for_date(discord_id, today, data)

        if not todos:
            earned = 0
            pct    = 0
        else:
            total_weight = len(todos)
            done_weight  = 0.0
            for t in todos:
                ops = t.get("ops", [])
                if ops:
                    done_ops = sum(1 for op in ops if op.get("done"))
                    done_weight += done_ops / len(ops)
                else:
                    if t["done"]:
                        done_weight += 1
            pct    = done_weight / total_weight
            earned = round(base * pct)

        for i, m in enumerate(data["members"]):
            if m["shadowId"] == shadow_id:
                old = int(m.get("echoCount", 0))
                data["members"][i]["echoCount"] = old + earned
                results.append({
                    "shadow_id": shadow_id,
                    "codename":  m.get("codename", shadow_id),
                    "earned":    earned,
                    "pct":       pct,
                    "total":     len(todos),
                    "done":      sum(1 for t in todos if t["done"]),
                    "new_total": old + earned,
                })
                break

        set_todos_for_date(discord_id, today, [], data)

    await save_data(data)
    await push_to_gas(data)

    if announce and results:
        ch = discord.utils.get(guild.text_channels, name="echo-log")
        if not ch:
            ch = discord.utils.get(guild.text_channels, name="general")
        if ch:
            lines = []
            for r in sorted(results, key=lambda x: -x["earned"]):
                bar_filled = round(r["pct"] * 10)
                bar = "█" * bar_filled + "░" * (10 - bar_filled)
                lines.append(
                    f"`{r['shadow_id']}` **{r['codename']}**\n"
                    f"`[{bar}]` {r['done']}/{r['total']} objectives · **+{r['earned']} echoes**"
                )
            embed = make_embed(
                "☽ NIGHTLY ECHO RECKONING",
                "\n\n".join(lines) or "The void recorded no activity this cycle.",
                color=0xF0A500
            )
            embed.set_footer(text=f"☽ SHADOWSEEKERS ORDER · Base resonance: {base} · {datetime.now().strftime('%d %b %Y')}")
            await ch.send(embed=embed)

    return results

# ── SCHEDULED TASK ────────────────────────────────────────────────
@tasks.loop(time=time(hour=EOD_HOUR, minute=EOD_MINUTE, tzinfo=pytz.timezone(TIMEZONE)))
async def daily_echo_task():
    for guild in bot.guilds:
        await run_end_of_day(guild)

# ══════════════════════════════════════════════════════════════════
#  STUDY SESSION COMMANDS
# ══════════════════════════════════════════════════════════════════

async def _start_session(interaction: discord.Interaction, task: str, session_type: str, duration_minutes: int = None):
    """Shared logic for /study and /pomodoro."""
    data      = await load_data()
    uid       = str(interaction.user.id)
    shadow_id = get_shadow_id(uid, data)

    if not shadow_id:
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "Link your Shadow ID first — use `/link <shadow_id> <n>`.", color=0xE63946)
        )
        return

    if uid in data["active_sessions"]:
        await interaction.response.send_message(
            embed=make_embed("▲ SESSION ACTIVE", "You already have an active session. Use `/endsession` to close it first.", color=0xE63946)
        )
        return

    member   = get_member(shadow_id, data)
    codename = member.get("codename", shadow_id) if member else shadow_id

    # Check if user is in VC
    in_vc      = False
    vc_channel = None
    if interaction.guild:
        member_obj = interaction.guild.get_member(interaction.user.id)
        if member_obj and member_obj.voice and member_obj.voice.channel:
            in_vc      = True
            vc_channel = member_obj.voice.channel.name

    now = time_module.time()

    # Determine timer end
    if session_type == "pomodoro":
        timer_end = now + (POMODORO_MINUTES * 60)
        timer_total = POMODORO_MINUTES * 60
    elif duration_minutes:
        timer_end   = now + (duration_minutes * 60)
        timer_total = duration_minutes * 60
    else:
        timer_end   = None
        timer_total = None

    session = {
        "task":         task,
        "start_time":   now,
        "session_type": session_type,
        "in_vc":        in_vc,
        "vc_channel":   vc_channel or "",
        "channel_id":   str(interaction.channel_id),
        "guild_id":     str(interaction.guild_id),
        "shadow_id":    shadow_id,
        "codename":     codename,
        "pomodoro_end": timer_end,   # reused for custom timer too
        "timer_total":  timer_total,
    }
    data["active_sessions"][uid] = session
    await save_data(data)

    vc_note = f"\n🎙️ Detected in **{vc_channel}** — VC bonus active!" if in_vc else "\n💡 Join a VC channel for a higher echo rate."

    if session_type == "pomodoro":
        type_note = f"🍅 **POMODORO** — 25 minutes locked."
    elif duration_minutes:
        type_note = f"⏱️ **TIMED SESSION** — {duration_minutes} minutes set."
    else:
        type_note = "☽ **STUDY SESSION** — open-ended."

    bar = make_progress_bar(0, timer_total or 3600)

    embed = make_embed(
        "◉ SESSION STARTED",
        f"**{task}**\n\n"
        f"{type_note}{vc_note}\n\n"
        f"`[{bar}]` **0m elapsed**\n\n"
        f"Echo rate: **{ECHO_PER_HOUR} echoes/hr**\n"
        f"Milestones: 3h +2 · 5h +3 · 7h +5 🏆\n\n"
        f"Use `/endsession` when done to submit proof and claim echoes.",
        color=0x10B981
    )
    embed.set_author(name=f"Operative: {codename}")

    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    _session_messages[uid] = msg

    # Post in focus-log channel if different
    focus_ch = discord.utils.get(interaction.guild.text_channels, name=FOCUS_LOG_CHANNEL)
    if focus_ch and focus_ch.id != interaction.channel_id:
        log_embed = make_embed(
            "☽ OPERATIVE LOCKED IN",
            f"{interaction.user.mention} started a {session_type} session\n**{task}**{vc_note}",
            color=0x7B2FBE
        )
        await focus_ch.send(embed=log_embed)


@tree.command(name="study", description="Start a focus session — open-ended or with a timer")
@app_commands.describe(
    task="What are you working on?",
    duration="Timer in minutes (e.g. 30 = 30 minutes, 60 = 1 hour). Leave blank for open-ended."
)
async def study(interaction: discord.Interaction, task: str, duration: int = None):
    if duration is not None and duration < 1:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID DURATION", "Duration must be at least 1 minute.", color=0xE63946)
        )
        return
    await _start_session(interaction, task, "study", duration_minutes=duration)


@tree.command(name="pomodoro", description="Start a 25-minute Pomodoro session")
@app_commands.describe(task="What are you working on?")
async def pomodoro(interaction: discord.Interaction, task: str):
    await _start_session(interaction, task, "pomodoro")


@tree.command(name="endsession", description="End your active session, submit proof, and claim echoes")
@app_commands.describe(
    proof="Upload an image OR paste a link/text as proof of your session",
    attachment="Upload a screenshot or photo as proof (renders in the embed)"
)
async def endsession(interaction: discord.Interaction, proof: str = None, attachment: discord.Attachment = None):
    data      = await load_data()
    uid       = str(interaction.user.id)
    shadow_id = get_shadow_id(uid, data)

    if not shadow_id:
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "No Shadow ID linked.", color=0xE63946)
        )
        return

    sess = data["active_sessions"].get(uid)
    if not sess:
        await interaction.response.send_message(
            embed=make_embed("▲ NO ACTIVE SESSION", "You don't have an active session. Start one with `/study` or `/pomodoro`.", color=0xE63946)
        )
        return

    if not proof and not attachment:
        await interaction.response.send_message(
            embed=make_embed("▲ PROOF REQUIRED", "Submit proof to end your session — upload an image or describe what you accomplished.", color=0xE63946)
        )
        return

    # Acknowledge immediately so Discord doesn't time out
    await interaction.response.defer()

    try:
        now              = time_module.time()
        duration_seconds = int(now - float(sess["start_time"]))
        today            = today_str()

        # Get daily earned so far
        daily_key     = f"{uid}_{today}"
        daily_earned  = data.get("daily_session_echoes", {}).get(daily_key, 0)

        echo_info = calculate_session_echoes(duration_seconds, daily_earned)

        # Award echoes
        sg_count = 0
        new_badge = False
        for i, m in enumerate(data["members"]):
            if m["shadowId"] == shadow_id:
                old = int(m.get("echoCount", 0))
                data["members"][i]["echoCount"] = old + echo_info["awarded"]

                # Badge tracking — Shadow Grind
                badges   = data["members"][i].get("badges", {})
                sg_count = badges.get("shadow_grind", 0)
                new_badge = echo_info["hours"] >= MAX_SESSION_HOURS
                if new_badge:
                    sg_count += 1
                    badges["shadow_grind"] = sg_count
                    data["members"][i]["badges"] = badges
                break

        # Update daily cap tracker
        if "daily_session_echoes" not in data:
            data["daily_session_echoes"] = {}
        data["daily_session_echoes"][daily_key] = daily_earned + echo_info["awarded"]

        # Remove active session
        del data["active_sessions"][uid]
        await save_data(data)

        # Remove live message
        _session_messages.pop(uid, None)

    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        print(f"[ENDSESSION ERROR] uid={uid}: {e}\n{err_detail}")
        short_err = type(e).__name__ + ": " + str(e)[:300]
        await interaction.followup.send(
            embed=make_embed(
                "▲ SESSION END ERROR",
                f"Something went wrong ending your session.\n```{short_err}```\nPlease screenshot this and report it.",
                color=0xE63946
            )
        )
        return

    # ── Resolve proof link ─────────────────────────────────────────
    # Priority: uploaded attachment > URL in proof text > plain text
    proof_image_url = None
    proof_display   = proof or ""

    if attachment:
        # Discord CDN link from the uploaded file
        proof_image_url = attachment.url
        proof_display   = attachment.url
    elif proof:
        is_image_url = (
            proof.startswith("http") and (
                any(proof.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"])
                or "cdn.discordapp.com" in proof
                or "imgur.com" in proof
                or "i.ibb.co" in proof
                or "media.discordapp.net" in proof
            )
        )
        if is_image_url:
            proof_image_url = proof

    # ── Build result embed ─────────────────────────────────────────
    milestone_lines = ""
    if echo_info["milestones"]:
        milestone_lines = "\n" + "\n".join(
            f"🏆 **{hr}h milestone** → +{bonus} echoes" for hr, bonus in echo_info["milestones"]
        )

    badge_line = f"\n\n🏅 **SHADOW GRIND BADGE EARNED!** You now have **{sg_count}** Shadow Grind badge(s)." if new_badge else ""
    cap_note   = "\n⚠️ Daily echo cap reached — some echoes were not awarded." if echo_info["capped"] else ""

    embed = make_embed(
        "☽ SESSION COMPLETE",
        f"**{sess['task']}**\n\n"
        f"⏱ Duration: **{format_duration(duration_seconds)}** ({echo_info['hours']}h completed)\n"
        f"Base echoes: **{echo_info['base']}** ({echo_info['hours']} hrs × {ECHO_PER_HOUR})"
        f"{milestone_lines}\n"
        f"**Total awarded: {echo_info['awarded']} echoes**{cap_note}"
        f"{badge_line}",
        color=0x10B981
    )
    embed.set_author(name=f"Operative: {sess['codename']}")

    if proof_image_url:
        embed.set_image(url=proof_image_url)
        embed.add_field(
            name="Proof",
            value=f"[View Image]({proof_image_url})",
            inline=False
        )
    elif proof_display:
        embed.add_field(name="Proof", value=proof_display[:1024], inline=False)

    await interaction.followup.send(embed=embed)

    # ── Push to GAS (fire-and-forget, won't block or cause timeout) ─
    sess_data = {
        **sess,
        "duration_seconds": duration_seconds,
        "hours":            echo_info["hours"],
        "awarded":          echo_info["awarded"],
        "proof_link":       proof_image_url or "",
        "proof_text":       proof_display if not proof_image_url else "",
        "shadow_id":        shadow_id,
        "codename":         sess.get("codename", shadow_id),
    }

    async def _background_push():
        try:
            await push_proof_to_gas(sess_data)
        except Exception as e:
            print(f"[BG PROOF PUSH ERROR] {e}")
        try:
            await push_to_gas(data)
        except Exception as e:
            print(f"[BG GAS PUSH ERROR] {e}")

    asyncio.create_task(_background_push())

    # Announce in focus-log
    focus_ch = discord.utils.get(interaction.guild.text_channels, name=FOCUS_LOG_CHANNEL)
    if focus_ch and focus_ch.id != interaction.channel_id:
        log_embed = make_embed(
            "✅ SESSION SUBMITTED",
            f"{interaction.user.mention} completed a {sess['session_type']} session\n"
            f"**{sess['task']}** · {format_duration(duration_seconds)} · **+{echo_info['awarded']} echoes**"
            f"{badge_line}",
            color=0x10B981
        )
        if proof_image_url:
            log_embed.set_image(url=proof_image_url)
        await focus_ch.send(embed=log_embed)


@tree.command(name="sessions", description="View your focus session stats")
async def sessions_cmd(interaction: discord.Interaction):
    data      = await load_data()
    uid       = str(interaction.user.id)
    shadow_id = get_shadow_id(uid, data)

    if not shadow_id:
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "Link your Shadow ID first.", color=0xE63946)
        )
        return

    member = get_member(shadow_id, data)
    if not member:
        await interaction.response.send_message(
            embed=make_embed("▲ NOT FOUND", "No member record found.", color=0xE63946)
        )
        return

    sg_count  = member.get("badges", {}).get("shadow_grind", 0)
    echo_count = int(member.get("echoCount", 0))
    today      = today_str()
    daily_key  = f"{uid}_{today}"
    daily_sess = data.get("daily_session_echoes", {}).get(daily_key, 0)

    active = data["active_sessions"].get(uid)
    active_note = ""
    if active:
        elapsed     = int(time_module.time() - active["start_time"])
        active_note = f"\n\n🔴 **Active session:** {active['task']} · {format_duration(elapsed)} elapsed"

    embed = make_embed(
        f"◈ {member.get('codename', shadow_id)}'s SESSION PROFILE",
        f"**Echo Resonance:** {echo_count:,}\n"
        f"**Session echoes today:** {daily_sess}/{DAILY_SESSION_CAP}\n"
        f"**Shadow Grind badges:** {'🏅 × ' + str(sg_count) if sg_count else 'None yet — hit 7h in a single session!'}"
        f"{active_note}",
        color=0xA855F7
    )
    embed.add_field(
        name="Echo Structure",
        value=f"3 echoes/hr · 3h +2 · 5h +3 · 7h +5 🏆\nDaily cap: {DAILY_SESSION_CAP} echoes",
        inline=False
    )
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  VC TRACKING
# ══════════════════════════════════════════════════════════════════

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Detect VC joins and prompt users to start a study session."""
    if member.bot:
        return

    uid  = str(member.id)
    data = await load_data()

    # Someone joined a VC (wasn't in one before)
    joined = before.channel is None and after.channel is not None
    # Someone left a VC
    left   = before.channel is not None and after.channel is None

    if joined:
        shadow_id = get_shadow_id(uid, data)
        if not shadow_id:
            return  # Not linked, ignore

        # Update active session VC status if they have one
        if uid in data["active_sessions"]:
            data["active_sessions"][uid]["in_vc"]      = True
            data["active_sessions"][uid]["vc_channel"] = after.channel.name
            await save_data(data)
            return

        # Post in focus-log channel prompting them to start a session
        focus_ch = discord.utils.get(member.guild.text_channels, name=FOCUS_LOG_CHANNEL)
        if focus_ch:
            m_obj    = get_member(shadow_id, data)
            codename = m_obj.get("codename", shadow_id) if m_obj else shadow_id
            embed    = make_embed(
                "☽ OPERATIVE ENTERED THE VOID",
                f"{member.mention} joined **{after.channel.name}**\n\n"
                f"Use `/study <task>` or `/pomodoro <task>` to lock in your session and earn echoes.\n"
                f"🎙️ VC detected — you'll earn at the active rate.",
                color=0x6B6B9A
            )
            embed.set_author(name=f"Operative: {codename}")
            await focus_ch.send(embed=embed)

    elif left:
        # If they had an active session and were in VC, update VC status
        if uid in data["active_sessions"]:
            data["active_sessions"][uid]["in_vc"]      = False
            data["active_sessions"][uid]["vc_channel"] = ""
            await save_data(data)

# ══════════════════════════════════════════════════════════════════
#  SLASH COMMANDS (existing)
# ══════════════════════════════════════════════════════════════════

# ── /todo ─────────────────────────────────────────────────────────
todo_group = app_commands.Group(name="todo", description="Manage your objectives")

@todo_group.command(name="date", description="Switch your active session date — todos go to that day")
@app_commands.describe(date="Date in MM/DD format e.g. 04/15. Leave blank to see your current active date.")
async def todo_date(interaction: discord.Interaction, date: str = None):
    data = await load_data()
    uid  = str(interaction.user.id)

    if not get_shadow_id(uid, data):
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "Link your Shadow ID first — use `/link <shadow_id> <n>`.", color=0xE63946)
        )
        return

    if date is None:
        active = get_active_date(uid, data)
        todos  = get_todos_for_date(uid, active, data)
        is_today = active == today_str()
        label    = " *(today)*" if is_today else ""
        await interaction.response.send_message(
            embed=make_embed(
                "◈ ACTIVE SESSION DATE",
                f"You're currently working on **{active}**{label}.\n`{len(todos)}` objective(s) on this date.",
                color=0xA855F7
            )
        )
        return

    import re
    if not re.match(r'^\d{2}/\d{2}$', date.strip()):
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID FORMAT", "Use `MM/DD` format — e.g. `04/15`.", color=0xE63946)
        )
        return

    try:
        datetime.strptime(date.strip() + f"/{datetime.now().year}", "%m/%d/%Y")
    except ValueError:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID DATE", f"`{date}` isn't a real date. Try again.", color=0xE63946)
        )
        return

    date_key = date.strip()
    if not isinstance(data["todos"].get(uid), dict):
        data["todos"][uid] = {"active_date": today_str(), "dates": {}}
    data["todos"][uid]["active_date"] = date_key
    await save_data(data)

    todos    = get_todos_for_date(uid, date_key, data)
    is_today = date_key == today_str()
    label    = " *(today)*" if is_today else ""

    await interaction.response.send_message(
        embed=make_embed(
            "◈ SESSION DATE SWITCHED",
            f"**{interaction.user.display_name}** is now working on **{date_key}**{label}.\n"
            f"`{len(todos)}` objective(s) already on this date.\n\n"
            f"All `/todo add`, `/todo done`, `/todo list` now apply to **{date_key}**.",
            color=0x10B981
        )
    )

@todo_group.command(name="add", description="Log a new objective to your dossier")
@app_commands.describe(task="The objective to be carried out")
async def todo_add(interaction: discord.Interaction, task: str):
    data = await load_data()
    uid  = str(interaction.user.id)

    if not get_shadow_id(uid, data):
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "Link your Shadow ID first — use `/link <shadow_id> <n>`.", color=0xE63946),
        )
        return

    active    = get_active_date(uid, data)
    todos     = get_todos_for_date(uid, active, data)
    todos.append({"task": task, "done": False, "priority": None})
    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    is_today  = active == today_str()
    date_note = "" if is_today else f" *(for {active})*"

    await interaction.response.send_message(
        embed=make_embed(
            "◉ OBJECTIVE ADDED",
            f"**{interaction.user.display_name}** logged objective **#{len(todos)}**{date_note}\n\n*{task}*",
            color=0x10B981
        )
    )

@todo_group.command(name="done", description="Mark objectives as fulfilled — single or comma-separated e.g. 1,3,5")
@app_commands.describe(numbers="Objective number(s), comma-separated e.g. 1,3,5")
async def todo_done(interaction: discord.Interaction, numbers: str):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    try:
        indices = sorted(set(int(n.strip()) for n in numbers.split(",") if n.strip()))
    except ValueError:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID INPUT", "Provide number(s) separated by commas e.g. `1,3,5`.", color=0xE63946)
        )
        return

    invalid = [n for n in indices if n < 1 or n > len(todos)]
    if not todos or invalid:
        await interaction.response.send_message(
            embed=make_embed("▲ OBJECTIVE NOT FOUND", f"Objective(s) {', '.join(f'#{n}' for n in invalid)} don't exist. Check `/todo list`.", color=0xE63946),
        )
        return

    completed = []
    for n in indices:
        todos[n - 1]["done"] = True
        completed.append(todos[n - 1]["task"])

    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    total = len(todos)
    done_weight = 0.0
    for t in todos:
        ops = t.get("ops", [])
        if ops:
            done_ops = sum(1 for op in ops if op.get("done"))
            done_weight += done_ops / len(ops)
        else:
            if t["done"]:
                done_weight += 1
    done  = sum(1 for t in todos if t["done"])
    pct   = round((done_weight / total) * 100) if total else 0
    base  = data.get("base_echo_rate", 10)
    proj  = round(base * done_weight / total) if total else 0

    is_today  = active == today_str()
    date_note = "" if is_today else f" *(session: {active})*"
    task_lines = "\n".join(f"*{task}*" for task in completed)
    title = "☽ OBJECTIVE FULFILLED" if len(completed) == 1 else f"☽ {len(completed)} OBJECTIVES FULFILLED"

    await interaction.response.send_message(
        embed=make_embed(
            title,
            f"**{interaction.user.display_name}** completed:{date_note}\n{task_lines}\n\n"
            f"`{done}/{total} objectives` · {pct}% complete\n"
            f"Projected echoes: **{proj}**",
            color=0x10B981
        )
    )

@todo_group.command(name="list", description="View your operative dossier")
async def todo_list(interaction: discord.Interaction):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos:
        is_today  = active == today_str()
        date_note = "today" if is_today else active
        await interaction.response.send_message(
            embed=make_embed("◈ DOSSIER EMPTY", f"No objectives for **{date_note}**. Add one with `/todo add`.", color=0x7B2FBE)
        )
        return

    lines = []
    done_weight = 0.0
    for i, t in enumerate(todos, 1):
        priority = t.get("priority")
        suffix   = f" {PRIORITY_EMOJI[priority]}" if priority else ""
        ops      = t.get("ops", [])

        if ops and all(op.get("done") for op in ops):
            t["done"] = True

        if t["done"]:
            lines.append(f"{DONE_EMOJI} ~~☽ {i}. {t['task']}~~{suffix}")
            done_weight += 1
        else:
            lines.append(f"{UNDONE_EMOJI} {i}. {t['task']}{suffix}")
            if ops:
                done_ops = sum(1 for op in ops if op.get("done"))
                done_weight += done_ops / len(ops)

        for op in ops:
            if op.get("done"):
                lines.append(f"    └ {DONE_EMOJI} ~~{op['task']}~~")
            else:
                lines.append(f"    └ {UNDONE_EMOJI} {op['task']}")

    total = len(todos)
    base  = data.get("base_echo_rate", 10)
    proj  = round(base * done_weight / total) if total else 0
    done  = sum(1 for t in todos if t["done"])

    is_today   = active == today_str()
    title_date = "TODAY'S" if is_today else active

    embed = make_embed(f"◈ {interaction.user.display_name}'s {title_date} OBJECTIVES", "\n".join(lines), color=0xA855F7)
    embed.add_field(name="Progress", value=f"{done}/{total} done · **{proj} echoes** on track", inline=False)
    await interaction.response.send_message(embed=embed)

@todo_group.command(name="clear", description="Purge your active date's dossier")
async def todo_clear(interaction: discord.Interaction):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    set_todos_for_date(uid, active, [], data)
    await save_data(data)

    is_today  = active == today_str()
    date_note = "today's" if is_today else f"{active}'s"

    await interaction.response.send_message(
        embed=make_embed("◈ OBJECTIVES CLEARED", f"**{interaction.user.display_name}** {date_note} dossier cleared. Fresh start.", color=0x6B6B9A)
    )

@todo_group.command(name="remove", description="Remove an objective from your dossier entirely")
@app_commands.describe(number="Objective number to remove (from /todo list)")
async def todo_remove(interaction: discord.Interaction, number: int):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos or number < 1 or number > len(todos):
        await interaction.response.send_message(
            embed=make_embed("▲ OBJECTIVE NOT FOUND", f"Objective #{number} doesn't exist. Check `/todo list`.", color=0xE63946)
        )
        return

    removed = todos.pop(number - 1)
    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    await interaction.response.send_message(
        embed=make_embed(
            "◈ OBJECTIVE REMOVED",
            f"**{interaction.user.display_name}** struck objective **#{number}** from the dossier:\n\n~~{removed['task']}~~\n\n"
            f"`{len(todos)}` objective(s) remaining.",
            color=0x6B6B9A
        )
    )

@todo_group.command(name="multiadd", description="Log multiple objectives at once (comma separated)")
@app_commands.describe(tasks="Objectives separated by commas e.g. Infiltrate base, Secure the relic, Vanish")
async def todo_multiadd(interaction: discord.Interaction, tasks: str):
    data = await load_data()
    uid  = str(interaction.user.id)

    if not get_shadow_id(uid, data):
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "Link your Shadow ID first — use `/link <shadow_id> <n>`.", color=0xE63946),
        )
        return

    task_list = [t.strip() for t in tasks.split(",") if t.strip()]
    if not task_list:
        await interaction.response.send_message(
            embed=make_embed("▲ NOTHING TO ADD", "No objectives found. Separate them with commas.", color=0xE63946),
        )
        return

    active      = get_active_date(uid, data)
    todos       = get_todos_for_date(uid, active, data)
    start_count = len(todos)
    for task in task_list:
        todos.append({"task": task, "done": False, "priority": None})
    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    is_today  = active == today_str()
    date_note = "" if is_today else f" *(for {active})*"
    lines     = [f"**#{start_count + i + 1}** · *{t}*" for i, t in enumerate(task_list)]

    await interaction.response.send_message(
        embed=make_embed(
            f"◉ {len(task_list)} OBJECTIVES ADDED",
            f"**{interaction.user.display_name}** added to the list{date_note}:\n\n" + "\n".join(lines),
            color=0x10B981
        )
    )

@todo_group.command(name="priority", description="Set priority on existing objectives (P1=critical, P2=important, P3=normal)")
@app_commands.describe(
    level="Priority level: p1, p2, p3, or none to clear",
    numbers="Task numbers to set, comma separated e.g. 1,3,5"
)
async def todo_priority(interaction: discord.Interaction, level: str, numbers: str):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos:
        await interaction.response.send_message(
            embed=make_embed("▲ DOSSIER EMPTY", "No objectives to prioritize. Add some with `/todo add`.", color=0xE63946)
        )
        return

    lvl = level.lower().strip()
    if lvl not in ("p1", "p2", "p3", "none"):
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID PRIORITY", "Use `p1`, `p2`, `p3`, or `none` to clear.", color=0xE63946)
        )
        return

    priority_val = None if lvl == "none" else lvl

    try:
        indices = [int(n.strip()) for n in numbers.split(",") if n.strip()]
    except ValueError:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID NUMBERS", "Provide task numbers separated by commas e.g. `1,3,5`.", color=0xE63946)
        )
        return

    invalid = [n for n in indices if n < 1 or n > len(todos)]
    if invalid:
        await interaction.response.send_message(
            embed=make_embed("▲ OUT OF RANGE", f"Task(s) {', '.join(str(n) for n in invalid)} don't exist. Check `/todo list`.", color=0xE63946)
        )
        return

    for n in indices:
        todos[n - 1]["priority"] = priority_val
    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    priority_labels = {"p1": "P1 — Critical", "p2": "P2 — Important", "p3": "P3 — Normal", "none": "None (cleared)"}
    color_map       = {"p1": 0xE63946, "p2": 0xF0A500, "p3": 0xF5C542, "none": 0x6B6B9A}
    task_names      = ", ".join(f"**#{n}**" for n in indices)

    await interaction.response.send_message(
        embed=make_embed(
            "◉ PRIORITY SET",
            f"{task_names} → **{priority_labels[lvl]}**",
            color=color_map[lvl]
        )
    )

tree.add_command(todo_group)

# ── /op ───────────────────────────────────────────────────────────
op_group = app_commands.Group(name="op", description="Manage ops (sub-tasks) under objectives")

@op_group.command(name="add", description="Add an op under an objective")
@app_commands.describe(objective="Objective number", op="The op to add")
async def op_add(interaction: discord.Interaction, objective: int, op: str):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos or objective < 1 or objective > len(todos):
        await interaction.response.send_message(
            embed=make_embed("▲ OBJECTIVE NOT FOUND", f"Objective #{objective} doesn't exist. Check `/todo list`.", color=0xE63946)
        )
        return

    t = todos[objective - 1]
    if "ops" not in t:
        t["ops"] = []
    t["ops"].append({"task": op, "done": False})
    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    op_num = len(t["ops"])
    await interaction.response.send_message(
        embed=make_embed(
            "◉ OP ADDED",
            f"Op **#{op_num}** added under Objective **#{objective}** · *{todos[objective-1]['task']}*\n\n`└ ○ {op}`",
            color=0x10B981
        )
    )

@op_group.command(name="done", description="Mark ops as complete — single or comma-separated e.g. 1,2")
@app_commands.describe(objective="Objective number", ops="Op number(s), comma-separated e.g. 1,2")
async def op_done(interaction: discord.Interaction, objective: int, ops: str):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos or objective < 1 or objective > len(todos):
        await interaction.response.send_message(
            embed=make_embed("▲ OBJECTIVE NOT FOUND", f"Objective #{objective} doesn't exist. Check `/todo list`.", color=0xE63946)
        )
        return

    t       = todos[objective - 1]
    op_list = t.get("ops", [])

    try:
        indices = sorted(set(int(n.strip()) for n in ops.split(",") if n.strip()))
    except ValueError:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID INPUT", "Provide op number(s) separated by commas e.g. `1,2`.", color=0xE63946)
        )
        return

    invalid = [n for n in indices if n < 1 or n > len(op_list)]
    if not op_list or invalid:
        await interaction.response.send_message(
            embed=make_embed("▲ OP NOT FOUND", f"Op(s) {', '.join(f'#{n}' for n in invalid)} don't exist under Objective #{objective}.", color=0xE63946)
        )
        return

    for n in indices:
        op_list[n - 1]["done"] = True

    if all(o["done"] for o in op_list):
        t["done"] = True

    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    done_ops  = sum(1 for o in op_list if o["done"])
    obj_label = t["task"]
    auto_note = "\n\n✅ All ops complete — **objective auto-fulfilled.**" if t["done"] else ""

    total_weight = len(todos)
    done_weight  = 0.0
    for task in todos:
        task_ops = task.get("ops", [])
        if task_ops:
            done_weight += sum(1 for o in task_ops if o["done"]) / len(task_ops)
        elif task["done"]:
            done_weight += 1
    proj = round(data.get("base_echo_rate", 10) * done_weight / total_weight) if total_weight else 0

    title = "☽ OP COMPLETE" if len(indices) == 1 else f"☽ {len(indices)} OPS COMPLETE"
    await interaction.response.send_message(
        embed=make_embed(
            title,
            f"Op(s) **{', '.join(f'#{n}' for n in indices)}** under *{obj_label}* fulfilled.\n`{done_ops}/{len(op_list)} ops done`{auto_note}\n\nProjected echoes: **{proj}**",
            color=0x10B981
        )
    )

@op_group.command(name="multiadd", description="Add multiple ops under an objective (comma-separated)")
@app_commands.describe(objective="Objective number", ops="Ops separated by commas e.g. Disable cameras, Find the vault")
async def op_multiadd(interaction: discord.Interaction, objective: int, ops: str):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos or objective < 1 or objective > len(todos):
        await interaction.response.send_message(
            embed=make_embed("▲ OBJECTIVE NOT FOUND", f"Objective #{objective} doesn't exist. Check `/todo list`.", color=0xE63946)
        )
        return

    op_list = [o.strip() for o in ops.split(",") if o.strip()]
    if not op_list:
        await interaction.response.send_message(
            embed=make_embed("▲ NOTHING TO ADD", "No ops found. Separate them with commas.", color=0xE63946)
        )
        return

    t = todos[objective - 1]
    if "ops" not in t:
        t["ops"] = []
    for o in op_list:
        t["ops"].append({"task": o, "done": False})
    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    lines = "\n".join(f"    └ ○ *{o}*" for o in op_list)
    await interaction.response.send_message(
        embed=make_embed(
            f"◉ {len(op_list)} OPS ADDED",
            f"Under Objective **#{objective}** · *{t['task']}*\n\n{lines}",
            color=0x10B981
        )
    )

@op_group.command(name="remove", description="Remove an op from an objective")
@app_commands.describe(objective="Objective number", op="Op number to remove")
async def op_remove(interaction: discord.Interaction, objective: int, op: int):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos or objective < 1 or objective > len(todos):
        await interaction.response.send_message(
            embed=make_embed("▲ OBJECTIVE NOT FOUND", f"Objective #{objective} doesn't exist. Check `/todo list`.", color=0xE63946)
        )
        return

    t   = todos[objective - 1]
    ops = t.get("ops", [])

    if not ops or op < 1 or op > len(ops):
        await interaction.response.send_message(
            embed=make_embed("▲ OP NOT FOUND", f"Op #{op} doesn't exist under Objective #{objective}.", color=0xE63946)
        )
        return

    removed = ops.pop(op - 1)
    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    await interaction.response.send_message(
        embed=make_embed(
            "◈ OP REMOVED",
            f"~~{removed['task']}~~ removed from Objective **#{objective}** · *{t['task']}*\n`{len(ops)}` op(s) remaining.",
            color=0x6B6B9A
        )
    )

@op_group.command(name="move", description="Convert objectives into ops under a target objective")
@app_commands.describe(
    sources="Objective numbers to convert, comma-separated e.g. 2,3",
    target="Target objective number they become ops under"
)
async def op_move(interaction: discord.Interaction, sources: str, target: int):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos or target < 1 or target > len(todos):
        await interaction.response.send_message(
            embed=make_embed("▲ OBJECTIVE NOT FOUND", f"Target objective #{target} doesn't exist.", color=0xE63946)
        )
        return

    try:
        src_nums = sorted(set(int(n.strip()) for n in sources.split(",") if n.strip()), reverse=True)
    except ValueError:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID INPUT", "Provide comma-separated numbers e.g. `2,3`.", color=0xE63946)
        )
        return

    invalid = [n for n in src_nums if n < 1 or n > len(todos) or n == target]
    if invalid:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID NUMBERS", f"Numbers {', '.join(str(n) for n in invalid)} are invalid or same as target.", color=0xE63946)
        )
        return

    target_task = todos[target - 1]
    if "ops" not in target_task:
        target_task["ops"] = []

    moved = []
    for n in src_nums:
        removed = todos.pop(n - 1)
        target_task["ops"].append({"task": removed["task"], "done": removed["done"]})
        moved.append(removed["task"])
        if n < target:
            target -= 1

    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    moved_lines = "\n".join(f"    └ ○ {m}" for m in reversed(moved))
    await interaction.response.send_message(
        embed=make_embed(
            "◈ OBJECTIVES CONVERTED TO OPS",
            f"Now under **{target_task['task']}**:\n{moved_lines}",
            color=0xA855F7
        )
    )

tree.add_command(op_group)

# ── /echoes ───────────────────────────────────────────────────────
@tree.command(name="echoes", description="Reveal your echo resonance and operative rank")
async def echoes(interaction: discord.Interaction):
    data      = await load_data()
    uid       = str(interaction.user.id)
    shadow_id = get_shadow_id(uid, data)

    if not shadow_id:
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "No Shadow ID linked. Use `/link <shadow_id> <n>` to get started.", color=0xE63946),
        )
        return

    member = get_member(shadow_id, data)
    if not member:
        await pull_from_gas(data)
        member = get_member(shadow_id, await load_data())

    if not member:
        await interaction.response.send_message(
            embed=make_embed("▲ OPERATIVE NOT FOUND", f"Shadow ID `{shadow_id}` has no record in the void.", color=0xE63946),
        )
        return

    count  = int(member.get("echoCount", 0))
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)
    done   = sum(1 for t in todos if t["done"])
    total  = len(todos)
    proj   = round(data.get("base_echo_rate", 10) * done / total) if total else 0

    is_today      = active == today_str()
    session_label = "Today's Objectives" if is_today else f"Objectives ({active})"

    # Session stats
    today     = today_str()
    daily_key = f"{uid}_{today}"
    daily_sess = data.get("daily_session_echoes", {}).get(daily_key, 0)
    sg_count   = member.get("badges", {}).get("shadow_grind", 0)

    embed = discord.Embed(title=f"☭ {member['codename']}", color=0x7B2FBE)
    embed.add_field(name="Shadow ID",      value=f"`{shadow_id}`",        inline=True)
    embed.add_field(name="Echo Resonance", value=f"**{count:,} echoes**", inline=True)
    embed.add_field(name=session_label,    value=f"{done}/{total} fulfilled · +{proj} echoes on track", inline=False)
    embed.add_field(name="Study Echoes Today", value=f"{daily_sess}/{DAILY_SESSION_CAP}", inline=True)
    if sg_count:
        embed.add_field(name="Shadow Grind", value=f"🏅 × {sg_count}", inline=True)
    embed.set_footer(text="☭ SHADOWSEEKERS ORDER · DEEP IN THE DARK, I DON'T NEED THE LIGHT")
    await interaction.response.send_message(embed=embed)

# ── /leaderboard ──────────────────────────────────────────────────
@tree.command(name="leaderboard", description="The most powerful operatives in the Order")
async def leaderboard(interaction: discord.Interaction):
    data = await load_data()
    if not data["members"]:
        await pull_from_gas(data)
        data = await load_data()

    sorted_m = sorted(data["members"], key=lambda m: int(m.get("echoCount", 0)), reverse=True)[:10]

    if not sorted_m:
        await interaction.response.send_message(
            embed=make_embed("▲ NO DATA", "No operatives found. Try `/sync` first.", color=0xE63946),
        )
        return

    lines  = []
    medals = ["🥇","🥈","🥉"]
    for i, m in enumerate(sorted_m):
        count    = int(m.get("echoCount", 0))
        rank     = medals[i] if i < 3 else f"`#{i+1}`"
        sg_count = m.get("badges", {}).get("shadow_grind", 0)
        badge    = f" · 🏅×{sg_count}" if sg_count else ""
        lines.append(f"{rank} **{m['codename']}** · `{m['shadowId']}` · **{count:,} echoes**{badge}")

    embed = make_embed("☽ LEADERBOARD — TOP OPERATIVES", "\n".join(lines), color=0xF0A500)
    embed.set_footer(text=f"☽ SHADOWSEEKERS ORDER · {len(data['members'])} total operatives")
    await interaction.response.send_message(embed=embed)

# ── /link ─────────────────────────────────────────────────────────
@tree.command(name="link", description="Bind your Discord identity to your Shadow ID")
@app_commands.describe(
    shadow_id="Your Shadow ID (e.g. SS0069)",
    name="Your operative name"
)
async def link(interaction: discord.Interaction, shadow_id: str, name: str):
    data     = await load_data()
    uid      = str(interaction.user.id)
    sid      = shadow_id.upper().strip()
    codename = name.strip()

    if get_shadow_id(uid, data):
        await interaction.response.send_message(
            embed=make_embed("▲ ALREADY LINKED", f"Already linked to `{data['links'][uid]['shadow_id']}`.", color=0xE63946),
        )
        return

    import re
    if not re.match(r'^SS\d{4}$', sid):
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID SHADOW ID", "The format must be `SS####` — e.g. `SS0069`. Check your credentials.", color=0xE63946),
        )
        return

    if not codename:
        await interaction.response.send_message(
            embed=make_embed("▲ NAME REQUIRED", "You must provide your operative name to link.", color=0xE63946),
        )
        return

    for existing_link in data["links"].values():
        if existing_link["shadow_id"] == sid and existing_link.get("approved"):
            await interaction.response.send_message(
                embed=make_embed("▲ ID ALREADY TAKEN", f"`{sid}` is already linked to someone else. Contact an admin if this is wrong.", color=0xE63946),
            )
            return

    data["pending_links"][uid] = {"shadow_id": sid, "codename": codename}
    await save_data(data)

    ch = discord.utils.get(interaction.guild.text_channels, name=APPROVE_CH)
    if ch:
        admin_role   = discord.utils.get(interaction.guild.roles, name=ADMIN_ROLE)
        role_mention = admin_role.mention if admin_role else f"@{ADMIN_ROLE}"
        embed = make_embed(
            "◈ LINK REQUEST",
            f"{interaction.user.mention} wants to link `{sid}` as **{codename}**\n\n"
            f"Use `/approve @{interaction.user.display_name}` to authorize.",
            color=0xF0A500
        )
        await ch.send(content=f"{role_mention} — new link request awaiting authorization.", embed=embed)

    await interaction.response.send_message(
        embed=make_embed(
            "◈ REQUEST SENT",
            f"Your request to link `{sid}` as **{codename}** has been sent into the void.\nAwait authorization from the Order.",
            color=0xA855F7
        ),
    )

# ── /approve ──────────────────────────────────────────────────────
@tree.command(name="approve", description="[HIGH CLEARANCE] Authorize an operative's identity bind")
@app_commands.describe(user="The operative to authorize")
async def approve(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ CLEARANCE DENIED", "This command is restricted to those with high clearance.", color=0xE63946))
        return

    data    = await load_data()
    uid     = str(user.id)
    pending = data["pending_links"].get(uid)

    if not pending:
        await interaction.response.send_message(
            embed=make_embed("▲ NO REQUEST FOUND", f"**{user.display_name}** has no pending link request.", color=0xE63946),
        )
        return

    if isinstance(pending, dict):
        sid      = pending["shadow_id"]
        codename = pending.get("codename", user.display_name)
    else:
        sid      = pending
        codename = user.display_name

    data["links"][uid] = {"shadow_id": sid, "approved": True, "codename": codename}
    del data["pending_links"][uid]

    new_member = {
        "shadowId":  sid,
        "codename":  codename,
        "discordId": uid,
        "echoCount": 0,
        "badges":    {},
    }
    gas_ok = await create_member_on_gas(new_member)

    if not any(m["shadowId"] == sid for m in data["members"]):
        data["members"].append(new_member)

    await save_data(data)

    status_note = "" if gas_ok else "\n⚠️ Shadow Records sync failed — member added locally, retry `/sync`."

    try:
        await user.send(embed=make_embed(
            "☽ LINK APPROVED",
            f"You're now linked to `{sid}` as **{codename}**.\nStep into the shadows — use `/todo` and `/echoes`.",
            color=0x10B981
        ))
    except:
        pass

    await interaction.response.send_message(
        embed=make_embed(
            "◉ APPROVED",
            f"**{user.display_name}** is now linked to `{sid}` as **{codename}**."
            f"\nShadow Records account created.{status_note}",
            color=0x10B981
        ),
    )

# ── /give ─────────────────────────────────────────────────────────
@tree.command(name="give", description="[HIGH CLEARANCE] Channel echoes to an operative")
@app_commands.describe(user="The operative to channel echoes to", amount="Echo amount (can be negative)")
async def give(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ CLEARANCE DENIED", "This command is restricted to those with high clearance.", color=0xE63946))
        return

    data      = await load_data()
    uid       = str(user.id)
    shadow_id = get_shadow_id(uid, data)

    if not shadow_id:
        await interaction.response.send_message(
            embed=make_embed("▲ OPERATIVE UNBOUND", f"**{user.display_name}** has no bound Shadow ID.", color=0xE63946),
        )
        return

    for i, m in enumerate(data["members"]):
        if m["shadowId"] == shadow_id:
            old = int(m.get("echoCount", 0))
            new = max(0, old + amount)
            data["members"][i]["echoCount"] = new
            await save_data(data)
            sign = "+" if amount >= 0 else ""
            await interaction.response.send_message(
                embed=make_embed(
                    "◉ ECHOES CHANNELED",
                    f"**{m['codename']}** (`{shadow_id}`)\n`{old:,}` → **{new:,}** ({sign}{amount:,})\nEcho count updated.",
                    color=0x10B981
                )
            )
            asyncio.create_task(push_to_gas(data))
            return

    await interaction.response.send_message(embed=make_embed("▲ OPERATIVE NOT FOUND", "No record found. Check the Shadow ID.", color=0xE63946))

# ── /setbase ──────────────────────────────────────────────────────
@tree.command(name="setbase", description="[HIGH CLEARANCE] Recalibrate the daily echo resonance threshold")
@app_commands.describe(amount="Base echoes per cycle for full dossier completion")
async def setbase(interaction: discord.Interaction, amount: int):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ CLEARANCE DENIED", "This command is restricted to those with high clearance.", color=0xE63946))
        return
    data = await load_data()
    data["base_echo_rate"] = max(1, amount)
    await save_data(data)
    await interaction.response.send_message(
        embed=make_embed("◉ RESONANCE RECALIBRATED", f"The daily echo threshold has been set to **{amount:,}**.", color=0x10B981)
    )

# ── /forceday ─────────────────────────────────────────────────────
@tree.command(name="forceday", description="[HIGH CLEARANCE] Force the midnight echo reckoning")
async def forceday(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ CLEARANCE DENIED", "This command is restricted to those with high clearance.", color=0xE63946))
        return
    await interaction.response.send_message(
        embed=make_embed("◉ DAILY RESET STARTED", "Calculating echoes for all members...", color=0xA855F7)
    )
    results     = await run_end_of_day(interaction.guild)
    total_given = sum(r["earned"] for r in results)
    await interaction.followup.send(
        embed=make_embed(
            "☽ DAILY RESET DONE",
            f"**{len(results)}** operatives assessed · **{total_given:,}** echoes awarded · Archive synced.",
            color=0x10B981
        )
    )

# ── /sync ─────────────────────────────────────────────────────────
@tree.command(name="sync", description="[HIGH CLEARANCE] Pull the latest operative data from the shadow archive")
async def sync_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ CLEARANCE DENIED", "This command is restricted to those with high clearance.", color=0xE63946))
        return
    await interaction.response.send_message(embed=make_embed("◉ SYNCING", "Fetching latest data from the archive...", color=0xA855F7))
    data = await load_data()
    ok   = await pull_from_gas(data)
    data = await load_data()
    if ok:
        await interaction.followup.send(
            embed=make_embed("◉ SYNC COMPLETE", f"**{len(data['members'])}** operatives loaded.", color=0x10B981),
        )
    else:
        await interaction.followup.send(
            embed=make_embed("▲ SYNC FAILED", "Could not reach the archive. Check the GAS URL.", color=0xE63946),
        )

# ── /syncids ──────────────────────────────────────────────────────
async def bulk_syncids_on_gas(members: list) -> dict:
    try:
        payload = json.dumps({"action": "syncids", "members": members})
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GAS_URL,
                data=payload,
                headers={"Content-Type": "text/plain"}
            ) as resp:
                text = await resp.text()
                result = json.loads(text)
                return result if isinstance(result, dict) else {}
    except Exception as e:
        print(f"[GAS SYNCIDS ERROR] {e}")
    return {}

@tree.command(name="syncids", description="[HIGH CLEARANCE] Create website records for all approved Discord-linked IDs")
async def syncids(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            embed=make_embed("▲ CLEARANCE DENIED", "This command is restricted to those with high clearance.", color=0xE63946)
        )
        return

    await interaction.response.send_message(
        embed=make_embed("◉ SYNCING IDs", "Scanning approved links and pushing missing records to Shadow Records...", color=0xA855F7)
    )

    data = await load_data()
    mongo_echo_cache = {m["shadowId"]: int(m.get("echoCount", 0)) for m in data["members"]}
    await pull_from_gas(data)
    data = await load_data()

    for m in data["members"]:
        if m["shadowId"] in mongo_echo_cache:
            m["echoCount"] = mongo_echo_cache[m["shadowId"]]

    to_sync     = []
    id_to_label = {}

    for discord_id, link in data["links"].items():
        if not link.get("approved"):
            continue
        sid      = link["shadow_id"]
        codename = link.get("codename", f"Operative {sid}")
        to_sync.append({
            "shadowId":  sid,
            "codename":  codename,
            "discordId": discord_id,
            "echoCount": mongo_echo_cache.get(sid, 0),
        })
        id_to_label[sid] = f"`{sid}` **{codename}**"

    if not to_sync:
        await interaction.followup.send(
            embed=make_embed("◈ NOTHING TO SYNC", "No approved links found.", color=0x6B6B9A)
        )
        return

    gas_result = await bulk_syncids_on_gas(to_sync)

    gas_created = gas_result.get("created", [])
    for m in to_sync:
        if m["shadowId"] in gas_created:
            if not any(x["shadowId"] == m["shadowId"] for x in data["members"]):
                data["members"].append(m)
    await save_data(data)
    await push_to_gas(data)

    created = [id_to_label.get(sid, f"`{sid}`") for sid in gas_result.get("created", [])]
    skipped = gas_result.get("skipped", [])
    failed  = [id_to_label.get(sid, f"`{sid}`") for sid in gas_result.get("failed",  [])]

    lines = []
    if created:
        lines.append(f"**✅ Created ({len(created)}):**\n" + "\n".join(created))
    if skipped:
        lines.append(f"**⏭ Already existed ({len(skipped)}):** " + ", ".join(f"`{s}`" for s in skipped))
    if failed:
        lines.append(f"**⚠️ Failed ({len(failed)}):**\n" + "\n".join(failed))
    if not lines:
        lines.append("No approved links found to process.")

    total_echoes = sum(m["echoCount"] for m in to_sync)

    await interaction.followup.send(
        embed=make_embed(
            "☽ ID SYNC COMPLETE",
            "\n\n".join(lines) + f"\n\n*Echo counts pushed for all {len(data['members'])} operatives · {total_echoes:,} total echoes on record.*",
            color=0x10B981 if not failed else 0xF0A500
        )
    )

# ── BOT EVENTS ────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[SHADOW BOT] Logged in as {bot.user} ({bot.user.id})")
    if MONGO_URI:
        print("[SHADOW BOT] MongoDB connected — data is persistent ✓")
    else:
        print("[SHADOW BOT] WARNING: MONGO_URI not set — using local file")

    try:
        synced = await tree.sync()
        print(f"[SHADOW BOT] Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"[SHADOW BOT] Sync error: {e}")

    data = await load_data()
    await pull_from_gas(data)
    loaded = await load_data()
    print(f"[SHADOW BOT] Loaded {len(loaded['members'])} members from GAS")

    # Seed VC join times for anyone already in voice on startup
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if not member.bot:
                    uid = str(member.id)
                    if uid not in loaded["active_sessions"]:
                        pass  # They're in VC but no session — will be prompted on next join

    daily_echo_task.start()
    session_ticker.start()
    print(f"[SHADOW BOT] Daily task scheduled at {EOD_HOUR}:{EOD_MINUTE:02d} {TIMEZONE}")
    print(f"[SHADOW BOT] Session ticker started")

    setup_ai_missions(bot, tree)
    ai_mission_task.start()
    print("[SHADOW BOT] AI Mission Generator started")

bot.run(TOKEN)
