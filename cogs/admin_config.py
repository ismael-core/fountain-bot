"""Admin commands: /set_link, /unban, /queue, /dashboard."""
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import audit_log
import config
import database

log = logging.getLogger("fountain.admin")


def _has_admin_role(member: discord.Member) -> bool:
    """True if the member has any role in ELEVATED_ROLE_IDS (admin or developer)."""
    return isinstance(member, discord.Member) and any(
        r.id in config.ELEVATED_ROLE_IDS for r in member.roles
    )


def _has_mod_or_admin(member: discord.Member) -> bool:
    """True if the member has the mod role OR any elevated role."""
    if not isinstance(member, discord.Member):
        return False
    allowed = set(config.ELEVATED_ROLE_IDS) | {config.MOD_ROLE_ID}
    return any(r.id in allowed for r in member.roles)


class AdminConfig(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------------------------------------------------------
    # /set_link — update the game link (admins only, in management channel)
    # ----------------------------------------------------------------

    @app_commands.command(
        name="set_link",
        description="Update the game link shown in tickets (admins only)",
    )
    @app_commands.describe(url="The new game link")
    async def set_link(self, interaction: discord.Interaction, url: str):
        if interaction.channel_id != config.MANAGEMENT_CHANNEL_ID:
            await interaction.response.send_message(
                f"❌ This command only works in <#{config.MANAGEMENT_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        if not _has_admin_role(interaction.user):
            await interaction.response.send_message(
                "❌ Only admins can update the game link.", ephemeral=True
            )
            return

        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await interaction.response.send_message(
                "❌ URL must start with http:// or https://", ephemeral=True
            )
            return

        old = database.get_config("game_link")
        database.set_config("game_link", url, updated_by=interaction.user.id)

        await interaction.response.send_message(
            f"✅ Game link updated.\n"
            f"**Old:** {old or '(none)'}\n"
            f"**New:** {url}"
        )

        await audit_log.log_event(
            self.bot,
            "link_updated",
            mod=interaction.user,
            details={"old": old or "(none)", "new": url},
        )

    # ----------------------------------------------------------------
    # /unban — manually remove a user from the blacklist
    # ----------------------------------------------------------------

    @app_commands.command(
        name="unban",
        description="Remove a user from the blacklist manually (admins only)",
    )
    @app_commands.describe(user="The user to unban")
    async def unban(self, interaction: discord.Interaction, user: discord.User):
        if not _has_admin_role(interaction.user):
            await interaction.response.send_message(
                "❌ Only admins can unban users.", ephemeral=True
            )
            return

        entry = database.get_blacklist_entry(user.id)
        if entry is None:
            await interaction.response.send_message(
                f"ℹ️ {user.mention} is not in the blacklist.", ephemeral=True
            )
            return

        removed = database.remove_from_blacklist(user.id)
        if not removed:
            await interaction.response.send_message(
                f"⚠️ Failed to remove {user.mention} from the blacklist.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ {user.mention} removed from the blacklist (previous strike count: {entry['strike_count']})."
        )

        await audit_log.log_event(
            self.bot,
            "unbanned",
            user=user,
            mod=interaction.user,
            details={"previous_strikes": entry["strike_count"]},
        )

        # Also post to the dedicated blacklist channel if configured
        if config.BLACKLIST_CHANNEL_ID:
            bl_channel = self.bot.get_channel(config.BLACKLIST_CHANNEL_ID)
            if bl_channel is not None:
                embed = discord.Embed(
                    title="✅ User unbanned",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(
                    name="User", value=f"{user.mention} (`{user.id}`)", inline=False
                )
                embed.add_field(
                    name="Unbanned by", value=f"{interaction.user.mention}", inline=True
                )
                embed.add_field(
                    name="Previous strikes",
                    value=str(entry["strike_count"]),
                    inline=True,
                )
                try:
                    await bl_channel.send(embed=embed)
                except discord.DiscordException:
                    log.exception("Failed to post unban to blacklist channel")

    # ----------------------------------------------------------------
    # /cleanup_staff_blacklist — remove every staff member who was wrongly blacklisted
    # ----------------------------------------------------------------

    @app_commands.command(
        name="cleanup_staff_blacklist",
        description="Unban anyone with a protected staff role who got blacklisted by mistake (admins only)",
    )
    async def cleanup_staff_blacklist(self, interaction: discord.Interaction):
        if not _has_admin_role(interaction.user):
            await interaction.response.send_message(
                "❌ Only admins can run this.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        protected_ids = set(config.PROTECTED_ROLE_IDS)

        cleaned = []
        skipped = []
        not_found = []

        for entry in database.list_blacklisted_users():
            uid = entry["user_id"]

            # Is this user a server owner or do they have a protected role?
            is_protected = False
            if guild.owner_id == uid:
                is_protected = True
            else:
                member = guild.get_member(uid)
                if member is None:
                    try:
                        member = await guild.fetch_member(uid)
                    except discord.DiscordException:
                        not_found.append(uid)
                        continue
                if any(r.id in protected_ids for r in member.roles):
                    is_protected = True

            if is_protected:
                database.remove_from_blacklist(uid)
                cleaned.append(uid)
                await audit_log.log_event(
                    self.bot,
                    "unbanned",
                    user=member if isinstance(member, discord.Member) else None,
                    mod=interaction.user,
                    details={
                        "previous_strikes": entry["strike_count"],
                        "reason": "cleanup_staff_blacklist: user has protected role",
                    },
                )
            else:
                skipped.append(uid)

        lines = [f"✅ Cleanup done. **{len(cleaned)} staff member(s) unbanned.**"]
        if cleaned:
            lines.append("Unbanned: " + ", ".join(f"<@{u}>" for u in cleaned))
        if skipped:
            lines.append(f"Kept blacklisted (no protected role): {len(skipped)}")
        if not_found:
            lines.append(f"Could not check (left server): {len(not_found)}")

        await interaction.followup.send(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions(users=False),
        )

    # ----------------------------------------------------------------
    # /queue — show the current waiting tickets
    # ----------------------------------------------------------------

    @app_commands.command(
        name="queue",
        description="Show tickets currently waiting for their turn",
    )
    async def queue(self, interaction: discord.Interaction):
        if not _has_mod_or_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Mod or admin only.", ephemeral=True
            )
            return

        waiting = database.get_waiting_queue()

        if not waiting:
            await interaction.response.send_message(
                "🟢 Queue is empty — no tickets waiting.", ephemeral=True
            )
            return

        lines = [f"**🕐 Tickets waiting ({len(waiting)}):**", ""]
        for i, t in enumerate(waiting, 1):
            opened_ts = int(t["created_at"].timestamp())
            lines.append(
                f"`{i}.` <@{t['user_id']}> — status `{t['status']}` — opened <t:{opened_ts}:R> "
                f"— <#{t['channel_id']}>"
            )

        if len(waiting) >= config.QUEUE_ALERT_THRESHOLD:
            lines.append("")
            lines.append(
                f"⚠️ Queue has {len(waiting)} tickets — consider opening the second server."
            )

        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ----------------------------------------------------------------
    # /dashboard — show all active tickets with their remaining time
    # ----------------------------------------------------------------

    @app_commands.command(
        name="dashboard",
        description="Show all active tickets with remaining AFK time (mods/admins)",
    )
    async def dashboard(self, interaction: discord.Interaction):
        if not _has_mod_or_admin(interaction.user):
            await interaction.response.send_message(
                "❌ Mod or admin only.", ephemeral=True
            )
            return

        active = database.get_active_tickets()
        waiting = database.get_waiting_queue()

        embed = discord.Embed(
            title="🪙 Fountain dashboard",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )

        if active:
            lines = []
            for t in active:
                expires_ts = int(t["expires_at"].timestamp())
                marker = "🟢" if t["status"] == database.TICKET_ACTIVE else "🟠"
                lines.append(
                    f"{marker} <@{t['user_id']}> — expires <t:{expires_ts}:R> "
                    f"({t['refresh_count']}/{config.MAX_REFRESHES_PER_TICKET} refreshes) "
                    f"— <#{t['channel_id']}>"
                )
            embed.add_field(name=f"Active ({len(active)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Active", value="*No active tickets*", inline=False)

        if waiting:
            wait_lines = []
            for t in waiting[:10]:
                opened_ts = int(t["created_at"].timestamp())
                wait_lines.append(
                    f"<@{t['user_id']}> — `{t['status']}` since <t:{opened_ts}:R>"
                )
            extra = f"\n*... and {len(waiting) - 10} more*" if len(waiting) > 10 else ""
            embed.add_field(
                name=f"Waiting ({len(waiting)})",
                value="\n".join(wait_lines) + extra,
                inline=False,
            )

        if len(waiting) >= config.QUEUE_ALERT_THRESHOLD:
            embed.add_field(
                name="⚠️ Queue alert",
                value=f"{len(waiting)} tickets waiting — consider opening the second server.",
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminConfig(bot))
