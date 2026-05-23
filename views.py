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
            # Refresh-phase: open modal first to capture buff time the mod saw in the photo,
            # then run the approval with that value. The modal calls back into _approve_refresh.
            await interaction.response.send_modal(
                BuffTimeModal(parent_view=self, ticket=ticket, proof_message=interaction.message)
            )

    async def _approve_robux(self, interaction: discord.Interaction, ticket: dict):
        """Approve Robux verification. If the Fountain is full, hold the link until
        the user's turn comes (active_ping). If the Fountain is low/empty, post the
        link immediately so they can act now."""
        from datetime import datetime, timezone

        database.set_ticket_phase(ticket["id"], database.TICKET_PHASE_REFRESH)
        database.set_ticket_status(ticket["id"], database.TICKET_WAITING_PROOF)

        # Disable this approval message's buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Robux balance approved by {interaction.user.mention}.",
            view=self,
        )

        user = interaction.guild.get_member(ticket["user_id"])
        user_mention = user.mention if user else f"<@{ticket['user_id']}>"

        # Decide: hold the link (fountain full) or post it now (fountain low)
        expires_at = database.get_buff_expires_at()
        now = datetime.now(timezone.utc)
        minutes_left = ((expires_at - now).total_seconds() / 60) if expires_at else 0
        fountain_is_full = expires_at is not None and minutes_left > config.QUEUE_ACTIVE_PING_MINUTES

        if fountain_is_full:
            # Hold the link — they'll get it when active_ping fires for their turn
            embed = discord.Embed(
                title="Robux verified — you're in the queue",
                description=(
                    f"Thanks {user_mention}, your Robux balance is verified.\n\n"
                    f"The Fountain is currently topped up "
                    f"(buff drops <t:{int(expires_at.timestamp())}:R>).\n"
                    f"**Please wait** — the bot will send you the game link in this channel "
                    f"when it's your turn, about **{config.QUEUE_PRE_WARN_MINUTES + config.QUEUE_ACTIVE_PING_MINUTES} "
                    f"minutes** before the buff drops.\n\n"
                    f"Stay around the Discord so you don't miss the ping. Thanks for helping keep "
                    f"the Fountain alive 💧"
                ),
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"Ticket #{ticket['id']}")
            await interaction.followup.send(content=user_mention, embed=embed)
        else:
            # Fountain low or no buff state → post link immediately so user can act now
            game_link = database.get_config("game_link") or "(no link configured — admin must run /set_link)"
            embed = discord.Embed(
                title="Now refresh the Fountain",
                description=(
                    f"💧 {user_mention}\n\n"
                    f"Your Robux balance is verified, and the Fountain needs a refresh now.\n\n"
                    f"**Game link:** {game_link}\n\n"
                    f"When you've done the refresh, tap the button below and upload the screenshot."
                ),
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"Ticket #{ticket['id']}")
            msg = await interaction.followup.send(
                content=user_mention,
                embed=embed,
                view=StartTicketView(),
            )
            try:
                await msg.pin(reason="Pinning game-link message for ticket")
            except discord.DiscordException:
                log.exception("Failed to pin refresh message in %s", interaction.channel_id)

        await audit_log.log_event(
            interaction.client,
            "robux_verified",
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
            details={
                "fountain_was_full": fountain_is_full,
                "minutes_left_in_buff": int(minutes_left) if expires_at else None,
            },
        )

    async def _approve_refresh(self, interaction: discord.Interaction, ticket: dict, buff_minutes: int, proof_message: discord.Message):
        """Approve a refresh screenshot — bumps the AFK timer AND registers the new buff state.

        Called from BuffTimeModal.on_submit. interaction is the modal's interaction.
        proof_message is the message that has the proof embed/attachment (passed through by the modal).
        buff_minutes is what the mod typed in the modal (how much time the Fountain has now).
        """
        if ticket["refresh_count"] >= config.MAX_REFRESHES_PER_TICKET:
            await interaction.response.send_message(
                f"❌ This ticket already reached the max of "
                f"{config.MAX_REFRESHES_PER_TICKET} refreshes "
                f"({config.MAX_REFRESHES_PER_TICKET * config.AFK_HOURS_PER_REFRESH}h). "
                f"User must open a new ticket.",
                ephemeral=True,
            )
            return

        # Pull proof URL from the proof_message (embed image or attachment fallback)
        proof_url = ""
        if proof_message.embeds:
            embed = proof_message.embeds[0]
            if embed.image:
                proof_url = embed.image.url
        if not proof_url and proof_message.attachments:
            proof_url = proof_message.attachments[0].url

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

        refreshes_used = ticket["refresh_count"] + 1
        if refreshes_used < config.MAX_REFRESHES_PER_TICKET:
            next_view = StartTicketView()
        else:
            next_view = None

        expires_ts = int(expires_at.timestamp())
        approval_text = (
            f"✅ Approved by {interaction.user.mention}.\n"
            f"⏰ Your AFK time expires on **<t:{expires_ts}:F>** (<t:{expires_ts}:R>).\n"
            f"📊 Refreshes used: **{refreshes_used}/{config.MAX_REFRESHES_PER_TICKET}**.\n"
            f"💧 Fountain buff registered: **{buff_minutes} min remaining**.\n"
        )
        if next_view is not None:
            approval_text += (
                f"💡 To extend before expiry, tap the button below and upload a new screenshot."
            )
        else:
            approval_text += (
                f"⛔ This ticket reached the max refreshes. After this expires you'll need a new ticket."
            )

        # Edit the proof message (not the modal interaction)
        try:
            await proof_message.edit(content=approval_text, view=next_view)
        except discord.DiscordException:
            pass
        # Acknowledge the modal so it closes cleanly
        try:
            await interaction.response.send_message(
                f"✅ Approval saved. Buff timer set to {buff_minutes} min.",
                ephemeral=True,
            )
        except discord.DiscordException:
            pass

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
                "buff_minutes_remaining": buff_minutes,
            },
        )

        # Schedule reminders for the new AFK window
        membership = interaction.client.get_cog("Membership")
        if membership is not None:
            membership.schedule_for_ticket(ticket["id"])

        # Register the new buff state and re-arm queue alerts
        refresh_queue_cog = interaction.client.get_cog("RefreshQueue")
        if refresh_queue_cog is not None:
            refresh_queue_cog.set_buff_state(buff_minutes)

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


# ====================================================================
# WRR (Weather Rolls) Views
# ====================================================================


def _wrr_minutes_for_amount(amount: int) -> int:
    """1 WRR = 0.6 min, so 100 WRR = 60 min. Amount must be multiple of 50."""
    return amount * 60 // 100


def _format_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


async def _set_wrr_tier_and_ask_balance(interaction: discord.Interaction, amount: int):
    """Shared logic: validate ticket state, set tier, ask for balance screenshot."""
    import database  # local import to avoid cycle issues
    ticket = database.get_wrr_ticket_by_channel(interaction.channel_id)
    if ticket is None:
        await interaction.response.send_message("❌ No WRR ticket found in this channel.", ephemeral=True)
        return
    if interaction.user.id != ticket["user_id"]:
        await interaction.response.send_message("❌ Only the ticket owner can pick a tier.", ephemeral=True)
        return
    if ticket["status"] != database.WRR_STATUS_WAITING_TIER:
        await interaction.response.send_message(
            f"⚠️ A tier was already selected for this ticket.",
            ephemeral=True,
        )
        return

    minutes = _wrr_minutes_for_amount(amount)
    database.set_wrr_ticket_tier(ticket["id"], minutes)
    database.set_wrr_ticket_status(ticket["id"], database.WRR_STATUS_WAITING_BALANCE)

    duration_str = _format_duration(minutes)
    embed = discord.Embed(
        title=f"Selected: {amount} WRR → {duration_str} of access",
        description=(
            f"📸 {interaction.user.mention}, now upload a **screenshot of your "
            f"current WRR balance** showing **at least {amount} WRR**.\n\n"
            f"A mod will check the screenshot. If it matches, you'll get the "
            f"game link to use your WRR."
        ),
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"WRR Ticket #{ticket['id']}")

    if not interaction.response.is_done():
        await interaction.response.send_message(content=interaction.user.mention, embed=embed)
    else:
        await interaction.followup.send(content=interaction.user.mention, embed=embed)

    await audit_log.log_event(
        interaction.client,
        "wrr_tier_selected",
        user=interaction.user,
        ticket_id=ticket["id"],
        details={"amount": amount, "minutes": minutes},
    )


class WRRCustomAmountModal(discord.ui.Modal, title="Custom WRR amount"):
    amount = discord.ui.TextInput(
        label="How much WRR will you use?",
        placeholder="e.g., 550 — must be a multiple of 50, minimum 50",
        min_length=2,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.strip()
        try:
            amount = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "❌ That's not a valid number. Enter only digits, e.g., `550`.",
                ephemeral=True,
            )
            return

        if amount < 50:
            await interaction.response.send_message(
                "❌ Minimum is **50 WRR**.",
                ephemeral=True,
            )
            return

        if amount % 50 != 0:
            await interaction.response.send_message(
                f"❌ Must be a multiple of **50** (e.g., 450, 500, 550). You entered {amount}.",
                ephemeral=True,
            )
            return

        await _set_wrr_tier_and_ask_balance(interaction, amount)


class WRRTierSelectView(discord.ui.View):
    """Buttons for picking a WRR tier. 8 fixed tiers + custom amount."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="50 WRR (30m)", style=discord.ButtonStyle.secondary, custom_id="wrr_tier_50", row=0)
    async def t50(self, interaction, button):
        await _set_wrr_tier_and_ask_balance(interaction, 50)

    @discord.ui.button(label="100 WRR (1h)", style=discord.ButtonStyle.secondary, custom_id="wrr_tier_100", row=0)
    async def t100(self, interaction, button):
        await _set_wrr_tier_and_ask_balance(interaction, 100)

    @discord.ui.button(label="150 WRR (1h30)", style=discord.ButtonStyle.secondary, custom_id="wrr_tier_150", row=0)
    async def t150(self, interaction, button):
        await _set_wrr_tier_and_ask_balance(interaction, 150)

    @discord.ui.button(label="200 WRR (2h)", style=discord.ButtonStyle.secondary, custom_id="wrr_tier_200", row=0)
    async def t200(self, interaction, button):
        await _set_wrr_tier_and_ask_balance(interaction, 200)

    @discord.ui.button(label="250 WRR (2h30)", style=discord.ButtonStyle.secondary, custom_id="wrr_tier_250", row=1)
    async def t250(self, interaction, button):
        await _set_wrr_tier_and_ask_balance(interaction, 250)

    @discord.ui.button(label="300 WRR (3h)", style=discord.ButtonStyle.secondary, custom_id="wrr_tier_300", row=1)
    async def t300(self, interaction, button):
        await _set_wrr_tier_and_ask_balance(interaction, 300)

    @discord.ui.button(label="350 WRR (3h30)", style=discord.ButtonStyle.secondary, custom_id="wrr_tier_350", row=1)
    async def t350(self, interaction, button):
        await _set_wrr_tier_and_ask_balance(interaction, 350)

    @discord.ui.button(label="400 WRR (4h)", style=discord.ButtonStyle.secondary, custom_id="wrr_tier_400", row=1)
    async def t400(self, interaction, button):
        await _set_wrr_tier_and_ask_balance(interaction, 400)

    @discord.ui.button(
        label="💬 Other amount",
        style=discord.ButtonStyle.primary,
        custom_id="wrr_tier_custom",
        row=2,
    )
    async def custom(self, interaction, button):
        import database
        ticket = database.get_wrr_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("❌ No WRR ticket here.", ephemeral=True)
            return
        if interaction.user.id != ticket["user_id"]:
            await interaction.response.send_message("❌ Only the ticket owner.", ephemeral=True)
            return
        if ticket["status"] != database.WRR_STATUS_WAITING_TIER:
            await interaction.response.send_message("⚠️ Tier already selected.", ephemeral=True)
            return
        await interaction.response.send_modal(WRRCustomAmountModal())


class WRRApprovalView(discord.ui.View):
    """Approve/Reject for WRR proof screenshots. Handles both balance and usage phases."""

    def __init__(self):
        super().__init__(timeout=None)

    def _is_mod(self, member) -> bool:
        if not isinstance(member, discord.Member):
            return False
        return any(r.id == config.MOD_ROLE_ID for r in member.roles)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="wrr_approve", emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        import database
        if not self._is_mod(interaction.user):
            await interaction.response.send_message("❌ Only mods can approve.", ephemeral=True)
            return

        ticket = database.get_wrr_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("❌ No WRR ticket here.", ephemeral=True)
            return

        if ticket["status"] == database.WRR_STATUS_PENDING_BALANCE:
            await self._approve_balance(interaction, ticket)
        elif ticket["status"] == database.WRR_STATUS_PENDING_USAGE:
            await self._approve_usage(interaction, ticket)
        else:
            await interaction.response.send_message(
                f"⚠️ Ticket isn't pending review (status: `{ticket['status']}`).",
                ephemeral=True,
            )

    async def _approve_balance(self, interaction, ticket):
        import database
        database.set_wrr_ticket_status(ticket["id"], database.WRR_STATUS_WAITING_USAGE)

        # Disable buttons on the proof message
        for child in self.children:
            child.disabled = True

        # Get game link (same as Fountain refresh — single shared config)
        game_link = database.get_config("game_link") or "(no link configured — admins set with /set_link)"

        user = interaction.guild.get_member(ticket["user_id"]) if interaction.guild else None
        user_mention = user.mention if user else f"<@{ticket['user_id']}>"

        await interaction.response.edit_message(
            content=f"✅ Balance approved by {interaction.user.mention}. Posting link...",
            view=self,
        )

        # Post the game link + usage instructions
        amount_used = ticket["tier_minutes"] * 100 // 60
        embed = discord.Embed(
            title="Now use your WRR in-game",
            description=(
                f"💧 **Game link:** {game_link}\n\n"
                f"You have **3 minutes** to:\n"
                f"1. Join the game\n"
                f"2. Use your {amount_used} WRR\n"
                f"3. Upload a screenshot showing the WRR consumed + the game chat visible\n\n"
                f"⚠️ If you don't upload the usage screenshot in 3 min, the ticket auto-cancels."
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"WRR Ticket #{ticket['id']}")
        link_msg = await interaction.followup.send(content=user_mention, embed=embed)
        try:
            await link_msg.pin(reason="WRR game link for active ticket")
        except discord.DiscordException:
            pass

        # Schedule 3-min timeout
        wrr_cog = interaction.client.get_cog("WRR")
        if wrr_cog:
            wrr_cog.schedule_usage_timeout(ticket["id"])

        await audit_log.log_event(
            interaction.client,
            "wrr_approved",
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
            details={"phase": "balance"},
        )

    async def _approve_usage(self, interaction, ticket):
        import database
        tier_minutes = ticket["tier_minutes"] or 30
        expires_at = database.approve_wrr_usage(ticket["id"], interaction.user.id, tier_minutes)

        for child in self.children:
            child.disabled = True

        expires_ts = int(expires_at.timestamp())
        amount_used = tier_minutes * 100 // 60
        duration_str = _format_duration(tier_minutes)

        await interaction.response.edit_message(
            content=(
                f"✅ Approved by {interaction.user.mention}.\n"
                f"⏰ WRR access (**{amount_used} WRR / {duration_str}**) active until "
                f"<t:{expires_ts}:F> (<t:{expires_ts}:R>).\n"
                f"Reminders at 10 min and 5 min before expiry."
            ),
            view=self,
        )

        wrr_cog = interaction.client.get_cog("WRR")
        if wrr_cog:
            wrr_cog.schedule_active_ticket(ticket["id"])

        user = interaction.guild.get_member(ticket["user_id"]) if interaction.guild else None
        if user:
            try:
                await user.send(
                    f"✅ Your WRR access is active for **{duration_str}**. "
                    f"Expires <t:{expires_ts}:R>. You'll be reminded before the timer runs out."
                )
            except discord.DiscordException:
                pass

        await audit_log.log_event(
            interaction.client,
            "wrr_approved",
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
            details={
                "phase": "usage",
                "tier_minutes": tier_minutes,
                "amount": amount_used,
                "expires_at": expires_at.isoformat(),
            },
        )

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="wrr_reject", emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        import database
        if not self._is_mod(interaction.user):
            await interaction.response.send_message("❌ Only mods can reject.", ephemeral=True)
            return

        ticket = database.get_wrr_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("❌ No WRR ticket here.", ephemeral=True)
            return

        if ticket["status"] == database.WRR_STATUS_PENDING_BALANCE:
            new_status = database.WRR_STATUS_WAITING_BALANCE
            kind = "balance"
        elif ticket["status"] == database.WRR_STATUS_PENDING_USAGE:
            new_status = database.WRR_STATUS_WAITING_USAGE
            kind = "usage"
        else:
            await interaction.response.send_message(
                f"⚠️ Ticket isn't pending review (status: `{ticket['status']}`).",
                ephemeral=True,
            )
            return

        database.set_wrr_ticket_status(ticket["id"], new_status)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"❌ {kind.capitalize()} proof rejected by {interaction.user.mention}. "
                f"Upload a clearer screenshot."
            ),
            view=self,
        )

        user = interaction.guild.get_member(ticket["user_id"]) if interaction.guild else None
        if user:
            try:
                await user.send(
                    f"❌ Your WRR {kind} screenshot was rejected. "
                    f"Upload a clearer one in the ticket."
                )
            except discord.DiscordException:
                pass

        await audit_log.log_event(
            interaction.client,
            "wrr_rejected",
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
            details={"phase": kind},
        )


class WRRBlacklistConfirmView(discord.ui.View):
    """Shown in #fountain-logs after a WRR ticket expires.
    Mods press the button if the user didn't leave the game → applies blacklist."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="User didn't leave → Blacklist",
        style=discord.ButtonStyle.danger,
        custom_id="wrr_blacklist_confirm",
        emoji="⛔",
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        import re
        import database

        if not isinstance(interaction.user, discord.Member) or not any(
            r.id == config.MOD_ROLE_ID for r in interaction.user.roles
        ):
            await interaction.response.send_message("❌ Only mods can apply this.", ephemeral=True)
            return

        # Parse ticket ID from embed footer
        ticket_id = None
        if interaction.message and interaction.message.embeds:
            footer = interaction.message.embeds[0].footer
            if footer and footer.text:
                m = re.search(r"WRR Ticket #(\d+)", footer.text)
                if m:
                    ticket_id = int(m.group(1))
        if ticket_id is None:
            await interaction.response.send_message("❌ Could not identify the ticket.", ephemeral=True)
            return

        ticket = database.get_wrr_ticket(ticket_id)
        if ticket is None:
            await interaction.response.send_message("❌ Ticket not found in DB.", ephemeral=True)
            return

        # Protect staff / owner
        wrr_cog = interaction.client.get_cog("WRR")
        if wrr_cog and await wrr_cog._is_protected(ticket["user_id"]):
            await interaction.response.send_message(
                "⚠️ This user has a protected role. Blacklist skipped.",
                ephemeral=True,
            )
            return

        entry = database.add_blacklist_strike(
            ticket["user_id"],
            config.BLACKLIST_DURATIONS_HOURS,
            reason="Did not leave game after WRR access expired",
        )

        for child in self.children:
            child.disabled = True

        if entry["banned_until"] is None:
            until_str = "permanently"
        else:
            until_ts = int(entry["banned_until"].timestamp())
            until_str = f"until <t:{until_ts}:F>"

        await interaction.response.edit_message(
            content=(
                f"⛔ Blacklist applied by {interaction.user.mention} — "
                f"strike #{entry['strike_count']} {until_str}."
            ),
            view=self,
        )

        user = None
        if interaction.guild:
            user = interaction.guild.get_member(ticket["user_id"])
        if user:
            try:
                await user.send(
                    f"⛔ You've been blacklisted {until_str} (strike #{entry['strike_count']}) "
                    f"for not leaving the game after your WRR access expired."
                )
            except discord.DiscordException:
                pass

        await audit_log.log_event(
            interaction.client,
            "wrr_blacklisted",
            user=user,
            mod=interaction.user,
            ticket_id=ticket["id"],
            details={
                "strike_count": entry["strike_count"],
                "banned_until": entry["banned_until"].isoformat() if entry["banned_until"] else "permanent",
            },
        )


class BuffTimeModal(discord.ui.Modal, title="Buff time remaining"):
    """Mod fills in how much time the Fountain has after this refresh, taken from the screenshot."""

    buff_time = discord.ui.TextInput(
        label="How much buff time? (read from the photo)",
        placeholder="e.g. 59m, 1h, 1h30m, 55m32s, 90m",
        default=f"{config.BUFF_DURATION_HOURS}h",
        min_length=2,
        max_length=15,
        required=True,
    )

    def __init__(self, parent_view, ticket, proof_message):
        super().__init__()
        self.parent_view = parent_view
        self.ticket = ticket
        self.proof_message = proof_message

    async def on_submit(self, interaction: discord.Interaction):
        # Import here to avoid circular import
        from cogs.refresh_queue import parse_time_to_minutes

        minutes = parse_time_to_minutes(self.buff_time.value)
        if minutes is None or minutes <= 0:
            await interaction.response.send_message(
                "❌ Invalid format. Try `30m`, `1h`, `1h30m`, `90m`, or `55m32s`.",
                ephemeral=True,
            )
            return

        await self.parent_view._approve_refresh(
            interaction=interaction,
            ticket=self.ticket,
            buff_minutes=minutes,
            proof_message=self.proof_message,
        )
