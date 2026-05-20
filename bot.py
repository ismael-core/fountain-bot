"""Entry point for the Fountain refresh bot."""
import logging

import discord
from discord.ext import commands

import config
import database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("fountain")

intents = discord.Intents.default()
# message_content is NOT needed because everything is slash commands


class FountainBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        database.init_db()
        await self.load_extension("cogs.refresh")
        await self.load_extension("cogs.scheduler")
        await self.load_extension("cogs.admin")

        # Sync slash commands to a single guild — instant, no 1-hour propagation
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
