"""WRR (Weather Rolls) ticket system.

Flow:
  1. User opens a ticket in WRR_TICKET_CATEGORY_ID (auto-detected).
  2. Bot posts tier-select view (50/100/.../400 buttons + custom amount modal).
  3. User picks tier  -> uploads balance screenshot -> mod approves.
  4. On balance approve, bot posts game link. User has 3 min to upload a
     usage screenshot (WRR consumed + game chat visible).
  5. User uploads usage screenshot -> mod approves -> timer starts.
  6. Reminders fire at 10 min and 5 min before expiry.
  7. At expiry: channel access revoked, user DM'd, mods pinged in logs
     with a "User didn't leave -> Blacklist" button.
  8. Ticket auto-closes after AUTO_CLOSE_HOURS_AFTER_EXPIRY hours.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import audit_log
import config
import database
from views import WRRTierSelectView, WRRApprovalView, WRRBlacklistConfirmView

log = logging.getLogger("fountain.wrr")


class WRR(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.TIMEZONE))

    async def cog_load(self):
        self.scheduler.start()
        # Re-arm scheduled jobs for tickets that are mid-flight (after a restart)
        for ticket in self._iter_open_tickets():
            try:
                if ticket["status"] == database.WRR_STATUS_WAITING_USAGE and ticket.get("started_at") is None:
                    # We don't know exactly when balance was approved; skip re-arming
                    # the 3-min timeout. Worst case: it just stays open until manually closed.
                    continue
                if ticket["status"] == database.WRR_STATUS_ACTIVE and ticket.get("expires_at"):
                    self.schedule_active_ticket(ticket["id"])
                elif ticket["status"] == database.WRR_STATUS_EXPIRED_GRACE and ticket.get("expires_at"):
                    close_at = ticket["expires_at"] + timedelta(hours=config.AUTO_CLOSE_HOURS_AFTER_EXPIRY)
                    if close_at > datetime.now(timezone.utc):
                        self.scheduler.add_job(
                            self._auto_close_ticket,
                            "date",
                            run_date=close_at,
                            args=[ticket["id"]],
                            id=f"wrr_close_{ticket['id']}",
                            replace_existing=True,
                        )
            except Exception:
                log.exception("Failed to re-arm WRR ticket %s", ticket["id"])

    def _iter_open_tickets(self):
        # Use the hydrated DB helper so datetimes come back tz-aware (UTC).
        return database.get_open_wrr_tickets()

    # =================================================================
    # Listeners
    # =================================================================

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not isinstance(channel, discord.TextChannel):
            return
        if config.WRR_TICKET_CATEGORY_ID == 0:
            return
        if channel.category is None or channel.category.id != config.WRR_TICKET_CATEGORY_ID:
            return

        # Wait for Ticket Tool to apply permission overwrites
        await asyncio.sleep(3)
        channel = self.bot.get_channel(channel.id) or channel

        # Avoid double-creation
        if database.get_wrr_ticket_by_channel(channel.id) is not None:
            return

        owner = self._find_ticket_owner(channel)
        if owner is None:
            log.warning("Could not detect WRR ticket owner for channel %s", channel.id)
            return

        # Blacklist check
        if database.is_blacklisted(owner.id):
            entry = database.get_blacklist_entry(owner.id)
            if entry and entry["banned_until"] is None:
                until_str = "permanently"
            elif entry:
                until_str = f"until <t:{int(entry['banned_until'].timestamp())}:F>"
            else:
                until_str = ""
            try:
                await channel.send(
                    f"⛔ {owner.mention} is blacklisted {until_str} and cannot open WRR tickets.\n"
                    f"<@&{config.MOD_ROLE_ID}> please review.",
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False),
                )
            except discord.DiscordException:
                pass
            return

        ticket_id = database.create_wrr_ticket(owner.id, channel.id)

        embed = discord.Embed(
            title="WRR access ticket",
            description=(
                f"👋 Hi {owner.mention}\n\n"
                f"Pick **how much WRR you're going to use**. The bot calculates your "
                f"access time automatically:\n"
                f"**1 WRR = 0.6 minutes** (100 WRR = 1 hour, 200 WRR = 2 hours, etc.)\n\n"
                f"⚠️ The amount **must be a multiple of 50** (50, 100, 150, 200, ...). "
                f"If you have 121 WRR you can use 100. If you have 352 you can use 300.\n\n"
                f"Pick a tier below or hit **Other amount** for a custom number."
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"WRR Ticket #{ticket_id}")

        try:
            await channel.send(content=owner.mention, embed=embed, view=WRRTierSelectView())
        except discord.DiscordException:
            log.exception("Failed to post WRR intro message in %s", channel.id)

        await audit_log.log_event(
            self.bot,
            "wrr_ticket_started",
            user=owner,
            ticket_id=ticket_id,
            details={"channel_id": channel.id},
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild or not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.category is None:
            return
        if message.channel.category.id != config.WRR_TICKET_CATEGORY_ID:
            return
        if not message.attachments:
            return

        ticket = database.get_wrr_ticket_by_channel(message.channel.id)
        if ticket is None:
            return
        if message.author.id != ticket["user_id"]:
            return

        status = ticket["status"]
        if status == database.WRR_STATUS_WAITING_BALANCE:
            phase = "balance"
            new_status = database.WRR_STATUS_PENDING_BALANCE
            amount = ticket["tier_minutes"] * 100 // 60 if ticket["tier_minutes"] else "?"
            title = "💰 WRR balance proof submitted"
            desc = (
                f"From {message.author.mention} — verify they have at least "
                f"**{amount} WRR** (the amount they selected)."
            )
        elif status == database.WRR_STATUS_WAITING_USAGE:
            phase = "usage"
            new_status = database.WRR_STATUS_PENDING_USAGE
            amount = ticket["tier_minutes"] * 100 // 60 if ticket["tier_minutes"] else "?"
            title = "🎮 WRR usage proof submitted"
            desc = (
                f"From {message.author.mention} — verify the **{amount} WRR were actually used** "
                f"(consumed in game, chat visible)."
            )
        else:
            return

        att = message.attachments[0]
        if not (att.content_type or "").startswith("image/"):
            try:
                await message.reply("❌ Attachment must be an image (screenshot).", mention_author=False)
            except discord.DiscordException:
                pass
            return

        database.set_wrr_ticket_status(ticket["id"], new_status)

        embed = discord.Embed(title=title, description=desc, color=discord.Color.light_grey())
        embed.set_image(url=att.url)
        embed.set_footer(text=f"WRR Ticket #{ticket['id']}")

        try:
            await message.channel.send(
                content=f"<@&{config.MOD_ROLE_ID}>",
                embed=embed,
                view=WRRApprovalView(),
                allowed_mentions=discord.AllowedMentions(roles=True, users=False),
            )
        except discord.DiscordException:
            log.exception("Failed to post WRR proof message in %s", message.channel.id)
            return

        await audit_log.log_event(
            self.bot,
            "wrr_proof_uploaded",
            user=message.author,
            ticket_id=ticket["id"],
            details={"phase": phase, "proof_url": att.url},
        )

    # =================================================================
    # Scheduler helpers
    # =================================================================

    def schedule_usage_timeout(self, ticket_id: int):
        """3 min after balance approval, if no usage proof uploaded → cancel."""
        run_at = datetime.now(timezone.utc) + timedelta(minutes=3)
        self.scheduler.add_job(
            self._handle_usage_timeout,
            "date",
            run_date=run_at,
            args=[ticket_id],
            id=f"wrr_usage_timeout_{ticket_id}",
            replace_existing=True,
        )

    async def _handle_usage_timeout(self, ticket_id: int):
        ticket = database.get_wrr_ticket(ticket_id)
        if ticket is None:
            return
        if ticket["status"] != database.WRR_STATUS_WAITING_USAGE:
            return  # already moved on, nothing to do

        database.set_wrr_ticket_status(ticket_id, database.WRR_STATUS_CLOSED)

        channel = self.bot.get_channel(ticket["channel_id"])
        if channel is not None:
            try:
                await channel.send(
                    f"❌ <@{ticket['user_id']}> Time's up — you didn't upload the usage "
                    f"screenshot in 3 minutes. Ticket cancelled.\n"
                    f"Open a new one if you want to try again."
                )
            except discord.DiscordException:
                pass
            user = await self._fetch_user(ticket["user_id"])
            if isinstance(user, discord.Member):
                try:
                    await channel.set_permissions(
                        user, view_channel=False, reason="WRR 3-min usage timeout"
                    )
                except discord.DiscordException:
                    pass

        await audit_log.log_event(
            self.bot,
            "wrr_timeout",
            user=await self._fetch_user(ticket["user_id"]),
            ticket_id=ticket_id,
        )

    def schedule_active_ticket(self, ticket_id: int):
        """Reminders at 10/5 min before expiry + expiry handler at expires_at."""
        ticket = database.get_wrr_ticket(ticket_id)
        if ticket is None or not ticket.get("expires_at"):
            return

        # Clear any pre-existing jobs for this ticket
        for jid in (
            f"wrr_remind_{ticket_id}_10",
            f"wrr_remind_{ticket_id}_5",
            f"wrr_expire_{ticket_id}",
        ):
            try:
                self.scheduler.remove_job(jid)
            except Exception:
                pass

        expires_at = ticket["expires_at"]
        now = datetime.now(timezone.utc)

        for minutes_before in (10, 5):
            run_at = expires_at - timedelta(minutes=minutes_before)
            if run_at > now:
                self.scheduler.add_job(
                    self._send_reminder,
                    "date",
                    run_date=run_at,
                    args=[ticket_id, minutes_before],
                    id=f"wrr_remind_{ticket_id}_{minutes_before}",
                )

        self.scheduler.add_job(
            self._handle_expiry,
            "date",
            run_date=expires_at,
            args=[ticket_id],
            id=f"wrr_expire_{ticket_id}",
        )

    async def _send_reminder(self, ticket_id: int, minutes_left: int):
        ticket = database.get_wrr_ticket(ticket_id)
        if ticket is None or ticket["status"] != database.WRR_STATUS_ACTIVE:
            return

        expires_ts = int(ticket["expires_at"].timestamp())
        channel = self.bot.get_channel(ticket["channel_id"])
        if channel is not None:
            try:
                await channel.send(
                    f"⏰ <@{ticket['user_id']}>, your WRR access expires in **{minutes_left} min** "
                    f"(at <t:{expires_ts}:t>). Wrap up and prepare to leave the game.",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.DiscordException:
                pass

        user = await self._fetch_user(ticket["user_id"])
        if user:
            try:
                await user.send(
                    f"⏰ Your WRR access expires in **{minutes_left} min**. "
                    f"Leave the game when time's up to avoid a blacklist."
                )
            except discord.DiscordException:
                pass

    async def _handle_expiry(self, ticket_id: int):
        ticket = database.get_wrr_ticket(ticket_id)
        if ticket is None or ticket["status"] != database.WRR_STATUS_ACTIVE:
            return

        database.set_wrr_ticket_status(ticket_id, database.WRR_STATUS_EXPIRED_GRACE)

        channel = self.bot.get_channel(ticket["channel_id"])
        user = await self._fetch_user(ticket["user_id"])

        if channel is not None:
            try:
                await channel.send(
                    f"⏱️ <@{ticket['user_id']}>, your WRR access has **expired**. "
                    f"Leave the game now or you'll get a blacklist."
                )
            except discord.DiscordException:
                pass
            if isinstance(user, discord.Member):
                try:
                    await channel.set_permissions(
                        user, view_channel=False, reason="WRR access expired"
                    )
                except discord.DiscordException:
                    pass

        if user:
            try:
                await user.send(
                    "⏱️ Your WRR access has expired. Leave the in-game server now to avoid a blacklist."
                )
            except discord.DiscordException:
                pass

        # Mod alert in #fountain-logs
        logs_channel = self.bot.get_channel(config.LOGS_CHANNEL_ID)
        if logs_channel is not None:
            user_mention = user.mention if user else f"<@{ticket['user_id']}>"
            amount = ticket["tier_minutes"] * 100 // 60 if ticket["tier_minutes"] else "?"
            embed = discord.Embed(
                title="⏱️ WRR access expired — verify user left the game",
                description=(
                    f"User: {user_mention}\n"
                    f"Tier: {amount} WRR ({ticket['tier_minutes']} min)\n\n"
                    f"Go check the in-game server. If they didn't leave on their own, "
                    f"kick them manually **and press the button below** to apply blacklist."
                ),
                color=discord.Color.orange(),
            )
            embed.set_footer(text=f"WRR Ticket #{ticket_id}")
            try:
                await logs_channel.send(
                    content=f"<@&{config.MOD_ROLE_ID}>",
                    embed=embed,
                    view=WRRBlacklistConfirmView(),
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False),
                )
            except discord.DiscordException:
                log.exception("Failed to post WRR expiry alert in logs channel")

        # Schedule auto-close
        close_at = datetime.now(timezone.utc) + timedelta(hours=config.AUTO_CLOSE_HOURS_AFTER_EXPIRY)
        self.scheduler.add_job(
            self._auto_close_ticket,
            "date",
            run_date=close_at,
            args=[ticket_id],
            id=f"wrr_close_{ticket_id}",
            replace_existing=True,
        )

        await audit_log.log_event(
            self.bot,
            "wrr_expired",
            user=user,
            ticket_id=ticket_id,
            details={"tier_minutes": ticket["tier_minutes"]},
        )

    async def _auto_close_ticket(self, ticket_id: int):
        ticket = database.get_wrr_ticket(ticket_id)
        if ticket is None or ticket["status"] == database.WRR_STATUS_CLOSED:
            return

        database.set_wrr_ticket_status(ticket_id, database.WRR_STATUS_CLOSED)
        channel = self.bot.get_channel(ticket["channel_id"])
        if channel is not None:
            try:
                await channel.delete(reason="Auto-close after WRR expiry")
            except discord.DiscordException:
                log.exception("Failed to auto-close WRR channel %s", channel.id)

        await audit_log.log_event(
            self.bot,
            "wrr_closed",
            user=await self._fetch_user(ticket["user_id"]),
            ticket_id=ticket_id,
        )

    # =================================================================
    # Helpers
    # =================================================================

    def _find_ticket_owner(self, channel: discord.TextChannel):
        for target, overwrite in channel.overwrites.items():
            if not isinstance(target, discord.Member):
                continue
            if target.bot:
                continue
            if overwrite.view_channel is True:
                return target
        return None

    async def _fetch_user(self, user_id: int):
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return None
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.DiscordException:
            try:
                return await self.bot.fetch_user(user_id)
            except discord.DiscordException:
                return None

    async def _is_protected(self, user_id: int) -> bool:
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return True
        if guild.owner_id == user_id:
            return True
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.DiscordException:
                return True  # fail-safe
        return any(r.id in config.PROTECTED_ROLE_IDS for r in member.roles)


async def setup(bot: commands.Bot):
    if config.WRR_TICKET_CATEGORY_ID == 0:
        log.info("WRR_TICKET_CATEGORY_ID not set; WRR cog will not be loaded.")
        return
    await bot.add_cog(WRR(bot))
