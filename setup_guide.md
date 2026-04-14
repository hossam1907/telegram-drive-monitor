# 📋 Setup Guide — Telegram Drive Monitor Bot

This guide walks you through setting up the bot from scratch, including Google Cloud, Telegram, and deployment options.

---

## Table of Contents

1. [Google Cloud Project Setup](#1-google-cloud-project-setup)
2. [Enable Google Drive API](#2-enable-google-drive-api)
3. [Create a Service Account](#3-create-a-service-account)
4. [Share Your Drive Folder](#4-share-your-drive-folder)
5. [Create a Telegram Bot](#5-create-a-telegram-bot)
6. [Configure the Project](#6-configure-the-project)
7. [Run Locally](#7-run-locally)
8. [Verify the Bot](#8-verify-the-bot)
9. [Deploy to Production](#9-deploy-to-production)
10. [Troubleshooting](#10-troubleshooting)
11. [Security Best Practices](#11-security-best-practices)

---

## 1. Google Cloud Project Setup

1. Go to [https://console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. Click the project dropdown at the top and select **New Project**.
3. Enter a project name (e.g. `drive-monitor`) and click **Create**.
4. Wait for the project to be created and make sure it is selected.

---

## 2. Enable Google Drive API

1. In the Google Cloud Console, open the navigation menu (☰) and go to **APIs & Services → Library**.
2. Search for **Google Drive API**.
3. Click on it and press **Enable**.

---

## 3. Create a Service Account

1. Go to **APIs & Services → Credentials**.
2. Click **Create Credentials → Service account**.
3. Fill in:
   - **Name**: `drive-monitor-bot` (or any name)
   - **Description**: optional
4. Click **Create and continue**.
5. Under **Grant this service account access to project**, select the role **Viewer** (or leave blank for now — the folder will be shared explicitly in the next step).
6. Click **Done**.
7. Back on the Credentials page, click the service account email you just created.
8. Go to the **Keys** tab → **Add Key → Create new key → JSON**.
9. Download the JSON file and save it as `credentials.json` in the project directory.

> ⚠️ Never commit `credentials.json` to version control.

---

## 4. Share Your Drive Folder

1. Open [Google Drive](https://drive.google.com) and navigate to the folder you want to monitor.
2. Right-click the folder → **Share**.
3. In the **Add people and groups** field, paste the service account email address (looks like `drive-monitor-bot@your-project.iam.gserviceaccount.com`).
4. Set the role to **Viewer**.
5. Click **Send** (or **Share**).
6. Note the folder ID from the URL:
   ```
   https://drive.google.com/drive/folders/<FOLDER_ID>
   ```

---

## 5. Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Follow the prompts:
   - Choose a display name (e.g. `Drive Monitor`)
   - Choose a username ending in `bot` (e.g. `my_drive_monitor_bot`)
4. BotFather will give you a **bot token** — copy it.
5. Find your own Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).

---

## 6. Configure the Project

```bash
# In the project directory
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ADMIN_USER_IDS=123456789          # Your Telegram user ID
DRIVE_FOLDER_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs   # From step 4
GOOGLE_CREDENTIALS_FILE=credentials.json

# Optional — adjust as needed
POLL_INTERVAL=300     # Seconds between Drive polls (default: 5 min)
PAGE_SIZE=10          # Files per page in /list
LOG_LEVEL=INFO
```

---

## 7. Run Locally

```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start the bot
python main.py
```

You should see log output similar to:

```
2024-01-15 10:00:00 [INFO] config: Configuration loaded. Poll interval: 300s, ...
2024-01-15 10:00:00 [INFO] database: Database initialised at 'drive_monitor.db'.
2024-01-15 10:00:00 [INFO] google_drive_service: GoogleDriveService initialised for folder '...'.
2024-01-15 10:00:00 [INFO] main: Starting Telegram Drive Monitor Bot…
```

---

## 8. Verify the Bot

1. Open Telegram and find your bot by its username.
2. Send `/start` — you should receive the welcome message.
3. Send `/status` — should show monitoring is active.
4. Add a file to the monitored Drive folder.
5. Wait for the next poll cycle (up to `POLL_INTERVAL` seconds) — you'll get an alert.

---

## 9. Deploy to Production

### Option A: systemd (Recommended for VPS)

Create the service file:

```bash
sudo nano /etc/systemd/system/drive-monitor.service
```

```ini
[Unit]
Description=Telegram Drive Monitor Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/telegram-drive-monitor
EnvironmentFile=/home/ubuntu/telegram-drive-monitor/.env
ExecStart=/home/ubuntu/telegram-drive-monitor/.venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable drive-monitor
sudo systemctl start drive-monitor
sudo systemctl status drive-monitor

# View logs
sudo journalctl -u drive-monitor -f
```

---

### Option B: Docker

Create a `Dockerfile` in the project directory:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Don't run as root
RUN useradd -m botuser && chown -R botuser /app
USER botuser

CMD ["python", "main.py"]
```

Build and run:

```bash
docker build -t drive-monitor .

docker run -d \
  --name drive-monitor \
  --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/credentials.json:/app/credentials.json:ro" \
  -v "$(pwd)/drive_monitor.db:/app/drive_monitor.db" \
  drive-monitor

# View logs
docker logs -f drive-monitor
```

---

### Option C: Google Cloud Run (Serverless)

> Note: Cloud Run is best for stateless services. Since this bot uses a polling loop and SQLite, it works better on a persistent VM (see systemd option above). For Cloud Run, consider mounting a Cloud SQL or Firestore backend instead of SQLite.

---

### Option D: VPS (DigitalOcean / Linode / Vultr)

1. Create a $4–6/month Ubuntu droplet.
2. SSH into the server.
3. Clone the repository and follow steps 2–7 above.
4. Set up as a systemd service (Option A).

---

## 10. Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Required environment variable 'X' is not set` | Missing value in `.env` | Fill in the value |
| `credentials.json not found` | Wrong path | Set `GOOGLE_CREDENTIALS_FILE` correctly |
| Bot ignores my messages | Wrong user ID | Check `ADMIN_USER_IDS` matches your Telegram ID |
| `HttpError 403` on Drive API | Service account not shared | Share the folder with the service account email |
| `HttpError 429` repeatedly | Polling too fast | Increase `POLL_INTERVAL` |
| Bot stops responding | Bot crashed | Check logs, restart with systemd |
| No notifications | Monitoring paused | Send `/monitor` to enable |
| Duplicate notifications | DB issue | Delete `drive_monitor.db` to reset state |

---

## 11. Security Best Practices

- **Never** commit `.env` or `credentials.json` — both are listed in `.gitignore`.
- Use the minimum required Drive permission (**Viewer** is enough).
- Limit `ADMIN_USER_IDS` to only trusted user IDs.
- Keep Python dependencies up-to-date (`pip install --upgrade -r requirements.txt`).
- Rotate the Service Account key and bot token periodically.
- On a VPS, use a firewall (ufw) and disable password SSH authentication.
- Store credentials in a secrets manager (e.g. AWS Secrets Manager, GCP Secret Manager) for production.
