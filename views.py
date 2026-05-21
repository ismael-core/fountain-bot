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
        """Approve the Robux verification step and post the game link next,
        with a 'Send proof' button so the user has a clear trigger to upload
        their refresh screenshot."""
        database.set_ticket_phase(ticket["id"], database.TICKET_PHASE_REFRESH)
        database.set_ticket_status(ticket["id"], database.TICKET_WAITING_PROOF)

        # Disable this approval message's buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"✅ Robux balance approved by {interaction.user.mention}. "
                f"Posting game link next."
            ),
            view=self,
        )

        user = interaction.guild.get_member(ticket["user_id"])
        game_link = database.get_config("game_link") or "(no link configured — admin must run /set_link)"
        user_mention = user.mention if user else f"<@{ticket['user_id']}>"

        embed = discord.Embed(
            title="Now refresh the Fountain",
            description=(
                f"💧 {user_mention}\n\n"
                f"Your Robux balance is verified. Now do the refresh in-game.\n\n"
                f"**Game link:** {game_link}\n\n"
                f"When you've done the refresh, tap the button below and upload the screenshot."
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Ticket #{ticket['id']}")

        await interaction.followup.send(
            content=user_mention,
            embed=embed,
            view=StartTicketView(),
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

        # After approval, replace the Approve/Reject buttons with a "Send proof"
        # button so the user can extend their AFK time later without scrolling up
        # to find the original button. Only attach it if they still have refreshes left.
        refreshes_used = ticket["refresh_count"] + 1
        if refreshes_used < config.MAX_REFRESHES_PER_TICKET:
            next_view = StartTicketView()
        else:
            next_view = None  # max reached, no more refreshes possible

        expires_ts = int(expires_at.timestamp())
        approval_text = (
            f"✅ Approved by {interaction.user.mention}.\n"
            f"⏰ Your AFK time expires on **<t:{expires_ts}:F>** (<t:{expires_ts}:R>).\n"
            f"📊 Refreshes used: **{refreshes_used}/{config.MAX_REFRESHES_PER_TICKET}**.\n"
        )
        if next_view is not None:
            approval_text += (
                f"💡 To extend before expiry, tap the button below and upload a new screenshot. "
                f"You'll get reminders 30 / 10 / 5 min before expiry."
            )
        else:
            approval_text += (
                f"⛔ This ticket reached the max refreshes. After this expires you'll need a new ticket."
            )

        await interaction.response.edit_message(
            content=approval_text,
            view=next_view,
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


# ====================================================================
# ModApplicationReviewView — for reviewing moderator applications
# ====================================================================

class ModApplicationReviewView(discord.ui.View):
    """Approve/Reject buttons shown on the final review embed for a moderator
    application. Only users with the Admin (Management) role can act on them."""

    def __init__(self):
        super().__init__(timeout=None)

    def _is_admin(self, member: discord.Member) -> bool:
        if not isinstance(member, discord.Member):
            return False
        return any(r.id == config.ADMIN_ROLE_ID for r in member.roles)

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="modapp_approve",
        emoji="✅",
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only Management can review moderator applications.",
                ephemeral=True,
            )
            return

        app = database.get_mod_application_by_channel(interaction.channel_id)
        if app is None:
            await interaction.response.send_message(
                "❌ No application found for this channel.", ephemeral=True
            )
            return

        if app["status"] in (database.MOD_APP_APPROVED, database.MOD_APP_REJECTED):
            await interaction.response.send_message(
                f"⚠️ This application was already reviewed (status: {app['status']}).",
                ephemeral=True,
            )
            return

        database.review_mod_application(app["id"], interaction.user.id, approved=True)

        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Application approved by {interaction.user.mention}.",
            view=self,
        )

        user = interaction.guild.get_member(app["user_id"])
        if user is not None:
            try:
                await user.send(
                    "✅ Your moderator application has been **approved**! "
                    "An admin will reach out shortly with next steps."
                )
            except discord.DiscordException:
                pass

        await audit_log.log_event(
            interaction.client,
            "mod_app_approved",
            user=user,
            mod=interaction.user,
            ticket_id=app["id"],  # reusing ticket_id col for app id
        )

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        custom_id="modapp_reject",
        emoji="❌",
    )
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Only Management can review moderator applications.",
                ephemeral=True,
            )
            return

        app = database.get_mod_application_by_channel(interaction.channel_id)
        if app is None:
            await interaction.response.send_message(
                "❌ No application found for this channel.", ephemeral=True
            )
            return

        if app["status"] in (database.MOD_APP_APPROVED, database.MOD_APP_REJECTED):
            await interaction.response.send_message(
                f"⚠️ This application was already reviewed (status: {app['status']}).",
                ephemeral=True,
            )
            return

        database.review_mod_application(app["id"], interaction.user.id, approved=False)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"❌ Application rejected by {interaction.user.mention}. Closing ticket in 30 seconds.",
            view=self,
        )

        user = interaction.guild.get_member(app["user_id"])
        if user is not None:
            try:
                await user.send(
                    "❌ Your moderator application was not accepted at this time. "
                    "Thanks for your interest! You're welcome to apply again in the future."
                )
            except discord.DiscordException:
                pass

        await audit_log.log_event(
            interaction.client,
            "mod_app_rejected",
            user=user,
            mod=interaction.user,
            ticket_id=app["id"],
        )

        # Wait 30s then delete the channel
        import asyncio
        await asyncio.sleep(30)
        try:
            await interaction.channel.delete(reason=f"Mod application rejected by {interaction.user}")
        except discord.DiscordException:
            log.exception("Failed to delete mod application channel %s", interaction.channel_id)
