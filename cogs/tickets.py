"""Ticket flow: /start_ticket command + on_message listener for proof uploads."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

import audit_log
import config
import database
from views import ApprovalView, StartTicketView

log = logging.getLogger("fountain.tickets")


def _in_ticket_category(channel: discord.abc.GuildChannel) -> bool:
    """True if the given channel is inside the configured ticket category."""
    if not isinstance(channel, discord.TextChannel):
        return False
    return channel.category is not None and channel.category.id == config.TICKET_CATEGORY_ID


def _has_mod_role(member: discord.Member) -> bool:
    return any(r.id == config.MOD_ROLE_ID for r in member.roles)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------------------------------------------------------
    # /start_ticket — mod posts the link + Send Proof button
    # ----------------------------------------------------------------

    @app_commands.command(
        name="start_ticket",
        description="Start the refresh flow for a user in this ticket (mods only)",
    )
    @app_commands.describe(user="The user who needs to refresh")
    async def start_ticket(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        # Must be inside a ticket channel
        if not _in_ticket_category(interaction.channel):
            await interaction.response.send_message(
                "❌ This command only works inside a ticket channel.",
                ephemeral=True,
            )
            return

        # Must be a mod
        if not _has_mod_role(interaction.user):
            await interaction.response.send_message(
                "❌ Only mods can start the ticket flow.",
                ephemeral=True,
            )
            return

        # User cannot already be blacklisted
        if database.is_blacklisted(user.id):
            entry = database.get_blacklist_entry(user.id)
            until = "permanently" if entry["banned_until"] is None else f"until <t:{int(entry['banned_until'].timestamp())}:F>"
            await interaction.response.send_message(
                f"❌ {user.mention} is blacklisted {until} (strike {entry['strike_count']}).",
                ephemeral=True,
            )
            return

        # Get the configured game link
        game_link = database.get_config("game_link")
        if not game_link:
            await interaction.response.send_message(
                f"❌ No game link configured. An admin needs to run `/set_link` "
                f"in <#{config.MANAGEMENT_CHANNEL_ID}> first.",
                ephemeral=True,
            )
            return

        # Don't create a duplicate active ticket for the same channel
        existing = database.get_ticket_by_channel(interaction.channel_id)
        if existing and existing["status"] not in (database.TICKET_CLOSED,):
            await interaction.response.send_message(
                f"⚠️ This channel already has an active ticket (#{existing['id']}, status: {existing['status']}). "
                f"Close it first if you want to start over.",
                ephemeral=True,
            )
            return

        # Create the ticket record
        ticket_id = database.create_ticket(user.id, interaction.channel_id)

        embed = discord.Embed(
            title="Fountain refresh required",
            description=(
                f"👋 Hi {user.mention}\n\n"
                f"To complete your access you need to refresh the Fountain in-game.\n\n"
                f"**Game link:** {game_link}\n\n"
                f"When you've done the refresh, tap the button below and upload the screenshot "
                f"showing the refresh and the in-game time."
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
            details={"channel_id": interaction.channel_id},
        )

    # ----------------------------------------------------------------
    # on_message — detect proof uploads in ticket channels
    # ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots (including ourselves)
        if message.author.bot:
            return

        # Only inside ticket channels
        if message.guild is None or not _in_ticket_category(message.channel):
            return

        # Must have at least one attachment
        if not message.attachments:
            return

        # Find ticket for this channel
        ticket = database.get_ticket_by_channel(message.channel.id)
        if ticket is None:
            return

        # Only process if the ticket is waiting for proof (or active and the user wants to extend)
        if ticket["status"] not in (database.TICKET_WAITING_PROOF, database.TICKET_ACTIVE, database.TICKET_EXPIRED_GRACE):
            return

        # Only the ticket owner's uploads count
        if message.author.id != ticket["user_id"]:
            return

        # First attachment must be an image
        att = message.attachments[0]
        ctype = att.content_type or ""
        if not ctype.startswith("image/"):
            try:
                await message.reply(
                    "❌ The attachment must be an image (screenshot of the refresh).",
                    mention_author=False,
                )
            except discord.DiscordException:
                pass
            return

        # If user is trying to extend (ACTIVE or EXPIRED_GRACE), check max
        if ticket["status"] in (database.TICKET_ACTIVE, database.TICKET_EXPIRED_GRACE) and \
                ticket["refresh_count"] >= config.MAX_REFRESHES_PER_TICKET:
            try:
                await message.reply(
                    f"❌ This ticket already has the max of {config.MAX_REFRESHES_PER_TICKET} refreshes used.",
                    mention_author=False,
                )
            except discord.DiscordException:
                pass
            return

        # Move ticket to PENDING and post the proof for mod approval
        database.set_ticket_status(ticket["id"], database.TICKET_PENDING)

        embed = discord.Embed(
            title="📸 Proof submitted",
            description=(
                f"From {message.author.mention} — mods please review.\n\n"
                f"Refresh #{ticket['refresh_count'] + 1}/{config.MAX_REFRESHES_PER_TICKET}"
            ),
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
            details={"proof_url": att.url},
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
