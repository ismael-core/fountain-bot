"""Membership lifecycle: AFK timer reminders, expiration, grace period, blacklist."""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import audit_log
import config
import database

log = logging.getLogger("fountain.membership")


def _job_id(ticket_id: int, kind: str) -> str:
    """Unique job id per (ticket, kind) so reschedules replace cleanly."""
    return f"ticket_{ticket_id}_{kind}"


class Membership(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tz = ZoneInfo(config.TIMEZONE)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)

    async def cog_load(self):
        self.scheduler.start()
        log.info("Membership scheduler started")
        # Reschedule jobs for any active tickets after a restart
        for ticket in database.get_active_tickets():
            self.schedule_for_ticket(ticket["id"])

    async def cog_unload(self):
        self.scheduler.shutdown(wait=False)

    # ----------------------------------------------------------------
    # Job scheduling
    # ----------------------------------------------------------------

    def schedule_for_ticket(self, ticket_id: int):
        """(Re)schedule all jobs for a given ticket based on its current state."""
        ticket = database.get_ticket(ticket_id)
        if ticket is None or ticket["expires_at"] is None:
            return

        expires_at = ticket["expires_at"]
        now = datetime.now(timezone.utc)

        # Pre-expiry reminders
        for minutes in config.PRE_EXPIRY_REMINDERS:
            when = expires_at - timedelta(minutes=minutes)
            self._safe_add(
                _job_id(ticket_id, f"remind_{minutes}"),
                self._send_reminder,
                when,
                [ticket_id, minutes],
            )

        # Expiration job (when AFK runs out)
        self._safe_add(
            _job_id(ticket_id, "expire"),
            self._handle_expiry,
            expires_at,
            [ticket_id],
        )

        # Blacklist deadline (grace_minutes after expiry)
        blacklist_at = expires_at + timedelta(minutes=config.GRACE_MINUTES)
        self._safe_add(
            _job_id(ticket_id, "blacklist"),
            self._handle_blacklist,
            blacklist_at,
            [ticket_id],
        )

    def _safe_add(self, job_id: str, func, when_utc: datetime, args: list):
        now = datetime.now(timezone.utc)
        if when_utc <= now:
            return  # don't schedule in the past
        self.scheduler.add_job(
            func,
            "date",
            run_date=when_utc,
            args=args,
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )

    # ----------------------------------------------------------------
    # Job handlers
    # ----------------------------------------------------------------

    async def _send_reminder(self, ticket_id: int, minutes_left: int):
        ticket = database.get_ticket(ticket_id)
        if ticket is None or ticket["status"] != database.TICKET_ACTIVE:
            return

        channel = self.bot.get_channel(ticket["channel_id"])
        user = await self._fetch_user(ticket["user_id"])

        expires_ts = int(ticket["expires_at"].timestamp())
        text = (
            f"⏰ Heads up <@{ticket['user_id']}>, your AFK time runs out in "
            f"**{minutes_left} min** (at <t:{expires_ts}:t> your local time, "
            f"<t:{expires_ts}:R>). "
            f"Upload a new refresh screenshot in this ticket if you want to extend."
        )

        if channel is not None:
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.DiscordException:
                log.exception("Failed to send reminder in channel %s", ticket["channel_id"])

        if user is not None:
            try:
                await user.send(
                    f"⏰ Your Fountain AFK time runs out in {minutes_left} min "
                    f"(at <t:{expires_ts}:t> your local time). "
                    f"Send a new screenshot in your ticket if you want to extend."
                )
            except discord.DiscordException:
                pass  # DMs closed; not a hard failure

    async def _handle_expiry(self, ticket_id: int):
        ticket = database.get_ticket(ticket_id)
        if ticket is None or ticket["status"] != database.TICKET_ACTIVE:
            return

        database.set_ticket_status(ticket_id, database.TICKET_EXPIRED_GRACE)

        channel = self.bot.get_channel(ticket["channel_id"])
        user = await self._fetch_user(ticket["user_id"])

        msg = (
            f"🚨 <@&{config.MOD_ROLE_ID}> AFK time expired for <@{ticket['user_id']}>. "
            f"They have **{config.GRACE_MINUTES} min** to leave the in-game server "
            f"or they'll be blacklisted. Verify their exit and close this ticket."
        )

        if channel is not None:
            try:
                await channel.send(
                    msg,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=True),
                )
            except discord.DiscordException:
                log.exception("Failed to send expiry alert")

        if user is not None:
            try:
                await user.send(
                    f"⏱️ Your AFK time has run out. You have {config.GRACE_MINUTES} min to leave "
                    f"the in-game server. If you stay past that, you'll be blacklisted."
                )
            except discord.DiscordException:
                pass

        await audit_log.log_event(
            self.bot,
            "ticket_expired",
            user=user,
            ticket_id=ticket_id,
            details={"grace_minutes": config.GRACE_MINUTES},
        )

        # Schedule auto-close X hours after expiry. If the user recovers
        # (extends AFK), _auto_close_ticket will detect the ACTIVE status
        # and skip deletion. Otherwise the channel is deleted to keep the
        # category clean.
        close_time = datetime.now(timezone.utc) + timedelta(hours=config.AUTO_CLOSE_HOURS_AFTER_EXPIRY)
        self.scheduler.add_job(
            self._auto_close_ticket,
            "date",
            run_date=close_time,
            args=[ticket_id],
            id=f"close_{ticket_id}",
            replace_existing=True,
        )

    async def _handle_blacklist(self, ticket_id: int):
        ticket = database.get_ticket(ticket_id)
        # Only blacklist if the ticket is still in EXPIRED_GRACE (mod hasn't manually closed it
        # and user hasn't refreshed to extend during grace period)
        if ticket is None or ticket["status"] != database.TICKET_EXPIRED_GRACE:
            return

        # Hard guard: never blacklist server owner or anyone with a staff role.
        # Checked at execution time even if it was checked at scheduling time,
        # because roles can change between then and now.
        if await self._is_protected(ticket["user_id"]):
            database.set_ticket_status(ticket_id, database.TICKET_CLOSED)
            channel = self.bot.get_channel(ticket["channel_id"])
            if channel is not None:
                try:
                    await channel.send(
                        f"⚠️ <@{ticket['user_id']}> has a protected role — skipping blacklist. "
                        f"Ticket closed.",
                        allowed_mentions=discord.AllowedMentions(users=False),
                    )
                except discord.DiscordException:
                    pass
            await audit_log.log_event(
                self.bot,
                "blacklisted",
                user=await self._fetch_user(ticket["user_id"]),
                ticket_id=ticket_id,
                details={"skipped": "user has protected role or is server owner"},
            )
            return

        user = await self._fetch_user(ticket["user_id"])

        entry = database.add_blacklist_strike(
            ticket["user_id"],
            config.BLACKLIST_DURATIONS_HOURS,
            reason="Did not leave server within grace period after AFK expired",
        )

        database.set_ticket_status(ticket_id, database.TICKET_CLOSED)

        channel = self.bot.get_channel(ticket["channel_id"])

        until = (
            "permanently"
            if entry["banned_until"] is None
            else f"until <t:{int(entry['banned_until'].timestamp())}:F>"
        )

        if channel is not None:
            try:
                await channel.send(
                    f"⛔ <@{ticket['user_id']}> has been blacklisted {until} "
                    f"(strike #{entry['strike_count']}) for not leaving in time.",
                    allowed_mentions=discord.AllowedMentions(users=False),
                )
            except discord.DiscordException:
                pass

        # Revoke channel access for the user so they can no longer see the game link
        # or upload anything in this ticket. They'll need to open a new ticket once
        # their blacklist expires.
        if channel is not None and isinstance(user, discord.Member):
            try:
                await channel.set_permissions(
                    user,
                    view_channel=False,
                    reason=f"Blacklist applied (strike #{entry['strike_count']})",
                )
            except discord.DiscordException:
                log.exception("Failed to revoke channel access for blacklisted user %s", user.id)

        if user is not None:
            try:
                await user.send(
                    f"⛔ You've been blacklisted {until} (strike #{entry['strike_count']}) "
                    f"for not leaving the server within {config.GRACE_MINUTES} min of your "
                    f"AFK time expiring. To appeal, contact an admin."
                )
            except discord.DiscordException:
                pass

        await audit_log.log_event(
            self.bot,
            "blacklisted",
            user=user,
            ticket_id=ticket_id,
            details={
                "strike_count": entry["strike_count"],
                "banned_until": entry["banned_until"].isoformat() if entry["banned_until"] else "permanent",
                "reason": entry["reason"],
            },
        )

        # Also post to the dedicated blacklist channel if configured
        if config.BLACKLIST_CHANNEL_ID:
            bl_channel = self.bot.get_channel(config.BLACKLIST_CHANNEL_ID)
            if bl_channel is not None:
                user_mention = user.mention if user else f"<@{ticket['user_id']}>"
                embed = discord.Embed(
                    title="⛔ User blacklisted",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(
                    name="User",
                    value=f"{user_mention} (`{ticket['user_id']}`)",
                    inline=False,
                )
                embed.add_field(name="Strike", value=str(entry["strike_count"]), inline=True)
                if entry["banned_until"] is None:
                    until_value = "**Permanent**"
                else:
                    until_value = f"<t:{int(entry['banned_until'].timestamp())}:F>"
                embed.add_field(name="Until", value=until_value, inline=True)
                embed.add_field(name="Reason", value=entry["reason"] or "—", inline=False)
                try:
                    await bl_channel.send(embed=embed)
                except discord.DiscordException:
                    log.exception("Failed to post to blacklist channel")

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    async def _fetch_user(self, user_id: int):
        """Return a discord.Member if possible (so .roles is available);
        fall back to a basic User only if the person has left the server."""
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return None
        member = guild.get_member(user_id)
        if member is not None:
            return member
        # Cache miss: ask Discord directly. This preserves .roles unlike fetch_user.
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            # User actually left the guild; fall back to a User (no roles)
            try:
                return await self.bot.fetch_user(user_id)
            except discord.DiscordException:
                return None
        except discord.DiscordException:
            return None

    async def _is_protected(self, user_id: int) -> bool:
        """True if the user has a protected staff role OR is the server owner.
        Used as a hard guard before any blacklist action."""
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            # Better to err on the side of NOT blacklisting if we can't verify
            return True
        if guild.owner_id == user_id:
            return True
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.DiscordException:
                # Can't verify roles right now → don't risk a wrong blacklist
                log.warning(
                    "Could not fetch member %s to verify protected role; skipping blacklist",
                    user_id,
                )
                return True
        return any(r.id in config.PROTECTED_ROLE_IDS for r in member.roles)

    async def _auto_close_ticket(self, ticket_id: int):
        """Delete the ticket channel X hours after AFK expiry if the user
        never recovered. Skipped if the user extended (status went back to ACTIVE)
        or the ticket was already closed by a mod."""
        ticket = database.get_ticket(ticket_id)
        if ticket is None:
            return
        # User recovered or already closed; nothing to do
        if ticket["status"] in (database.TICKET_ACTIVE, database.TICKET_CLOSED):
            return

        channel = self.bot.get_channel(ticket["channel_id"])
        if channel is None:
            # Channel already gone for some reason; just mark closed in DB
            database.set_ticket_status(ticket_id, database.TICKET_CLOSED)
            return

        database.set_ticket_status(ticket_id, database.TICKET_CLOSED)

        await audit_log.log_event(
            self.bot,
            "ticket_closed",
            user=await self._fetch_user(ticket["user_id"]),
            ticket_id=ticket_id,
            details={
                "reason": f"auto-close: inactive {config.AUTO_CLOSE_HOURS_AFTER_EXPIRY}h after AFK expired",
            },
        )

        try:
            await channel.delete(
                reason=f"Auto-close: inactive {config.AUTO_CLOSE_HOURS_AFTER_EXPIRY}h after AFK expired"
            )
        except discord.DiscordException:
            log.exception("Failed to auto-close ticket channel %s", channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Membership(bot))
