"""Refresh queue + tiered alerts for the Fountain buff cycle.

Replaces the simpler cogs/scheduler.py. Manages:
  - Buff state (when the current buff expires)
  - FIFO queue of users with open refresh tickets in 'waiting' status
  - Scheduled events around buff expiry:
      T-QUEUE_PRE_WARN before active ping: gentle warning to next queued user
      T-QUEUE_ACTIVE_PING:                 ping next queued user with link + 10min window
      T-QUEUE_GENERAL_ALERT:               #fountain-general alert IF queue empty
      T-QUEUE_URGENT_ALERT:                @here urgent alert IF queue still empty
  - /setbuff slash command (manual override for special cases)
  - on_guild_channel_create listener: when fountain is full, tell new ticket to wait
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import database

log = logging.getLogger("fountain.refresh_queue")

# Job IDs
JOB_PRE_WARN = "queue_pre_warn"
JOB_ACTIVE_PING = "queue_active_ping"
JOB_GENERAL_ALERT = "queue_general_alert"
JOB_URGENT_ALERT = "queue_urgent_alert"

# Parse "1h30m", "90m", "55m32s", "1h", etc.
_TIME_PATTERN = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", re.IGNORECASE)


def parse_time_to_minutes(raw: str) -> int | None:
    """Parse '1h30m' / '90m' / '55m32s' / '1h' to minutes. Returns None on bad format."""
    if not raw:
        return None
    raw = raw.strip().lower().replace(" ", "")
    # Pure number → assume minutes
    if raw.isdigit():
        return int(raw)
    m = _TIME_PATTERN.fullmatch(raw)
    if m is None:
        return None
    h, mm, ss = m.groups()
    if not any([h, mm, ss]):
        return None
    total_seconds = (int(h or 0) * 3600) + (int(mm or 0) * 60) + int(ss or 0)
    if total_seconds <= 0:
        return None
    # Round up: 55m32s -> 56 min (so alerts have safety margin)
    return (total_seconds + 59) // 60


class RefreshQueue(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tz = ZoneInfo(config.TIMEZONE)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)

    async def cog_load(self):
        self.scheduler.start()
        # Re-arm based on persisted buff state (in case bot restarted)
        expires_at = database.get_buff_expires_at()
        if expires_at and expires_at > datetime.now(timezone.utc):
            self._reschedule_jobs(expires_at)
            log.info("Re-armed queue jobs from persisted buff state: %s", expires_at.isoformat())

    async def cog_unload(self):
        self.scheduler.shutdown(wait=False)

    # =================================================================
    # Public API — called from views.py BuffTimeModal and /setbuff
    # =================================================================

    def set_buff_state(self, minutes_remaining: int):
        """Set the current buff expiry to now + minutes_remaining, then re-arm all jobs."""
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes_remaining)
        database.set_buff_expires_at(expires_at)
        self._reschedule_jobs(expires_at)
        return expires_at

    # =================================================================
    # Scheduling
    # =================================================================

    def _reschedule_jobs(self, expires_at: datetime):
        now = datetime.now(timezone.utc)
        timeline = [
            (JOB_PRE_WARN, config.QUEUE_ACTIVE_PING_MINUTES + config.QUEUE_PRE_WARN_MINUTES, self._fire_pre_warn),
            (JOB_ACTIVE_PING, config.QUEUE_ACTIVE_PING_MINUTES, self._fire_active_ping),
            (JOB_GENERAL_ALERT, config.QUEUE_GENERAL_ALERT_MINUTES, self._fire_general_alert),
            (JOB_URGENT_ALERT, config.QUEUE_URGENT_ALERT_MINUTES, self._fire_urgent_alert),
        ]
        for job_id, minutes_before, func in timeline:
            run_at = expires_at - timedelta(minutes=minutes_before)
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
            if run_at > now:
                self.scheduler.add_job(
                    func,
                    "date",
                    run_date=run_at,
                    id=job_id,
                    replace_existing=True,
                    misfire_grace_time=60,
                )

    # =================================================================
    # Alert handlers
    # =================================================================

    async def _fire_pre_warn(self):
        """Gentle heads-up to the next queued user that their turn is coming."""
        queue = database.get_refresh_queue()
        if not queue:
            return
        ticket = queue[0]
        channel = self.bot.get_channel(ticket["channel_id"])
        if channel is None:
            return
        try:
            await channel.send(
                f"<@{ticket['user_id']}>\n"
                f"Heads up — your turn to refresh the Fountain is coming up in about "
                f"{config.QUEUE_PRE_WARN_MINUTES} minutes. Please make sure you're online "
                f"and ready in the game. The link will be sent here when it's your turn.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.DiscordException:
            log.exception("Failed to send pre-warn in %s", channel.id)

    async def _fire_active_ping(self):
        """Ping next queued user with the game link, give them a window to refresh."""
        queue = database.get_refresh_queue()
        if not queue:
            return
        ticket = queue[0]
        channel = self.bot.get_channel(ticket["channel_id"])
        if channel is None:
            return

        game_link = database.get_config("game_link") or "(link not configured — admins use /set_link)"
        deadline_ts = int((datetime.now(timezone.utc) + timedelta(minutes=config.QUEUE_ACTIVE_PING_MINUTES)).timestamp())

        embed = discord.Embed(
            title="It's your turn — please refresh the Fountain",
            description=(
                f"Hi <@{ticket['user_id']}>,\n\n"
                f"The Fountain buff is about to drop. You have **~{config.QUEUE_ACTIVE_PING_MINUTES} minutes** "
                f"to refresh it (until <t:{deadline_ts}:t>).\n\n"
                f"**Game link:** {game_link}\n\n"
                f"Steps:\n"
                f"1. Join the game using the link above\n"
                f"2. Refresh the Fountain in-game\n"
                f"3. Come back here and upload your refresh screenshot\n\n"
                f"Thanks for keeping the Fountain alive 💧"
            ),
            color=discord.Color.blue(),
        )
        try:
            await channel.send(
                content=f"<@{ticket['user_id']}>",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.DiscordException:
            log.exception("Failed to send active ping in %s", channel.id)

    async def _fire_general_alert(self):
        """Polite alert in #fountain-general if no one is queued yet."""
        queue = database.get_refresh_queue()
        if queue:
            return  # someone's already on it
        channel = self.bot.get_channel(config.FOUNTAIN_GENERAL_CHANNEL_ID)
        if channel is None:
            return
        try:
            await channel.send(
                f"💧 The Fountain has about **{config.QUEUE_GENERAL_ALERT_MINUTES} minutes** "
                f"of buff left and no one is currently queued to refresh.\n"
                f"If you can refresh, please open a ticket and let's keep the buff alive 🍀",
            )
        except discord.DiscordException:
            log.exception("Failed to send general alert in fountain-general")

    async def _fire_urgent_alert(self):
        """Escalated @here alert if buff is critically low and still no queue."""
        queue = database.get_refresh_queue()
        if queue:
            return
        channel = self.bot.get_channel(config.FOUNTAIN_GENERAL_CHANNEL_ID)
        if channel is None:
            return
        try:
            await channel.send(
                f"@here ⚠️ Only **{config.QUEUE_URGENT_ALERT_MINUTES} minutes** of Fountain buff left "
                f"and no one is refreshing. If anyone is online and can help, please open a refresh "
                f"ticket now — the buff will drop otherwise.",
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
        except discord.DiscordException:
            log.exception("Failed to send urgent alert in fountain-general")

    # =================================================================
    # Listener: auto-message new refresh tickets when fountain is full
    # =================================================================

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not isinstance(channel, discord.TextChannel):
            return
        if config.TICKET_CATEGORY_ID == 0:
            return
        if channel.category is None or channel.category.id != config.TICKET_CATEGORY_ID:
            return

        # Give tickets.py time to create the ticket record + post intro
        await asyncio.sleep(5)

        ticket = database.get_ticket_by_channel(channel.id)
        if ticket is None:
            return

        expires_at = database.get_buff_expires_at()
        if expires_at is None:
            return  # no buff state, do nothing — fresh deploy or pre-init
        now = datetime.now(timezone.utc)
        minutes_left = (expires_at - now).total_seconds() / 60
        if minutes_left <= config.QUEUE_ACTIVE_PING_MINUTES:
            return  # buff is already low, no need for the "wait" message

        # Calculate queue position (counts only users in waiting+refresh phase, FIFO by created_at)
        queue = database.get_refresh_queue()
        position = 1
        for q in queue:
            if q["user_id"] == ticket["user_id"]:
                position = queue.index(q) + 1
                break
        else:
            # User isn't in queue yet (still in robux phase) — position is "after current queue"
            position = len(queue) + 1

        eta_ts = int(expires_at.timestamp())
        try:
            await channel.send(
                f"<@{ticket['user_id']}>\n"
                f"Thanks for opening a refresh ticket. The Fountain is currently topped up "
                f"(buff drops around <t:{eta_ts}:t>, in <t:{eta_ts}:R>).\n"
                f"You're approximately **#{position}** in the queue. We'll ping you in this channel "
                f"when it's your turn — usually about {config.QUEUE_PRE_WARN_MINUTES + config.QUEUE_ACTIVE_PING_MINUTES} "
                f"minutes before the buff drops.\n\n"
                f"You can complete the Robux verification step now if you haven't already — that way "
                f"when your turn comes you only need to refresh and upload proof. Thanks for waiting 💧",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.DiscordException:
            log.exception("Failed to post 'fountain full, wait' message in %s", channel.id)

    # =================================================================
    # /setbuff slash command — admin override
    # =================================================================

    @app_commands.command(name="setbuff", description="Manually set how much time the Fountain buff has left")
    @app_commands.describe(time="Time remaining, e.g. 55m, 1h30m, 90m, 59m32s")
    async def setbuff(self, interaction: discord.Interaction, time: str):
        member = interaction.user
        if not isinstance(member, discord.Member) or not any(
            r.id in (config.ADMIN_ROLE_ID, config.DEVELOPER_ROLE_ID, config.MOD_ROLE_ID)
            for r in member.roles
        ):
            await interaction.response.send_message(
                "❌ Only mods and admins can use this command.", ephemeral=True
            )
            return

        minutes = parse_time_to_minutes(time)
        if minutes is None or minutes <= 0:
            await interaction.response.send_message(
                "❌ Invalid format. Use `55m`, `1h`, `1h30m`, `90m`, `59m32s`.",
                ephemeral=True,
            )
            return

        expires_at = self.set_buff_state(minutes)
        expires_ts = int(expires_at.timestamp())

        await interaction.response.send_message(
            f"⏰ Buff timer set: **{minutes} min remaining**\n"
            f"Expires <t:{expires_ts}:t> (<t:{expires_ts}:R>).\n"
            f"Queue alerts re-armed.",
            ephemeral=False,
        )
        log.info("setbuff by %s: %s minutes (expires %s)", interaction.user, minutes, expires_at.isoformat())

        # If buff is now in active-ping window AND there's someone in queue, ping immediately
        if minutes <= config.QUEUE_ACTIVE_PING_MINUTES:
            queue = database.get_refresh_queue()
            if queue:
                await self._fire_active_ping()


async def setup(bot: commands.Bot):
    await bot.add_cog(RefreshQueue(bot))
