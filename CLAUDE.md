# Coach Outreach System - Project Context

## Overview
Automated recruiting outreach platform for **Keelan Underwood**, 2026 OL from Florida (6'3", 295 lbs, The Benjamin School).
Sends personalized AI-generated emails to college football coaches.

## Architecture

### Local (MacBook)
- **Ollama** generates personalized AI emails (llama3.2:3b model)
- **LaunchAgent** triggers on wake/login: `com.coachoutreach.daily.plist`
- **Scripts:**
  - `run_daily_emails.sh` - Main trigger script
  - `daily_generate.py` - Generates ~33 AI emails/day (API limit)
  - `daily_send.py` - Verifies cloud sync

### Cloud (Railway)
- **Flask app** (`app.py`) - Handles sending via Gmail API
- **Google Sheets** (`bardeen`) - Stores coaches, AI emails, settings
- **Auto-send scheduler** - Sends at optimal times based on open tracking

### Data Flow
```
MacBook wakes
  → daily_generate.py (Ollama creates AI emails)
  → Syncs to Google Sheets (ai_emails sheet)
  → Railway reads from Sheets
  → Railway sends at optimal time via Gmail API
  → Marks sent, tracks opens
```

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Main Flask app (Railway deployment) |
| `daily_generate.py` | AI email generation with Ollama |
| `daily_send.py` | Verifies cloud sync status |
| `run_daily_emails.sh` | LaunchAgent trigger script |
| `sheets/cloud_emails.py` | Cloud storage for AI emails |
| `scheduler/email_scheduler.py` | Email scheduling logic |
| `enterprise/email_generator.py` | AI email generation engine |

## Data Locations

| Path | Contents |
|------|----------|
| `~/.coach_outreach/settings.json` | Local settings |
| `~/.coach_outreach/pregenerated_emails.json` | AI emails cache |
| `~/.coach_outreach/credentials.json` | Google service account |
| `~/.coach_outreach/daily.log` | Daily execution log |
| Google Sheets `bardeen` → `ai_emails` | Cloud AI emails |
| Google Sheets `bardeen` → `Settings` | Cloud settings |
| Google Sheets `bardeen` → `Sheet1` | Coach list |

## Settings

### Local (`~/.coach_outreach/settings.json`)
- `email.auto_send` / `email.auto_send_enabled` - Enable auto-send
- `email.paused_until` - Pause date (null to disable)
- `email.auto_send_count` - Max emails per day (default 25)
- `email.days_between_emails` - Gap between emails to same coach

### Cloud (Google Sheets → Settings)
- `auto_send_enabled` - Railway checks this
- `paused_until` - Railway checks this for pause

## Railway Environment Variables
```
GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
GOOGLE_CREDENTIALS (service account JSON, one line)
EMAIL_ADDRESS
TZ_OFFSET (-5 for EST)
```

## Email Sending Logic (app.py /api/email/send)
1. Check for AI-generated email from cloud/local first
2. If no AI email → fall back to templates
3. Send via Gmail API
4. Mark AI email as sent in cloud storage
5. Update contacted date in Sheet1

## Common Issues & Fixes

### Emails not sending
1. Check `paused_until` in both local settings and cloud Settings sheet
2. Verify `auto_send_enabled: true` in cloud Settings
3. Check Railway logs for errors
4. Verify Gmail API credentials on Railway

### AI emails not being used
- Fixed in app.py: `/api/email/send` now checks `get_ai_email_for_school()` first
- AI emails stored in `ai_emails` Google Sheet

### Settings mismatch
- Code expects `auto_send_enabled`, settings may have `auto_send`
- Need both fields set to true

## Deploy Changes to Railway
```bash
cd ~/coach-outreach-project
git add -A
git commit -m "description"
git push  # or: railway up
```

## Test Commands
```bash
# Generate AI emails
./venv/bin/python3 daily_generate.py

# Check cloud sync status
./venv/bin/python3 daily_send.py

# Check cloud stats
./venv/bin/python3 -c "
from sheets.cloud_emails import get_cloud_storage
s = get_cloud_storage()
s.connect()
print(s.get_stats())
"

# Run full daily flow
./run_daily_emails.sh
```

## Current Status (as of 2026-01-15)
- 343 AI emails pending in cloud
- 0 sent (needs Railway deploy with fixes)
- Local generation working
- Cloud sync working
