# Fountain Bot

Discord bot that manages hourly Fountain refresh duty in a Roblox/game server.
Tracks who refreshes, assigns weekly slots, pings the assignee 5 min before
their hour, and alerts the channel if a refresh is missed.

## Features

- `/refresh` — log a refresh you just did
- `/leaderboard [days]` — public ranking of refreshes per person
- `/stats` — your personal counts (week / month / all-time)
- `/slot add <day> <hour>` — sign up for a fixed weekly hour
- `/slot remove <day> <hour>` — leave a slot
- `/slot list` — view the full weekly schedule
- `/slot mine` — view your assigned slots
- Automatic ping to the next slot's owner 5 min before the hour
- Automatic alert if no refresh was logged 5 min into the hour

## Stack

- Python 3.11+
- discord.py 2.x (slash commands)
- SQLite (single file, no external DB needed)
- APScheduler (cron-style triggers)

---

## 1. Create the bot on Discord

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Open the **Bot** tab → **Reset Token** → copy the token (you'll only see it once).
3. Under **Privileged Gateway Intents**, leave everything **off**. The bot uses only slash commands.
4. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: `Send Messages`, `Embed Links`, `Mention Everyone`
   - Copy the generated URL, open it in your browser, and invite the bot to your server.

Enable **Developer Mode** in Discord (Settings → Advanced) so you can right-click
your server and channel to copy their IDs.

---

## 2. Local setup

```bash
git clone <your-repo-url> fountain-bot
cd fountain-bot

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# edit .env and fill in DISCORD_TOKEN, GUILD_ID, CHANNEL_ID

python bot.py
```

When you see `Slash commands synced to guild ...` the bot is ready.
Type `/` in your server and the commands should appear.

---

## 3. Deploy on a VPS (DigitalOcean / Hetzner / etc.) with systemd

Assuming Ubuntu 24.04 and that you cloned the repo to `/opt/fountain-bot`:

```bash
sudo apt update && sudo apt install -y python3-venv

sudo useradd --system --shell /usr/sbin/nologin --home /opt/fountain-bot fountain
sudo chown -R fountain:fountain /opt/fountain-bot

sudo -u fountain python3 -m venv /opt/fountain-bot/.venv
sudo -u fountain /opt/fountain-bot/.venv/bin/pip install -r /opt/fountain-bot/requirements.txt

# create .env at /opt/fountain-bot/.env (chmod 600 + chown fountain:fountain)
```

Create `/etc/systemd/system/fountain-bot.service`:

```ini
[Unit]
Description=Fountain Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=fountain
Group=fountain
WorkingDirectory=/opt/fountain-bot
EnvironmentFile=/opt/fountain-bot/.env
ExecStart=/opt/fountain-bot/.venv/bin/python /opt/fountain-bot/bot.py
Restart=on-failure
RestartSec=5

# basic hardening
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fountain-bot
sudo systemctl status fountain-bot
sudo journalctl -u fountain-bot -f   # live logs
```

---

## 4. Backup

The whole state lives in one file: `fountain.db`.
A daily cron is enough:

```cron
0 4 * * * cp /opt/fountain-bot/fountain.db /opt/fountain-bot/backups/fountain-$(date +\%F).db && find /opt/fountain-bot/backups -name 'fountain-*.db' -mtime +30 -delete
```

---

## 5. Customizing schedule timing

Edit `.env`:

- `PRE_PING_MINUTES=5` → pings the next slot owner at `:55` of each hour
- `ALERT_DELAY_MINUTES=5` → if no `/refresh` was logged by `:05` of the hour, posts an alert
- `TIMEZONE=Atlantic/Canary` → IANA timezone the schedule runs in

Restart the bot after editing.

---

## Notes & limits

- There's no anti-cheat on `/refresh`. The system relies on social accountability
  (everyone sees the leaderboard). If someone fakes refreshes, the buff won't
  actually be active in the game and it gets noticed within an hour.
- Slots are one user per `(day, hour)` pair. If you want shared/co-covered slots,
  drop the `UNIQUE(day_of_week, hour)` constraint in `database.py` and adjust
  the scheduler's ping logic to mention all assignees.
- All timestamps in the DB are UTC. Display conversion happens at the boundary.
