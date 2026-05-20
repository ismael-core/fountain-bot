"""Read-only commands over the refresh history.

The /refresh command itself was removed in favor of the ticket flow.
These commands still work and read from the same `refreshes` table that
the ticket approval pipeline writes into.
"""
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import database

BUFF_DURATION = timedelta(hours=1)


class Refresh(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="leaderboard",
        description="Show refreshes per person over the last N days",
    )
    @app_commands.describe(days="Number of days to look back (default 7, max 365)")
    async def leaderboard(self, interaction: discord.Interaction, days: int = 7):
        if not (1 <= days <= 365):
            await interaction.response.send_message(
                "Days must be between 1 and 365",
                ephemeral=True,
            )
            return

        rows = database.get_leaderboard(days=days)
        if not rows:
            await interaction.response.send_message(
                f"No refreshes logged in the last {days} days."
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = [f"**🏆 Leaderboard — last {days} days**", ""]
        for i, row in enumerate(rows):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{prefix} <@{row['user_id']}> — **{row['count']}** refreshes")

        total = sum(r["count"] for r in rows)
        lines.append("")
        lines.append(f"*Total: {total} refreshes logged*")

        await interaction.response.send_message(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="stats",
        description="See your personal refresh stats",
    )
    async def stats(self, interaction: discord.Interaction):
        s = database.get_user_stats(interaction.user.id)
        await interaction.response.send_message(
            f"**Your stats**\n"
            f"This week: **{s['week']}** refreshes\n"
            f"This month: **{s['month']}** refreshes\n"
            f"All-time: **{s['total']}** refreshes",
            ephemeral=True,
        )

    @app_commands.command(
        name="buff_status",
        description="Show the current Fountain buff status (time remaining)",
    )
    async def buff_status(self, interaction: discord.Interaction):
        last = database.get_last_refresh()
        if last is None:
            await interaction.response.send_message(
                "No refreshes logged yet."
            )
            return

        last_ts = last["timestamp"]
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        expires_at = last_ts + BUFF_DURATION
        now = datetime.now(timezone.utc)

        if expires_at <= now:
            await interaction.response.send_message(
                f"❌ Buff is **down**. Last refresh was by <@{last['user_id']}> "
                f"<t:{int(last_ts.timestamp())}:R>.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.send_message(
            f"🟢 Buff is **active**. Expires <t:{int(expires_at.timestamp())}:R> "
            f"(last refresh by <@{last['user_id']}>).",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Refresh(bot))
