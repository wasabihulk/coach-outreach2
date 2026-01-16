#!/usr/bin/env python3
"""
daily_send.py - Verify AI emails are synced to cloud for Railway
============================================================================
After daily_generate.py creates AI emails, this script verifies they're
synced to Google Sheets so Railway can send them automatically.

Railway handles:
- Reading AI emails from Google Sheets
- Sending at optimal times based on open tracking data
- Marking emails as sent
- Tracking opens and responses

Author: Coach Outreach System
============================================================================
"""

import sys
import json
import logging
from datetime import datetime
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Paths
DATA_DIR = Path.home() / '.coach_outreach'
PREGENERATED_FILE = DATA_DIR / 'pregenerated_emails.json'


def main():
    """Verify AI emails are synced to cloud."""
    logger.info("=" * 60)
    logger.info("VERIFYING CLOUD SYNC")
    logger.info(f"Started: {datetime.now()}")
    logger.info("=" * 60)

    # Check local AI emails
    local_count = 0
    if PREGENERATED_FILE.exists():
        with open(PREGENERATED_FILE) as f:
            local_emails = json.load(f)
            for school, emails in local_emails.items():
                if isinstance(emails, list):
                    local_count += len(emails)

    logger.info(f"Local AI emails: {local_count}")

    # Check cloud sync
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from sheets.cloud_emails import get_cloud_storage

        storage = get_cloud_storage()
        if storage.connect():
            stats = storage.get_stats()

            logger.info("")
            logger.info("Cloud Status (Google Sheets):")
            logger.info(f"  Total emails: {stats.get('total_emails', 0)}")
            logger.info(f"  Pending: {stats.get('pending', 0)}")
            logger.info(f"  Sent: {stats.get('sent', 0)}")
            logger.info(f"  Opened: {stats.get('opened', 0)}")
            logger.info(f"  Open rate: {stats.get('open_rate', 0)}%")
            logger.info(f"  Responses: {stats.get('responses', 0)}")

            pending = stats.get('pending', 0)
            if pending > 0:
                logger.info("")
                logger.info(f"Railway will automatically send {pending} pending emails")
                logger.info("at the optimal time based on open tracking data.")
            else:
                logger.info("")
                logger.info("No pending emails - all caught up!")

            storage.disconnect()
        else:
            logger.error("Could not connect to cloud storage")
            return 1

    except Exception as e:
        logger.error(f"Error checking cloud: {e}")
        return 1

    logger.info("")
    logger.info("=" * 60)
    logger.info("DONE - Railway handles automatic sending")
    logger.info("=" * 60)

    return 0


if __name__ == '__main__':
    sys.exit(main())
