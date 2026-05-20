"""Persistent Discord UI views used in the ticket flow.

Two views:
- StartTicketView: shown when `/start_ticket` runs. Has one button ("Send proof")
  that puts the ticket in WAITING_PROOF status.
- ApprovalView: shown on the proof message. Has two buttons (Approve / Reject)
  that only users with the MOD_ROLE can use.

Both views use timeout=None and stable custom_ids so they survive bot restarts.
The bot must register them in setup_hook with bot.add_view(...).
"""
import logging

import discord

import audit_log
import config
import database

log = logging.getLogger("fountain.views")


# ====================================================================
# StartTicketView — shown in the ticket channel after /start_ticket
# ====================================================================

class StartTicketView(discord.ui.View):
    """View with a single 'Send proof' button. Persistent across restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Send proof",
        style=discord.ButtonStyle.primary,
        custom_id="fountain_send_proof",
        emoji="📸",
    )
    async def send_proof(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        # Find the ticket for this channel
        ticket = database.get_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                "❌ No ticket found for this channel.",
                ephemeral=True,
            )
            return

        # Only the ticket owner can click this button
        if interaction.user.id != ticket["user_id"]:
            await interaction.response.send_message(
                "❌ Only the ticket owner can use this button.",
                ephemeral=True,
            )
            return

        # Mark the ticket as waiting for proof
        database.set_ticket_status(ticket["id"], database.TICKET_WAITING_PROOF)

        await interaction.response.send_message(
            "📤 Now upload your screenshot as a normal message in this channel. "
            "Make sure the in-game time is visible. I'll detect it automatically.",
            ephemeral=True,
        )


# ====================================================================
# ApprovalView — shown on the proof message for mods to approve/reject
# ====================================================================

class ApprovalView(discord.ui.View):
    """View with Approve / Reject buttons. Only MOD_ROLE_ID can use them."""

    def __init__(self):
        super().__init__(timeout=None)

    def _is_mod(self, member: discord.Member) -> bool:
        if member is None or not isinstance(member, discord.Member):
            return False
        return any(r.id == config.MOD_ROLE_ID for r in member.roles)

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="fountain_approve",
        emoji="✅",
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not self._is_mod(interaction.user):
            await interaction.response.send_message(
                "❌ Only mods can approve.", ephemeral=True
            )
            return

        ticket = database.get_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                "❌ No ticket found for this channel.", ephemeral=True
            )
            return

        # Cap at MAX_REFRESHES_PER_TICKET
        if ticket["refresh_count"] >= config.MAX_REFRESHES_PER_TICKET:
            await interaction.response.send_message(
                f"❌ This ticket already reached the max of "
                f"{config.MAX_REFRESHES_PER_TICKET} refreshes "
                f"({config.MAX_REFRESHES_PER_TICKET * config.AFK_HOURS_PER_REFRESH}h). "
                f"User must open a new ticket.",
                ephemeral=True,
            )
            return

        # Find the proof URL from the original message (first attachment in the proof message)
        proof_url = ""
        if interaction.message.embeds:
            embed = interaction.message.embeds[0]
            if embed.image:
                proof_url = embed.image.url
        if not proof_url and interaction.message.attachments:
            proof_url = interaction.message.attachments[0].url

        # Approve: bump count, set new expires_at
        expires_at = database.approve_refresh(
            ticket["id"],
            mod_id=interaction.user.id,
            afk_hours=config.AFK_HOURS_PER_REFRESH,
        )
        is_extension = ticket["refresh_count"] >= 1

        # Log the refresh in the refreshes table for leaderboard/stats
        user = interaction.guild.get_member(ticket["user_id"])
        username = str(user) if user else f"user_{ticket['user_id']}"
        database.log_refresh(
            user_id=ticket["user_id"],
            username=username,
            proof_url=proof_url,
            ticket_id=ticket["id"],
        )

        # Disable the buttons on this message
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content=(
                f"✅ Approved by {interaction.user.mention}. "
                f"AFK time now expires <t:{int(expires_at.timestamp())}:R> "
                f"({ticket['refresh_count'] + 1}/{config.MAX_REFRESHES_PER_TICKET} refreshes used)."
            ),
            view=self,
        )

        # Audit log
        event_type = "refresh_extended" if is_extension else "refresh_approved"
        await audit_log.log_event(
            interaction.client,
            event_type,
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
            details={
                "refresh_count": ticket["refresh_count"] + 1,
                "expires_at": expires_at.isoformat(),
                "proof_url": proof_url or "(none)",
            },
        )

        # Reschedule the membership reminders for this ticket (AFK timer, expiry, blacklist)
        membership = interaction.client.get_cog("Membership")
        if membership is not None:
            membership.schedule_for_ticket(ticket["id"])

        # Reschedule the in-game buff timer (1h pre_alert/post_check pings)
        # because every approved refresh resets the buff to 1h in-game
        scheduler_cog = interaction.client.get_cog("Scheduler")
        if scheduler_cog is not None:
            from datetime import datetime, timezone
            scheduler_cog.reschedule_after_refresh(datetime.now(timezone.utc))

        # DM the user
        if user is not None:
            try:
                await user.send(
                    f"✅ Your refresh was approved by {interaction.user.display_name}. "
                    f"Your AFK time now expires <t:{int(expires_at.timestamp())}:R>."
                )
            except discord.DiscordException:
                pass  # User has DMs closed; not a hard failure

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        custom_id="fountain_reject",
        emoji="❌",
    )
    async def reject(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not self._is_mod(interaction.user):
            await interaction.response.send_message(
                "❌ Only mods can reject.", ephemeral=True
            )
            return

        ticket = database.get_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                "❌ No ticket found for this channel.", ephemeral=True
            )
            return

        # Revert ticket back to WAITING_PROOF so the user can try again
        database.set_ticket_status(ticket["id"], database.TICKET_WAITING_PROOF)

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            content=(
                f"❌ Rejected by {interaction.user.mention}. "
                f"Please upload a clearer screenshot showing the refresh and the in-game time."
            ),
            view=self,
        )

        user = interaction.guild.get_member(ticket["user_id"])
        await audit_log.log_event(
            interaction.client,
            "refresh_rejected",
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
        )

        if user is not None:
            try:
                await user.send(
                    f"❌ Your screenshot was rejected by {interaction.user.display_name}. "
                    f"Please upload a clearer one in the ticket."
                )
            except discord.DiscordException:
                pass
