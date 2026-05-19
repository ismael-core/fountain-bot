"""Weekly slot management commands."""
import discord
from discord import app_commands
from discord.ext import commands

import database

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DAY_CHOICES = [
    app_commands.Choice(name="Monday", value=0),
    app_commands.Choice(name="Tuesday", value=1),
    app_commands.Choice(name="Wednesday", value=2),
    app_commands.Choice(name="Thursday", value=3),
    app_commands.Choice(name="Friday", value=4),
    app_commands.Choice(name="Saturday", value=5),
    app_commands.Choice(name="Sunday", value=6),
]


class Slots(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    slot_group = app_commands.Group(
        name="slot",
        description="Manage Fountain refresh slots",
    )

    @slot_group.command(name="add", description="Sign up for a recurring weekly slot")
    @app_commands.choices(day=DAY_CHOICES)
    @app_commands.describe(hour="Hour of the day (0-23) in the bot's timezone")
    async def slot_add(
        self,
        interaction: discord.Interaction,
        day: app_commands.Choice[int],
        hour: int,
    ):
        if not (0 <= hour <= 23):
            await interaction.response.send_message(
                "Hour must be between 0 and 23",
                ephemeral=True,
            )
            return

        ok = database.add_slot(
            interaction.user.id,
            str(interaction.user),
            day.value,
            hour,
        )
        if not ok:
            current = database.get_slot_for(day.value, hour)
            await interaction.response.send_message(
                f"❌ That slot is already covered by <@{current['user_id']}>",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.send_message(
            f"✅ {interaction.user.mention} assigned to **{day.name} {hour:02d}:00**"
        )

    @slot_group.command(name="remove", description="Remove yourself from a slot")
    @app_commands.choices(day=DAY_CHOICES)
    @app_commands.describe(hour="Hour of the day (0-23)")
    async def slot_remove(
        self,
        interaction: discord.Interaction,
        day: app_commands.Choice[int],
        hour: int,
    ):
        if not (0 <= hour <= 23):
            await interaction.response.send_message(
                "Hour must be between 0 and 23",
                ephemeral=True,
            )
            return

        ok = database.remove_slot(interaction.user.id, day.value, hour)
        if not ok:
            await interaction.response.send_message(
                "❌ You don't have that slot assigned",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Slot **{day.name} {hour:02d}:00** released"
        )

    @slot_group.command(name="list", description="Show all slots and who covers them")
    async def slot_list(self, interaction: discord.Interaction):
        slots = database.get_all_slots()
        if not slots:
            await interaction.response.send_message("No slots assigned yet.")
            return

        by_day: dict[int, list[dict]] = {i: [] for i in range(7)}
        for s in slots:
            by_day[s["day_of_week"]].append(s)

        lines = ["**📅 Weekly slots**", ""]
        for day_idx in range(7):
            day_slots = by_day[day_idx]
            if not day_slots:
                continue
            lines.append(f"**{DAY_NAMES[day_idx]}**")
            for s in day_slots:
                lines.append(f"  `{s['hour']:02d}:00` — <@{s['user_id']}>")
            lines.append("")

        coverage = len(slots)
        total = 24 * 7
        pct = coverage * 100 // total
        lines.append(f"*Coverage: {coverage}/{total} slots ({pct}%)*")

        await interaction.response.send_message(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @slot_group.command(name="mine", description="See your assigned slots")
    async def slot_mine(self, interaction: discord.Interaction):
        slots = database.get_user_slots(interaction.user.id)
        if not slots:
            await interaction.response.send_message(
                "You have no slots assigned.",
                ephemeral=True,
            )
            return

        lines = [f"**Your slots ({len(slots)} per week)**", ""]
        for s in slots:
            lines.append(f"  `{DAY_NAMES[s['day_of_week']]} {s['hour']:02d}:00`")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Slots(bot))
