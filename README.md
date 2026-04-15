# YouTube to PDF Telegram Bot

A Telegram bot that converts YouTube videos into PDF slide documents and sends them to a Telegram channel.

## Features

- Convert YouTube videos to PDF (extracts key frames as slides)
- Sends PDF to Telegram channel `@alluserpdf`
- Supports long videos (up to 2 hours, up to 50 hours for admin)
- Multi-user parallel processing (up to 50 concurrent requests)
- Admin commands: broadcast, user count, export users to Excel

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/broadcast <msg>` | Admin: send message to all users |
| `/usercount` | Admin: show total user count |
| `/sendexcel` | Admin: send users list as Excel file |
| Send YouTube URL | Bot converts video to PDF |

---

## Deploy on Render

### Step 1 — Push this folder to GitHub

Create a new GitHub repository and push the contents of this `telegram-bot/` folder to it.

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### Step 2 — Create a Background Worker on Render

1. Go to [render.com](https://render.com) and log in.
2. Click **New → Background Worker**.
3. Connect your GitHub repository.
4. Set the following:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python run.py`
   - **Environment:** Python 3

### Step 3 — Add Environment Variable

In Render's **Environment** tab, add:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |

### Step 4 — Deploy

Click **Create Background Worker**. Render will install dependencies and start your bot automatically.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Your Telegram bot token from @BotFather |

---

## Local Development

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=your_token_here
python run.py
```

---

## Notes

- Uses `opencv-python-headless` — no display server needed, works perfectly on cloud servers.
- Large video processing is CPU-intensive; use Render's Standard or higher plan for best performance.
- A `cookies.txt` file (Netscape format) can be placed in the root directory to bypass YouTube age restrictions or bot detection.
