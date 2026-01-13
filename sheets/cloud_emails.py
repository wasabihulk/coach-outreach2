"""
sheets/cloud_emails.py - Cloud Storage for Pregenerated AI Emails
============================================================================
Syncs pregenerated emails to Google Sheets so Railway can access them
when the laptop is offline.

Features:
- Upload pregenerated emails to Google Sheets
- Download emails on Railway for sending
- Track sent status and responses
- Store successful email templates for AI learning

Author: Coach Outreach System
Version: 1.0.0
============================================================================
"""

import os
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path

try:
    import gspread
    from gspread.exceptions import APIError, WorksheetNotFound
    HAS_GSPREAD = True
except ImportError:
    HAS_GSPREAD = False

try:
    from google.oauth2.service_account import Credentials
    USE_GOOGLE_AUTH = True
except ImportError:
    USE_GOOGLE_AUTH = False

logger = logging.getLogger(__name__)

# Local cache paths
DATA_DIR = Path.home() / '.coach_outreach'
PREGENERATED_FILE = DATA_DIR / 'pregenerated_emails.json'
SUCCESSFUL_EMAILS_FILE = DATA_DIR / 'successful_emails.json'

# Sheet configuration
EMAILS_SHEET_NAME = 'ai_emails'
EMAILS_HEADERS = [
    'school', 'coach_name', 'coach_email', 'email_type',
    'subject', 'body', 'created_at', 'sent_at',
    'opened', 'response_received', 'response_sentiment'
]


class CloudEmailStorage:
    """
    Manages cloud storage of pregenerated AI emails in Google Sheets.
    Enables Railway to access emails when laptop is offline.
    """

    def __init__(self):
        self._client = None
        self._spreadsheet = None
        self._emails_sheet = None
        self._connected = False

    def connect(self) -> bool:
        """Connect to Google Sheets."""
        if not HAS_GSPREAD:
            logger.error("gspread not installed")
            return False

        try:
            google_creds_json = os.environ.get('GOOGLE_CREDENTIALS', '')

            if google_creds_json:
                import json as json_module
                creds_dict = json_module.loads(google_creds_json)
                if USE_GOOGLE_AUTH:
                    scopes = [
                        'https://www.googleapis.com/auth/spreadsheets',
                        'https://www.googleapis.com/auth/drive'
                    ]
                    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
                    self._client = gspread.authorize(creds)
                else:
                    from oauth2client.service_account import ServiceAccountCredentials
                    scopes = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
                    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scopes)
                    self._client = gspread.authorize(creds)
            else:
                creds_file = Path.home() / '.coach_outreach' / 'credentials.json'
                if not creds_file.exists():
                    creds_file = Path('credentials.json')

                if USE_GOOGLE_AUTH:
                    scopes = [
                        'https://www.googleapis.com/auth/spreadsheets',
                        'https://www.googleapis.com/auth/drive'
                    ]
                    creds = Credentials.from_service_account_file(str(creds_file), scopes=scopes)
                    self._client = gspread.authorize(creds)
                else:
                    from oauth2client.service_account import ServiceAccountCredentials
                    scopes = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
                    creds = ServiceAccountCredentials.from_json_keyfile_name(str(creds_file), scopes)
                    self._client = gspread.authorize(creds)

            # Load settings to get spreadsheet name
            settings_file = DATA_DIR / 'settings.json'
            spreadsheet_name = 'bardeen'
            if settings_file.exists():
                with open(settings_file) as f:
                    settings = json.load(f)
                    spreadsheet_name = settings.get('sheets', {}).get('spreadsheet_name', 'bardeen')

            self._spreadsheet = self._client.open(spreadsheet_name)
            self._get_or_create_emails_sheet()
            self._connected = True
            logger.info("Connected to cloud email storage")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to cloud storage: {e}")
            return False

    def disconnect(self):
        """Disconnect from Google Sheets."""
        self._client = None
        self._spreadsheet = None
        self._emails_sheet = None
        self._connected = False

    def _get_or_create_emails_sheet(self):
        """Get or create the ai_emails sheet."""
        try:
            self._emails_sheet = self._spreadsheet.worksheet(EMAILS_SHEET_NAME)
        except WorksheetNotFound:
            # Create the sheet with headers
            self._emails_sheet = self._spreadsheet.add_worksheet(
                title=EMAILS_SHEET_NAME,
                rows=1000,
                cols=len(EMAILS_HEADERS)
            )
            self._emails_sheet.update('A1:K1', [EMAILS_HEADERS])
            logger.info(f"Created '{EMAILS_SHEET_NAME}' sheet")

    def upload_all_emails(self) -> Dict[str, int]:
        """
        Upload all pregenerated emails from local cache to Google Sheets.
        Returns count of uploaded emails.
        """
        if not self._connected:
            if not self.connect():
                return {'uploaded': 0, 'error': 'Not connected'}

        if not PREGENERATED_FILE.exists():
            return {'uploaded': 0, 'error': 'No local emails file'}

        with open(PREGENERATED_FILE) as f:
            local_emails = json.load(f)

        # Get existing emails in sheet to avoid duplicates
        existing = self._get_existing_email_keys()

        # Prepare rows to upload
        rows_to_add = []
        for school, email_list in local_emails.items():
            if not isinstance(email_list, list):
                continue

            for email in email_list:
                # Create unique key
                key = f"{school}|{email.get('coach_email', '')}|{email.get('email_type', '')}"
                if key in existing:
                    continue  # Skip duplicates

                row = [
                    school,
                    email.get('coach_name', ''),
                    email.get('coach_email', ''),
                    email.get('email_type', 'intro'),
                    email.get('subject', f"2026 OL - Keelan Underwood - {school}"),
                    email.get('personalized_content', ''),
                    email.get('created_at', datetime.now().isoformat()),
                    '',  # sent_at
                    'FALSE',  # opened
                    'FALSE',  # response_received
                    ''  # response_sentiment
                ]
                rows_to_add.append(row)

        if rows_to_add:
            # Batch append for efficiency
            self._emails_sheet.append_rows(rows_to_add, value_input_option='RAW')
            logger.info(f"Uploaded {len(rows_to_add)} emails to cloud")

        return {'uploaded': len(rows_to_add), 'skipped_duplicates': len(existing)}

    def _get_existing_email_keys(self) -> set:
        """Get set of existing email keys to avoid duplicates."""
        try:
            records = self._emails_sheet.get_all_records()
            keys = set()
            for r in records:
                key = f"{r.get('school', '')}|{r.get('coach_email', '')}|{r.get('email_type', '')}"
                keys.add(key)
            return keys
        except Exception as e:
            logger.error(f"Error getting existing emails: {e}")
            return set()

    def download_pending_emails(self) -> List[Dict]:
        """
        Download emails that haven't been sent yet.
        Used by Railway to get emails to send.
        """
        if not self._connected:
            if not self.connect():
                return []

        try:
            records = self._emails_sheet.get_all_records()
            pending = []
            for r in records:
                # Skip if already sent
                if r.get('sent_at'):
                    continue
                pending.append({
                    'school': r.get('school', ''),
                    'coach_name': r.get('coach_name', ''),
                    'coach_email': r.get('coach_email', ''),
                    'email_type': r.get('email_type', 'intro'),
                    'subject': r.get('subject', ''),
                    'body': r.get('body', ''),
                    'created_at': r.get('created_at', '')
                })
            return pending
        except Exception as e:
            logger.error(f"Error downloading emails: {e}")
            return []

    def mark_email_sent(self, school: str, coach_email: str, email_type: str):
        """Mark an email as sent in the cloud sheet."""
        if not self._connected:
            return

        try:
            # Find the row
            records = self._emails_sheet.get_all_records()
            for i, r in enumerate(records):
                if (r.get('school') == school and
                    r.get('coach_email') == coach_email and
                    r.get('email_type') == email_type and
                    not r.get('sent_at')):
                    # Update sent_at (row index + 2 for header and 0-index)
                    row_num = i + 2
                    self._emails_sheet.update_cell(row_num, 8, datetime.now().isoformat())
                    logger.info(f"Marked email sent: {school} - {email_type}")
                    return
        except Exception as e:
            logger.error(f"Error marking email sent: {e}")

    def mark_email_opened(self, school: str, coach_email: str):
        """Mark that an email was opened."""
        if not self._connected:
            if not self.connect():
                return

        try:
            records = self._emails_sheet.get_all_records()
            for i, r in enumerate(records):
                if r.get('school') == school and r.get('coach_email') == coach_email:
                    row_num = i + 2
                    self._emails_sheet.update_cell(row_num, 9, 'TRUE')
                    return
        except Exception as e:
            logger.error(f"Error marking email opened: {e}")

    def mark_response_received(self, school: str, coach_email: str, sentiment: str = ''):
        """Mark that a response was received."""
        if not self._connected:
            if not self.connect():
                return

        try:
            records = self._emails_sheet.get_all_records()
            for i, r in enumerate(records):
                if r.get('school') == school and r.get('coach_email') == coach_email:
                    row_num = i + 2
                    self._emails_sheet.update_cell(row_num, 10, 'TRUE')
                    if sentiment:
                        self._emails_sheet.update_cell(row_num, 11, sentiment)
                    return
        except Exception as e:
            logger.error(f"Error marking response: {e}")

    def get_successful_emails(self) -> List[Dict]:
        """
        Get emails that received positive responses.
        Used to train AI to generate better emails.
        """
        if not self._connected:
            if not self.connect():
                return []

        try:
            records = self._emails_sheet.get_all_records()
            successful = []
            for r in records:
                if r.get('response_received') == 'TRUE':
                    sentiment = r.get('response_sentiment', '').lower()
                    # Include positive or neutral responses
                    if sentiment in ['positive', 'interested', 'neutral', '']:
                        successful.append({
                            'school': r.get('school', ''),
                            'email_type': r.get('email_type', ''),
                            'body': r.get('body', ''),
                            'sentiment': sentiment
                        })
            return successful
        except Exception as e:
            logger.error(f"Error getting successful emails: {e}")
            return []

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about cloud-stored emails."""
        if not self._connected:
            if not self.connect():
                return {'error': 'Not connected'}

        try:
            records = self._emails_sheet.get_all_records()
            total = len(records)
            sent = sum(1 for r in records if r.get('sent_at'))
            opened = sum(1 for r in records if r.get('opened') == 'TRUE')
            responses = sum(1 for r in records if r.get('response_received') == 'TRUE')

            return {
                'total_emails': total,
                'sent': sent,
                'pending': total - sent,
                'opened': opened,
                'open_rate': round(opened / sent * 100, 1) if sent > 0 else 0,
                'responses': responses,
                'response_rate': round(responses / sent * 100, 1) if sent > 0 else 0
            }
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {'error': str(e)}


# Singleton instance
_cloud_storage = None


def get_cloud_storage() -> CloudEmailStorage:
    """Get singleton instance of cloud storage."""
    global _cloud_storage
    if _cloud_storage is None:
        _cloud_storage = CloudEmailStorage()
    return _cloud_storage


def sync_emails_to_cloud() -> Dict[str, int]:
    """
    Convenience function to sync local emails to cloud.
    Call this after generating new emails.
    """
    storage = get_cloud_storage()
    return storage.upload_all_emails()


def get_cloud_email_for_coach(school: str, coach_email: str, email_type: str = 'intro') -> Optional[Dict]:
    """
    Get a specific email from cloud storage.
    Used by Railway scheduler when sending.
    """
    storage = get_cloud_storage()
    if not storage._connected:
        if not storage.connect():
            return None

    try:
        records = storage._emails_sheet.get_all_records()
        for r in records:
            if (r.get('school', '').lower() == school.lower() and
                r.get('coach_email', '').lower() == coach_email.lower() and
                r.get('email_type') == email_type and
                not r.get('sent_at')):  # Not yet sent
                return {
                    'subject': r.get('subject', ''),
                    'body': r.get('body', ''),
                    'is_ai': True
                }
        return None
    except Exception as e:
        logger.error(f"Error getting cloud email: {e}")
        return None
