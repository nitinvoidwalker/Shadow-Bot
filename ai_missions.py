"""
╔══════════════════════════════════════════════════════╗
║         SHADOW BOT · AI MISSION GENERATOR            ║
║   Groq-powered personalized daily mission engine     ║
║   Auto-generates at 6 AM · /acceptmissions to add   ║
╚══════════════════════════════════════════════════════╝

HOW TO INTEGRATE INTO bot-6-2.py:
──────────────────────────────────
1. pip install aiohttp (already installed)
2. Add env var: GROQ_API_KEY=your_key_here
3. Add env var: GENERAL_CHANNEL=general  (or your channel name)
4. Copy this entire file next to bot-6-2.py
5. In bot-6-2.py, add at the top (after imports):
       from ai_missions import setup_ai_missions
6. In bot-6-2.py on_ready(), add at the bottom:
       setup_ai_missions(bot, tree)
       ai_mission_task.start()

That's it. All commands and scheduled tasks register automatically.
"""

import os
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, time
import pytz

# ── CONFIG ────────────────────────────────────────────────────────
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
GROQ_API_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL        = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GENERAL_CHANNEL   = os.getenv("GENERAL_CHANNEL", "general")
MISSION_GEN_HOUR  = int(os.getenv("MISSION_GEN_HOUR", "6"))
MISSION_GEN_MIN   = int(os.getenv("MISSION_GEN_MIN", "0"))
TIMEZONE          = os.getenv("TIMEZONE", "Asia/Kolkata")

# Pending missions store: uid -> list of mission strings
# These are shown in general chat, waiting for /acceptmissions
_pending_missions: dict[str, list[str]] = {}

# ── GROQ CALL ─────────────────────────────────────────────────────
async def call_groq(prompt: str) -> str | None:
    """
    Call Groq API with a prompt. Returns the text response or None on failure.
    """
    if not GROQ_API_KEY:
        print("[AI MISSIONS] ERROR: GROQ_API_KEY not set.")
        return None

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the Shadow Mission AI, the intelligence engine of the ShadowSeekers Order — "
                    "a secret society of high-performance operatives. "
                    "Your role is to generate sharp, actionable daily missions based on an operative's history. "
                    "Missions must be specific, grounded in real tasks, and slightly challenging. "
                    "Never use vague phrases like 'work on your goals'. "
                    "Keep the tone like a covert handler briefing a field agent — precise, atmospheric, elite."
                )
            },
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.85,
        "max_tokens": 400,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_API_URL, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"[AI MISSIONS] Groq error {resp.status}: {text[:300]}")
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[AI MISSIONS] Groq request failed: {e}")
        return None


# ── HISTORY BUILDER ───────────────────────────────────────────────
def get_last_7_days_objectives(uid: str, data: dict) -> list[dict]:
    """
    Returns a list of dicts: [{date, text, done, ops_total, ops_done}]
    for the last 7 date keys found in the user's todo history.
    """
    entry = data["todos"].get(uid)
    if not entry or not isinstance(entry, dict):
        return []

    dates_map = entry.get("dates", {})
    # Sort date keys MM/DD — get up to 7 most recent
    def date_sort_key(d):
        try:
            parts = d.split("/")
            return (int(parts[0]), int(parts[1]))
        except Exception:
            return (0, 0)

    sorted_dates = sorted(dates_map.keys(), key=date_sort_key, reverse=True)[:7]
    result = []

    for date_key in sorted_dates:
        todos = dates_map.get(date_key, [])
        for t in todos:
            ops = t.get("ops", [])
            ops_total = len(ops)
            ops_done  = sum(1 for op in ops if op.get("done"))
            result.append({
                "date":      date_key,
                "text":      t.get("text", ""),
                "done":      t.get("done", False),
                "ops_total": ops_total,
                "ops_done":  ops_done,
            })

    return result


def build_mission_prompt(codename: str, tier_name: str, history: list[dict], plan_context: str = "") -> str:
    """
    Build the Groq prompt using the operative's history and tier.
    """
    if history:
        history_lines = []
        for h in history:
            status = "✓" if h["done"] else "✗"
            ops_note = f" [{h['ops_done']}/{h['ops_total']} ops done]" if h["ops_total"] > 0 else ""
            history_lines.append(f"  [{h['date']}] {status} {h['text']}{ops_note}")
        history_block = "\n".join(history_lines)
    else:
        history_block = "  No recorded objectives yet — this operative is fresh."

    prompt = f"""
Operative Codename: {codename}
Current Rank Tier: {tier_name}
{plan_context}
Objective History (last 7 days):
{history_block}

Generate 3 to 5 personalized daily missions for today based STRICTLY on this operative's history.
Critical rules:
- Look at what subjects/topics they actually study — generate missions in THOSE exact areas only
- If they study math, give math tasks. If they code, give coding tasks. Mirror their actual work.
- Each mission must name a specific topic, chapter, or skill — never generic phrases
- Example good mission: "Complete 20 integration problems from Chapter 5"
- Example bad mission: "Work on your studies" or "Complete your daily tasks"
- If no history exists, generate general productivity missions (pomodoro sessions, revision, planning)
- Scale difficulty to rank: {tier_name}
- If they failed frequently, give 3 easier missions. If they completed everything, give 5 harder ones.
- Output ONLY the missions, one per line, no bullets, no numbers, no extra text, nothing else
""".strip()

    return prompt


def parse_missions(raw_text: str) -> list[str]:
    """
    Parse raw Groq output into a clean list of mission strings.
    Strips empty lines, bullets, numbers, etc.
    """
    missions = []
    for line in raw_text.strip().splitlines():
        line = line.strip()
        # Remove leading bullets, dashes, numbers
        for prefix in ["- ", "• ", "* "]:
            if line.startswith(prefix):
                line = line[len(prefix):]
        # Remove "1. ", "2. " etc
        if len(line) > 2 and line[0].isdigit() and line[1] in (".", ")"):
            line = line[2:].strip()
        if line:
            missions.append(line)
    return missions[:5]  # Cap at 5 missions max


# ── MISSION EMBED BUILDER ─────────────────────────────────────────
def make_mission_embed(codename: str, tier_name: str, missions: list[str]) -> discord.Embed:
    tier_colors = {
        "Initiate": 0x6B6B9A,
        "Seeker":   0x7B2FBE,
        "Phantom":  0xA855F7,
        "Wraith":   0xE63946,
        "Voidborn": 0xF0A500,
    }
    color = tier_colors.get(tier_name, 0xA855F7)

    mission_lines = "\n".join(f"◈ {m}" for m in missions)

    embed = discord.Embed(
        title="🧠 AI MISSION BRIEF — TODAY'S OPERATIONS",
        description=(
            f"**Operative:** {codename} · **Tier:** {tier_name}\n\n"
            f"{mission_lines}\n\n"
            f"*Use `/acceptmissions` to deploy these to your dossier.*"
        ),
        color=color
    )
    embed.set_footer(text="☽ SHADOWSEEKERS ORDER · AI MISSION ENGINE · DEEP IN THE DARK, I DON'T NEED THE LIGHT")
    return embed


# ── GENERATE FOR ONE USER ─────────────────────────────────────────
async def generate_missions_for_user(uid: str, data: dict) -> list[str] | None:
    """
    Full pipeline: build prompt → call Groq → parse → return missions.
    Also stores them in _pending_missions[uid] for /acceptmissions.
    """
    from ai_missions import get_last_7_days_objectives, build_mission_prompt, parse_missions, call_groq

    link = data["links"].get(uid)
    if not link or not link.get("approved"):
        return None

    shadow_id = link["shadow_id"]
    member    = next((m for m in data["members"] if m["shadowId"] == shadow_id), None)
    if not member:
        return None

    codename   = member.get("codename", shadow_id)
    echo_count = int(member.get("echoCount", 0))

    # Determine tier
    tier_name = "Initiate"
    for t in [
        ("Voidborn", 5000), ("Wraith", 3000), ("Phantom", 1500),
        ("Seeker", 500),    ("Initiate", 0)
    ]:
        if echo_count >= t[1]:
            tier_name = t[0]
            break

    history = get_last_7_days_objectives(uid, data)

    # Inject saved plan if exists
    plan = data.get("plans", {}).get(uid)
    plan_context = ""
    if plan:
        plan_context = f"""
Operative's saved plan:
- Goal: {plan.get('goal', '')}
- Subjects: {', '.join(plan.get('subjects', []))}
- Daily hours: {plan.get('hours_per_day', '?')}
- Timeline: {plan.get('timeline', '?')}
Use this plan as the primary source for mission topics.
"""

    prompt  = build_mission_prompt(codename, tier_name, history, plan_context)
    raw     = await call_groq(prompt)

    if not raw:
        return None

    missions = parse_missions(raw)
    if missions:
        _pending_missions[uid] = missions

    return missions


# ── SCHEDULED TASK (runs at MISSION_GEN_HOUR:MISSION_GEN_MIN) ─────
_bot_ref  = None
_tree_ref = None

@tasks.loop(time=time(
    hour=MISSION_GEN_HOUR,
    minute=MISSION_GEN_MIN,
    tzinfo=pytz.timezone(TIMEZONE)
))
async def ai_mission_task():
    """
    Scheduled task: generate and post missions for all approved operatives.
    Runs daily at configured time (default 6:00 AM IST).
    """
    if _bot_ref is None:
        return

    # Import load_data from the main bot file at runtime
    # (avoids circular import — both files are in same directory)
    try:
        from bot_6_2 import load_data  # adjust filename if needed
    except ImportError:
        try:
            import importlib, sys
            # Try to find load_data from the already-loaded main module
            main_mod = sys.modules.get("__main__")
            if main_mod and hasattr(main_mod, "load_data"):
                load_data = main_mod.load_data
            else:
                print("[AI MISSIONS] Could not import load_data — skipping scheduled run")
                return
        except Exception as e:
            print(f"[AI MISSIONS] Import error: {e}")
            return

    data = await load_data()

    for guild in _bot_ref.guilds:
        general_ch = discord.utils.get(guild.text_channels, name=GENERAL_CHANNEL)
        if not general_ch:
            print(f"[AI MISSIONS] #{GENERAL_CHANNEL} not found in {guild.name}")
            continue

        approved_uids = [
            uid for uid, link in data["links"].items()
            if link.get("approved")
        ]

        print(f"[AI MISSIONS] Generating for {len(approved_uids)} operatives...")

        for uid in approved_uids:
            try:
                # Rate limit: small delay between users to avoid API bursts
                await asyncio.sleep(1.5)

                missions = await generate_missions_for_user(uid, data)
                if not missions:
                    print(f"[AI MISSIONS] No missions generated for uid={uid}")
                    continue

                link      = data["links"][uid]
                shadow_id = link["shadow_id"]
                member    = next((m for m in data["members"] if m["shadowId"] == shadow_id), None)
                codename  = member.get("codename", shadow_id) if member else shadow_id
                echo_count = int(member.get("echoCount", 0)) if member else 0

                tier_name = "Initiate"
                for t in [("Voidborn", 5000), ("Wraith", 3000), ("Phantom", 1500), ("Seeker", 500), ("Initiate", 0)]:
                    if echo_count >= t[1]:
                        tier_name = t[0]
                        break

                # Try to mention the user
                discord_member = guild.get_member(int(uid))
                mention = discord_member.mention if discord_member else f"`{codename}`"

                embed = make_mission_embed(codename, tier_name, missions)
                await general_ch.send(content=f"{mention} — your missions for today have arrived.", embed=embed)

            except Exception as e:
                print(f"[AI MISSIONS] Error for uid={uid}: {e}")

        print(f"[AI MISSIONS] Daily mission broadcast complete for {guild.name}")


# ── /acceptmissions COMMAND ───────────────────────────────────────
def register_commands(tree: app_commands.CommandTree):
    """Register all AI mission slash commands onto the bot's command tree."""

    @tree.command(name="acceptmissions", description="Deploy today's AI missions to your dossier (e.g. all, or pick: 1,3,5)")
    @app_commands.describe(numbers="Which missions to accept: 'all' or comma-separated numbers like '1,3,5'")
    async def acceptmissions(interaction: discord.Interaction, numbers: str = "all"):
        uid = str(interaction.user.id)

        missions = _pending_missions.get(uid)
        if not missions:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="◈ NO MISSIONS PENDING",
                    description=(
                        "You have no pending AI missions right now.\n\n"
                        "Missions are broadcast daily at **6 AM**. "
                        "Use `/generatemissions` to generate yours manually right now."
                    ),
                    color=0x6B6B9A
                ).set_footer(text="☽ SHADOWSEEKERS ORDER · AI MISSION ENGINE")
            )
            return

        # ── Parse which missions to accept ──
        if numbers.strip().lower() == "all":
            selected = missions
        else:
            try:
                indices = [int(x.strip()) for x in numbers.split(",")]
                invalid = [n for n in indices if n < 1 or n > len(missions)]
                if invalid:
                    await interaction.response.send_message(
                        embed=discord.Embed(
                            title="▲ INVALID NUMBERS",
                            description=f"Mission numbers {', '.join(str(n) for n in invalid)} don't exist. You have {len(missions)} missions. Use `/mymissions` to see them.",
                            color=0xE63946
                        )
                    )
                    return
                selected = [missions[i - 1] for i in indices]
            except ValueError:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="▲ INVALID INPUT",
                        description="Use `all` or comma-separated numbers like `1,3,5`.",
                        color=0xE63946
                    )
                )
                return

        # Import set_todos_for_date and helpers from main bot
        try:
            import sys
            main_mod = sys.modules.get("__main__")
            if not main_mod:
                raise ImportError("Main module not found")
            load_data        = main_mod.load_data
            save_data        = main_mod.save_data
            set_todos_for_date = main_mod.set_todos_for_date
            get_todos_for_date = main_mod.get_todos_for_date
            get_shadow_id    = main_mod.get_shadow_id
            today_str        = main_mod.today_str
            make_embed       = main_mod.make_embed
        except Exception as e:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="▲ INTEGRATION ERROR",
                    description=f"Could not connect to dossier system: `{e}`",
                    color=0xE63946
                )
            )
            return

        data      = await load_data()
        shadow_id = get_shadow_id(uid, data)

        if not shadow_id:
            await interaction.response.send_message(
                embed=make_embed("▲ NOT LINKED", "Link your Shadow ID first — `/link <shadow_id> <n>`.", color=0xE63946)
            )
            return

        today    = today_str()
        existing = get_todos_for_date(uid, today, data)

        # Build new todo entries (same format as /todo add)
        new_todos = [
            {
                "text":     mission,
                "done":     False,
                "ops":      [],
                "priority": "p2",  # default priority for AI missions
                "source":   "ai",  # tag so they're identifiable
            }
            for mission in selected
        ]

        updated = existing + new_todos
        set_todos_for_date(uid, today, updated, data)
        await save_data(data)

        # Clear pending only if all missions were accepted
        if len(selected) == len(missions):
            del _pending_missions[uid]

        mission_lines = "\n".join(f"◈ {m}" for m in selected)

        await interaction.response.send_message(
            embed=make_embed(
                "✅ MISSIONS DEPLOYED",
                f"**{len(selected)} AI mission{'s' if len(selected) != 1 else ''}** added to your dossier:\n\n"
                f"{mission_lines}\n\n"
                f"View with `/todo list`.",
                color=0x10B981
            )
        )

    @tree.command(name="generatemissions", description="Manually trigger AI mission generation for yourself")
    async def generatemissions(interaction: discord.Interaction):
        uid = str(interaction.user.id)

        await interaction.response.defer(thinking=True)

        try:
            import sys
            main_mod  = sys.modules.get("__main__")
            load_data = main_mod.load_data
            get_shadow_id = main_mod.get_shadow_id
            make_embed    = main_mod.make_embed
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="▲ INTEGRATION ERROR",
                    description=f"`{e}`",
                    color=0xE63946
                )
            )
            return

        data      = await load_data()
        shadow_id = get_shadow_id(uid, data)

        if not shadow_id:
            await interaction.followup.send(
                embed=make_embed("▲ NOT LINKED", "Link your Shadow ID first — `/link <shadow_id> <n>`.", color=0xE63946)
            )
            return

        missions = await generate_missions_for_user(uid, data)

        if not missions:
            await interaction.followup.send(
                embed=make_embed(
                    "▲ MISSION GENERATION FAILED",
                    "The AI engine is offline or returned no missions. Check your `GROQ_API_KEY` or try again shortly.",
                    color=0xE63946
                )
            )
            return

        member     = next((m for m in data["members"] if m["shadowId"] == shadow_id), None)
        codename   = member.get("codename", shadow_id) if member else shadow_id
        echo_count = int(member.get("echoCount", 0)) if member else 0

        tier_name = "Initiate"
        for t in [("Voidborn", 5000), ("Wraith", 3000), ("Phantom", 1500), ("Seeker", 500), ("Initiate", 0)]:
            if echo_count >= t[1]:
                tier_name = t[0]
                break

        embed = make_mission_embed(codename, tier_name, missions)
        await interaction.followup.send(embed=embed)

    @tree.command(name="mymissions", description="View your pending AI missions (not yet added to dossier)")
    async def mymissions(interaction: discord.Interaction):
        uid      = str(interaction.user.id)
        missions = _pending_missions.get(uid)

        if not missions:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="◈ NO PENDING MISSIONS",
                    description=(
                        "No pending missions. Use `/generatemissions` to generate now, "
                        "or wait for the 6 AM broadcast."
                    ),
                    color=0x6B6B9A
                ).set_footer(text="☽ SHADOWSEEKERS ORDER · AI MISSION ENGINE")
            )
            return

        mission_lines = "\n".join(f"`{i+1}.` {m}" for i, m in enumerate(missions))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🧠 YOUR PENDING MISSIONS",
                description=(
                    f"{mission_lines}\n\n"
                    f"*Use `/acceptmissions` to add these to your dossier.*"
                ),
                color=0xA855F7
            ).set_footer(text="☽ SHADOWSEEKERS ORDER · AI MISSION ENGINE")
        )


# ── SETUP FUNCTION (called from on_ready) ─────────────────────────
def setup_ai_missions(bot: discord.ext.commands.Bot, tree: app_commands.CommandTree):
    """
    Call this from on_ready() in bot-6-2.py:
        setup_ai_missions(bot, tree)
        ai_mission_task.start()
    """
    global _bot_ref, _tree_ref
    _bot_ref  = bot
    _tree_ref = tree
    register_commands(tree)
    print("[AI MISSIONS] AI Mission Generator registered ✓")
    print(f"[AI MISSIONS] Daily broadcast scheduled at {MISSION_GEN_HOUR}:{MISSION_GEN_MIN:02d} {TIMEZONE}")
    print(f"[AI MISSIONS] Model: {GROQ_MODEL}")
