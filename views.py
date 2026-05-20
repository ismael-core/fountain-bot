"""Persistent Discord UI views used by scheduler alerts."""
from datetime import timedelta

import discord

import database

BUFF_DURATION = timedelta(hours=1)


class RefreshView(discord.ui.View):
    """Persistent view with a single 'Refresh now' button.

    The view is persistent (timeout=None + button custom_id), so it survives
    bot restarts. The bot must call `bot.add_view(RefreshView())` once on
    startup so Discord can route button clicks to this callback.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh now",
        style=discord.ButtonStyle.success,
        custom_id="fountain_refresh_now",
        emoji="💧",
    )
    async def refresh_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        # 1. Log the refresh in the DB
        ts = database.log_refresh(interaction.user.id, str(interaction.user))

        # 2. Reschedule the next pre-alert / post-check from this refresh
        scheduler_cog = interaction.client.get_cog("Scheduler")
        if scheduler_cog is not None:
            scheduler_cog.reschedule_after_refresh(ts)

        # 3. Disable the button so nobody else clicks it
        button.disabled = True
        button.label = f"Refreshed by {interaction.user.display_name}"

        # 4. Edit the original alert message to show the result
        expires_at = ts + BUFF_DURATION
        await interaction.response.edit_message(
            content=(
                f"✅ Refreshed by {interaction.user.mention}. "
                f"Buff expires <t:{int(expires_at.timestamp())}:R>."
            ),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
