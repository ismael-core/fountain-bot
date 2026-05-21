"""Admin commands for resetting data. Restricted to server administrators."""
import discord
from discord import app_commands
from discord.ext import commands

import database


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    reset_group = app_commands.Group(
        name="reset",
        description="Admin: reset bot data",
        default_permissions=discord.Permissions(administrator=True),
    )

    @reset_group.command(
        name="refreshes",
        description="Delete refresh history (all users, or just one if specified)",
    )
    @app_commands.describe(
        confirm="Set to True to actually reset",
        user="Optional: only delete this user's refreshes (omit to delete everyone's)",
    )
    async def reset_refreshes(
        self,
        interaction: discord.Interaction,
        confirm: bool = False,
        user: discord.Member | None = None,
    ):
        if not confirm:
            if user is None:
                msg = ("⚠️ This will delete **ALL** refresh history. "
                       "Run `/reset refreshes confirm:True` to confirm, "
                       "or add `user:@someone` to only reset one person.")
            else:
                msg = (f"⚠️ This will delete refresh history for {user.mention} only. "
                       f"Run `/reset refreshes confirm:True user:{user.mention}` to confirm.")
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if user is None:
            deleted = database.clear_refreshes()
            await interaction.response.send_message(
                f"✅ Cleared **{deleted}** refresh entries from the entire leaderboard."
            )
        else:
            deleted = database.clear_refreshes_for_user(user.id)
            await interaction.response.send_message(
                f"✅ Cleared **{deleted}** refresh entries for {user.mention}. "
                f"Other users on the leaderboard untouched.",
                allowed_mentions=discord.AllowedMentions(users=False),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
