# RecruitSignal - Project Context

## Overview
Multi-tenant recruiting outreach platform. Originally built for **Keelan Underwood** (2026 OL, 6'3" 295 lbs, The Benjamin School, FL). Now supports multiple athletes with separate accounts, school selections, and Gmail credentials.

Sends template-based emails (customizable by athletes) to college football coaches via Gmail API.

## Architecture

### Database: Supabase (PostgreSQL)
- **ALL data lives in Supabase** — Google Sheets was fully removed (Jan 2026)
- Tables: `athletes`, `schools`, `coaches`, `outreach_tracking`, `email_templates`, `athlete_credentials`, `athlete_schools`, `settings`
- Supabase URL: `https://sdugzlvnlfejiwmrrysf.supabase.co`

### Cloud (Railway)
- **Flask app** (`app.py`) — main web app
- Serves the full single-page app (HTML/CSS/JS inline in Python template)
- Gmail API for sending emails
- Auto-send scheduler runs in background thread
- Open/click tracking via pixel + redirect

### Email System
- **Templates** — Athletes can create/customize email templates in the app
- Templates support variables: `{coach_name}`, `{school}`, `{athlete_name}`, `{position}`, etc.
- Intro emails and follow-up sequences
- No AI generation — all templates are human-written and customizable

## Multi-Tenant System (added Feb 2026)

### How It Works
- **Keelan is admin** (`is_admin = true` on athletes table)
- Admin creates athlete accounts via the Admin tab
- Each athlete logs in with email/password (Flask sessions)
- Each athlete selects which schools they want to email (from shared school database)
- Each athlete can choose coach preference per school: `position_coach`, `rc` (recruiting coordinator), or `both`
- Gmail credentials are stored **encrypted** (Fernet) in Supabase per athlete
- Scraper tools only visible to admin
- Schools and coaches are shared; everything else is per-athlete

### Auth System
- Simple password auth (werkzeug hash), NOT Supabase Auth
- Flask sessions with 30-day lifetime
- `@login_required` decorator for authenticated routes
- `@admin_required` decorator for admin-only routes
- `@app.before_request` sets `g.athlete_id`, `g.is_admin`, `g.athlete_name`
- `get_athlete_gmail_service()` builds Gmail API service from encrypted DB credentials
- Falls back to global Railway env vars if no per-athlete credentials

### Database Tables for Multi-Tenant
```sql
-- Auth columns on athletes table
athletes.password_hash TEXT
athletes.is_admin BOOLEAN DEFAULT false
athletes.is_active BOOLEAN DEFAULT true

-- Per-athlete encrypted Gmail credentials
athlete_credentials (athlete_id, gmail_client_id, gmail_client_secret, gmail_refresh_token, gmail_email)

-- Per-athlete school selection (junction table)
athlete_schools (athlete_id, school_id, coach_preference)
```

### Admin Features
- View all athletes with stats (emails sent, schools selected, Gmail status)
- Create new athlete accounts with profile fields
- Set/edit Gmail credentials per athlete (stored encrypted)
- Missing coach data alerts (schools where position coach or RC email is missing)

### Keelan's Login
- Email: `underwoodkeelan@gmail.com`
- Password: `Keelan2026!`
- Athlete ID: `ff757c0f-0bb0-46cc-8699-21531b0d4a95`

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Main Flask app — routes, templates, JS, all in one |
| `db/supabase_client.py` | Supabase database client — all DB operations |
| `enterprise/templates.py` | Email template management |
| `enterprise/followups.py` | Follow-up email logic |
| `enterprise/responses.py` | Response tracking |
| `scripts/setup_multitenant.py` | Setup script for password + credential migration |

## Key Methods in `db/supabase_client.py`

### Auth
- `authenticate_athlete(email, password)` — login verification
- `set_athlete_password(athlete_id, password)` — set/change password
- `create_athlete_account(name, email, password, **profile)` — create new athlete

### Credentials (encrypted)
- `save_athlete_credentials(athlete_id, client_id, secret, refresh_token, email, encryption_key)`
- `get_athlete_credentials(athlete_id, encryption_key)` — decrypt and return
- `has_athlete_credentials(athlete_id)` — check if configured

### School Selection
- `add_athlete_school(athlete_id, school_id, coach_preference)`
- `remove_athlete_school(athlete_id, school_id)`
- `get_athlete_schools(athlete_id)` — with school details joined
- `get_coaches_for_athlete_schools(athlete_id, limit, days_between)` — email queue filtered by athlete's schools + preference

### Admin
- `get_all_athletes()` — list all
- `get_athlete_stats_summary(athlete_id)` — sent/replied/schools/gmail/profile stats
- `get_missing_coaches_for_athlete(athlete_id)` — alerts for missing data

### Core (existing)
- `search_schools(query, division, state, conference, limit)`
- `get_coaches_to_email(limit, days_between)` — legacy, still works
- `record_email_sent(coach_id, school_name, ...)`
- `save_settings(settings)` / `get_settings()`

## API Routes (key ones)

### Auth
- `GET/POST /login` — login page + handler
- `POST /logout` — clear session
- `GET /api/auth/status` — check login state

### Admin (requires `@admin_required`)
- `GET /api/admin/athletes` — list with stats
- `POST /api/admin/athletes/create` — create account
- `POST /api/admin/athletes/<id>/credentials` — save encrypted Gmail creds
- `GET /api/admin/missing-coaches` — missing data alerts

### Athlete Schools
- `GET /api/athlete/schools` — my selected schools
- `POST /api/athlete/schools/add` — add school with coach preference
- `POST /api/athlete/schools/remove` — remove school

### Email
- `POST /api/email/send` — send emails (uses athlete's schools + Gmail)
- `GET /api/tracking/stats` — open/click tracking stats
- `POST /api/schools/search` — search Supabase schools (returns IDs)

### Templates
- `GET /api/templates` — list all templates
- `POST /api/templates` — create template
- `PUT /api/templates/<id>` — update template
- `DELETE /api/templates/<id>` — delete template

## Railway Environment Variables
```
SUPABASE_URL=https://sdugzlvnlfejiwmrrysf.supabase.co
SUPABASE_SERVICE_KEY=eyJhbG... (service role key)
GMAIL_CLIENT_ID          (Keelan's — fallback if no per-athlete creds)
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
EMAIL_ADDRESS=underwoodkeelan@gmail.com
TZ_OFFSET=-5
FLASK_SECRET_KEY=recruitsignal-secret-2026    (session cookies)
CREDENTIALS_ENCRYPTION_KEY=w8_n1ShcJUagxSp8a3NAfzlVCxLp0rtS8tQOqS8pFJ8=   (Fernet key for Gmail cred encryption)
```

## Deploy Changes
```bash
cd ~/Desktop/coach-outreach-supabase
git add -A
git commit -m "description"
git push
# Railway auto-deploys from GitHub
```

## Common Issues & Fixes

### Login not working
1. Check `FLASK_SECRET_KEY` is set in Railway env vars
2. Check password was set via `scripts/setup_multitenant.py`
3. Check `is_active = true` for the athlete in Supabase

### Gmail credentials not working for athlete
1. Check `CREDENTIALS_ENCRYPTION_KEY` is set in Railway
2. Verify credentials were saved via admin panel
3. Falls back to global Railway Gmail env vars if no per-athlete creds

### Emails not sending
1. Check `paused_until` in Supabase settings table
2. Check auto_send_enabled in settings
3. Check Railway logs for errors
4. Verify Gmail API credentials (per-athlete or global)

### School search returns no results
- Search now uses Supabase, not local data file
- Schools must exist in Supabase `schools` table
- Run scraper to populate if empty

### Adding a new athlete (full flow)
1. Login as admin (Keelan)
2. Go to Admin tab → + NEW ATHLETE
3. Fill in profile + password
4. After creating, click CREDS to add their Gmail OAuth credentials
5. Athlete logs in, goes to Find, searches schools, clicks "+ My List"
6. Emails will only go to coaches at their selected schools

## Migration History
1. **v1-v2**: Google Sheets for everything
2. **v3** (Jan 2026): Migrated to Supabase — all data in PostgreSQL, removed all Google Sheets code
3. **v4** (Feb 2026): Multi-tenant — login system, admin panel, per-athlete schools, encrypted Gmail credentials
4. **v5** (Feb 2026): Removed AI email generation — now uses customizable templates only

## Project Structure
```
coach-outreach-supabase/
├── app.py                          # Main Flask app (everything)
├── db/
│   └── supabase_client.py          # All database operations
├── enterprise/
│   ├── templates.py                # Email template management
│   ├── followups.py                # Follow-up logic
│   └── responses.py                # Response tracking
├── scrapers/                       # Coach email/Twitter scrapers
├── scheduler/
│   └── email_scheduler.py          # Auto-send scheduler
├── scripts/
│   └── setup_multitenant.py        # Multi-tenant setup script
├── requirements.txt                # Python dependencies
├── Procfile                        # Railway deployment
└── CLAUDE.md                       # This file
```
