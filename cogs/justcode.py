"""Temporary fun command. To remove: delete this file and the load_extension line in bot.py."""
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


async def setup(bot: commands.Bot):
    await bot.add_cog(JustCode(bot))
