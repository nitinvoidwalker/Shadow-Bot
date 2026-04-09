"""
╔══════════════════════════════════════════════════════╗
║         SHADOW BOT · ShadowSeekers Order             ║
║  Todo tracking + Echo management + GAS sync          ║
╚══════════════════════════════════════════════════════╝

COMMANDS (all slash commands):
  /todo add <task>          — add a task to today's list
  /todo multiadd <tasks>    — bulk add tasks (comma separated)
  /todo done <number>       — mark task as complete
  /todo list                — view your current list
  /todo clear               — wipe your list (start fresh)
  /echoes              — see your echo count + tier
  /leaderboard         — top 10 operatives by echoes
  /link <shadow_id>    — link your Discord to a Shadow ID

ADMIN ONLY:
  /approve @user       — approve a link request
  /give @user <amount> — manually award echoes
  /setbase <number>    — set daily base echo rate (default 500)
  /forceday            — manually trigger end-of-day calculation
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import asyncio
import aiohttp
from datetime import datetime, time
import pytz

# ── CONFIG ────────────────────────────────────────────────────────
TOKEN        = os.getenv("DISCORD_TOKEN")
GAS_URL      = os.getenv("GAS_URL", "https://script.google.com/macros/s/AKfycbyTadW-WF4vnpaciFv8Qv58ahWSQ7KVmQfxJA75_z5fZN3UEBunnDPAeq_i5jiu35sYjQ/exec")
ADMIN_ROLE   = os.getenv("ADMIN_ROLE", "Admin")          # Discord role name for admins
APPROVE_CH   = os.getenv("APPROVE_CHANNEL", "admin-log") # Channel for link approvals
TIMEZONE     = os.getenv("TIMEZONE", "Asia/Kolkata")      # IST by default
EOD_HOUR     = int(os.getenv("EOD_HOUR", "23"))           # End-of-day hour (23 = 11 PM)
EOD_MINUTE   = int(os.getenv("EOD_MINUTE", "55"))
DATA_FILE    = "data.json"

# ── DATA STRUCTURE ────────────────────────────────────────────────
# data.json schema:
# {
#   "base_echo_rate": 500,
#   "links": { "discord_user_id": {"shadow_id": "SS0001", "approved": true} },
#   "pending_links": { "discord_user_id": "SS0001" },
#   "todos": { "discord_user_id": [ {"task": "...", "done": false}, ... ] },
#   "members": [ { ...shadowrecord member objects... } ]
# }

def load_data():
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

def save_data(data):
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
    e.set_footer(text="SHADOWSEEKERS ORDER · SHADOW BOT")
    return e

# ── GAS SYNC ──────────────────────────────────────────────────────
async def pull_from_gas(data: dict):
    """Pull latest members from GAS sheet into local data."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GAS_URL + "?action=read", allow_redirects=True) as resp:
                text = await resp.text()
                members = json.loads(text)
                if isinstance(members, list) and members:
                    data["members"] = members
                    save_data(data)
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

# ── END OF DAY CALCULATION ────────────────────────────────────────
async def run_end_of_day(guild: discord.Guild, announce=True):
    """Calculate echoes for all linked members based on todo completion."""
    data = load_data()
    base = data.get("base_echo_rate", 500)
    results = []

    for discord_id, link in data["links"].items():
        if not link.get("approved"):
            continue
        shadow_id = link["shadow_id"]
        todos = data["todos"].get(discord_id, [])

        if not todos:
            # No tasks = 0 echoes today
            earned = 0
            pct = 0
        else:
            total = len(todos)
            done  = sum(1 for t in todos if t["done"])
            pct   = done / total
            earned = round(base * pct)

        # Update echo count in members list
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

        # Clear todos for next day
        data["todos"][discord_id] = []

    save_data(data)
    await push_to_gas(data)

    # Announce in a channel if there's a results channel
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
                    f"`[{bar}]` {r['done']}/{r['total']} tasks · **+{r['earned']} echoes**"
                )
            embed = make_embed(
                "◈ DAILY ECHO REPORT",
                "\n\n".join(lines) or "No activity recorded today.",
                color=0xF0A500
            )
            embed.set_footer(text=f"Base rate: {base} · {datetime.now().strftime('%d %b %Y')}")
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
todo_group = app_commands.Group(name="todo", description="Manage your daily task list")

@todo_group.command(name="add", description="Add a task to today's list")
@app_commands.describe(task="What do you need to do?")
async def todo_add(interaction: discord.Interaction, task: str):
    data = load_data()
    uid  = str(interaction.user.id)

    if not get_shadow_id(uid, data):
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "Use `/link <shadow_id>` first to connect your Discord to your Shadow ID.", color=0xE63946),
            ephemeral=True
        )
        return

    data["todos"].setdefault(uid, [])
    data["todos"][uid].append({"task": task, "done": False})
    save_data(data)

    count = len(data["todos"][uid])
    await interaction.response.send_message(
        embed=make_embed("◉ TASK ADDED", f"**{interaction.user.display_name}** added task **#{count}**\n\n{task}", color=0x10B981)
    )

@todo_group.command(name="done", description="Mark a task as complete")
@app_commands.describe(number="Task number (from /todo list)")
async def todo_done(interaction: discord.Interaction, number: int):
    data = load_data()
    uid  = str(interaction.user.id)
    todos = data["todos"].get(uid, [])

    if not todos or number < 1 or number > len(todos):
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID", f"Task #{number} not found. Use `/todo list` to see your tasks.", color=0xE63946),
            ephemeral=True
        )
        return

    todos[number - 1]["done"] = True
    data["todos"][uid] = todos
    save_data(data)

    done  = sum(1 for t in todos if t["done"])
    total = len(todos)
    pct   = round((done / total) * 100)
    base  = data.get("base_echo_rate", 500)
    proj  = round(base * done / total)

    await interaction.response.send_message(
        embed=make_embed(
            "✓ TASK COMPLETE",
            f"**{interaction.user.display_name}** completed: **{todos[number-1]['task']}**\n\n"
            f"`{done}/{total} tasks` · {pct}% complete\n"
            f"Projected echoes today: **{proj}**",
            color=0x10B981
        )
    )

@todo_group.command(name="list", description="View your current task list")
async def todo_list(interaction: discord.Interaction):
    data  = load_data()
    uid   = str(interaction.user.id)
    todos = data["todos"].get(uid, [])

    if not todos:
        await interaction.response.send_message(
            embed=make_embed("◈ YOUR TASKS", "No tasks yet. Use `/todo add <task>` to start.", color=0x7B2FBE)
        )
        return

    lines = []
    for i, t in enumerate(todos, 1):
        check = "✓" if t["done"] else "○"
        strike = f"~~{t['task']}~~" if t["done"] else t["task"]
        lines.append(f"`{check}` **{i}.** {strike}")

    done  = sum(1 for t in todos if t["done"])
    total = len(todos)
    base  = data.get("base_echo_rate", 500)
    proj  = round(base * done / total) if total else 0

    embed = make_embed(f"◈ {interaction.user.display_name}'s TASKS", "\n".join(lines), color=0xA855F7)
    embed.add_field(name="Progress", value=f"{done}/{total} done · **{proj} echoes** projected", inline=False)
    await interaction.response.send_message(embed=embed)

@todo_group.command(name="clear", description="Clear your task list and start fresh")
async def todo_clear(interaction: discord.Interaction):
    data = load_data()
    uid  = str(interaction.user.id)
    data["todos"][uid] = []
    save_data(data)
    await interaction.response.send_message(
        embed=make_embed("◈ LIST CLEARED", f"**{interaction.user.display_name}** wiped their task list. Fresh start.", color=0x6B6B9A)
    )

@todo_group.command(name="multiadd", description="Bulk add multiple tasks at once (comma separated)")
@app_commands.describe(tasks="Tasks separated by commas e.g. Task 1, Task 2, Task 3")
async def todo_multiadd(interaction: discord.Interaction, tasks: str):
    data = load_data()
    uid  = str(interaction.user.id)

    if not get_shadow_id(uid, data):
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "Use `/link <shadow_id>` first to connect your Discord to your Shadow ID.", color=0xE63946),
            ephemeral=True
        )
        return

    task_list = [t.strip() for t in tasks.split(",") if t.strip()]
    if not task_list:
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID", "No tasks found. Separate tasks with commas.", color=0xE63946),
            ephemeral=True
        )
        return

    data["todos"].setdefault(uid, [])
    start_count = len(data["todos"][uid])
    for task in task_list:
        data["todos"][uid].append({"task": task, "done": False})
    save_data(data)

    lines = [f"**#{start_count + i + 1}** · {t}" for i, t in enumerate(task_list)]
    await interaction.response.send_message(
        embed=make_embed(
            f"◉ {len(task_list)} TASKS ADDED",
            f"**{interaction.user.display_name}** added:\n\n" + "\n".join(lines),
            color=0x10B981
        )
    )

tree.add_command(todo_group)

# ── /echoes ───────────────────────────────────────────────────────
@tree.command(name="echoes", description="Check your Echo count and tier")
async def echoes(interaction: discord.Interaction):
    data      = load_data()
    uid       = str(interaction.user.id)
    shadow_id = get_shadow_id(uid, data)

    if not shadow_id:
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", "Use `/link <shadow_id>` to connect your Discord account.", color=0xE63946),
            ephemeral=True
        )
        return

    member = get_member(shadow_id, data)
    if not member:
        # Pull fresh from GAS
        await pull_from_gas(data)
        member = get_member(shadow_id, load_data())

    if not member:
        await interaction.response.send_message(
            embed=make_embed("▲ NOT FOUND", f"Shadow ID `{shadow_id}` not found in records.", color=0xE63946),
            ephemeral=True
        )
        return

    count = int(member.get("echoCount", 0))
    tier  = get_tier(count)
    todos = data["todos"].get(uid, [])
    done  = sum(1 for t in todos if t["done"])
    total = len(todos)
    proj  = round(data.get("base_echo_rate", 500) * done / total) if total else 0

    embed = discord.Embed(title=f"◈ {member['codename']}", color=tier["color"])
    embed.add_field(name="Shadow ID",   value=f"`{shadow_id}`",              inline=True)
    embed.add_field(name="Echo Count",  value=f"**{count:,}**",              inline=True)
    embed.add_field(name="Tier",        value=f"**{tier['name'].upper()}**",  inline=True)
    embed.add_field(name="Today's Tasks", value=f"{done}/{total} done · +{proj} projected", inline=False)
    embed.set_footer(text="SHADOWSEEKERS ORDER · SHADOW BOT")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── /leaderboard ──────────────────────────────────────────────────
@tree.command(name="leaderboard", description="Top 10 operatives by Echo count")
async def leaderboard(interaction: discord.Interaction):
    data = load_data()
    if not data["members"]:
        await pull_from_gas(data)
        data = load_data()

    sorted_m = sorted(data["members"], key=lambda m: int(m.get("echoCount", 0)), reverse=True)[:10]

    if not sorted_m:
        await interaction.response.send_message(
            embed=make_embed("▲ NO DATA", "No members found. Sync with GAS first.", color=0xE63946),
            ephemeral=True
        )
        return

    lines = []
    medals = ["🥇","🥈","🥉"]
    for i, m in enumerate(sorted_m):
        count = int(m.get("echoCount", 0))
        tier  = get_tier(count)
        rank  = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(f"{rank} **{m['codename']}** · `{m['shadowId']}` · **{count:,}** _{tier['name']}_")

    embed = make_embed("◈ ECHO LEADERBOARD", "\n".join(lines), color=0xF0A500)
    embed.set_footer(text=f"SHADOWSEEKERS ORDER · {len(data['members'])} operatives total")
    await interaction.response.send_message(embed=embed)

# ── /link ─────────────────────────────────────────────────────────
@tree.command(name="link", description="Link your Discord account to your Shadow ID")
@app_commands.describe(shadow_id="Your Shadow ID (e.g. SS0069)")
async def link(interaction: discord.Interaction, shadow_id: str):
    data     = load_data()
    uid      = str(interaction.user.id)
    sid      = shadow_id.upper().strip()

    # Already linked
    if get_shadow_id(uid, data):
        await interaction.response.send_message(
            embed=make_embed("▲ ALREADY LINKED", f"You're already linked to `{data['links'][uid]['shadow_id']}`.", color=0xE63946),
            ephemeral=True
        )
        return

    # Check shadow_id format
    import re
    if not re.match(r'^SS\d{4}$', sid):
        await interaction.response.send_message(
            embed=make_embed("▲ INVALID ID", "Format must be `SS####` e.g. `SS0069`", color=0xE63946),
            ephemeral=True
        )
        return

    # Check if shadow_id already claimed
    for existing_link in data["links"].values():
        if existing_link["shadow_id"] == sid and existing_link.get("approved"):
            await interaction.response.send_message(
                embed=make_embed("▲ ALREADY CLAIMED", f"`{sid}` is already linked to another account.", color=0xE63946),
                ephemeral=True
            )
            return

    # Store pending link
    data["pending_links"][uid] = sid
    save_data(data)

    # Notify admin channel
    ch = discord.utils.get(interaction.guild.text_channels, name=APPROVE_CH)
    if ch:
        embed = make_embed(
            "◈ LINK REQUEST",
            f"{interaction.user.mention} wants to link to `{sid}`\n\n"
            f"Use `/approve {interaction.user.id}` to approve.",
            color=0xF0A500
        )
        await ch.send(embed=embed)

    await interaction.response.send_message(
        embed=make_embed(
            "◈ LINK REQUESTED",
            f"Your request to link `{sid}` has been sent to admins for approval.\nYou'll be notified once approved.",
            color=0xA855F7
        ),
        ephemeral=True
    )

# ── /approve ──────────────────────────────────────────────────────
@tree.command(name="approve", description="[ADMIN] Approve a member's link request")
@app_commands.describe(user="The Discord user to approve")
async def approve(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ ACCESS DENIED", "Admins only.", color=0xE63946), ephemeral=True)
        return

    data = load_data()
    uid  = str(user.id)
    sid  = data["pending_links"].get(uid)

    if not sid:
        await interaction.response.send_message(
            embed=make_embed("▲ NO PENDING REQUEST", f"{user.display_name} has no pending link request.", color=0xE63946),
            ephemeral=True
        )
        return

    data["links"][uid] = {"shadow_id": sid, "approved": True}
    del data["pending_links"][uid]
    save_data(data)

    # DM the user
    try:
        await user.send(embed=make_embed(
            "◈ LINK APPROVED",
            f"Your Discord has been linked to `{sid}`.\nYou can now use `/todo` and `/echoes`.",
            color=0x10B981
        ))
    except:
        pass

    await interaction.response.send_message(
        embed=make_embed("◉ APPROVED", f"{user.display_name} linked to `{sid}`.", color=0x10B981),
        ephemeral=True
    )

# ── /give ─────────────────────────────────────────────────────────
@tree.command(name="give", description="[ADMIN] Manually award echoes to an operative")
@app_commands.describe(user="Discord user to award", amount="Echo amount (can be negative)")
async def give(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ ACCESS DENIED", "Admins only.", color=0xE63946), ephemeral=True)
        return

    data      = load_data()
    uid       = str(user.id)
    shadow_id = get_shadow_id(uid, data)

    if not shadow_id:
        await interaction.response.send_message(
            embed=make_embed("▲ NOT LINKED", f"{user.display_name} has no linked Shadow ID.", color=0xE63946),
            ephemeral=True
        )
        return

    for i, m in enumerate(data["members"]):
        if m["shadowId"] == shadow_id:
            old = int(m.get("echoCount", 0))
            new = max(0, old + amount)
            data["members"][i]["echoCount"] = new
            save_data(data)
            await push_to_gas(data)
            sign = "+" if amount >= 0 else ""
            await interaction.response.send_message(
                embed=make_embed(
                    "◉ ECHOES AWARDED",
                    f"**{m['codename']}** (`{shadow_id}`)\n`{old:,}` → **{new:,}** ({sign}{amount:,})",
                    color=0x10B981
                )
            )
            return

    await interaction.response.send_message(embed=make_embed("▲ NOT FOUND", "Member not in records.", color=0xE63946), ephemeral=True)

# ── /setbase ──────────────────────────────────────────────────────
@tree.command(name="setbase", description="[ADMIN] Set the daily base echo rate")
@app_commands.describe(amount="Base echoes per day for 100% completion")
async def setbase(interaction: discord.Interaction, amount: int):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ ACCESS DENIED", "Admins only.", color=0xE63946), ephemeral=True)
        return
    data = load_data()
    data["base_echo_rate"] = max(1, amount)
    save_data(data)
    await interaction.response.send_message(
        embed=make_embed("◉ BASE RATE SET", f"Daily base echo rate is now **{amount:,}** echoes.", color=0x10B981)
    )

# ── /forceday ─────────────────────────────────────────────────────
@tree.command(name="forceday", description="[ADMIN] Manually trigger end-of-day echo calculation")
async def forceday(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ ACCESS DENIED", "Admins only.", color=0xE63946), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=make_embed("◉ RUNNING EOD", "Calculating echoes for all operatives...", color=0xA855F7)
    )
    results = await run_end_of_day(interaction.guild)
    total_given = sum(r["earned"] for r in results)
    await interaction.followup.send(
        embed=make_embed(
            "◉ EOD COMPLETE",
            f"Processed **{len(results)}** operatives · **{total_given:,}** echoes awarded · Synced to sheet.",
            color=0x10B981
        )
    )

# ── /sync ─────────────────────────────────────────────────────────
@tree.command(name="sync", description="[ADMIN] Pull latest member data from Google Sheet")
async def sync_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("▲ ACCESS DENIED", "Admins only.", color=0xE63946), ephemeral=True)
        return
    await interaction.response.send_message(embed=make_embed("◉ SYNCING", "Pulling from GAS sheet...", color=0xA855F7), ephemeral=True)
    data = load_data()
    ok   = await pull_from_gas(data)
    data = load_data()
    if ok:
        await interaction.followup.send(
            embed=make_embed("◉ SYNCED", f"Pulled **{len(data['members'])}** operatives from sheet.", color=0x10B981),
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            embed=make_embed("▲ SYNC FAILED", "Could not reach GAS. Check URL and deployment.", color=0xE63946),
            ephemeral=True
        )

# ── BOT EVENTS ────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[SHADOW BOT] Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await tree.sync()
        print(f"[SHADOW BOT] Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"[SHADOW BOT] Sync error: {e}")

    # Pull initial data from GAS
    data = load_data()
    await pull_from_gas(data)
    print(f"[SHADOW BOT] Loaded {len(load_data()['members'])} members from GAS")

    daily_echo_task.start()
    print(f"[SHADOW BOT] Daily task scheduled at {EOD_HOUR}:{EOD_MINUTE:02d} {TIMEZONE}")

bot.run(TOKEN)
