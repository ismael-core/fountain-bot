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
            f"✅ Refresh registrado para {interaction.user.mention} a las "
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
                f"No hay refreshes registrados en los últimos {days} días."
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = [f"**🏆 Leaderboard — últimos {days} días**", ""]
        for i, row in enumerate(rows):
            prefix = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{prefix} <@{row['user_id']}> — **{row['count']}** refreshes")

        total = sum(r["count"] for r in rows)
        lines.append("")
        lines.append(f"*Total: {total} refreshes registrados*")

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
            f"**Tus stats**\n"
            f"Esta semana: **{s['week']}** refreshes\n"
            f"Este mes: **{s['month']}** refreshes\n"
            f"Total histórico: **{s['total']}** refreshes",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Refresh(bot))
