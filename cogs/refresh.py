"""Commands related to logging refreshes and viewing stats."""
import discord
from discord import app_commands
from discord.ext import commands

import database


class Refresh(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="refresh",
        description="Log a Fountain refresh you just did",
    )
    async def refresh(self, interaction: discord.Interaction):
        ts = database.log_refresh(interaction.user.id, str(interaction.user))
        await interaction.response.send_message(
            f"✅ Refresh logged for {interaction.user.mention} at "
            f"<t:{int(ts.timestamp())}:T>"
        )

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


async def setup(bot: commands.Bot):
    await bot.add_cog(Refresh(bot))
