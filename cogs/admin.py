"""Admin commands for clearing data. Restricted to server administrators."""
import discord
from discord import app_commands
from discord.ext import commands

import database


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    clear_group = app_commands.Group(
        name="clear",
        description="Admin: clear data from the bot",
        default_permissions=discord.Permissions(administrator=True),
    )

    @clear_group.command(
        name="refreshes",
        description="Delete all refresh history (irreversible)",
    )
    @app_commands.describe(confirm="Set to True to actually clear")
    async def clear_refreshes(
        self,
        interaction: discord.Interaction,
        confirm: bool = False,
    ):
        if not confirm:
            await interaction.response.send_message(
                "⚠️ This will delete **ALL** refresh history. "
                "Run `/clear refreshes confirm:True` to confirm.",
                ephemeral=True,
            )
            return

        deleted = database.clear_refreshes()
        await interaction.response.send_message(
            f"✅ Cleared **{deleted}** refresh entries."
        )

    @clear_group.command(
        name="slots",
        description="Delete all slot assignments (irreversible)",
    )
    @app_commands.describe(confirm="Set to True to actually clear")
    async def clear_slots(
        self,
        interaction: discord.Interaction,
        confirm: bool = False,
    ):
        if not confirm:
            await interaction.response.send_message(
                "⚠️ This will delete **ALL** slot assignments. "
                "Run `/clear slots confirm:True` to confirm.",
                ephemeral=True,
            )
            return

        deleted = database.clear_slots()
        await interaction.response.send_message(
            f"✅ Cleared **{deleted}** slot assignments."
        )

    @clear_group.command(
        name="all",
        description="Delete ALL data: refreshes AND slots (irreversible)",
    )
    @app_commands.describe(confirm="Set to True to actually clear everything")
    async def clear_all(
        self,
        interaction: discord.Interaction,
        confirm: bool = False,
    ):
        if not confirm:
            await interaction.response.send_message(
                "⚠️ This will delete **EVERYTHING** (refreshes AND slots). "
                "Run `/clear all confirm:True` to confirm.",
                ephemeral=True,
            )
            return

        refreshes, slots = database.clear_all()
        await interaction.response.send_message(
            f"✅ Cleared **{refreshes}** refreshes and **{slots}** slot assignments."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
