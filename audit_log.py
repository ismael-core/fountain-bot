"""Audit log helper: writes structured records to DB and posts a human-readable
summary to the configured logs channel.

Every event the bot handles should call `log_event` so there's a single
source of truth for moderators reviewing what happened.
"""
import logging
from typing import Optional

import discord

import config
import database

log = logging.getLogger("fountain.audit")


# Pretty color mapping per event type for the embed posted to #fountain-logs
_COLORS = {
    "ticket_started": discord.Color.blue(),
    "proof_uploaded": discord.Color.light_grey(),
    "refresh_approved": discord.Color.green(),
    "refresh_rejected": discord.Color.orange(),
    "refresh_extended": discord.Color.teal(),
    "ticket_expired": discord.Color.gold(),
    "blacklisted": discord.Color.red(),
    "unbanned": discord.Color.purple(),
    "link_updated": discord.Color.dark_grey(),
}


async def log_event(
    bot: discord.Client,
    event_type: str,
    *,
    user: Optional[discord.abc.User] = None,
    mod: Optional[discord.abc.User] = None,
    ticket_id: Optional[int] = None,
    details: Optional[dict] = None,
    description: str = "",
):
    """Persist the event in the DB and post a summary embed to the logs channel."""
    # 1. Persist to DB (always)
    try:
        database.write_audit_entry(
            event_type=event_type,
            user_id=user.id if user else None,
            mod_id=mod.id if mod else None,
            ticket_id=ticket_id,
            details=details,
        )
    except Exception:
        log.exception("Failed to persist audit entry for event %s", event_type)

    # 2. Post embed to the logs channel (best-effort)
    channel = bot.get_channel(config.LOGS_CHANNEL_ID)
    if channel is None:
        log.warning("Logs channel %s not found, skipping embed post", config.LOGS_CHANNEL_ID)
        return

    title = event_type.replace("_", " ").title()
    color = _COLORS.get(event_type, discord.Color.default())

    embed = discord.Embed(title=title, description=description or None, color=color)
    if user is not None:
        embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
    if mod is not None:
        embed.add_field(name="Moderator", value=f"{mod.mention} (`{mod.id}`)", inline=True)
    if ticket_id is not None:
        embed.add_field(name="Ticket", value=f"#{ticket_id}", inline=True)
    if details:
        # Render details compactly without overwhelming the embed
        rendered = "\n".join(f"**{k}:** {v}" for k, v in details.items())
        if len(rendered) > 1000:
            rendered = rendered[:1000] + "…"
        embed.add_field(name="Details", value=rendered, inline=False)

    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        log.exception("Failed to send audit embed to logs channel")
