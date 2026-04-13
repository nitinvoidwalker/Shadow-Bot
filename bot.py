"""
╔══════════════════════════════════════════════════════╗
║         SHADOW BOT · ShadowSeekers Order             ║
║   Objective tracking · Echo management · GAS sync    ║
╚══════════════════════════════════════════════════════╝

COMMANDS (all slash commands):
  /todo add <objective>        — log a new objective
  /todo multiadd <objectives>  — log multiple objectives at once
  /todo done <number>          — mark an objective as fulfilled
  /todo remove <number>        — remove an objective from your dossier
  /todo list                   — view your active dossier
  /todo clear                  — purge your dossier
  /todo date <MM/DD>           — switch active date (default: today)
  /echoes                      — reveal your echo count + rank
  /leaderboard                 — top 10 operatives by echo power
  /link <shadow_id> <n>     — bind your identity to a Shadow ID

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

# ── CONFIG ────────────────────────────────────────────────────────
TOKEN        = os.getenv("DISCORD_TOKEN")
GAS_URL      = os.getenv("GAS_URL", "https://script.google.com/macros/s/AKfycbyTadW-WF4vnpaciFv8Qv58ahWSQ7KVmQfxJA75_z5fZN3UEBunnDPAeq_i5jiu35sYjQ/exec")
ADMIN_ROLE   = os.getenv("ADMIN_ROLE", "Admin")
APPROVE_CH   = os.getenv("APPROVE_CHANNEL", "admin-log")
TIMEZONE     = os.getenv("TIMEZONE", "Asia/Kolkata")
EOD_HOUR     = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE   = int(os.getenv("EOD_MINUTE", "55"))
MONGO_URI    = os.getenv("MONGO_URI")

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
# "data" dict schema:
# {
#   "base_echo_rate": 500,
#   "links":         { uid: {"shadow_id": "SS0001", "approved": true, "codename": "..."} },
#   "pending_links": { uid: {"shadow_id": "SS0001", "codename": "..."} },
#   "todos":         { uid: {"active_date": "04/14", "dates": {"04/14": [{task, done}]}} },
#   "members":       [ { ...shadowrecord member objects... } ]
# }

DATA_FILE = "data.json"

async def load_data():
    db = get_db()
    if db is not None:
        doc         = await db["config"].find_one({"_id": "main"}) or {}
        members_doc = await db["members"].find_one({"_id": "list"}) or {}
        return {
            "base_echo_rate": doc.get("base_echo_rate", 500),
            "links":          doc.get("links", {}),
            "pending_links":  doc.get("pending_links", {}),
            "todos":          doc.get("todos", {}),
            "members":        members_doc.get("members", []),
        }
    else:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        return {
            "base_echo_rate": 500,
            "links": {},
            "pending_links": {},
            "todos": {},
            "members": []
        }

async def save_data(data):
    db = get_db()
    if db is not None:
        await db["config"].update_one(
            {"_id": "main"},
            {"$set": {
                "base_echo_rate": data.get("base_echo_rate", 500),
                "links":          data.get("links", {}),
                "pending_links":  data.get("pending_links", {}),
                "todos":          data.get("todos", {}),
            }},
            upsert=True
        )
        await db["members"].update_one(
            {"_id": "list"},
            {"$set": {"members": data.get("members", [])}},
            upsert=True
        )
    else:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)

# ── BOT SETUP ─────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

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
    """Returns today's date as MM/DD in the configured timezone."""
    tz  = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    return now.strftime("%m/%d")

def get_active_date(uid: str, data: dict) -> str:
    """Returns the user's currently active date, defaulting to today."""
    entry = data["todos"].get(uid)
    if isinstance(entry, dict):
        return entry.get("active_date", today_str())
    return today_str()

def get_todos_for_date(uid: str, date_key: str, data: dict) -> list:
    """Returns the todo list for a specific date key (MM/DD)."""
    entry = data["todos"].get(uid)
    # Migrate old flat list format -> new dict format
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
    """Saves a todo list for a specific date key."""
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

# ── GAS SYNC ──────────────────────────────────────────────────────
async def pull_from_gas(data: dict):
    """Pull latest members from GAS sheet into local data."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GAS_URL + "?action=read", allow_redirects=True) as resp:
                text    = await resp.text()
                members = json.loads(text)
                if isinstance(members, list) and members:
                    data["members"] = members
                    await save_data(data)
                    return True
    except Exception as e:
        print(f"[GAS PULL ERROR] {e}")
    return False

async def push_to_gas(data: dict):
    """Push updated members to GAS sheet."""
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

async def create_member_on_gas(member: dict) -> bool:
    """Create a new member record on the Shadow Records website via GAS."""
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

# ── END OF DAY CALCULATION ────────────────────────────────────────
async def run_end_of_day(guild: discord.Guild, announce=True):
    """Calculate echoes for all linked members based on today's todo completion."""
    data    = await load_data()
    base    = data.get("base_echo_rate", 500)
    today   = today_str()
    results = []

    for discord_id, link in data["links"].items():
        if not link.get("approved"):
            continue
        shadow_id = link["shadow_id"]
        todos     = get_todos_for_date(discord_id, today, data)

        if not todos:
            earned = 0
            pct    = 0
        else:
            total  = len(todos)
            done   = sum(1 for t in todos if t["done"])
            pct    = done / total
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

        # Clear only today's todos after reckoning
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
            embed.set_footer(text=f"☽ SHADOWSEEKERS ORDER · DEEP IN THE DARK, I DON'T NEED THE LIGHT · Base resonance: {base} · {datetime.now().strftime('%d %b %Y')}")
            await ch.send(embed=embed)

    return results

# ── SCHEDULED TASK ────────────────────────────────────────────────
@tasks.loop(time=time(hour=EOD_HOUR, minute=EOD_MINUTE, tzinfo=pytz.timezone(TIMEZONE)))
async def daily_echo_task():
    for guild in bot.guilds:
        await run_end_of_day(guild)

# ══════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
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

    # No date provided — just show current active date
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

    # Validate MM/DD format
    import re
    if not re.match(r'^\d{2}/\d{2}$', date.strip()):
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID FORMAT", "Use `MM/DD` format — e.g. `04/15`.", color=0xE63946)
        )
        return

    # Validate it's an actual calendar date
    try:
        datetime.strptime(date.strip() + f"/{datetime.now().year}", "%m/%d/%Y")
    except ValueError:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID DATE", f"`{date}` isn't a real date. Try again.", color=0xE63946)
        )
        return

    date_key = date.strip()

    # Init the user's todo entry if needed
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
    todos.append({"task": task, "done": False})
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

@todo_group.command(name="done", description="Mark an objective as fulfilled")
@app_commands.describe(number="Objective number (from /todo list)")
async def todo_done(interaction: discord.Interaction, number: int):
    data   = await load_data()
    uid    = str(interaction.user.id)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)

    if not todos or number < 1 or number > len(todos):
        await interaction.response.send_message(
            embed=make_embed("▲ OBJECTIVE NOT FOUND", f"Objective #{number} doesn't exist. Check `/todo list`.", color=0xE63946),
        )
        return

    todos[number - 1]["done"] = True
    set_todos_for_date(uid, active, todos, data)
    await save_data(data)

    done  = sum(1 for t in todos if t["done"])
    total = len(todos)
    pct   = round((done / total) * 100)
    base  = data.get("base_echo_rate", 500)
    proj  = round(base * done / total)

    is_today  = active == today_str()
    date_note = "" if is_today else f" *(session: {active})*"

    await interaction.response.send_message(
        embed=make_embed(
            "☽ OBJECTIVE FULFILLED",
            f"**{interaction.user.display_name}** completed: *{todos[number-1]['task']}*{date_note}\n\n"
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
    for i, t in enumerate(todos, 1):
        check  = "☽" if t["done"] else "○"
        strike = f"~~{t['task']}~~" if t["done"] else t["task"]
        lines.append(f"`{check}` **{i}.** {strike}")

    done  = sum(1 for t in todos if t["done"])
    total = len(todos)
    base  = data.get("base_echo_rate", 500)
    proj  = round(base * done / total) if total else 0

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
        todos.append({"task": task, "done": False})
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

tree.add_command(todo_group)

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
    tier   = get_tier(count)
    active = get_active_date(uid, data)
    todos  = get_todos_for_date(uid, active, data)
    done   = sum(1 for t in todos if t["done"])
    total  = len(todos)
    proj   = round(data.get("base_echo_rate", 500) * done / total) if total else 0

    is_today      = active == today_str()
    session_label = "Today's Objectives" if is_today else f"Objectives ({active})"

    embed = discord.Embed(title=f"☽ {member['codename']}", color=tier["color"])
    embed.add_field(name="Shadow ID",      value=f"`{shadow_id}`",             inline=True)
    embed.add_field(name="Echo Resonance", value=f"**{count:,}**",             inline=True)
    embed.add_field(name="Rank",           value=f"**{tier['name'].upper()}**", inline=True)
    embed.add_field(name=session_label,    value=f"{done}/{total} fulfilled · +{proj} resonating", inline=False)
    embed.set_footer(text="☽ SHADOWSEEKERS ORDER · DEEP IN THE DARK, I DON'T NEED THE LIGHT")
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
        count = int(m.get("echoCount", 0))
        tier  = get_tier(count)
        rank  = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(f"{rank} **{m['codename']}** · `{m['shadowId']}` · **{count:,}** _{tier['name']}_")

    embed = make_embed("☽ LEADERBOARD — TOP OPERATIVES", "\n".join(lines), color=0xF0A500)
    embed.set_footer(text=f"☽ SHADOWSEEKERS ORDER · DEEP IN THE DARK, I DON'T NEED THE LIGHT · {len(data['members'])} total operatives")
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

    # Support both old format (plain string) and new format (dict)
    if isinstance(pending, dict):
        sid      = pending["shadow_id"]
        codename = pending.get("codename", user.display_name)
    else:
        sid      = pending
        codename = user.display_name

    data["links"][uid] = {"shadow_id": sid, "approved": True, "codename": codename}
    del data["pending_links"][uid]

    # Create member record on GAS (Shadow Records website)
    new_member = {
        "shadowId":  sid,
        "codename":  codename,
        "discordId": uid,
        "echoCount": 0,
    }
    gas_ok = await create_member_on_gas(new_member)

    # Also add to local members cache if not already there
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
            await push_to_gas(data)
            sign = "+" if amount >= 0 else ""
            await interaction.response.send_message(
                embed=make_embed(
                    "◉ ECHOES CHANNELED",
                    f"**{m['codename']}** (`{shadow_id}`)\n`{old:,}` → **{new:,}** ({sign}{amount:,})\nEcho count updated.",
                    color=0x10B981
                )
            )
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
    """Send all missing members to GAS in a single syncids POST."""
    try:
        payload = json.dumps({"action": "syncids", "members": members})
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GAS_URL,
                data=payload,
                headers={"Content-Type": "text/plain"}
            ) as resp:
                text = await resp.text()
                print(f"[GAS SYNCIDS] Status: {resp.status} | Response: {text[:300]}")
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

    # MongoDB is the source of truth for echoCount.
    # Save this BEFORE pull_from_gas overwrites data["members"] with GAS values
    # (which may have stale/zero echoes).
    mongo_echo_cache = {m["shadowId"]: int(m.get("echoCount", 0)) for m in data["members"]}

    # Pull GAS so local cache has the full member list (for save_data later)
    await pull_from_gas(data)
    data = await load_data()

    # Restore MongoDB echo values — don't let GAS zeros overwrite real data
    for m in data["members"]:
        if m["shadowId"] in mongo_echo_cache:
            m["echoCount"] = mongo_echo_cache[m["shadowId"]]

    # Build list of all approved members to send to GAS
    # GAS handles duplicate detection itself (skips existing shadowIds)
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
            "echoCount": mongo_echo_cache.get(sid, 0),  # real echoes from MongoDB
        })
        id_to_label[sid] = f"`{sid}` **{codename}**"

    if not to_sync:
        await interaction.followup.send(
            embed=make_embed("◈ NOTHING TO SYNC", "No approved links found.", color=0x6B6B9A)
        )
        return

    gas_result = await bulk_syncids_on_gas(to_sync)

    # Add newly created members to local cache
    gas_created = gas_result.get("created", [])
    for m in to_sync:
        if m["shadowId"] in gas_created:
            if not any(x["shadowId"] == m["shadowId"] for x in data["members"]):
                data["members"].append(m)
    await save_data(data)

    # Always push latest echo counts for ALL members to GAS sheet
    # This ensures existing members whose echoes were updated by /todo also get synced
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
        print("[SHADOW BOT] WARNING: MONGO_URI not set — using local file (data will reset on redeploy!)")

    try:
        synced = await tree.sync()
        print(f"[SHADOW BOT] Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"[SHADOW BOT] Sync error: {e}")

    data = await load_data()
    await pull_from_gas(data)
    loaded = await load_data()
    print(f"[SHADOW BOT] Loaded {len(loaded['members'])} members from GAS")

    daily_echo_task.start()
    print(f"[SHADOW BOT] Daily task scheduled at {EOD_HOUR}:{EOD_MINUTE:02d} {TIMEZONE}")

bot.run(TOKEN)
