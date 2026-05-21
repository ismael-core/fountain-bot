"""Configuration loaded from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# Server-wide IDs needed for the ticket flow
MANAGEMENT_CHANNEL_ID = int(os.getenv("MANAGEMENT_CHANNEL_ID", "0"))
LOGS_CHANNEL_ID = int(os.getenv("LOGS_CHANNEL_ID", "0"))
BLACKLIST_CHANNEL_ID = int(os.getenv("BLACKLIST_CHANNEL_ID", "0"))  # optional
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
MOD_TICKET_CATEGORY_ID = int(os.getenv("MOD_TICKET_CATEGORY_ID", "0"))  # category for mod-recruitment applications
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
DEVELOPER_ROLE_ID = int(os.getenv("DEVELOPER_ROLE_ID", "0"))
OWNER_ROLE_ID = int(os.getenv("OWNER_ROLE_ID", "0"))  # optional, for a custom Owner role
BLOXLINK_VERIFIED_ROLE_ID = int(os.getenv("BLOXLINK_VERIFIED_ROLE_ID", "0"))  # optional, shown in mod application summary

# Roles immune from being blacklisted. Defaults to admin + developer + mod
# + owner so staff can never accidentally lock themselves out of their own server.
# The guild owner (Discord's built-in server owner) is also always protected,
# checked separately in membership.py.
PROTECTED_ROLE_IDS = list({
    rid for rid in (ADMIN_ROLE_ID, DEVELOPER_ROLE_ID, MOD_ROLE_ID, OWNER_ROLE_ID) if rid
})

# Roles that can run elevated commands like /set_link and /unban.
# Includes both admin and developer.
ELEVATED_ROLE_IDS = list({
    rid for rid in (ADMIN_ROLE_ID, DEVELOPER_ROLE_ID) if rid
})

TIMEZONE = os.getenv("TIMEZONE", "Atlantic/Canary")
PRE_PING_MINUTES = int(os.getenv("PRE_PING_MINUTES", "5"))
ALERT_DELAY_MINUTES = int(os.getenv("ALERT_DELAY_MINUTES", "5"))

# Per-refresh AFK time and max refreshes allowed in a single active ticket
AFK_HOURS_PER_REFRESH = int(os.getenv("AFK_HOURS_PER_REFRESH", "6"))
MAX_REFRESHES_PER_TICKET = int(os.getenv("MAX_REFRESHES_PER_TICKET", "4"))

# Grace period (minutes) between AFK expiration and blacklisting
GRACE_MINUTES = int(os.getenv("GRACE_MINUTES", "10"))
AUTO_CLOSE_HOURS_AFTER_EXPIRY = int(os.getenv("AUTO_CLOSE_HOURS_AFTER_EXPIRY", "24"))

# Comma-separated list of minutes before AFK expiration to send reminders
PRE_EXPIRY_REMINDERS = [
    int(x.strip()) for x in os.getenv("PRE_EXPIRY_REMINDERS", "30,10,5").split(",")
    if x.strip().isdigit()
]

# Queue size that triggers the "consider opening second server" alert
QUEUE_ALERT_THRESHOLD = int(os.getenv("QUEUE_ALERT_THRESHOLD", "5"))

# Blacklist progression in hours (4th strike means permanent)
BLACKLIST_DURATIONS_HOURS = [24, 48, 168]  # 1st=24h, 2nd=48h, 3rd=7d, 4th=permanent

# --- Validation ---

_required = {
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "GUILD_ID": GUILD_ID,
    "CHANNEL_ID": CHANNEL_ID,
    "MANAGEMENT_CHANNEL_ID": MANAGEMENT_CHANNEL_ID,
    "LOGS_CHANNEL_ID": LOGS_CHANNEL_ID,
    "TICKET_CATEGORY_ID": TICKET_CATEGORY_ID,
    "MOD_ROLE_ID": MOD_ROLE_ID,
    "ADMIN_ROLE_ID": ADMIN_ROLE_ID,
}
for _name, _val in _required.items():
    if not _val:
        raise RuntimeError(f"{_name} is required in .env")

if not (0 < PRE_PING_MINUTES < 60):
    raise RuntimeError("PRE_PING_MINUTES must be between 1 and 59")
if not (0 <= ALERT_DELAY_MINUTES < 60):
    raise RuntimeError("ALERT_DELAY_MINUTES must be between 0 and 59")
if AFK_HOURS_PER_REFRESH <= 0:
    raise RuntimeError("AFK_HOURS_PER_REFRESH must be positive")
if MAX_REFRESHES_PER_TICKET <= 0:
    raise RuntimeError("MAX_REFRESHES_PER_TICKET must be positive")
