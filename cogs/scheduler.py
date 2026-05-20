"""Dynamic scheduler: pre-alerts and post-checks based on the last refresh.

Each time /refresh runs we reschedule:
  - pre_alert at (refresh + 1h - PRE_PING_MINUTES)  -> "buff expires soon"
  - post_check at (refresh + 1h + ALERT_DELAY_MINUTES) -> "no refresh logged, buff down"
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import database

log = logging.getLogger("fountain.scheduler")

BUFF_DURATION = timedelta(hours=1)
PRE_ALERT_ID = "pre_alert"
POST_CHECK_ID = "post_check"


class Scheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tz = ZoneInfo(config.TIMEZONE)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)

    async def cog_load(self):
        self.scheduler.start()
        log.info("Scheduler started (tz=%s)", config.TIMEZONE)
        # On startup, reschedule based on the last known refresh (if any and still relevant)
        self._reschedule_from_last_refresh()

    async def cog_unload(self):
        self.scheduler.shutdown(wait=False)

    def _channel(self) -> discord.TextChannel | None:
        ch = self.bot.get_channel(config.CHANNEL_ID)
        if ch is None:
            log.warning("Channel %s not found", config.CHANNEL_ID)
        return ch

    # ---------- Public API used from /refresh ----------

    def reschedule_after_refresh(self, refresh_time_utc: datetime):
        """Cancel any pending jobs and schedule fresh ones for this refresh."""
        pre_alert_at = refresh_time_utc + BUFF_DURATION - timedelta(minutes=config.PRE_PING_MINUTES)
        post_check_at = refresh_time_utc + BUFF_DURATION + timedelta(minutes=config.ALERT_DELAY_MINUTES)

        self._safe_add(PRE_ALERT_ID, self.pre_alert, pre_alert_at)
        self._safe_add(POST_CHECK_ID, self.post_check, post_check_at)

        log.info(
            "Rescheduled: pre_alert at %s, post_check at %s",
            pre_alert_at.isoformat(),
            post_check_at.isoformat(),
        )

    # ---------- Internals ----------

    def _safe_add(self, job_id: str, func, when_utc: datetime):
        """Add a date-triggered job, replacing any existing job with the same id.
        If the time has already passed, skip silently.
        """
        now = datetime.now(timezone.utc)
        if when_utc <= now:
            # Time already passed; don't schedule something in the past.
            return
        self.scheduler.add_job(
            func,
            "date",
            run_date=when_utc,
            id=job_id,
            replace_existing=True,
            misfire_grace_time=60,
        )

    def _reschedule_from_last_refresh(self):
        """On bot startup, if the last refresh is recent enough, reschedule jobs."""
        last = database.get_last_refresh()
        if last is None:
            log.info("No previous refreshes found; waiting for the first /refresh.")
            return

        last_ts = last["timestamp"]
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        # If the buff already expired more than ALERT_DELAY_MINUTES ago, nothing to schedule
        expiry = last_ts + BUFF_DURATION
        now = datetime.now(timezone.utc)
        if now > expiry + timedelta(minutes=config.ALERT_DELAY_MINUTES):
            log.info("Last refresh too old to reschedule; waiting for /refresh.")
            return

        self.reschedule_after_refresh(last_ts)

    # ---------- Job handlers ----------

    async def pre_alert(self):
        """Fires shortly before the current buff is about to expire."""
        channel = self._channel()
        if channel is None:
            return
        await channel.send(
            f"🔔 **Fountain buff expires in {config.PRE_PING_MINUTES} min.** "
            f"Anyone available, use `/refresh` when you do it."
        )

    async def post_check(self):
        """Fires shortly after the buff should have been renewed."""
        last = database.get_last_refresh()
        channel = self._channel()
        if channel is None:
            return

        now = datetime.now(timezone.utc)
        if last is not None:
            last_ts = last["timestamp"]
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            # If the most recent refresh is newer than (now - ALERT_DELAY_MINUTES), buff is fine
            if last_ts >= now - timedelta(minutes=config.ALERT_DELAY_MINUTES + 1):
                return

        await channel.send(
            "🚨 **No refresh was logged.** The Fountain buff is down — anyone "
            "who can refresh, please do it and run `/refresh`."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduler(bot))
