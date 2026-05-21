"""Ticket flow: auto-detect new tickets + /start_ticket fallback + on_message listener."""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import audit_log
import config
import database
from views import ApprovalView, StartTicketView

log = logging.getLogger("fountain.tickets")


# ====================================================================
# Helpers
# ====================================================================

def _in_ticket_category(channel: discord.abc.GuildChannel) -> bool:
    """True if the given channel is inside the configured ticket category."""
    if not isinstance(channel, discord.TextChannel):
        return False
    return channel.category is not None and channel.category.id == config.TICKET_CATEGORY_ID


def _has_mod_role(member: discord.Member) -> bool:
    return any(r.id == config.MOD_ROLE_ID for r in member.roles)


def _find_ticket_owner(channel: discord.TextChannel) -> discord.Member | None:
    """Identify the ticket owner from the channel's permission overwrites.
    Ticket Tool adds the user who clicked 'Create ticket' as an explicit Member
    overwrite with View Channel = True. We pick the first non-bot Member that
    matches that pattern.
    """
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Member):
            continue
        if target.bot:
            continue
        if overwrite.view_channel is True:
            return target
    return None


def _build_robux_embed(owner: discord.Member, ticket_id: int) -> discord.Embed:
    return discord.Embed(
        title="Verify your Robux balance",
        description=(
            f"👋 Hi {owner.mention}\n\n"
            f"Before we share the game link, we need to verify you have enough "
            f"Robux for the refresh.\n\n"
            f"📸 **Upload a screenshot of your current Robux balance directly in this channel.** "
            f"I'll detect it automatically and forward it to the mods for review."
        ),
        color=discord.Color.blue(),
    ).set_footer(text=f"Ticket #{ticket_id}")


# ====================================================================
# Cog
# ====================================================================

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------------------------------------------------------
    # on_guild_channel_create — auto-start the flow when a user creates a ticket
    # ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not isinstance(channel, discord.TextChannel):
            return
        if channel.category is None or channel.category.id != config.TICKET_CATEGORY_ID:
            return

        # Give Ticket Tool ~3s to set up perms and post its welcome message,
        # otherwise overwrites might still be empty when we read them.
        await asyncio.sleep(3)
        channel = self.bot.get_channel(channel.id) or channel

        # Skip if a ticket already exists for this channel (e.g. mod ran /start_ticket faster)
        if database.get_ticket_by_channel(channel.id) is not None:
            return

        owner = _find_ticket_owner(channel)
        if owner is None:
            log.warning("Could not auto-detect owner for new ticket channel %s", channel.id)
            return

        # If blacklisted, post a warning but don't start the flow
        if database.is_blacklisted(owner.id):
            entry = database.get_blacklist_entry(owner.id)
            until = "permanently" if entry["banned_until"] is None else f"until <t:{int(entry['banned_until'].timestamp())}:F>"
            try:
                await channel.send(
                    f"⛔ {owner.mention} is currently blacklisted {until} (strike {entry['strike_count']}). "
                    f"<@&{config.MOD_ROLE_ID}> please review.",
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False),
                )
            except discord.DiscordException:
                log.exception("Failed to post blacklist notice in new ticket")
            return

        # No game link configured yet — tell the user, don't auto-start
        if not database.get_config("game_link"):
            try:
                await channel.send(
                    f"⚠️ No game link is configured yet. An admin needs to run `/set_link` first. "
                    f"After that a mod can run `/start_ticket user:{owner.mention}` to begin manually.",
                    allowed_mentions=discord.AllowedMentions(users=False),
                )
            except discord.DiscordException:
                pass
            return

        # Create the ticket record and go straight into WAITING_PROOF (no button needed)
        ticket_id = database.create_ticket(owner.id, channel.id)
        database.set_ticket_status(ticket_id, database.TICKET_WAITING_PROOF)

        try:
            await channel.send(
                content=owner.mention,
                embed=_build_robux_embed(owner, ticket_id),
            )
        except discord.DiscordException:
            log.exception("Failed to post Robux verification embed in %s", channel.id)
            return

        await audit_log.log_event(
            self.bot,
            "ticket_started",
            user=owner,
            ticket_id=ticket_id,
            details={"channel_id": channel.id, "phase": "robux", "auto": True},
        )

    # ----------------------------------------------------------------
    # /start_ticket — manual fallback if auto-detect missed it
    # ----------------------------------------------------------------

    @app_commands.command(
        name="start_ticket",
        description="Manually start the refresh flow (fallback if auto-detect missed the ticket)",
    )
    @app_commands.describe(user="The user who needs to refresh")
    async def start_ticket(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        if not _in_ticket_category(interaction.channel):
            await interaction.response.send_message(
                "❌ This command only works inside a ticket channel.",
                ephemeral=True,
            )
            return

        if not _has_mod_role(interaction.user):
            await interaction.response.send_message(
                "❌ Only mods can start the ticket flow.",
                ephemeral=True,
            )
            return

        if database.is_blacklisted(user.id):
            entry = database.get_blacklist_entry(user.id)
            until = "permanently" if entry["banned_until"] is None else f"until <t:{int(entry['banned_until'].timestamp())}:F>"
            await interaction.response.send_message(
                f"❌ {user.mention} is blacklisted {until} (strike {entry['strike_count']}).",
                ephemeral=True,
            )
            return

        if not database.get_config("game_link"):
            await interaction.response.send_message(
                f"❌ No game link configured. An admin needs to run `/set_link` "
                f"in <#{config.MANAGEMENT_CHANNEL_ID}> first.",
                ephemeral=True,
            )
            return

        existing = database.get_ticket_by_channel(interaction.channel_id)
        if existing and existing["status"] != database.TICKET_CLOSED:
            await interaction.response.send_message(
                f"⚠️ This channel already has an active ticket (#{existing['id']}, status: {existing['status']}). "
                f"Close it first if you want to start over.",
                ephemeral=True,
            )
            return

        ticket_id = database.create_ticket(user.id, interaction.channel_id)
        database.set_ticket_status(ticket_id, database.TICKET_WAITING_PROOF)

        await interaction.response.send_message(
            content=user.mention,
            embed=_build_robux_embed(user, ticket_id),
        )

        await audit_log.log_event(
            self.bot,
            "ticket_started",
            user=user,
            mod=interaction.user,
            ticket_id=ticket_id,
            details={"channel_id": interaction.channel_id, "phase": "robux", "auto": False},
        )

    # ----------------------------------------------------------------
    # /start_refresh — for OLD tickets where Robux verification doesn't apply
    # ----------------------------------------------------------------

    @app_commands.command(
        name="start_refresh",
        description="Start the refresh flow directly, skipping Robux verification (for old tickets)",
    )
    @app_commands.describe(user="The user who needs to refresh")
    async def start_refresh(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        if not _in_ticket_category(interaction.channel):
            await interaction.response.send_message(
                "❌ This command only works inside a ticket channel.",
                ephemeral=True,
            )
            return

        if not _has_mod_role(interaction.user):
            await interaction.response.send_message(
                "❌ Only mods can start the refresh flow.",
                ephemeral=True,
            )
            return

        if database.is_blacklisted(user.id):
            entry = database.get_blacklist_entry(user.id)
            until = "permanently" if entry["banned_until"] is None else f"until <t:{int(entry['banned_until'].timestamp())}:F>"
            await interaction.response.send_message(
                f"❌ {user.mention} is blacklisted {until} (strike {entry['strike_count']}).",
                ephemeral=True,
            )
            return

        game_link = database.get_config("game_link")
        if not game_link:
            await interaction.response.send_message(
                f"❌ No game link configured. An admin needs to run `/set_link` "
                f"in <#{config.MANAGEMENT_CHANNEL_ID}> first.",
                ephemeral=True,
            )
            return

        existing = database.get_ticket_by_channel(interaction.channel_id)
        if existing and existing["status"] != database.TICKET_CLOSED:
            await interaction.response.send_message(
                f"⚠️ This channel already has an active ticket (#{existing['id']}, status: {existing['status']}). "
                f"Close it first if you want to start over.",
                ephemeral=True,
            )
            return

        # Create the ticket directly in refresh phase + WAITING_PROOF status
        ticket_id = database.create_ticket(user.id, interaction.channel_id)
        database.set_ticket_phase(ticket_id, database.TICKET_PHASE_REFRESH)
        database.set_ticket_status(ticket_id, database.TICKET_WAITING_PROOF)

        embed = discord.Embed(
            title="Now refresh the Fountain",
            description=(
                f"💧 {user.mention}\n\n"
                f"**Game link:** {game_link}\n\n"
                f"When you've done the refresh in-game, tap the button below and "
                f"upload the screenshot showing the refresh."
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"Ticket #{ticket_id}")

        await interaction.response.send_message(
            content=user.mention,
            embed=embed,
            view=StartTicketView(),
        )

        await audit_log.log_event(
            self.bot,
            "ticket_started",
            user=user,
            mod=interaction.user,
            ticket_id=ticket_id,
            details={"channel_id": interaction.channel_id, "phase": "refresh", "skip_robux": True},
        )

    # ----------------------------------------------------------------
    # on_message — detect proof uploads in ticket channels
    # ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None or not _in_ticket_category(message.channel):
            return
        if not message.attachments:
            return

        ticket = database.get_ticket_by_channel(message.channel.id)
        if ticket is None:
            return

        if ticket["status"] not in (
            database.TICKET_WAITING_PROOF,
            database.TICKET_ACTIVE,
            database.TICKET_EXPIRED_GRACE,
        ):
            return

        if message.author.id != ticket["user_id"]:
            return

        att = message.attachments[0]
        ctype = att.content_type or ""
        if not ctype.startswith("image/"):
            try:
                await message.reply(
                    "❌ The attachment must be an image (screenshot).",
                    mention_author=False,
                )
            except discord.DiscordException:
                pass
            return

        # Refresh-phase extensions cap at MAX_REFRESHES_PER_TICKET
        if ticket["phase"] == database.TICKET_PHASE_REFRESH and \
                ticket["status"] in (database.TICKET_ACTIVE, database.TICKET_EXPIRED_GRACE) and \
                ticket["refresh_count"] >= config.MAX_REFRESHES_PER_TICKET:
            try:
                await message.reply(
                    f"❌ This ticket already used the max of {config.MAX_REFRESHES_PER_TICKET} refreshes.",
                    mention_author=False,
                )
            except discord.DiscordException:
                pass
            return

        database.set_ticket_status(ticket["id"], database.TICKET_PENDING)

        is_robux_phase = ticket["phase"] == database.TICKET_PHASE_ROBUX
        if is_robux_phase:
            title = "💰 Robux balance proof submitted"
            description = f"From {message.author.mention} — mods please verify they have enough Robux."
        else:
            title = "📸 Refresh proof submitted"
            description = (
                f"From {message.author.mention} — mods please verify.\n\n"
                f"Refresh #{ticket['refresh_count'] + 1}/{config.MAX_REFRESHES_PER_TICKET}"
            )

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.light_grey(),
        )
        embed.set_image(url=att.url)
        embed.set_footer(text=f"Ticket #{ticket['id']}")

        await message.channel.send(
            content=f"<@&{config.MOD_ROLE_ID}>",
            embed=embed,
            view=ApprovalView(),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False),
        )

        await audit_log.log_event(
            self.bot,
            "proof_uploaded",
            user=message.author,
            ticket_id=ticket["id"],
            details={"proof_url": att.url, "phase": ticket["phase"]},
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
