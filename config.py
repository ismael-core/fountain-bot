"""Configuration loaded from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Atlantic/Canary")
PRE_PING_MINUTES = int(os.getenv("PRE_PING_MINUTES", "5"))
ALERT_DELAY_MINUTES = int(os.getenv("ALERT_DELAY_MINUTES", "5"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is required in .env")
if not GUILD_ID:
    raise RuntimeError("GUILD_ID is required in .env")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is required in .env")
if not (0 < PRE_PING_MINUTES < 60):
    raise RuntimeError("PRE_PING_MINUTES must be between 1 and 59")
if not (0 <= ALERT_DELAY_MINUTES < 60):
    raise RuntimeError("ALERT_DELAY_MINUTES must be between 0 and 59")
