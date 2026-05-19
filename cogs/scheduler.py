"""Scheduled jobs: pre-ping the assigned user and alert on missed refreshes."""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import database

log = logging.getLogger("fountain.scheduler")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class Scheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tz = ZoneInfo(config.TIMEZONE)
        self.scheduler = AsyncIOScheduler(timezone=self.tz)

    async def cog_load(self):
        pre_ping_minute = 60 - config.PRE_PING_MINUTES
        self.scheduler.add_job(
            self.pre_ping_next_slot,
            CronTrigger(minute=pre_ping_minute, timezone=self.tz),
            id="pre_ping",
            misfire_grace_time=60,
        )
        self.scheduler.add_job(
            self.check_missed_refresh,
            CronTrigger(minute=config.ALERT_DELAY_MINUTES, timezone=self.tz),
            id="post_check",
            misfire_grace_time=60,
        )
        self.scheduler.start()
        log.info(
            "Scheduler started. Pre-ping at :%02d, post-check at :%02d (tz=%s)",
            pre_ping_minute,
            config.ALERT_DELAY_MINUTES,
            config.TIMEZONE,
        )

    async def cog_unload(self):
        self.scheduler.shutdown(wait=False)

    def _channel(self) -> discord.TextChannel | None:
        ch = self.bot.get_channel(config.CHANNEL_ID)
        if ch is None:
            log.warning("Channel %s not found", config.CHANNEL_ID)
        return ch

    async def pre_ping_next_slot(self):
        """Ping the user assigned to the upcoming hour."""
        now_local = datetime.now(self.tz)
        # The next hour we're about to enter
        next_hour_dt = (
            now_local + timedelta(minutes=config.PRE_PING_MINUTES + 1)
        ).replace(minute=0, second=0, microsecond=0)

        day = next_hour_dt.weekday()
        hour = next_hour_dt.hour

        slot = database.get_slot_for(day, hour)
        channel = self._channel()
        if channel is None:
            return

        if slot is None:
            await channel.send(
                f"⚠️ **Uncovered slot**: {DAY_NAMES[day]} {hour:02d}:00 "
                f"(in {config.PRE_PING_MINUTES} min). If anyone wants to take it, "
                f"use `/refresh` when the hour starts."
            )
            return

        await channel.send(
            f"⏰ <@{slot['user_id']}>, you're up for the **{hour:02d}:00** refresh "
            f"(in {config.PRE_PING_MINUTES} min). Use `/refresh` when you do it."
        )

    async def check_missed_refresh(self):
        """If the current hour started but no refresh was logged, post an alert."""
        now_local = datetime.now(self.tz)
        hour_start_local = now_local.replace(minute=0, second=0, microsecond=0)
        hour_start_utc = hour_start_local.astimezone(timezone.utc)

        if database.has_refresh_since(hour_start_utc):
            return

        day = hour_start_local.weekday()
        hour = hour_start_local.hour
        slot = database.get_slot_for(day, hour)

        channel = self._channel()
        if channel is None:
            return

        if slot:
            await channel.send(
                f"🚨 No refresh logged for **{hour:02d}:00**. "
                f"<@{slot['user_id']}> had this slot — can you cover it, "
                f"or someone take over?"
            )
        else:
            await channel.send(
                f"🚨 No refresh logged for **{hour:02d}:00** "
                f"and this slot has no one assigned. Anyone able to cover?"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduler(bot))
