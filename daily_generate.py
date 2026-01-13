#!/usr/bin/env python3
"""
daily_generate.py - Daily AI Email Generation & Cloud Sync
============================================================================
Run this script when your laptop starts to:
1. Generate AI emails for schools that need them
2. Sync all emails to cloud for Railway to access

Usage:
    python daily_generate.py           # Generate 10 emails and sync
    python daily_generate.py --all     # Generate for ALL schools
    python daily_generate.py --sync    # Only sync, no generation
    python daily_generate.py -n 5      # Generate 5 emails

To auto-run on Mac startup:
1. Open System Preferences > Users & Groups > Login Items
2. Add this script or create a .plist in ~/Library/LaunchAgents/

Author: Coach Outreach System
============================================================================
"""

import os
import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add project to path
PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

DATA_DIR = Path.home() / '.coach_outreach'


def load_settings():
    """Load settings from config file."""
    settings_file = DATA_DIR / 'settings.json'
    if settings_file.exists():
        with open(settings_file) as f:
            return json.load(f)
    return {}


def get_schools_needing_emails():
    """Get list of schools that need AI emails generated."""
    try:
        from sheets.manager import SheetsManager
        from pathlib import Path

        # Load existing pregenerated emails
        pregenerated_file = DATA_DIR / 'pregenerated_emails.json'
        existing = {}
        if pregenerated_file.exists():
            with open(pregenerated_file) as f:
                existing = json.load(f)

        existing_schools = set(existing.keys())

        # Get schools from sheet
        sheets = SheetsManager()
        if not sheets.connect():
            logger.error("Could not connect to Google Sheets")
            return []

        try:
            data = sheets.get_all_data()
            if len(data) < 2:
                return []

            headers = [h.lower() for h in data[0]]
            school_col = next((i for i, h in enumerate(headers) if 'school' in h), -1)
            ol_name_col = next((i for i, h in enumerate(headers) if 'oline' in h or 'ol coach' in h), -1)
            ol_email_col = next((i for i, h in enumerate(headers) if 'oc email' in h or 'ol email' in h), -1)

            schools_needing = []
            for row in data[1:]:
                school = row[school_col].strip() if school_col >= 0 and school_col < len(row) else ''
                coach_name = row[ol_name_col].strip() if ol_name_col >= 0 and ol_name_col < len(row) else ''
                coach_email = row[ol_email_col].strip() if ol_email_col >= 0 and ol_email_col < len(row) else ''

                if school and coach_email and school.lower() not in [s.lower() for s in existing_schools]:
                    schools_needing.append({
                        'school': school,
                        'coach_name': coach_name,
                        'coach_email': coach_email
                    })

            return schools_needing
        finally:
            sheets.disconnect()

    except Exception as e:
        logger.error(f"Error getting schools: {e}")
        return []


def generate_emails(limit: int = 10):
    """Generate AI emails for schools that need them."""
    try:
        from enterprise.email_generator import get_email_generator

        schools = get_schools_needing_emails()
        if not schools:
            logger.info("All schools already have AI emails!")
            return 0

        logger.info(f"Found {len(schools)} schools needing emails, generating for {min(limit, len(schools))}")

        generator = get_email_generator()
        generated = 0

        for school_data in schools[:limit]:
            school = school_data['school']
            coach_name = school_data['coach_name']
            coach_email = school_data['coach_email']

            logger.info(f"Generating emails for {school}...")

            try:
                emails = generator.pregenerate_for_school(
                    school=school,
                    coach_name=coach_name,
                    coach_email=coach_email,
                    num_emails=3  # intro + 2 followups
                )
                if emails:
                    generated += len(emails)
                    logger.info(f"  Generated {len(emails)} emails for {school}")
            except Exception as e:
                logger.error(f"  Error generating for {school}: {e}")

        return generated

    except Exception as e:
        logger.error(f"Generation error: {e}")
        return 0


def sync_to_cloud():
    """Sync all pregenerated emails to cloud storage."""
    try:
        from sheets.cloud_emails import sync_emails_to_cloud

        logger.info("Syncing emails to cloud...")
        result = sync_emails_to_cloud()

        uploaded = result.get('uploaded', 0)
        logger.info(f"Uploaded {uploaded} emails to cloud")
        return uploaded

    except Exception as e:
        logger.error(f"Cloud sync error: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description='Daily AI Email Generation')
    parser.add_argument('-n', '--num', type=int, default=33, help='Number of schools to generate for (default: 33)')
    parser.add_argument('--all', action='store_true', help='Generate for ALL schools needing emails')
    parser.add_argument('--sync', action='store_true', help='Only sync to cloud, skip generation')
    parser.add_argument('--no-sync', action='store_true', help='Skip cloud sync after generation')
    args = parser.parse_args()

    print("=" * 60)
    print("DAILY AI EMAIL GENERATION")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    generated = 0
    uploaded = 0

    if not args.sync:
        # Generate ALL emails first (default 33 schools = ~99 emails)
        limit = 9999 if args.all else args.num
        print(f"\nGenerating for up to {limit} schools...")
        print("(This may take a while - will sync to cloud when ALL are done)\n")
        generated = generate_emails(limit)
        print(f"\nFinished generating {generated} new AI emails")

    if not args.no_sync:
        # Only sync AFTER all generation is complete
        print("\nSyncing all emails to cloud...")
        uploaded = sync_to_cloud()
        print(f"Synced {uploaded} emails to cloud")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  Schools processed: {args.num if not args.all else 'ALL'}")
    print(f"  Emails generated: {generated}")
    print(f"  Emails synced to cloud: {uploaded}")
    print("=" * 60)

    # Log to file for tracking improvements over time
    log_file = DATA_DIR / 'daily_generate.log'
    with open(log_file, 'a') as f:
        f.write(f"{datetime.now().isoformat()} - Generated: {generated}, Synced: {uploaded}\n")

    # Also log to AI improvement history
    history_file = DATA_DIR / 'ai_improvement_history.json'
    history = []
    if history_file.exists():
        try:
            with open(history_file) as f:
                history = json.load(f)
        except:
            pass

    history.append({
        'date': datetime.now().isoformat(),
        'emails_generated': generated,
        'emails_synced': uploaded,
        'schools_processed': limit if not args.all else 'all'
    })

    # Keep last 100 entries
    history = history[-100:]
    with open(history_file, 'w') as f:
        json.dump(history, f, indent=2)


if __name__ == '__main__':
    main()
