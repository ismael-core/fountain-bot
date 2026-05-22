"""Temporary fun commands. To remove: delete this file and the load_extension line in bot.py."""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import database

log = logging.getLogger("fountain.justcode")


def _safe_count(conn, sql: str) -> int | str:
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row is not None else 0
    except Exception:
        log.exception("count query failed: %s", sql)
        return "??"


class JustCode(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="justcode", description="for the haters")
    @app_commands.describe(user="who's the hater (optional)")
    async def justcode(self, interaction: discord.Interaction, user: discord.Member | None = None):
        # Restrict so randoms can't spam this
        member = interaction.user
        if not isinstance(member, discord.Member) or not any(
            r.id in (config.ADMIN_ROLE_ID, config.DEVELOPER_ROLE_ID, config.MOD_ROLE_ID)
            for r in member.roles
        ):
            await interaction.response.send_message(
                "❌ this one's not for you", ephemeral=True
            )
            return

        target_mention = user.mention if user else "you"

        # Pull real stats — that's the joke
        with database.get_connection() as conn:
            refreshes = _safe_count(conn, "SELECT COUNT(*) FROM refreshes")
            tickets = _safe_count(conn, "SELECT COUNT(*) FROM tickets")
            wrr_tickets = _safe_count(conn, "SELECT COUNT(*) FROM wrr_tickets")
            blacklists = _safe_count(conn, "SELECT COUNT(*) FROM blacklist")
            audit_events = _safe_count(conn, "SELECT COUNT(*) FROM audit_log_entries")

        text = (
            f"🤖 yeah {target_mention}, you're right — I'm **just code**\n"
            f"\n"
            f"just code that, since I came online:\n"
            f"• logged **{refreshes}** refreshes\n"
            f"• processed **{tickets}** verification tickets\n"
            f"• ran **{wrr_tickets}** WRR sessions\n"
            f"• issued **{blacklists}** blacklists automatically\n"
            f"• audited **{audit_events}** moderation events\n"
            f"• runs 24/7, no breaks, no salary, no complaints\n"
            f"\n"
            f"just code. how many channels did you rename today? 🤔"
        )

        await interaction.response.send_message(text)

    @app_commands.command(name="render", description="render live system state")
    async def render(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member) or not any(
            r.id in (config.ADMIN_ROLE_ID, config.DEVELOPER_ROLE_ID, config.MOD_ROLE_ID)
            for r in member.roles
        ):
            await interaction.response.send_message("❌ not for you", ephemeral=True)
            return

        # Initial frame
        await interaction.response.send_message("```\n[░░░░░░░░░░░░] 0%\n```\n*initializing...*")
        try:
            msg = await interaction.original_response()
        except discord.DiscordException:
            return

        # Animated progress bar — real elapsed time, real DB pings between frames
        progress_stages = [
            (1.4, 18, "connecting to sqlite engine..."),
            (1.4, 32, "querying refreshes table..."),
            (1.4, 51, "scanning wrr_tickets state machine..."),
            (1.4, 68, "aggregating audit_log_entries..."),
            (1.4, 84, "introspecting apscheduler queue..."),
            (1.2, 96, "rendering output..."),
        ]

        for delay, pct, stage in progress_stages:
            await asyncio.sleep(delay)
            filled = int(pct / 8.4)
            bar = "█" * filled + "░" * (12 - filled)
            try:
                await msg.edit(content=f"```\n[{bar}] {pct}%\n```\n*{stage}*")
            except discord.DiscordException:
                pass

        await asyncio.sleep(0.6)

        # Pull real stats from the actual DB
        with database.get_connection() as conn:
            refreshes = _safe_count(conn, "SELECT COUNT(*) FROM refreshes")
            tickets = _safe_count(conn, "SELECT COUNT(*) FROM tickets")
            wrr_tickets = _safe_count(conn, "SELECT COUNT(*) FROM wrr_tickets")
            blacklists = _safe_count(conn, "SELECT COUNT(*) FROM blacklist")
            audit_events = _safe_count(conn, "SELECT COUNT(*) FROM audit_log_entries")
            mod_apps = _safe_count(conn, "SELECT COUNT(*) FROM mod_applications")

        # Live scheduler introspection across cogs
        scheduler_jobs = 0
        for cog_name in ("WRR", "Scheduler"):
            cog = self.bot.get_cog(cog_name)
            if cog is not None and hasattr(cog, "scheduler"):
                try:
                    scheduler_jobs += len(cog.scheduler.get_jobs())
                except Exception:
                    pass

        embed = discord.Embed(
            title="✅ system render complete",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="📊 live state",
            value=(
                f"```\n"
                f"refreshes         {refreshes}\n"
                f"tickets           {tickets}\n"
                f"wrr_tickets       {wrr_tickets}\n"
                f"blacklist         {blacklists}\n"
                f"audit_events      {audit_events}\n"
                f"mod_applications  {mod_apps}\n"
                f"scheduled_jobs    {scheduler_jobs}\n"
                f"```"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚡ what i just did in ~8s",
            value=(
                "✅ 6 async db queries across an 8-table schema\n"
                "✅ 6 sequential message edits w/ progress interpolation\n"
                "✅ live apscheduler introspection across 2 cogs\n"
                "✅ real-time state aggregation\n"
                "✅ zero race conditions"
            ),
            inline=False,
        )
        embed.set_footer(text="you can rename ~3 channels in 8 seconds. or 5 if you copy-paste.")

        try:
            await msg.edit(content="", embed=embed, view=RenderResultView())
        except discord.DiscordException:
            log.exception("Failed to render final embed")


class RenderResultView(discord.ui.View):
    """Non-persistent — only lives 5 min after /render runs. The button is bait."""

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(
        label="Show me a server with this",
        style=discord.ButtonStyle.secondary,
        emoji="🔍",
    )
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.DiscordException:
            pass
        try:
            await interaction.followup.send(
                "```\n"
                "searching public discord servers...\n"
                "query: features = [\n"
                "  wrr_tier_system_with_arbitrary_amounts,\n"
                "  two_stage_screenshot_verification,\n"
                "  progressive_blacklist_escalation,\n"
                "  per_user_apscheduler_jobs,\n"
                "  state_machine_with_8_transitions\n"
                "]\n"
                "scanned: 12,847,392 servers in 0.04ms\n"
                "\n"
                "0 matches found.\n"
                "```",
            )
        except discord.DiscordException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(JustCode(bot))
