"""Moderator-recruitment ticket flow.

When a user creates a ticket in MOD_TICKET_CATEGORY_ID, the bot asks a series
of questions one by one. Each user message is treated as the answer to the
current question. After the last question, a summary embed is posted with
Approve/Reject buttons for Management to review.
"""
import asyncio
import logging

import discord
from discord.ext import commands

import audit_log
import config
import database
from views import ModApplicationReviewView

log = logging.getLogger("fountain.mod_apps")


# ====================================================================
# Questions — edit this list to change what's asked.
# Order matters; questions are asked one by one in this exact order.
# ====================================================================

QUESTIONS = [
    "What's your in-game name (Roblox username)?",
    "What's your timezone, and what hours are you typically available?",
    "How long have you been part of this server, and how active are you?",
    "Do you have previous moderation experience? If yes, briefly describe.",
    "Why do you want to be a moderator here?",
    "How would you handle a user being toxic but not technically breaking explicit rules?",
    "Anything else you'd like us to know?",
]


# ====================================================================
# Helpers
# ====================================================================

def _in_mod_ticket_category(channel: discord.abc.GuildChannel) -> bool:
    if not isinstance(channel, discord.TextChannel):
        return False
    if config.MOD_TICKET_CATEGORY_ID == 0:
        return False
    return channel.category is not None and channel.category.id == config.MOD_TICKET_CATEGORY_ID


def _find_applicant(channel: discord.TextChannel) -> discord.Member | None:
    """Find the user who opened the ticket via the channel's member overwrites."""
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Member):
            continue
        if target.bot:
            continue
        if overwrite.view_channel is True:
            return target
    return None


def _question_embed(index: int, total: int, question: str, applicant: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title=f"Question {index + 1} of {total}",
        description=question,
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"Reply with your answer as a normal message.")
    return embed


def _summary_embed(applicant: discord.Member | None, answers: list[dict], application_id: int) -> discord.Embed:
    name = applicant.mention if applicant else f"<@{answers[0]['application_id']}>"

    desc_parts = [f"**Applicant:** {name}"]

    # Optional: show Bloxlink verification status if the role ID is configured.
    # Purely informational — does NOT block anyone, even unverified applicants
    # are still reviewed normally. Helps admins spot if someone went through
    # the trouble of linking their Roblox account.
    if config.BLOXLINK_VERIFIED_ROLE_ID and isinstance(applicant, discord.Member):
        is_verified = any(r.id == config.BLOXLINK_VERIFIED_ROLE_ID for r in applicant.roles)
        if is_verified:
            desc_parts.append("**Bloxlink:** ✅ Verified")
        else:
            desc_parts.append("**Bloxlink:** ⚠️ Not verified")

    desc_parts.append("Full answers below. Management can approve or reject.")

    embed = discord.Embed(
        title="📋 Moderator application — ready for review",
        description="\n\n".join(desc_parts),
        color=discord.Color.gold(),
    )
    for entry in answers:
        # Discord embed field values cap at 1024 chars
        answer = entry["answer_text"][:1020] + "…" if len(entry["answer_text"]) > 1024 else entry["answer_text"]
        embed.add_field(
            name=f"{entry['question_index'] + 1}. {entry['question_text'][:240]}",
            value=answer or "(no answer)",
            inline=False,
        )
    embed.set_footer(text=f"Application #{application_id}")
    return embed


# ====================================================================
# Cog
# ====================================================================

class ModApplications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------------------------------------------------------
    # on_guild_channel_create — auto-start when a mod ticket is opened
    # ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not _in_mod_ticket_category(channel):
            return

        # Wait for Ticket Tool to set up perms
        await asyncio.sleep(3)
        channel = self.bot.get_channel(channel.id) or channel

        # Don't double-start
        if database.get_mod_application_by_channel(channel.id) is not None:
            return

        applicant = _find_applicant(channel)
        if applicant is None:
            log.warning("Could not detect applicant for mod ticket channel %s", channel.id)
            return

        application_id = database.create_mod_application(applicant.id, channel.id)

        intro = discord.Embed(
            title="👋 Moderator application started",
            description=(
                f"Hi {applicant.mention}, thanks for your interest in becoming a moderator!\n\n"
                f"I'll ask you **{len(QUESTIONS)} questions**, one at a time. "
                f"Just reply with your answer as a normal message in this channel, "
                f"and the next question will appear automatically.\n\n"
                f"Take your time — there's no rush."
            ),
            color=discord.Color.blue(),
        )
        intro.set_footer(text=f"Application #{application_id}")

        try:
            await channel.send(content=applicant.mention, embed=intro)
            await channel.send(embed=_question_embed(0, len(QUESTIONS), QUESTIONS[0], applicant))
        except discord.DiscordException:
            log.exception("Failed to post intro/first question in %s", channel.id)
            return

        await audit_log.log_event(
            self.bot,
            "mod_app_started",
            user=applicant,
            ticket_id=application_id,
            details={"channel_id": channel.id},
        )

    # ----------------------------------------------------------------
    # on_message — collect each answer in order
    # ----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None or not _in_mod_ticket_category(message.channel):
            return
        # Plain text only — ignore attachment-only messages
        if not message.content.strip():
            return

        app = database.get_mod_application_by_channel(message.channel.id)
        if app is None:
            return
        if app["status"] != database.MOD_APP_ASKING:
            return  # already completed/reviewed, don't process more answers
        if message.author.id != app["user_id"]:
            return  # only the applicant's messages count

        index = app["current_question"]
        if index >= len(QUESTIONS):
            return  # shouldn't happen, but defensive

        # Save the answer
        question_text = QUESTIONS[index]
        database.save_mod_application_answer(
            application_id=app["id"],
            question_index=index,
            question=question_text,
            answer=message.content,
        )

        next_index = index + 1
        database.advance_mod_application_question(app["id"], next_index)

        if next_index < len(QUESTIONS):
            # More questions to go
            try:
                await message.channel.send(
                    embed=_question_embed(next_index, len(QUESTIONS), QUESTIONS[next_index], message.author),
                )
            except discord.DiscordException:
                log.exception("Failed to post next question in %s", message.channel.id)
        else:
            # Final question answered — finalize the application
            database.complete_mod_application(app["id"])
            answers = database.get_mod_application_answers(app["id"])

            try:
                await message.channel.send(
                    content=f"<@&{config.ADMIN_ROLE_ID}>",
                    embed=_summary_embed(message.author, answers, app["id"]),
                    view=ModApplicationReviewView(),
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False),
                )
            except discord.DiscordException:
                log.exception("Failed to post summary for application %s", app["id"])

            try:
                await message.channel.send(
                    f"✅ Thanks {message.author.mention}, that's all the questions. "
                    f"A Management member will review your application and get back to you soon."
                )
            except discord.DiscordException:
                pass

            await audit_log.log_event(
                self.bot,
                "mod_app_completed",
                user=message.author,
                ticket_id=app["id"],
            )


async def setup(bot: commands.Bot):
    # Only load the cog if the mod ticket category is actually configured
    if config.MOD_TICKET_CATEGORY_ID == 0:
        log.info("MOD_TICKET_CATEGORY_ID not set; mod_applications cog will not be loaded.")
        return
    await bot.add_cog(ModApplications(bot))
