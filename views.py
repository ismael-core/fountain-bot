"""Persistent Discord UI views used in the ticket flow.

Three views:
- RobuxVerifyView: shown at the start. Button puts the ticket in WAITING_PROOF
  to upload the Robux balance screenshot (phase='robux').
- StartTicketView: shown after Robux is approved. Button puts the ticket in
  WAITING_PROOF to upload the refresh screenshot (phase='refresh').
- ApprovalView: shown on every proof message. The Approve callback behavior
  depends on the ticket's current phase.

All three use timeout=None and stable custom_ids so they survive bot restarts.
The bot must register them in setup_hook with bot.add_view(...).
"""
import logging
from datetime import datetime, timezone

import discord

import audit_log
import config
import database

log = logging.getLogger("fountain.views")


# ====================================================================
# Helpers
# ====================================================================

async def _mark_waiting_for_proof(
    interaction: discord.Interaction,
    proof_kind: str,
):
    """Common code for both proof-request buttons: validate the ticket and
    mark it as WAITING_PROOF, then tell the user to upload an image.

    `proof_kind` is the user-facing label ('Robux balance' or 'refresh').
    """
    ticket = database.get_ticket_by_channel(interaction.channel_id)
    if ticket is None:
        await interaction.response.send_message(
            "❌ No ticket found for this channel.",
            ephemeral=True,
        )
        return

    if interaction.user.id != ticket["user_id"]:
        await interaction.response.send_message(
            "❌ Only the ticket owner can use this button.",
            ephemeral=True,
        )
        return

    database.set_ticket_status(ticket["id"], database.TICKET_WAITING_PROOF)

    await interaction.response.send_message(
        f"📤 Now upload your **{proof_kind} screenshot** as a normal message in this channel. "
        f"I'll detect it automatically.",
        ephemeral=True,
    )


# ====================================================================
# RobuxVerifyView — shown first, asks for Robux balance proof
# ====================================================================

class RobuxVerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Send Robux proof",
        style=discord.ButtonStyle.primary,
        custom_id="fountain_send_robux",
        emoji="💰",
    )
    async def send_robux(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await _mark_waiting_for_proof(interaction, "Robux balance")


# ====================================================================
# StartTicketView — shown after Robux is approved, asks for refresh proof
# ====================================================================

class StartTicketView(discord.ui.View):
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
        await _mark_waiting_for_proof(interaction, "refresh")


# ====================================================================
# ApprovalView — Approve / Reject buttons used in both phases
# ====================================================================

class ApprovalView(discord.ui.View):
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

        if ticket["phase"] == database.TICKET_PHASE_ROBUX:
            await self._approve_robux(interaction, ticket)
        else:
            await self._approve_refresh(interaction, ticket)

    async def _approve_robux(self, interaction: discord.Interaction, ticket: dict):
        """Approve the Robux verification step and post the game link next.
        No button is attached — the user just uploads the refresh screenshot
        directly and the on_message listener picks it up."""
        # Move ticket to refresh phase, status straight to WAITING_PROOF (no button needed)
        database.set_ticket_phase(ticket["id"], database.TICKET_PHASE_REFRESH)
        database.set_ticket_status(ticket["id"], database.TICKET_WAITING_PROOF)

        # Disable this approval message
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"✅ Robux balance approved by {interaction.user.mention}. "
                f"Posting game link next."
            ),
            view=self,
        )

        # Find the user and the game link
        user = interaction.guild.get_member(ticket["user_id"])
        game_link = database.get_config("game_link") or "(no link configured — admin must run /set_link)"
        user_mention = user.mention if user else f"<@{ticket['user_id']}>"

        embed = discord.Embed(
            title="Now refresh the Fountain",
            description=(
                f"💧 {user_mention}\n\n"
                f"Your Robux balance is verified. Now do the refresh in-game.\n\n"
                f"**Game link:** {game_link}\n\n"
                f"📸 **When you've done it, upload the screenshot of the refresh "
                f"directly in this channel.** I'll detect it and forward it to the mods."
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Ticket #{ticket['id']}")

        await interaction.followup.send(
            content=user_mention,
            embed=embed,
        )

        await audit_log.log_event(
            interaction.client,
            "robux_verified",
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
        )

    async def _approve_refresh(self, interaction: discord.Interaction, ticket: dict):
        """Approve a refresh screenshot — bumps the AFK timer."""
        if ticket["refresh_count"] >= config.MAX_REFRESHES_PER_TICKET:
            await interaction.response.send_message(
                f"❌ This ticket already reached the max of "
                f"{config.MAX_REFRESHES_PER_TICKET} refreshes "
                f"({config.MAX_REFRESHES_PER_TICKET * config.AFK_HOURS_PER_REFRESH}h). "
                f"User must open a new ticket.",
                ephemeral=True,
            )
            return

        # Pull proof URL from the message (embed image or attachment fallback)
        proof_url = ""
        if interaction.message.embeds:
            embed = interaction.message.embeds[0]
            if embed.image:
                proof_url = embed.image.url
        if not proof_url and interaction.message.attachments:
            proof_url = interaction.message.attachments[0].url

        expires_at = database.approve_refresh(
            ticket["id"],
            mod_id=interaction.user.id,
            afk_hours=config.AFK_HOURS_PER_REFRESH,
        )
        is_extension = ticket["refresh_count"] >= 1

        user = interaction.guild.get_member(ticket["user_id"])
        username = str(user) if user else f"user_{ticket['user_id']}"
        database.log_refresh(
            user_id=ticket["user_id"],
            username=username,
            proof_url=proof_url,
            ticket_id=ticket["id"],
        )

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

        # Schedule reminders for the new AFK window
        membership = interaction.client.get_cog("Membership")
        if membership is not None:
            membership.schedule_for_ticket(ticket["id"])

        # Reschedule the in-game buff timer (the refresh just reset the buff to 1h)
        scheduler_cog = interaction.client.get_cog("Scheduler")
        if scheduler_cog is not None:
            scheduler_cog.reschedule_after_refresh(datetime.now(timezone.utc))

        # DM the user
        if user is not None:
            try:
                await user.send(
                    f"✅ Your refresh was approved by {interaction.user.display_name}. "
                    f"Your AFK time now expires <t:{int(expires_at.timestamp())}:R>."
                )
            except discord.DiscordException:
                pass

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

        # Always return the ticket to WAITING_PROOF so the user can retry
        database.set_ticket_status(ticket["id"], database.TICKET_WAITING_PROOF)

        for child in self.children:
            child.disabled = True

        kind = "Robux balance" if ticket["phase"] == database.TICKET_PHASE_ROBUX else "refresh"
        await interaction.response.edit_message(
            content=(
                f"❌ Rejected by {interaction.user.mention}. "
                f"Please upload a clearer **{kind} screenshot**."
            ),
            view=self,
        )

        user = interaction.guild.get_member(ticket["user_id"])
        event_type = "robux_rejected" if ticket["phase"] == database.TICKET_PHASE_ROBUX else "refresh_rejected"
        await audit_log.log_event(
            interaction.client,
            event_type,
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
        )

        if user is not None:
            try:
                await user.send(
                    f"❌ Your {kind} screenshot was rejected by {interaction.user.display_name}. "
                    f"Please upload a clearer one in the ticket."
                )
            except discord.DiscordException:
                pass
