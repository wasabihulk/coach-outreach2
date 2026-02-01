#!/usr/bin/env python3
"""
Migrate all data from Google Sheets → Supabase
Run once: python migrate_to_supabase.py

Migrates:
  1. Schools + coaches from Sheet1 (bardeen)
  2. AI emails from ai_emails sheet
  3. Tracking data from Email_Tracking sheet
  4. Settings from Settings sheet
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))

from db import get_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================================================
# CONNECT TO GOOGLE SHEETS
# ============================================================================

def connect_sheets():
    """Connect to Google Sheets and return the spreadsheet object."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.error("Install: pip install gspread google-auth")
        return None

    google_creds_json = os.environ.get('GOOGLE_CREDENTIALS', '')
    if google_creds_json:
        creds_dict = json.loads(google_creds_json.strip())
    else:
        # Try multiple credential locations
        creds_file = None
        for p in [
            Path(__file__).parent / 'credentials.json',
            Path.home() / '.coach_outreach' / 'credentials.json',
            Path('credentials.json'),
        ]:
            if p.exists():
                creds_file = p
                break
        if not creds_file:
            logger.error("No credentials.json found")
            return None
        with open(creds_file) as f:
            creds_dict = json.load(f)

    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    # Load settings to get spreadsheet name
    settings_file = Path.home() / '.coach_outreach' / 'settings.json'
    name = 'bardeen'
    if settings_file.exists():
        with open(settings_file) as f:
            s = json.load(f)
            name = s.get('sheets', {}).get('spreadsheet_name', 'bardeen')

    spreadsheet = client.open(name)
    logger.info(f"Connected to Google Sheets: {name}")
    return spreadsheet


# ============================================================================
# MIGRATE SCHOOLS + COACHES (Sheet1)
# ============================================================================

def migrate_schools_and_coaches(spreadsheet, db):
    """Migrate schools and coaches from Sheet1."""
    sheet = spreadsheet.sheet1
    rows = sheet.get_all_values()

    if not rows:
        logger.warning("Sheet1 is empty")
        return 0, 0

    # Skip header row
    header = rows[0] if rows else []
    data_rows = rows[1:]

    school_count = 0
    coach_count = 0

    for row in data_rows:
        # Pad row to at least 12 columns
        while len(row) < 12:
            row.append('')

        school_name = row[0].strip()
        staff_url = row[1].strip()
        rc_name = row[2].strip()
        ol_name = row[3].strip()
        rc_twitter = row[4].strip()
        ol_twitter = row[5].strip()
        rc_email = row[6].strip()
        ol_email = row[7].strip()
        rc_contacted = row[8].strip()
        ol_contacted = row[9].strip()

        if not school_name:
            continue

        # Add school
        db.add_school(name=school_name, staff_url=staff_url if staff_url else None)
        school_count += 1

        # Add RC if exists
        if rc_name and not rc_name.startswith('REVIEW:'):
            db.add_coach(
                school_name=school_name,
                name=rc_name,
                role='rc',
                email=rc_email if rc_email else None,
                twitter=rc_twitter if rc_twitter else None,
            )
            coach_count += 1

        # Add OL coach if exists
        if ol_name and not ol_name.startswith('REVIEW:'):
            db.add_coach(
                school_name=school_name,
                name=ol_name,
                role='ol',
                email=ol_email if ol_email else None,
                twitter=ol_twitter if ol_twitter else None,
            )
            coach_count += 1

    logger.info(f"Migrated {school_count} schools, {coach_count} coaches")
    return school_count, coach_count


# ============================================================================
# MIGRATE AI EMAILS
# ============================================================================

def migrate_ai_emails(spreadsheet, db):
    """Migrate AI emails from ai_emails sheet."""
    try:
        sheet = spreadsheet.worksheet('ai_emails')
    except Exception:
        logger.warning("No ai_emails sheet found")
        return 0

    records = sheet.get_all_records()
    count = 0

    for r in records:
        school = r.get('school', '').strip()
        body = r.get('body', '').strip()
        if not school or not body:
            continue

        coach_name = r.get('coach_name', '').strip() or None
        coach_email = r.get('coach_email', '').strip() or None
        subject = r.get('subject', '').strip() or None
        sent_at = r.get('sent_at', '').strip()
        email_type = r.get('email_type', 'intro').strip()

        status = 'sent' if sent_at else 'ready'

        # Store in Supabase
        try:
            data = {
                'athlete_id': db.athlete_id,
                'school_name': school,
                'body': body,
                'coach_name': coach_name,
                'subject': subject,
                'coach_role': 'ol' if 'ol' in (email_type or '').lower() else None,
                'status': status,
            }
            data = {k: v for k, v in data.items() if v is not None}
            db.client.table('ai_emails').insert(data).execute()
            count += 1
        except Exception as e:
            logger.warning(f"Failed to migrate AI email for {school}: {e}")

    logger.info(f"Migrated {count} AI emails")
    return count


# ============================================================================
# MIGRATE TRACKING DATA
# ============================================================================

def migrate_tracking(spreadsheet, db):
    """Migrate tracking data from Email_Tracking sheet + local file."""
    # Try local tracking file first (more complete)
    tracking_file = Path.home() / '.coach_outreach' / 'email_tracking.json'
    tracking_data = {}

    if tracking_file.exists():
        with open(tracking_file) as f:
            tracking_data = json.load(f)
        logger.info(f"Loaded local tracking: {len(tracking_data.get('sent', {}))} sent emails")

    # Also try the sheet
    try:
        sheet = spreadsheet.worksheet('Email_Tracking')
        sheet_rows = sheet.get_all_records()
        logger.info(f"Found {len(sheet_rows)} rows in Email_Tracking sheet")

        # Merge sheet data into tracking_data
        for r in sheet_rows:
            tid = r.get('tracking_id', '')
            if tid and tid not in tracking_data.get('sent', {}):
                if 'sent' not in tracking_data:
                    tracking_data['sent'] = {}
                tracking_data['sent'][tid] = {
                    'to': r.get('to', ''),
                    'school': r.get('school', ''),
                    'coach': r.get('coach', ''),
                    'subject': r.get('subject', ''),
                    'sent_at': r.get('sent_at', ''),
                }
                if r.get('opened_at'):
                    if 'opens' not in tracking_data:
                        tracking_data['opens'] = {}
                    tracking_data['opens'][tid] = [{'opened_at': r.get('opened_at')}]
    except Exception as e:
        logger.warning(f"No Email_Tracking sheet or error: {e}")

    # Now push all sent emails to Supabase as outreach records
    sent = tracking_data.get('sent', {})
    opens = tracking_data.get('opens', {})
    count = 0

    for tid, info in sent.items():
        coach_email = info.get('to', '')
        school = info.get('school', '')
        coach_name = info.get('coach', '')
        subject = info.get('subject', '')
        sent_at = info.get('sent_at', '')

        if not coach_email:
            continue

        # Check if already in Supabase (by coach_email + school)
        if db.was_coach_contacted(coach_email):
            continue

        try:
            outreach = db.create_outreach(
                coach_email=coach_email,
                coach_name=coach_name,
                school_name=school,
                subject=subject,
                body='',  # Body not stored in tracking
            )
            if outreach:
                # Mark as sent
                update = {
                    'status': 'sent',
                    'sent_at': sent_at if sent_at else datetime.now().isoformat(),
                }
                # Check if opened
                if tid in opens and opens[tid]:
                    open_list = opens[tid]
                    update['opened'] = True
                    update['open_count'] = len(open_list)
                    update['opened_at'] = open_list[0].get('opened_at', '')

                db.client.table('outreach').update(update).eq('id', outreach['id']).execute()
                count += 1
        except Exception as e:
            logger.warning(f"Failed to migrate tracking for {coach_email}: {e}")

    logger.info(f"Migrated {count} outreach/tracking records")
    return count


# ============================================================================
# MIGRATE SETTINGS
# ============================================================================

def migrate_settings(spreadsheet, db):
    """Migrate settings from Settings sheet."""
    try:
        sheet = spreadsheet.worksheet('Settings')
    except Exception:
        logger.warning("No Settings sheet found")
        return False

    try:
        rows = sheet.get_all_values()
        settings = {}
        for row in rows[1:]:  # skip header
            if len(row) >= 2 and row[0]:
                key = row[0].strip()
                val = row[1].strip()
                # Parse types
                if val.lower() == 'true':
                    val = True
                elif val.lower() == 'false':
                    val = False
                elif val.isdigit():
                    val = int(val)
                elif val.lower() in ('none', 'null', ''):
                    continue
                settings[key] = val

        # Map to Supabase settings columns
        mapped = {}
        if 'auto_send_enabled' in settings:
            mapped['auto_send_enabled'] = settings['auto_send_enabled']
        if 'auto_send_count' in settings:
            mapped['auto_send_count'] = settings['auto_send_count']
        if 'days_between_emails' in settings:
            mapped['days_between_followups'] = settings['days_between_emails']
        if 'paused_until' in settings and settings['paused_until']:
            mapped['paused_until'] = settings['paused_until']

        if mapped:
            db.save_settings(**mapped)
            logger.info(f"Migrated settings: {list(mapped.keys())}")
            return True
    except Exception as e:
        logger.warning(f"Failed to migrate settings: {e}")

    return False


# ============================================================================
# ALSO MIGRATE LOCAL PREGENERATED EMAILS
# ============================================================================

def migrate_local_emails(db):
    """Migrate locally cached pregenerated emails."""
    pregen_file = Path.home() / '.coach_outreach' / 'pregenerated_emails.json'
    if not pregen_file.exists():
        logger.info("No local pregenerated_emails.json found")
        return 0

    with open(pregen_file) as f:
        emails = json.load(f)

    count = 0
    for school, email_list in emails.items():
        if not isinstance(email_list, list):
            continue
        for email in email_list:
            body = email.get('personalized_content', '') or email.get('body', '')
            if not body:
                continue
            try:
                db.store_ai_email(
                    school_name=school,
                    body=body,
                    coach_name=email.get('coach_name'),
                    coach_role=email.get('coach_role'),
                    subject=email.get('subject'),
                    hook=email.get('hook'),
                    hook_category=email.get('hook_category'),
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to store local email for {school}: {e}")

    logger.info(f"Migrated {count} local pregenerated emails")
    return count


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print("  MIGRATE GOOGLE SHEETS → SUPABASE")
    print("=" * 60)
    print()

    # Connect to Supabase
    db = get_db()
    athlete_email = os.environ.get('ATHLETE_EMAIL', os.environ.get('EMAIL_ADDRESS', ''))
    athlete_name = os.environ.get('ATHLETE_NAME', 'Keelan Underwood')
    if athlete_email:
        db.get_or_create_athlete(athlete_name, athlete_email)
    print(f"Supabase connected, athlete_id: {db.athlete_id}")
    print()

    # Connect to Google Sheets
    spreadsheet = connect_sheets()
    if not spreadsheet:
        print("Could not connect to Google Sheets.")
        print("Migrating local data only...\n")
        local_count = migrate_local_emails(db)
        print(f"\nLocal emails migrated: {local_count}")
        return

    # Run all migrations
    print("1/5 Migrating schools & coaches...")
    s, c = migrate_schools_and_coaches(spreadsheet, db)
    print(f"     → {s} schools, {c} coaches\n")

    print("2/5 Migrating AI emails from sheet...")
    ai = migrate_ai_emails(spreadsheet, db)
    print(f"     → {ai} AI emails\n")

    print("3/5 Migrating tracking/outreach data...")
    tr = migrate_tracking(spreadsheet, db)
    print(f"     → {tr} outreach records\n")

    print("4/5 Migrating settings...")
    migrate_settings(spreadsheet, db)
    print()

    print("5/5 Migrating local pregenerated emails...")
    local = migrate_local_emails(db)
    print(f"     → {local} local emails\n")

    # Final counts
    print("=" * 60)
    print("  MIGRATION COMPLETE — Final counts:")
    print("=" * 60)
    for table in ['schools', 'coaches', 'outreach', 'ai_emails', 'templates', 'settings']:
        try:
            r = db.client.table(table).select('*', count='exact').limit(0).execute()
            print(f"  {table:20s} {r.count} rows")
        except:
            print(f"  {table:20s} (error)")
    print()


if __name__ == '__main__':
    main()
