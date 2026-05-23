"""Entry point for the Fountain ticket bot."""
import logging

import discord
from discord.ext import commands

import config
import database
from views import (
    ApprovalView,
    ModApplicationReviewView,
    RobuxVerifyView,
    StartTicketView,
    WRRApprovalView,
    WRRBlacklistConfirmView,
    WRRTierSelectView,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("fountain")

# Default intents + message content (required so the bot can read attachments
# in messages users post in ticket channels for proof verification) + members
# (required so guild.get_member() returns a Member with .roles, which we need
# to check protected staff roles before blacklisting anyone).
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class FountainBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        database.init_db()

        # Load all cogs
        await self.load_extension("cogs.refresh")
        await self.load_extension("cogs.refresh_queue")
        await self.load_extension("cogs.admin")
        await self.load_extension("cogs.admin_config")
        await self.load_extension("cogs.tickets")
        await self.load_extension("cogs.membership")
        await self.load_extension("cogs.mod_applications")
        await self.load_extension("cogs.wrr")
        await self.load_extension("cogs.justcode")  # TEMP — remove when the joke gets old

        # Register persistent views so buttons keep working after restarts
        self.add_view(RobuxVerifyView())
        self.add_view(StartTicketView())
        self.add_view(ApprovalView())
        self.add_view(ModApplicationReviewView())
        self.add_view(WRRTierSelectView())
        self.add_view(WRRApprovalView())
        self.add_view(WRRBlacklistConfirmView())

        # Sync slash commands to the guild for instant availability
        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s", config.GUILD_ID)

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)


def main():
    bot = FountainBot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
