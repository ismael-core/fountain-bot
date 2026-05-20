"""Refresh logging, leaderboard, personal stats, and buff status."""
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
        name="refresh",
        description="Log a Fountain refresh you just did (screenshot required)",
    )
    @app_commands.describe(
        proof="Screenshot showing you refreshed the Fountain in-game",
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        proof: discord.Attachment,
    ):
        # Validate the attachment is actually an image
        content_type = proof.content_type or ""
        if not content_type.startswith("image/"):
            await interaction.response.send_message(
                "❌ The attached file must be an image (a screenshot of your refresh).",
                ephemeral=True,
            )
            return

        ts = database.log_refresh(
            interaction.user.id,
            str(interaction.user),
            proof.url,
        )

        # Reschedule pre-alert and post-check from this new refresh
        scheduler_cog = self.bot.get_cog("Scheduler")
        if scheduler_cog is not None:
            scheduler_cog.reschedule_after_refresh(ts)

        expires_at = ts + BUFF_DURATION
        embed = discord.Embed(
            description=(
                f"✅ Refresh logged for {interaction.user.mention}. "
                f"Buff expires <t:{int(expires_at.timestamp())}:R>."
            ),
            color=discord.Color.green(),
        )
        embed.set_image(url=proof.url)
        await interaction.response.send_message(embed=embed)

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
        description="Show the current buff status (time remaining)",
    )
    async def buff_status(self, interaction: discord.Interaction):
        last = database.get_last_refresh()
        if last is None:
            await interaction.response.send_message(
                "No refreshes logged yet. Use `/refresh` to start the cycle."
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
                f"<t:{int(last_ts.timestamp())}:R>. Someone refresh!",
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
