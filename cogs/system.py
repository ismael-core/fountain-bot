"""System-level controls (developer-only).

Provides /system pause|resume|status|reset for managing the bot's runtime
state. Pause silences queue alerts and blacklist application without blocking
the rest of the bot — useful for taking the system offline during a game
update without breaking in-flight tickets. Reset wipes every data table
except the `game_link` config entry — destructive, two-step confirm, intended
as a one-time pre-launch cleanup of test noise.
"""
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import audit_log
import config
import database

log = logging.getLogger("fountain.system")


def _is_developer(member: discord.Member) -> bool:
    """True if the member has the configured DEVELOPER_ROLE_ID."""
    if not isinstance(member, discord.Member):
        return False
    if not config.DEVELOPER_ROLE_ID:
        return False
    return any(r.id == config.DEVELOPER_ROLE_ID for r in member.roles)


class ResetConfirmView(discord.ui.View):
    """Two-step confirm for /system reset.

    Bound to the developer who ran the command — nobody else can click the
    red button. Non-persistent (10-min timeout) because reset is a deliberate
    one-shot action, not something you'd want surviving a bot restart in an
    unconfirmed state.
    """

    def __init__(self, invoker_id: int):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id

    def _is_invoker(self, user: discord.abc.User) -> bool:
        return user.id == self.invoker_id

    @discord.ui.button(
        label="Yes, wipe everything",
        style=discord.ButtonStyle.danger,
        emoji="💣",
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_invoker(interaction.user):
            await interaction.response.send_message(
                "❌ Only the developer who ran `/system reset` can confirm.",
                ephemeral=True,
            )
            return

        # Do the wipe. Auto-pauses the system inside reset_all_data().
        deleted = database.reset_all_data()

        # Best-effort: clear in-memory scheduler jobs across cogs so they don't
        # fire against now-deleted rows. If a cog isn't loaded we skip silently.
        cleared_jobs = 0
        for cog_name in ("RefreshQueue", "Membership", "WRR"):
            cog = interaction.client.get_cog(cog_name)
            sched = getattr(cog, "scheduler", None) if cog else None
            if sched is None:
                continue
            try:
                jobs = sched.get_jobs()
                for job in jobs:
                    try:
                        job.remove()
                        cleared_jobs += 1
                    except Exception:
                        pass
            except Exception:
                log.exception("Failed to enumerate jobs for cog %s", cog_name)

        # Disable both buttons on the confirm message
        for child in self.children:
            child.disabled = True

        summary_lines = [
            f"💣 **System wiped by {interaction.user.mention}.**",
            "",
            "**Rows deleted:**",
        ]
        for table, count in deleted.items():
            summary_lines.append(f"• `{table}`: {count}")
        summary_lines.extend([
            "",
            f"• In-memory scheduler jobs cancelled: **{cleared_jobs}**",
            "",
            "✅ `game_link` was preserved.",
            "⏸️ System is now **paused** — run `/system resume` to activate.",
        ])

        await interaction.response.edit_message(
            content="\n".join(summary_lines),
            view=self,
        )

        await audit_log.log_event(
            interaction.client,
            "system_reset",
            user=interaction.user,
            mod=interaction.user,
            details=deleted,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_invoker(interaction.user):
            await interaction.response.send_message(
                "❌ Only the developer who ran `/system reset` can cancel.",
                ephemeral=True,
            )
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="✅ Reset cancelled. Nothing was deleted.",
            view=self,
        )


class System(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    system_group = app_commands.Group(
        name="system",
        description="Developer: pause, resume, status, or reset the bot",
    )

    async def _reject_non_dev(self, interaction: discord.Interaction) -> bool:
        """Ephemeral-reject + return True if the user is NOT a developer."""
        if not _is_developer(interaction.user):
            await interaction.response.send_message(
                "❌ This command is restricted to the **Developer** role.",
                ephemeral=True,
            )
            return True
        return False

    # ----------------------------------------------------------------
    # /system pause
    # ----------------------------------------------------------------

    @system_group.command(name="pause", description="Silence queue alerts and skip blacklist application")
    async def pause(self, interaction: discord.Interaction):
        if await self._reject_non_dev(interaction):
            return
        if database.is_paused():
            await interaction.response.send_message(
                "⏸️ System is already paused.", ephemeral=True
            )
            return
        database.set_paused(True, by_user_id=interaction.user.id)
        await interaction.response.send_message(
            f"⏸️ **System paused by {interaction.user.mention}.**\n"
            f"• Queue alerts (`pre_warn`, `active_ping`, `general`, `urgent`) won't fire.\n"
            f"• Blacklists won't be applied (refresh expiry + WRR confirm).\n"
            f"• Everything else (tickets, approvals, leaderboard counts) keeps working.\n\n"
            f"Run `/system resume` to bring it back.",
            allowed_mentions=discord.AllowedMentions(users=False),
        )
        await audit_log.log_event(
            self.bot, "system_paused", user=interaction.user, mod=interaction.user,
        )

    # ----------------------------------------------------------------
    # /system resume
    # ----------------------------------------------------------------

    @system_group.command(name="resume", description="Re-enable queue alerts and blacklist application")
    async def resume(self, interaction: discord.Interaction):
        if await self._reject_non_dev(interaction):
            return
        if not database.is_paused():
            await interaction.response.send_message(
                "▶️ System is already active (not paused).", ephemeral=True
            )
            return
        database.set_paused(False, by_user_id=interaction.user.id)
        await interaction.response.send_message(
            f"▶️ **System resumed by {interaction.user.mention}.**\n"
            f"Queue alerts and blacklist application are back ON.\n\n"
            f"⚠️ If you want the queue scheduler to actually arm alerts, "
            f"run `/setbuff time:<remaining>` so the bot knows the current buff state.",
            allowed_mentions=discord.AllowedMentions(users=False),
        )
        await audit_log.log_event(
            self.bot, "system_resumed", user=interaction.user, mod=interaction.user,
        )

    # ----------------------------------------------------------------
    # /system status
    # ----------------------------------------------------------------

    @system_group.command(name="status", description="Show pause state, buff state, and row counts")
    async def status(self, interaction: discord.Interaction):
        if await self._reject_non_dev(interaction):
            return

        paused = database.is_paused()
        buff_expires = database.get_buff_expires_at()
        queue = database.get_refresh_queue()
        counts = database.reset_dry_run_counts()

        if buff_expires is None:
            buff_line = "❌ No active buff state (run `/setbuff` to arm)"
        elif buff_expires > datetime.now(timezone.utc):
            buff_line = f"💧 Buff drops <t:{int(buff_expires.timestamp())}:R> (<t:{int(buff_expires.timestamp())}:F>)"
        else:
            buff_line = f"⏰ Buff already dropped (was <t:{int(buff_expires.timestamp())}:R>)"

        embed = discord.Embed(
            title=("⏸️ System: PAUSED" if paused else "▶️ System: ACTIVE"),
            color=(discord.Color.orange() if paused else discord.Color.green()),
        )
        embed.add_field(
            name="Buff state",
            value=buff_line,
            inline=False,
        )
        embed.add_field(
            name="Queue",
            value=f"{len(queue)} ticket(s) waiting for refresh proof",
            inline=False,
        )
        embed.add_field(
            name="Data (row counts)",
            value=(
                f"• refreshes: **{counts.get('refreshes', 0)}**\n"
                f"• tickets: **{counts.get('tickets', 0)}**\n"
                f"• blacklist: **{counts.get('blacklist', 0)}**\n"
                f"• wrr_tickets: **{counts.get('wrr_tickets', 0)}**\n"
                f"• mod_applications: **{counts.get('mod_applications', 0)}**\n"
                f"• audit_log_entries: **{counts.get('audit_log_entries', 0)}**"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------
    # /system reset
    # ----------------------------------------------------------------

    @system_group.command(
        name="reset",
        description="DESTRUCTIVE: wipe all data (refreshes, tickets, blacklist, WRR, audit log). Keeps game_link.",
    )
    async def reset(self, interaction: discord.Interaction):
        if await self._reject_non_dev(interaction):
            return

        counts = database.reset_dry_run_counts()
        total_rows = sum(counts.values())

        if total_rows == 0:
            await interaction.response.send_message(
                "ℹ️ Nothing to wipe — all data tables are already empty.",
                ephemeral=True,
            )
            return

        warning = discord.Embed(
            title="⚠️ DESTRUCTIVE — Confirm system wipe",
            description=(
                "This will **permanently delete** every row from these tables. "
                "`game_link` is preserved. The system will be **paused** after the wipe."
            ),
            color=discord.Color.red(),
        )
        warning.add_field(
            name="About to delete",
            value=(
                f"• refreshes (leaderboard): **{counts.get('refreshes', 0)}**\n"
                f"• tickets: **{counts.get('tickets', 0)}**\n"
                f"• blacklist: **{counts.get('blacklist', 0)}**\n"
                f"• wrr_tickets: **{counts.get('wrr_tickets', 0)}**\n"
                f"• mod_applications: **{counts.get('mod_applications', 0)}**\n"
                f"• mod_application_answers: **{counts.get('mod_application_answers', 0)}**\n"
                f"• audit_log_entries: **{counts.get('audit_log_entries', 0)}**\n"
                f"• other config keys: **{counts.get('config_keys_non_game_link', 0)}**"
            ),
            inline=False,
        )
        warning.set_footer(text="Only you (the invoker) can press the red button. Cancel = noop.")

        view = ResetConfirmView(invoker_id=interaction.user.id)
        await interaction.response.send_message(embed=warning, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(System(bot))
