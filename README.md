# 📁 Telegram Drive Monitor Bot

A production-ready Telegram Bot that monitors a Google Drive folder in real-time and sends instant notifications whenever files are added or updated. Includes full file browsing and search via Telegram commands.

---

## ✨ Features

- 🔔 **Real-time notifications** — alerts for new and updated files, sent instantly after detection
- 🔍 **File search** — find files by name directly from Telegram
- 📂 **File browsing** — paginated `/list` command with clickable Drive links
- 🔒 **Admin-only access** — all commands protected by Telegram User ID
- ♻️ **Change detection** — SQLite-backed version tracking prevents duplicate alerts
- 📈 **Monitoring stats** — live status with counters for new and updated files
- ⚡ **Exponential backoff** — graceful handling of Google Drive API rate limits
- 🧩 **Configurable** — polling interval, page size, log level, and more via `.env`

---

## 🏗️ Architecture

```
telegram-drive-monitor/
├── main.py                 # Bot application, command handlers, polling task
├── google_drive_service.py # Google Drive API v3 wrapper (async-safe)
├── database.py             # SQLite change tracking and version comparison
├── config.py               # Environment loading, validation, constants
├── utils.py                # Formatting, pagination, Telegram helpers
├── requirements.txt        # Pinned dependencies
├── .env.example            # Configuration template
├── setup_guide.md          # Step-by-step setup for all platforms
└── README.md               # This file
```

**Data flow:**

```
[Google Drive Folder]
        │  poll every N minutes
        ▼
[google_drive_service.py]  ──►  [database.py]  ──►  compare versions
                                                          │
                                          change detected │
                                                          ▼
                                             [Telegram Bot Alert]
                                             to all admin user IDs
```

---

## 🚀 Quick Start (< 5 minutes)

### Prerequisites

- Python 3.8 or higher
- A Google Cloud project with the Drive API enabled
- A Telegram Bot token from [@BotFather](https://t.me/BotFather)
- A Google Service Account with access to your Drive folder

> See **[setup_guide.md](setup_guide.md)** for detailed step-by-step instructions.

### 1. Clone the repository

```bash
git clone https://github.com/hossam1907/telegram-drive-monitor.git
cd telegram-drive-monitor
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ADMIN_USER_IDS=123456789
DRIVE_FOLDER_ID=your_folder_id_here
GOOGLE_CREDENTIALS_FILE=credentials.json
```

### 4. Place your Service Account credentials

Copy the downloaded `credentials.json` file into the project directory (or set `GOOGLE_CREDENTIALS_FILE` to its path).

### 5. Run the bot

```bash
python main.py
```

---

## 📋 Commands

| Command | Description |
|---------|-------------|
| `/start` | Show the welcome message and help |
| `/list` | Browse files in the monitored folder (paginated) |
| `/search <name>` | Search for files by name substring |
| `/monitor` | Toggle monitoring on / off |
| `/status` | Show monitoring statistics and last poll time |

All commands are restricted to the user IDs listed in `ADMIN_USER_IDS`.

---

## ⚙️ Configuration Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `ADMIN_USER_IDS` | ✅ | — | Comma-separated Telegram user IDs |
| `DRIVE_FOLDER_ID` | ✅ | — | Google Drive folder ID to monitor |
| `GOOGLE_CREDENTIALS_FILE` | ✅ | — | Path to Service Account JSON |
| `POLL_INTERVAL` | ❌ | `300` | Seconds between polls (60–3600) |
| `PAGE_SIZE` | ❌ | `10` | Files per page in /list (1–50) |
| `DATABASE_PATH` | ❌ | `drive_monitor.db` | SQLite database file path |
| `LOG_LEVEL` | ❌ | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `REQUEST_TIMEOUT` | ❌ | `30` | API request timeout in seconds |

---

## 🚢 Deployment

### systemd (Linux VPS)

Create `/etc/systemd/system/drive-monitor.service`:

```ini
[Unit]
Description=Telegram Drive Monitor Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/telegram-drive-monitor
ExecStart=/home/ubuntu/telegram-drive-monitor/.venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable drive-monitor
sudo systemctl start drive-monitor
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

```bash
docker build -t drive-monitor .
docker run -d --env-file .env -v $(pwd)/credentials.json:/app/credentials.json drive-monitor
```

---

## 🛠️ Troubleshooting

| Problem | Solution |
|---------|----------|
| `Required environment variable … is not set` | Copy `.env.example` to `.env` and fill in all required values |
| `credentials.json not found` | Set `GOOGLE_CREDENTIALS_FILE` to the correct path |
| Bot doesn't respond to commands | Check `ADMIN_USER_IDS` contains your Telegram user ID |
| No Drive notifications | Ensure the Service Account has at least Viewer access to the folder |
| `HttpError 403` from Drive API | Check the Service Account permissions and folder sharing settings |
| Rate limit errors | Increase `POLL_INTERVAL` to reduce API call frequency |

---

## 🔐 Security Best Practices

- Never commit `.env` or `credentials.json` to version control (both are in `.gitignore`)
- Use a dedicated Service Account with read-only Drive access
- Keep `ADMIN_USER_IDS` to the minimum required set of users
- Rotate bot tokens and Service Account keys periodically

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.
