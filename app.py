"""
Coach Outreach Pro - Enterprise Edition
============================================================================
Professional recruiting outreach platform for college athletes.

Features:
- Intelligent school search with natural language processing
- Smart email campaigns with deduplication
- Twitter/X direct messaging integration
- Automated form filling
- Analytics and tracking dashboard
- Extended athlete profiles
- Auto-send with random timing
- Phone notifications via ntfy.sh
- Gmail API for sending/reading emails (works on Railway)

Version: 8.4.0 Enterprise (Railway/Render Ready)
============================================================================
"""

import os
import sys
import json
import time
import queue
import logging
import threading
import webbrowser
import traceback
import smtplib
import re
import random
import base64
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Load .env file for local development (if exists)
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        load_dotenv(env_path)
        print("Loaded .env file")
except ImportError:
    pass  # dotenv not installed, will use environment variables directly

from flask import Flask, render_template_string, jsonify, request, Response, stream_with_context, make_response, session, redirect, url_for, g
from functools import wraps

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

# Supabase database integration
try:
    from db import get_db
    SUPABASE_AVAILABLE = False  # Will be set True after init
except ImportError:
    SUPABASE_AVAILABLE = False
    get_db = None

# Import AI email functions from scheduler
try:
    from scheduler.email_scheduler import load_pregenerated_emails, get_ai_email_for_school
    AI_EMAILS_AVAILABLE = True
except ImportError:
    AI_EMAILS_AVAILABLE = False
    def load_pregenerated_emails():
        return {}
    def get_ai_email_for_school(school, coach_name, email_type='intro'):
        return None

# ============================================================================
# CONFIGURATION
# ============================================================================

CONFIG_DIR = Path.home() / '.coach_outreach'
CONFIG_FILE = CONFIG_DIR / 'settings.json'
CREDENTIALS_FILE = CONFIG_DIR / 'google_credentials.json'
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Cloud settings keys that persist in Google Sheets (survive Railway deploys)
CLOUD_SETTINGS_KEYS = ['paused_until', 'holiday_mode', 'auto_send_enabled', 'auto_send_count', 'days_between_emails']

# ============================================================================
# ENVIRONMENT VARIABLES (for deployment - keeps secrets out of code)
# ============================================================================
# These are loaded from environment variables when deployed to Railway/Render
# Locally, they fall back to defaults or .env file

def get_env(key: str, default: str = '') -> str:
    """Get environment variable with fallback to default."""
    return os.environ.get(key, default)

# Sensitive credentials from environment (NEVER hardcode in production)
ENV_EMAIL_ADDRESS = get_env('EMAIL_ADDRESS', '')
ENV_APP_PASSWORD = get_env('APP_PASSWORD', '')
ENV_GOOGLE_CREDENTIALS = get_env('GOOGLE_CREDENTIALS', '')  # JSON string of service account
ENV_NTFY_CHANNEL = get_env('NTFY_CHANNEL', '')

# Gmail API credentials (for Railway - SMTP is blocked)
ENV_GMAIL_CLIENT_ID = get_env('GMAIL_CLIENT_ID', '')
ENV_GMAIL_CLIENT_SECRET = get_env('GMAIL_CLIENT_SECRET', '')
ENV_GMAIL_REFRESH_TOKEN = get_env('GMAIL_REFRESH_TOKEN', '')

def has_gmail_api():
    """Check if Gmail API credentials are configured."""
    return bool(ENV_GMAIL_CLIENT_ID and ENV_GMAIL_CLIENT_SECRET and ENV_GMAIL_REFRESH_TOKEN)

# Athlete info from environment (persists on Railway)
ENV_ATHLETE_NAME = get_env('ATHLETE_NAME', 'Keelan Underwood')
ENV_ATHLETE_EMAIL = get_env('ATHLETE_EMAIL', 'underwoodkeelan@gmail.com')
ENV_ATHLETE_PHONE = get_env('ATHLETE_PHONE', '9107471140')
ENV_ATHLETE_GRAD_YEAR = get_env('ATHLETE_GRAD_YEAR', '2026')
ENV_ATHLETE_HEIGHT = get_env('ATHLETE_HEIGHT', "6'3")
ENV_ATHLETE_WEIGHT = get_env('ATHLETE_WEIGHT', '295')
ENV_ATHLETE_POSITION = get_env('ATHLETE_POSITION', 'OL')
ENV_ATHLETE_GPA = get_env('ATHLETE_GPA', '3.0')
ENV_ATHLETE_SCHOOL = get_env('ATHLETE_SCHOOL', 'The Benjamin School')
ENV_ATHLETE_STATE = get_env('ATHLETE_STATE', 'FL')
ENV_HUDL_LINK = get_env('HUDL_LINK', 'https://x.com/underwoodkeelan/status/1995522905841746075?s=46')
ENV_AUTO_SEND = get_env('AUTO_SEND_ENABLED', 'true').lower() == 'true'
ENV_NOTIFICATIONS = get_env('NOTIFICATIONS_ENABLED', 'true').lower() == 'true'

DEFAULT_SETTINGS = {
    'athlete': {
        'name': ENV_ATHLETE_NAME, 
        'graduation_year': ENV_ATHLETE_GRAD_YEAR, 
        'height': ENV_ATHLETE_HEIGHT, 
        'weight': ENV_ATHLETE_WEIGHT,
        'positions': ENV_ATHLETE_POSITION, 
        'high_school': ENV_ATHLETE_SCHOOL, 
        'city': '', 
        'state': ENV_ATHLETE_STATE,
        'gpa': ENV_ATHLETE_GPA, 
        'highlight_url': ENV_HUDL_LINK, 
        'phone': ENV_ATHLETE_PHONE, 
        'email': ENV_ATHLETE_EMAIL,
    },
    'email': {
        'smtp_server': 'smtp.gmail.com', 'smtp_port': 587,
        'email_address': ENV_EMAIL_ADDRESS,
        'app_password': ENV_APP_PASSWORD,
        'max_per_day': 100,  # Gmail limit
        'delay_seconds': 3,
        'days_between_emails': 4,  # Wait 4 days before next email (~2 emails per week max)
        'followup_sequence': ['intro', 'followup_1', 'followup_2'],  # Email sequence
        'auto_send_enabled': ENV_AUTO_SEND,
        'auto_send_count': 100,
        # Holiday/Pause modes
        'holiday_mode': False,       # No follow-ups, max 5 intros/day
        'paused_until': None,        # ISO date string when pause ends (e.g., '2025-01-04')
    },
    'sheets': {'spreadsheet_name': 'bardeen', 'credentials_configured': False},
    'scraper': {'start_from_bottom': False, 'batch_size': 10, 'min_delay': 2.0, 'max_delay': 5.0},
    'notifications': {'enabled': ENV_NOTIFICATIONS, 'channel': ENV_NTFY_CHANNEL},
    'setup_complete': True, 'first_run': False,
}

def load_settings() -> Dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                settings = json.loads(json.dumps(DEFAULT_SETTINGS))
                for key in saved:
                    if isinstance(saved[key], dict) and key in settings:
                        settings[key].update(saved[key])
                    else:
                        settings[key] = saved[key]
                
                # ALWAYS prioritize environment variables over saved settings
                if ENV_EMAIL_ADDRESS:
                    settings['email']['email_address'] = ENV_EMAIL_ADDRESS
                if ENV_APP_PASSWORD:
                    settings['email']['app_password'] = ENV_APP_PASSWORD
                if ENV_NTFY_CHANNEL:
                    if 'notifications' not in settings:
                        settings['notifications'] = {}
                    settings['notifications']['channel'] = ENV_NTFY_CHANNEL
                
                # Fix corrupted/empty password - use env var or default if invalid
                if 'email' in settings:
                    pwd = settings['email'].get('app_password')
                    email = settings['email'].get('email_address')
                    if not pwd or pwd is True or pwd == '********' or not isinstance(pwd, str) or len(pwd) < 5:
                        if ENV_APP_PASSWORD:
                            settings['email']['app_password'] = ENV_APP_PASSWORD
                        else:
                            settings['email']['app_password'] = DEFAULT_SETTINGS['email']['app_password']
                        try: logger.warning("Using env/default app_password (saved was invalid)")
                        except NameError: pass
                    if not email or not isinstance(email, str) or '@' not in email:
                        if ENV_EMAIL_ADDRESS:
                            settings['email']['email_address'] = ENV_EMAIL_ADDRESS
                        else:
                            settings['email']['email_address'] = DEFAULT_SETTINGS['email']['email_address']
                        try: logger.warning("Using env/default email_address (saved was invalid)")
                        except NameError: pass
                
                return settings
        except: pass
    
    # No saved settings - return defaults (which already use env vars)
    return json.loads(json.dumps(DEFAULT_SETTINGS))

def save_settings(s: Dict):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(s, f, indent=2)
    # Sync to Supabase
    if SUPABASE_AVAILABLE and _supabase_db:
        try:
            email_s = s.get('email', {})
            sb_settings = {}
            if 'auto_send_enabled' in email_s:
                sb_settings['auto_send_enabled'] = bool(email_s['auto_send_enabled'])
            if 'auto_send_count' in email_s:
                sb_settings['auto_send_count'] = int(email_s.get('auto_send_count', 25))
            if 'paused_until' in email_s and email_s['paused_until']:
                sb_settings['paused_until'] = email_s['paused_until']
            if 'days_between_emails' in email_s:
                sb_settings['days_between_followups'] = int(email_s.get('days_between_emails', 7))
            notif = s.get('notifications', {})
            if notif.get('enabled') is not None:
                sb_settings['notifications_enabled'] = bool(notif['enabled'])
            if notif.get('channel'):
                sb_settings['ntfy_channel'] = notif['channel']
            if sb_settings:
                _supabase_db.save_settings(**sb_settings)
        except Exception as sb_e:
            logger.warning(f"Supabase settings sync error: {sb_e}")


# ============================================================================
# SETTINGS (Supabase-backed, no more Google Sheets)
# ============================================================================

settings = load_settings()

def ensure_cloud_settings():
    """Load settings from Supabase if available."""
    pass  # Settings are now loaded from Supabase at startup

# ============================================================================
# LOGGING & STATE
# ============================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', os.urandom(24).hex())
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# Encryption key for per-athlete Gmail credentials stored in Supabase
CREDENTIALS_ENCRYPTION_KEY = os.environ.get('CREDENTIALS_ENCRYPTION_KEY', '')

# Register enterprise blueprint
try:
    from enterprise.routes import enterprise_bp
    app.register_blueprint(enterprise_bp)
    logger.info("Enterprise features loaded")
except ImportError as e:
    logger.warning(f"Enterprise features not available: {e}")

# Initialize Supabase
_supabase_db = None
try:
    if get_db:
        _supabase_db = get_db()
        # For backwards compat / auto-send scheduler, set default athlete from env
        _default_athlete_email = os.environ.get('ATHLETE_EMAIL', os.environ.get('EMAIL_ADDRESS', ''))
        _default_athlete_name = os.environ.get('ATHLETE_NAME', 'Keelan Underwood')
        if _default_athlete_email:
            _supabase_db.get_or_create_athlete(_default_athlete_name, _default_athlete_email)
        SUPABASE_AVAILABLE = True
        logger.info("Supabase database connected")

        # Auto-sync templates from TemplateManager → Supabase on startup
        try:
            from enterprise.templates import get_template_manager
            tm = get_template_manager()
            existing_sb = _supabase_db.get_templates()
            existing_names = {t['name'] for t in existing_sb}
            synced = 0
            for t in tm.templates.values():
                if t.name not in existing_names:
                    _supabase_db.create_template(
                        name=t.name,
                        body=t.body,
                        subject=t.subject,
                        template_type=t.template_type if t.template_type in ('email', 'dm', 'followup') else 'email',
                        coach_type=t.template_type if t.template_type in ('rc', 'ol', 'any') else 'any',
                    )
                    synced += 1
            if synced:
                logger.info(f"Synced {synced} templates to Supabase")
        except Exception as te:
            logger.warning(f"Template sync to Supabase failed: {te}")

        # Auto-sync settings → Supabase on startup
        try:
            s = load_settings()
            email_s = s.get('email', {})
            sb_settings = {}
            if email_s.get('auto_send_enabled') is not None:
                sb_settings['auto_send_enabled'] = bool(email_s['auto_send_enabled'])
            if email_s.get('auto_send_count'):
                sb_settings['auto_send_count'] = int(email_s['auto_send_count'])
            if email_s.get('paused_until'):
                sb_settings['paused_until'] = email_s['paused_until']
            if email_s.get('days_between_emails'):
                sb_settings['days_between_followups'] = int(email_s['days_between_emails'])
            notif = s.get('notifications', {})
            if notif.get('enabled') is not None:
                sb_settings['notifications_enabled'] = bool(notif['enabled'])
            if notif.get('channel'):
                sb_settings['ntfy_channel'] = notif['channel']
            if sb_settings:
                _supabase_db.save_settings(**sb_settings)
                logger.info(f"Synced settings to Supabase: {list(sb_settings.keys())}")
        except Exception as se:
            logger.warning(f"Settings sync to Supabase failed: {se}")

except Exception as e:
    logger.warning(f"Supabase not available, using legacy tracking: {e}")
    SUPABASE_AVAILABLE = False

# ============================================================================
# AUTHENTICATION
# ============================================================================

def login_required(f):
    """Require user to be logged in."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'athlete_id' not in session:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Login required'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    """Require admin access."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'athlete_id' not in session:
            return jsonify({'success': False, 'error': 'Login required'}), 401
        if not session.get('is_admin'):
            return jsonify({'success': False, 'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated

@app.before_request
def load_athlete_context():
    """Set per-request athlete context from session."""
    # Skip auth for login page, static, and health check
    if request.path in ('/login', '/health', '/api/track/open') or request.path.startswith('/api/track/'):
        return
    g.athlete_id = session.get('athlete_id')
    g.is_admin = session.get('is_admin', False)
    g.athlete_name = session.get('athlete_name', '')
    if g.athlete_id and _supabase_db:
        _supabase_db.set_context_athlete(g.athlete_id)

def get_athlete_gmail_service(athlete_id=None):
    """Get Gmail API service for a specific athlete using their encrypted credentials."""
    aid = athlete_id or getattr(g, 'athlete_id', None)
    if not aid or not _supabase_db or not CREDENTIALS_ENCRYPTION_KEY:
        return None
    creds_data = _supabase_db.get_athlete_credentials(aid, CREDENTIALS_ENCRYPTION_KEY)
    if not creds_data:
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials(
            token=None,
            refresh_token=creds_data['gmail_refresh_token'],
            client_id=creds_data['gmail_client_id'],
            client_secret=creds_data['gmail_client_secret'],
            token_uri="https://oauth2.googleapis.com/token"
        )
        return build('gmail', 'v1', credentials=creds)
    except Exception as e:
        logger.error(f"Gmail API error for athlete {aid}: {e}")
        return None

event_queue = queue.Queue()
active_task = None
task_thread = None
stop_requested = False
cached_responses = []  # Store found responses for display

# Email tracking data
email_tracking = {
    'sent': {},      # tracking_id -> {to, school, coach, sent_at, subject, template_id}
    'opens': {},     # tracking_id -> [{opened_at, ip, user_agent}]
    'clicks': {},    # tracking_id -> [{clicked_at, url, ip}]
    'responses': {}  # tracking_id -> {responded_at, sentiment, snippet}
}
TRACKING_FILE = CONFIG_DIR / 'email_tracking.json'

# ============================================================================
# RESPONSE SENTIMENT ANALYZER
# ============================================================================

SENTIMENT_KEYWORDS = {
    'interested': {
        'positive': ['interested', 'love to', 'would like to', 'excited', 'let\'s talk', 'schedule a call',
                     'come visit', 'visit campus', 'looking forward', 'send me', 'great film', 'impressed',
                     'offer', 'scholarship', 'want to meet', 'call me', 'phone call', 'zoom', 'official visit'],
        'weight': 3
    },
    'needs_info': {
        'positive': ['send your', 'need more', 'transcript', 'test scores', 'gpa', 'grades',
                     'updated film', 'more film', 'junior film', 'senior film', 'schedule',
                     'what position', 'measurables', 'combine', 'camp'],
        'weight': 2
    },
    'follow_up_later': {
        'positive': ['check back', 'reach out', 'touch base', 'next year', 'in the spring',
                     'after the season', 'few months', 'later', 'down the road', 'keep in touch',
                     'stay in contact', 'recruiting class', 'next cycle'],
        'weight': 1
    },
    'soft_no': {
        'positive': ['full', 'roster is', 'no spots', 'committed', 'class is full', 'not recruiting',
                     'different direction', 'unfortunately', 'at this time', 'good luck',
                     'best of luck', 'wish you well', 'not a fit', 'position filled'],
        'weight': 0
    }
}

def analyze_response_sentiment(text: str) -> dict:
    """Analyze response text to determine coach's interest level and sentiment."""
    if not text:
        return {'sentiment': 'unknown', 'confidence': 0, 'label': 'Unknown'}

    text_lower = text.lower()
    scores = {}

    for sentiment, data in SENTIMENT_KEYWORDS.items():
        score = 0
        matched_keywords = []
        for keyword in data['positive']:
            if keyword in text_lower:
                score += 1
                matched_keywords.append(keyword)
        scores[sentiment] = {'score': score, 'keywords': matched_keywords}

    # Find highest scoring sentiment
    best_sentiment = 'unknown'
    best_score = 0
    for sentiment, data in scores.items():
        if data['score'] > best_score:
            best_score = data['score']
            best_sentiment = sentiment

    # Map to display labels and colors
    labels = {
        'interested': {'label': '🔥 Interested', 'color': '#22c55e', 'priority': 1},
        'needs_info': {'label': '📋 Needs Info', 'color': '#f59e0b', 'priority': 2},
        'follow_up_later': {'label': '📅 Follow Up', 'color': '#6366f1', 'priority': 3},
        'soft_no': {'label': '❌ Not Now', 'color': '#ef4444', 'priority': 4},
        'unknown': {'label': '❓ Review', 'color': '#888', 'priority': 5}
    }

    result = labels.get(best_sentiment, labels['unknown'])
    result['sentiment'] = best_sentiment
    result['confidence'] = min(best_score * 33, 100)  # Rough confidence
    result['matched'] = scores.get(best_sentiment, {}).get('keywords', [])

    return result

def load_tracking():
    """Load tracking data from local file."""
    global email_tracking
    if TRACKING_FILE.exists():
        try:
            with open(TRACKING_FILE) as f:
                email_tracking = json.load(f)
        except:
            pass

def save_tracking():
    """Save tracking data to local file."""
    try:
        with open(TRACKING_FILE, 'w') as f:
            json.dump(email_tracking, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error saving tracking locally: {e}")

def generate_tracking_id(to_email: str, school: str) -> str:
    """Generate unique tracking ID for an email."""
    import hashlib
    data = f"{to_email}{school}{datetime.now().isoformat()}{random.random()}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]

load_tracking()  # Load on startup

def add_log(msg: str, level: str = 'info'):
    entry = {'time': datetime.now().strftime('%H:%M:%S'), 'msg': msg, 'level': level}
    event_queue.put({'type': 'log', 'data': entry})
    getattr(logger, level, logger.info)(msg)


# ============================================================================
# GMAIL API FUNCTIONS (for Railway - SMTP is blocked)
# ============================================================================

def get_gmail_service():
    """Get authenticated Gmail API service. Tries per-athlete credentials first, then global env vars."""
    # Try per-athlete credentials first
    athlete_service = get_athlete_gmail_service()
    if athlete_service:
        logger.info("Using per-athlete Gmail credentials")
        return athlete_service

    # Fall back to global env vars
    if not has_gmail_api():
        logger.error("Gmail API credentials not set")
        return None

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        logger.info(f"Creating Gmail credentials with client_id: {ENV_GMAIL_CLIENT_ID[:20]}...")

        creds = Credentials(
            token=None,
            refresh_token=ENV_GMAIL_REFRESH_TOKEN,
            client_id=ENV_GMAIL_CLIENT_ID,
            client_secret=ENV_GMAIL_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token"
        )

        service = build('gmail', 'v1', credentials=creds)
        logger.info("Gmail service created successfully")
        return service
    except Exception as e:
        logger.error(f"Gmail API error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


@app.route('/api/debug/gmail-test')
def api_debug_gmail_test():
    """Test Gmail API connection with detailed error."""
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request
        
        creds = Credentials(
            token=None,
            refresh_token=ENV_GMAIL_REFRESH_TOKEN,
            client_id=ENV_GMAIL_CLIENT_ID,
            client_secret=ENV_GMAIL_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token"
        )
        
        # Force refresh to test credentials
        creds.refresh(Request())
        
        # Try to list labels
        service = build('gmail', 'v1', credentials=creds)
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])
        
        return jsonify({
            'success': True,
            'message': 'Gmail API working!',
            'labels_count': len(labels),
            'token_valid': creds.valid
        })
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__,
            'traceback': traceback.format_exc()
        })


@app.route('/api/debug/gmail-config')
def api_debug_gmail_config():
    """Debug endpoint to check Gmail configuration."""
    client_id = ENV_GMAIL_CLIENT_ID
    client_secret = ENV_GMAIL_CLIENT_SECRET
    refresh_token = ENV_GMAIL_REFRESH_TOKEN
    
    return jsonify({
        'has_gmail_api': has_gmail_api(),
        'client_id_set': bool(client_id),
        'client_id_preview': client_id[:20] + '...' if client_id and len(client_id) > 20 else client_id,
        'client_id_length': len(client_id) if client_id else 0,
        'client_secret_set': bool(client_secret),
        'client_secret_length': len(client_secret) if client_secret else 0,
        'refresh_token_set': bool(refresh_token),
        'refresh_token_length': len(refresh_token) if refresh_token else 0,
        'refresh_token_preview': refresh_token[:10] + '...' if refresh_token and len(refresh_token) > 10 else 'not set'
    })


def send_email_gmail_api(to_email: str, subject: str, body: str, from_email: str = None, school: str = '', coach_name: str = '', template_id: str = 'default') -> bool:
    """Send email using Gmail API with open tracking (works on Railway)."""
    service = get_gmail_service()
    if not service:
        logger.error("Gmail API not configured")
        return False

    try:
        from_email = from_email or ENV_EMAIL_ADDRESS

        # Generate tracking ID
        tracking_id = generate_tracking_id(to_email, school)

        # Get the app URL for tracking pixel
        app_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')
        if app_url and not app_url.startswith('http'):
            app_url = f"https://{app_url}"
        if not app_url:
            app_url = "https://coach-outreach.up.railway.app"

        # Create HTML version with tracking pixel
        html_body = f"""
        <div style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6;">
            {body.replace(chr(10), '<br>')}
        </div>
        <img src="{app_url}/api/track/open/{tracking_id}" width="1" height="1" style="display:none;" alt="">
        """

        # Create message with both plain and HTML
        message = MIMEMultipart('alternative')
        message['to'] = to_email
        message['from'] = from_email
        message['subject'] = subject

        # Attach plain text first, then HTML (email clients prefer the last one)
        message.attach(MIMEText(body, 'plain'))
        message.attach(MIMEText(html_body, 'html'))

        # Encode in base64
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        # Send via Gmail API
        result = service.users().messages().send(
            userId='me',
            body={'raw': raw}
        ).execute()

        # Store tracking info with template_id for A/B testing
        email_tracking['sent'][tracking_id] = {
            'to': to_email,
            'school': school,
            'coach': coach_name,
            'subject': subject,
            'sent_at': datetime.now().isoformat(),
            'message_id': result.get('id'),
            'template_id': template_id  # For A/B testing
        }
        email_tracking['opens'][tracking_id] = []
        save_tracking()

        # Save to Supabase for persistent, queryable tracking
        if SUPABASE_AVAILABLE and _supabase_db:
            try:
                outreach = _supabase_db.create_outreach(
                    coach_email=to_email,
                    coach_name=coach_name,
                    school_name=school,
                    subject=subject,
                    body=body,
                )
                if outreach:
                    _supabase_db.mark_sent(outreach['id'])
                    # Map the local tracking_id to the Supabase tracking_id
                    supabase_tid = outreach.get('tracking_id')
                    if supabase_tid:
                        email_tracking['sent'][tracking_id]['supabase_tracking_id'] = supabase_tid
                        email_tracking['sent'][tracking_id]['supabase_outreach_id'] = outreach['id']
                    logger.info(f"Outreach saved to Supabase: {outreach['id']}")
            except Exception as e:
                logger.warning(f"Failed to save outreach to Supabase: {e}")

        logger.info(f"Email sent via Gmail API to {to_email}, ID: {result.get('id')}, tracking: {tracking_id}, template: {template_id}")
        return True

    except Exception as e:
        logger.error(f"Gmail API send error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def check_inbox_gmail_api(query: str = "is:unread") -> list:
    """Check inbox for emails using Gmail API."""
    service = get_gmail_service()
    if not service:
        return []
    
    try:
        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=20
        ).execute()
        
        messages = results.get('messages', [])
        emails = []
        
        for msg in messages:
            msg_data = service.users().messages().get(
                userId='me',
                id=msg['id'],
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date']
            ).execute()
            
            headers = {h['name']: h['value'] for h in msg_data.get('payload', {}).get('headers', [])}
            emails.append({
                'id': msg['id'],
                'from': headers.get('From', ''),
                'subject': headers.get('Subject', ''),
                'date': headers.get('Date', ''),
                'snippet': msg_data.get('snippet', '')
            })
        
        return emails
        
    except Exception as e:
        logger.error(f"Gmail API inbox error: {e}")
        return []


def send_email_auto(to_email: str, subject: str, body: str, from_email: str = None) -> bool:
    """
    Send email using best available method:
    1. Gmail API (if configured - works on Railway)
    2. SMTP (fallback for local/Render)
    """
    from_email = from_email or ENV_EMAIL_ADDRESS
    
    # Try Gmail API first (works on Railway)
    if has_gmail_api():
        logger.info(f"Sending via Gmail API to {to_email}")
        return send_email_gmail_api(to_email, subject, body, from_email)
    
    # Fallback to SMTP
    app_pass = ENV_APP_PASSWORD
    if not from_email or not app_pass:
        logger.error("No email credentials configured")
        return False
    
    try:
        logger.info(f"Sending via SMTP to {to_email}")
        msg = MIMEMultipart()
        msg['From'] = from_email
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        smtp = smtplib.SMTP('smtp.gmail.com', 587, timeout=30)
        smtp.starttls()
        smtp.login(from_email, app_pass)
        smtp.sendmail(from_email, to_email, msg.as_string())
        smtp.quit()
        
        logger.info(f"Email sent via SMTP to {to_email}")
        return True
        
    except Exception as e:
        logger.error(f"SMTP error: {e}")
        return False


# ============================================================================
# OPTIONAL IMPORTS
# ============================================================================

HAS_SCRAPER = False
HAS_TWITTER_SCRAPER = False
HAS_EMAIL_SCRAPER = False
HAS_SHEETS = False

try:
    from scrapers import CoachScraper, ScraperConfig
    HAS_SCRAPER = True
except Exception as e:
    logger.warning(f"Name scraper unavailable: {e}")

try:
    from scrapers import TwitterScraper, TwitterScraperConfig
    HAS_TWITTER_SCRAPER = True
except Exception as e:
    logger.warning(f"Twitter scraper unavailable: {e}")

try:
    from scrapers import EmailScraper, EmailScraperConfig
    HAS_EMAIL_SCRAPER = True
except Exception as e:
    logger.warning(f"Email scraper unavailable: {e}")

HAS_SHEETS = False  # Google Sheets removed — Supabase is now the sole data store

def get_sheet():
    """Legacy stub — returns None. All data is now in Supabase."""
    return None

def is_railway_deployment():
    """Check if we're running on Railway."""
    return bool(ENV_EMAIL_ADDRESS and ENV_APP_PASSWORD)

# ============================================================================
# LOGIN PAGE TEMPLATE
# ============================================================================

LOGIN_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - RecruitSignal</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        :root{--bg:#050505;--bg2:#111;--border:#333;--text:#fff;--muted:#888;--accent:#00ff88;--glow:rgba(0,255,136,0.4)}
        body{font-family:'Space Grotesk',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center}
        .login-box{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:40px;width:90%;max-width:400px}
        .logo{font-size:28px;font-weight:800;text-align:center;margin-bottom:8px;text-transform:uppercase;letter-spacing:-1px}
        .logo .hl{color:var(--accent)}
        .sub{text-align:center;color:var(--muted);font-size:14px;margin-bottom:32px}
        .fg{margin-bottom:20px}
        label{display:block;font-size:13px;color:var(--muted);margin-bottom:8px}
        input{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:12px;border-radius:6px;width:100%;font-size:14px;font-family:inherit}
        input:focus{outline:none;border-color:var(--accent)}
        .btn{background:var(--accent);color:#000;border:none;padding:14px;border-radius:6px;cursor:pointer;font-size:15px;font-weight:600;width:100%;box-shadow:0 0 15px var(--glow);font-family:inherit}
        .btn:hover{box-shadow:0 0 25px var(--glow)}
        .btn:disabled{opacity:.5;cursor:not-allowed}
        .err{background:rgba(239,68,68,.1);border:1px solid #ef4444;color:#ef4444;padding:12px;border-radius:6px;margin-bottom:20px;font-size:13px;display:none}
        .err.show{display:block}
    </style>
</head>
<body>
    <div class="login-box">
        <div class="logo">Recruit<span class="hl">Signal</span></div>
        <div class="sub">Pro Athlete Platform</div>
        <div id="err" class="err"></div>
        <form onsubmit="doLogin(event)">
            <div class="fg"><label>Email</label><input type="email" id="email" required autocomplete="email"></div>
            <div class="fg"><label>Password</label><input type="password" id="pw" required autocomplete="current-password"></div>
            <button type="submit" class="btn" id="btn">LOG IN</button>
        </form>
    </div>
    <script>
    async function doLogin(e){
        e.preventDefault();
        const btn=document.getElementById('btn'),err=document.getElementById('err');
        btn.disabled=true;btn.textContent='Logging in...';err.classList.remove('show');
        try{
            const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value,password:document.getElementById('pw').value})});
            const d=await r.json();
            if(d.success){window.location.href='/'}else{err.textContent=d.error||'Login failed';err.classList.add('show');btn.disabled=false;btn.textContent='LOG IN'}
        }catch(x){err.textContent='Connection error';err.classList.add('show');btn.disabled=false;btn.textContent='LOG IN'}
    }
    </script>
</body>
</html>'''

# ============================================================================
# HTML TEMPLATE - ENTERPRISE UI
# ============================================================================


HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RecruitSignal Pro</title>
    <!-- PWA Support -->
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#050505">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="RecruitSignal">
    <link rel="apple-touch-icon" href="/icon-192.png">
    <!-- Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg: #050505; --bg2: #111111; --bg3: #1a1a1a;
            --border: #333; --text: #fff; --muted: #888888;
            --accent: #00ff88; --accent-glow: rgba(0, 255, 136, 0.4); --success: #22c55e; --warn: #f59e0b; --err: #ef4444;
        }
        body { font-family: 'Space Grotesk', -apple-system, BlinkMacSystemFont, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

        /* Layout */
        .app { display: flex; flex-direction: column; height: 100vh; }
        header { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; position: relative; }
        .header-left { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
        .header-center { flex: 1; text-align: center; min-width: 200px; }
        .header-center .logo { font-size: 20px; font-weight: 800; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; text-transform: uppercase; letter-spacing: -1px; }
        .header-center .logo .highlight { color: var(--accent); }
        .athlete-name { font-size: 16px; font-weight: 700; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: linear-gradient(90deg, #fff, var(--accent)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; white-space: nowrap; }
        .header-actions { display: flex; gap: 12px; align-items: center; justify-content: flex-end; flex-shrink: 0; }
        .gear-btn { background: none; border: none; color: var(--muted); font-size: 20px; cursor: pointer; padding: 8px; transition: color 0.3s; }
        .gear-btn:hover { color: var(--text); }

        /* Tabs */
        nav { background: var(--bg2); border-bottom: 1px solid var(--border); display: flex; padding: 0 24px; }
        .tab { padding: 14px 20px; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; transition: all 0.2s; }
        .tab:hover { color: var(--text); }
        .tab.active { color: var(--accent); border-bottom-color: var(--accent); }

        /* Main content */
        main { flex: 1; overflow-y: auto; padding: 24px; }
        .page { display: none; }
        .page.active { display: block; }

        /* Cards */
        .card { background: var(--bg2); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 16px; transition: transform 0.3s, border-color 0.3s; }
        .card:hover { transform: translateY(-2px); border-color: var(--accent); }
        .card-header { font-size: 14px; font-weight: 600; color: var(--accent); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1.5px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }

        /* Stats grid */
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .stat { background: var(--bg2); border: 1px solid var(--border); border-radius: 12px; padding: 16px; text-align: center; transition: transform 0.3s, border-color 0.3s; }
        .stat:hover { transform: translateY(-2px); border-color: var(--accent); }
        .stat-value { font-size: 32px; font-weight: 700; color: var(--accent); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
        .stat-label { font-size: 12px; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 1px; }
        /* Color variants for stat values */
        .stat-value.highlight { color: var(--accent); }
        .stat-value.cyan { color: var(--accent); }
        .stat-value.success { color: var(--success); }

        /* Buttons */
        .btn { background: var(--accent); color: #000; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 600; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; transition: all 0.3s ease; box-shadow: 0 0 15px var(--accent-glow); }
        .btn:hover { background: transparent; color: var(--accent); box-shadow: 0 0 25px var(--accent-glow); border: 1px solid var(--accent); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-primary { background: var(--accent); color: #000; }
        .btn-outline, .btn-secondary { background: transparent; border: 1px solid var(--border); color: var(--text); box-shadow: none; }
        .btn-outline:hover, .btn-secondary:hover { border-color: var(--accent); color: var(--accent); background: transparent; box-shadow: none; }
        .btn-sm { padding: 6px 12px; font-size: 13px; }
        .btn-success { background: var(--success); color: #000; box-shadow: none; }
        .btn-warn { background: var(--warn); color: #000; box-shadow: none; }

        /* Inputs */
        input, select, textarea { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 10px 12px; border-radius: 6px; width: 100%; font-size: 14px; }
        input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
        label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
        .form-group { margin-bottom: 16px; }

        /* Table */
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid var(--border); }
        th { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 500; }
        tr:hover { background: var(--bg3); }

        /* Modal */
        .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); display: none; align-items: center; justify-content: center; z-index: 1000; }
        .modal-overlay.active { display: flex; }
        .modal { background: var(--bg2); border-radius: 12px; padding: 24px; width: 90%; max-width: 600px; max-height: 90vh; overflow-y: auto; border: 1px solid var(--border); }
        .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .modal-title { font-size: 18px; font-weight: 600; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
        .modal-close { background: none; border: none; color: var(--muted); font-size: 24px; cursor: pointer; transition: color 0.3s; }
        .modal-close:hover { color: var(--text); }

        /* Toast */
        .toast { position: fixed; bottom: 24px; right: 24px; background: var(--bg2); border: 1px solid var(--border); padding: 12px 20px; border-radius: 8px; z-index: 2000; animation: slideIn 0.3s; }
        .toast.success { border-color: var(--accent); }
        .toast.error { border-color: var(--err); }
        @keyframes slideIn { from { transform: translateX(100px); opacity: 0; } }

        /* Template toggle */
        .template-item { display: flex; align-items: center; justify-content: space-between; padding: 12px; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 8px; }
        .template-info { flex: 1; }
        .template-name { font-weight: 500; }
        .template-type { font-size: 12px; color: var(--muted); }
        .toggle { position: relative; width: 44px; height: 24px; }
        .toggle input { opacity: 0; width: 0; height: 0; }
        .toggle-slider { position: absolute; inset: 0; background: var(--bg); border: 1px solid var(--border); border-radius: 24px; cursor: pointer; transition: 0.2s; }
        .toggle-slider:before { content: ''; position: absolute; height: 18px; width: 18px; left: 2px; bottom: 2px; background: var(--muted); border-radius: 50%; transition: 0.2s; }
        .toggle input:checked + .toggle-slider { background: var(--accent); border-color: var(--accent); }
        .toggle input:checked + .toggle-slider:before { transform: translateX(20px); background: white; }

        /* Toggle switch (auto-send) */
        .toggle-switch { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--muted); cursor: pointer; }
        .toggle-switch input { width: auto; }
        .toggle-track { position: relative; width: 44px; height: 24px; background: var(--bg); border: 1px solid var(--border); border-radius: 24px; cursor: pointer; transition: 0.2s; flex-shrink: 0; }
        .toggle-track:before { content: ''; position: absolute; height: 18px; width: 18px; left: 2px; bottom: 2px; background: var(--muted); border-radius: 50%; transition: 0.2s; }
        .toggle-switch input:checked + .toggle-track { background: var(--accent); border-color: var(--accent); }
        .toggle-switch input:checked + .toggle-track:before { transform: translateX(20px); background: white; }
        .toggle-switch input { opacity: 0; width: 0; height: 0; position: absolute; }

        /* DM Card */
        .dm-card { background: var(--bg3); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 12px; }
        .dm-header { display: flex; justify-content: space-between; margin-bottom: 12px; }
        .dm-school { font-weight: 600; }
        .dm-coach { color: var(--muted); font-size: 14px; }
        .dm-textarea { min-height: 80px; resize: vertical; margin-bottom: 8px; }
        .char-count { text-align: right; font-size: 12px; color: var(--muted); }
        .char-count.over { color: var(--err); }
        .dm-actions { display: flex; gap: 8px; margin-top: 12px; }

        /* Response list */
        .response-item { display: flex; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border); }
        .response-avatar { width: 40px; height: 40px; background: var(--accent); border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 600; }
        .response-content { flex: 1; }
        .response-school { font-weight: 500; }
        .response-snippet { font-size: 13px; color: var(--muted); margin-top: 4px; }
        .response-time { font-size: 12px; color: var(--muted); }

        /* Hot leads */
        .lead-item { display: flex; justify-content: space-between; align-items: center; padding: 10px; border-bottom: 1px solid var(--border); }
        .lead-school { font-weight: 500; }
        .lead-coach { font-size: 13px; color: var(--muted); }
        .lead-badge { background: var(--warn); color: #000; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }

        /* Command panel (auto-send) */
        .command-panel { background: var(--bg2); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; }
        .command-info h3 { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
        .command-display { display: flex; align-items: baseline; gap: 8px; }
        .command-number { font-size: 36px; font-weight: 700; color: var(--accent); }
        .command-unit { font-size: 14px; color: var(--muted); }
        .command-time { font-size: 18px; font-weight: 600; color: var(--accent); }
        .command-breakdown { font-size: 12px; color: var(--muted); margin-top: 6px; }
        .command-actions { display: flex; align-items: center; gap: 16px; }

        /* Perf grid */
        .perf-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
        .perf-stat { text-align: center; padding: 12px; background: var(--bg3); border-radius: 8px; }
        .perf-value { font-size: 24px; font-weight: 700; color: var(--accent); }
        .perf-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }

        /* Loading state */
        .loading-state { display: flex; align-items: center; gap: 12px; padding: 20px; color: var(--muted); }
        .spinner { width: 20px; height: 20px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; flex-shrink: 0; }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* Utility */
        .flex { display: flex; }
        .gap-2 { gap: 8px; }
        .gap-4 { gap: 16px; }
        .mt-2 { margin-top: 8px; }
        .mt-4 { margin-top: 16px; }
        .mb-4 { margin-bottom: 16px; }
        .text-center { text-align: center; }
        .text-muted { color: var(--muted); }
        .text-sm { font-size: 13px; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

        /* kbd */
        kbd { background: var(--bg3); border: 1px solid var(--border); border-radius: 3px; padding: 2px 6px; font-family: monospace; font-size: 11px; }

        /* ============================================
           GLOBAL OVERFLOW PREVENTION
           ============================================ */
        html, body { overflow-x: hidden; width: 100%; }
        .app { overflow-x: hidden; width: 100%; }
        main { overflow-x: hidden; }
        img, video, iframe, embed, object { max-width: 100%; height: auto; }
        pre, code { overflow-x: auto; max-width: 100%; word-break: break-all; }
        * { min-width: 0; }

        /* Fix all inline grid/flex containers to prevent overflow */
        [style*="display:flex"], [style*="display: flex"] { flex-wrap: wrap; }
        [style*="display:grid"], [style*="display: grid"] { overflow: hidden; }
        [style*="grid-template-columns"] { overflow: hidden; }

        /* ============================================
           TABLET RESPONSIVE (1024px and below)
           ============================================ */
        @media (max-width: 1024px) {
            .grid-2 { grid-template-columns: 1fr; gap: 12px; }
            .perf-grid { grid-template-columns: repeat(3, 1fr); gap: 10px; }
            .stats { grid-template-columns: repeat(3, 1fr) !important; gap: 10px; }
        }

        /* ============================================
           MOBILE RESPONSIVE STYLES
           ============================================ */
        @media (max-width: 768px) {
            header {
                flex-direction: column;
                padding: 10px 14px;
                gap: 4px;
                align-items: stretch;
            }
            .header-left {
                display: flex;
                align-items: center;
                justify-content: space-between;
                width: 100%;
                order: 1;
            }
            .header-center {
                display: none;
            }
            .header-actions {
                position: absolute;
                right: 14px;
                top: 10px;
                order: 2;
            }
            .athlete-name {
                font-size: 16px;
                max-width: 60vw;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            #header-info {
                font-size: 12px;
                margin-left: 8px;
                white-space: nowrap;
            }
            #connection-status { display: none; }

            nav {
                padding: 0;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                scrollbar-width: none;
            }
            nav::-webkit-scrollbar { display: none; }
            .tab {
                flex: 1;
                padding: 12px 8px;
                font-size: 13px;
                white-space: nowrap;
                text-align: center;
                min-width: 0;
            }

            main { padding: 12px; }

            .stats {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 8px;
                margin-bottom: 16px;
            }
            .stat {
                padding: 12px 8px;
                border-radius: 8px;
            }
            .stat-value { font-size: 22px; }
            .stat-label { font-size: 10px; letter-spacing: 0.5px; }

            .card {
                padding: 14px;
                margin-bottom: 12px;
                border-radius: 8px;
                overflow: hidden;
            }
            .card:hover { transform: none; }
            .card-header { font-size: 11px; margin-bottom: 10px; letter-spacing: 1px; flex-wrap: wrap; }

            .btn {
                padding: 12px 16px;
                font-size: 14px;
                width: 100%;
                text-align: center;
            }
            .btn-sm {
                padding: 10px 14px;
                font-size: 13px;
                width: auto;
            }

            input, select, textarea {
                padding: 12px;
                font-size: 16px; /* prevents iOS zoom */
            }

            .grid-2 { grid-template-columns: 1fr; gap: 10px; }

            .modal-overlay { align-items: flex-end; }
            .modal {
                width: 100%;
                height: 95vh;
                max-width: 100%;
                max-height: 95vh;
                border-radius: 16px 16px 0 0;
                padding: 16px;
                overflow-y: auto;
                -webkit-overflow-scrolling: touch;
            }

            .toast {
                left: 8px;
                right: 8px;
                bottom: 8px;
                text-align: center;
                font-size: 13px;
            }

            .dm-card { padding: 12px; }
            .dm-header { flex-direction: column; gap: 4px; }
            .dm-textarea { min-height: 100px; font-size: 16px; }
            .dm-actions { flex-direction: column; gap: 8px; }

            .response-item { padding: 10px 0; gap: 10px; flex-wrap: wrap; }
            .response-avatar { width: 32px; height: 32px; font-size: 13px; flex-shrink: 0; }
            .response-content { min-width: 0; overflow: hidden; }
            .response-snippet { word-break: break-word; }

            .command-panel { flex-direction: column; padding: 16px; gap: 12px; }
            .command-number { font-size: 28px; }
            .command-display { flex-wrap: wrap; }
            .command-actions { width: 100%; justify-content: space-between; flex-wrap: wrap; gap: 10px; }

            .perf-grid { grid-template-columns: repeat(3, 1fr); gap: 8px; }
            .perf-value { font-size: 18px; }

            /* Table card layout on mobile */
            table { display: block; width: 100%; }
            thead { display: none; }
            tbody { display: block; width: 100%; }
            tr {
                display: block;
                padding: 12px;
                margin-bottom: 8px;
                background: var(--bg3);
                border-radius: 8px;
                border: 1px solid var(--border);
            }
            td {
                display: flex;
                justify-content: space-between;
                padding: 4px 0;
                border: none;
                font-size: 13px;
                word-break: break-word;
            }
            td:before { content: attr(data-label); font-weight: 500; color: var(--muted); margin-right: 8px; flex-shrink: 0; }

            /* Fix email page inline stat grids */
            .stats[style*="grid-template-columns:repeat(4"] {
                grid-template-columns: repeat(2, 1fr) !important;
            }

            /* Fix queue status bar */
            #email-pause-footer { flex-direction: column; align-items: stretch; }
            #email-pause-footer > div { width: 100%; }
            #email-pause-footer input[type="date"] { flex: 1; }

            /* Fix all inline flex containers on mobile */
            [style*="display:flex;gap:12px"], [style*="display:flex;gap:10px"],
            [style*="display:flex; gap:12px"], [style*="display:flex; gap:10px"] {
                flex-wrap: wrap !important;
            }

            /* Fix scraper tools grid */
            .form-group { margin-bottom: 12px; }

            /* Template item responsive */
            .template-item { flex-wrap: wrap; gap: 8px; }
            .template-info { min-width: 0; }
            .template-name { word-break: break-word; }

            /* Settings modal grid fix */
            .modal .grid-2 { grid-template-columns: 1fr; }

            .hide-mobile { display: none !important; }
        }

        /* ============================================
           SMALL PHONE (380px and below)
           ============================================ */
        @media (max-width: 380px) {
            .athlete-name { font-size: 14px; max-width: 50vw; }
            .tab { padding: 10px 6px; font-size: 12px; }
            .stats { grid-template-columns: repeat(2, 1fr) !important; }
            .stat-value { font-size: 18px; }
            .stat-label { font-size: 9px; }
            main { padding: 8px; }
            .card { padding: 12px; }
            .btn { padding: 10px 12px; font-size: 13px; }
            .perf-grid { grid-template-columns: 1fr 1fr 1fr; gap: 6px; }
            .perf-value { font-size: 16px; }
            .perf-label { font-size: 9px; }
            .command-number { font-size: 24px; }
            .command-time { font-size: 16px; }
        }

        /* ============================================
           LANDSCAPE PHONE
           ============================================ */
        @media (max-height: 500px) and (orientation: landscape) {
            header { padding: 6px 14px; }
            nav .tab { padding: 8px 12px; }
            main { padding: 8px 12px; }
            .stats { grid-template-columns: repeat(3, 1fr) !important; gap: 6px; margin-bottom: 10px; }
            .stat { padding: 8px 6px; }
            .stat-value { font-size: 20px; }
            .stat-label { font-size: 9px; }
            .card { padding: 12px; margin-bottom: 8px; }
            .command-panel { padding: 12px; gap: 8px; flex-direction: row; }
            .command-number { font-size: 24px; }
            .grid-2 { grid-template-columns: 1fr 1fr; gap: 10px; }
            .modal { max-height: 100vh; height: 100vh; border-radius: 0; }
        }

        /* ============================================
           SAFE AREA (notch phones)
           ============================================ */
        @supports (padding: env(safe-area-inset-top)) {
            header { padding-top: max(12px, env(safe-area-inset-top)); }
            main { padding-left: max(12px, env(safe-area-inset-left)); padding-right: max(12px, env(safe-area-inset-right)); }
            .toast { bottom: max(8px, env(safe-area-inset-bottom)); }
        }
    </style>
</head>
<body>
    <div class="app">
        <header>
            <div class="header-left">
                <span class="athlete-name" id="header-name">{{ athlete_name }}</span>
                <span class="text-muted text-sm" id="header-info">2026 OL</span>
            </div>
            <div class="header-center">
                <div class="logo">Recruit<span class="highlight">Signal</span></div>
            </div>
            <div class="header-actions">
                <span id="connection-status" class="text-sm text-muted">Connecting...</span>
                <button class="gear-btn" onclick="openSettings()">&#9881;</button>
                <button class="gear-btn" onclick="doLogout()" title="Logout" style="font-size:14px;">Logout</button>
            </div>
        </header>

        <nav>
            <div class="tab active" data-page="home">Home</div>
            <div class="tab" data-page="find">Find</div>
            <div class="tab" data-page="email">Email</div>
            <div class="tab" data-page="dms">DMs</div>
            {% if is_admin %}<div class="tab" data-page="admin">Admin</div>{% endif %}
        </nav>

        <main>
            <!-- HOME PAGE -->
            <div id="page-home" class="page active">
                <!-- Stats Grid - Scoreboard Style -->
                <div class="stats">
                    <div class="stat">
                        <div class="stat-value highlight" id="stat-sent">0</div>
                        <div class="stat-label">Emails Sent</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value cyan" id="stat-responses">0</div>
                        <div class="stat-label">Responses</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value" id="stat-rate">0%</div>
                        <div class="stat-label">Response Rate</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value" id="stat-opens">0%</div>
                        <div class="stat-label">Open Rate</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value" id="stat-followups">0</div>
                        <div class="stat-label">Follow-ups Due</div>
                    </div>
                    <div class="stat" style="cursor:pointer;" onclick="window.open(hudlUrl, '_blank')" title="View Film">
                        <div class="stat-value success" id="stat-hudl-views"><span class="spinner" style="width:24px;height:24px;"></span></div>
                        <div class="stat-label">Film Views</div>
                    </div>
                </div>

                <!-- Command Panel - Auto Send -->
                <div class="command-panel">
                    <div class="command-info">
                        <h3>Next Auto-Send</h3>
                        <div class="command-display">
                            <span class="command-number" id="tomorrow-count">—</span>
                            <span class="command-unit">coaches</span>
                            <span style="color:var(--muted);margin:0 8px;">@</span>
                            <span class="command-time" id="optimal-time">--:--</span>
                        </div>
                        <div class="command-breakdown" id="tomorrow-breakdown"></div>
                    </div>
                    <div class="command-actions">
                        <label class="toggle-switch">
                            <input type="checkbox" id="auto-send-toggle" onchange="toggleAutoSend(this.checked)">
                            <span class="toggle-track"></span>
                            Auto-send daily
                        </label>
                        <button class="btn btn-primary btn-sm" onclick="runAutoSendNow()">RUN NOW</button>
                    </div>
                </div>
                <div id="auto-send-status" class="text-sm text-muted mb-4"></div>

                <!-- Dashboard Grid -->
                <div class="grid-2">
                    <!-- Left Column -->
                    <div>
                        <div class="card">
                            <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                                Coach Responses
                                <button class="btn btn-secondary btn-sm" onclick="checkInbox()">Check Inbox</button>
                            </div>
                            <div id="recent-responses">
                                <div class="loading-state">
                                    <div class="spinner"></div>
                                    <span>Checking for responses...</span>
                                </div>
                            </div>
                        </div>

                        <div class="card">
                            <div class="card-header">Email Opens</div>
                            <div id="recent-opens" style="max-height:220px;overflow-y:auto;">
                                <div class="loading-state">
                                    <div class="spinner"></div>
                                    <span>Loading...</span>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Right Column -->
                    <div>
                        <div class="card">
                            <div class="card-header">Email Performance</div>
                            <div class="perf-grid">
                                <div class="perf-stat">
                                    <div class="perf-value" id="perf-sent">—</div>
                                    <div class="perf-label">Tracked</div>
                                </div>
                                <div class="perf-stat">
                                    <div class="perf-value" style="color:var(--accent);" id="perf-opened">—</div>
                                    <div class="perf-label">Opened</div>
                                </div>
                                <div class="perf-stat">
                                    <div class="perf-value" style="color:var(--success);" id="perf-replied">—</div>
                                    <div class="perf-label">Replied</div>
                                </div>
                            </div>
                            <div class="text-sm" id="perf-best-time" style="color:var(--muted);text-align:center;padding:12px;background:var(--bg3);border:1px solid var(--border);">
                                <span class="spinner" style="width:14px;height:14px;border-width:2px;vertical-align:middle;margin-right:8px;"></span>
                                Analyzing best send times...
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- FIND PAGE -->
            <div id="page-find" class="page">
                <div class="card">
                    <div class="card-header">Search Schools</div>
                    <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;">
                        <input type="text" id="school-search" placeholder="Search by name..." style="flex:2;min-width:200px;">
                        <select id="division-filter" style="flex:1;min-width:120px;">
                            <option value="">All Divisions</option>
                            <option value="FBS">FBS</option>
                            <option value="FCS">FCS</option>
                            <option value="D2">D2</option>
                            <option value="D3">D3</option>
                            <option value="NAIA">NAIA</option>
                            <option value="JUCO">JUCO</option>
                        </select>
                        <select id="state-filter" style="flex:1;min-width:100px;">
                            <option value="">All States</option>
                        </select>
                        <button class="btn btn-primary" onclick="searchSchools()">SEARCH</button>
                    </div>

                    <div style="overflow-x:auto;max-width:100%;">
                        <table id="schools-table">
                            <thead>
                                <tr><th>School</th><th>Division</th><th>State</th><th>Conference</th><th>Actions</th></tr>
                            </thead>
                            <tbody id="schools-body">
                                <tr><td colspan="5" class="text-center text-muted" style="padding:40px;">Enter a search query above to find schools</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <div class="card">
                    <div class="card-header">My Selected Schools</div>
                    <div id="my-schools-list" style="padding:12px;">Loading...</div>
                </div>

                {% if is_admin %}
                <div class="card">
                    <div class="card-header">Scraper Tools</div>
                    <p class="text-sm text-muted mb-4">Extract coach names, emails, and Twitter handles from your Google Sheet schools</p>

                    <div class="grid-2">
                        <div>
                            <div class="form-group">
                                <label>Data Type</label>
                                <select id="scrape-type">
                                    <option value="emails">Coach Emails</option>
                                    <option value="twitter">Twitter Handles</option>
                                    <option value="names">Coach Names</option>
                                    <option value="all">All Info</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Scope</label>
                                <select id="scrape-scope">
                                    <option value="missing">Only missing data</option>
                                    <option value="all">All schools</option>
                                    <option value="selected">Selected school only</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Batch Size</label>
                                <input type="number" id="scrape-batch" value="10" min="1" max="50">
                            </div>
                        </div>
                        <div>
                            <div class="form-group">
                                <label>Target School (optional)</label>
                                <input type="text" id="scrape-school" placeholder="Enter school name">
                            </div>
                            <div style="display:flex;gap:10px;margin-top:24px;">
                                <button class="btn btn-primary" onclick="startScraper()">START SCRAPE</button>
                                <button class="btn btn-secondary" onclick="stopScraper()">STOP</button>
                            </div>
                            <div id="scraper-status" class="mt-4 text-sm"></div>
                        </div>
                    </div>

                    <div id="scraper-log" class="mt-4" style="max-height:200px;overflow:auto;font-family:monospace;font-size:11px;background:var(--bg);border:1px solid var(--border);padding:12px;"></div>
                </div>
                {% endif %}
            </div>

            <!-- EMAIL PAGE -->
            <div id="page-email" class="page">
                <!-- Email Stats Row -->
                <div class="stats" style="grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));margin-bottom:20px;">
                    <div class="stat">
                        <div class="stat-value highlight" id="email-ready">0</div>
                        <div class="stat-label">Ready to Send</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value cyan" id="email-today">0</div>
                        <div class="stat-label">Sent Today</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value" id="email-followups">0</div>
                        <div class="stat-label">Follow-ups Due</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value success" id="email-responded">0</div>
                        <div class="stat-label">Responded</div>
                    </div>
                </div>

                <!-- Queue Summary Bar -->
                <div style="background:var(--bg3);border:1px solid var(--border);border-left:3px solid var(--accent);padding:16px 20px;margin-bottom:20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;overflow:hidden;">
                    <div>
                        <div style="font-family:monospace;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;">Queue Status</div>
                        <div class="text-sm" id="email-queue-summary" style="margin-top:4px;"><span class="spinner" style="width:12px;height:12px;border-width:2px;"></span> Loading queue...</div>
                    </div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;">
                        <button class="btn btn-secondary btn-sm" onclick="scanPastResponses()">Scan Responses</button>
                        <button class="btn btn-secondary btn-sm" onclick="cleanupSheet()">Cleanup</button>
                        <button class="btn btn-secondary btn-sm" onclick="loadEmailQueueStatus()">Refresh</button>
                    </div>
                </div>

                <div class="grid-2">
                    <div class="card">
                        <div class="card-header">Send Emails</div>
                        <div id="auto-send-info" class="mb-4" style="background:var(--bg3);border:1px solid var(--border);padding:14px;font-family:monospace;font-size:12px;">
                            <div style="display:flex;justify-content:space-between;"><span style="color:var(--muted);">Last auto-send:</span> <span id="last-auto-send">Never</span></div>
                            <div style="display:flex;justify-content:space-between;margin-top:6px;"><span style="color:var(--muted);">Next scheduled:</span> <span id="next-auto-send">Not scheduled</span></div>
                        </div>

                        <div id="tomorrow-preview" class="mb-4" style="background:var(--bg3);border:1px solid var(--border);border-left:3px solid var(--accent);padding:16px;">
                            <div style="font-weight:600;margin-bottom:12px;font-size:13px;color:var(--accent);">TOMORROW'S QUEUE</div>
                            <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(160px, 1fr));gap:10px;font-size:12px;">
                                <div style="display:flex;justify-content:space-between;"><span class="text-muted">Total ready:</span> <strong id="tomorrow-total">-</strong></div>
                                <div style="display:flex;justify-content:space-between;"><span class="text-muted">AI personalized:</span> <strong id="tomorrow-ai" style="color:var(--success);">-</strong></div>
                                <div style="display:flex;justify-content:space-between;"><span class="text-muted">Template fallback:</span> <strong id="tomorrow-template" style="color:var(--warn);">-</strong></div>
                                <div style="display:flex;justify-content:space-between;"><span class="text-muted">Daily limit:</span> <strong id="tomorrow-limit">25</strong></div>
                            </div>
                            <button class="btn btn-secondary btn-sm mt-4" onclick="loadTomorrowPreview()">Refresh Preview</button>
                        </div>

                        <div class="form-group">
                            <label>Max Emails to Send</label>
                            <input type="number" id="email-limit" value="100" min="1" max="100">
                        </div>
                        <div class="form-group">
                            <label>Template Mode</label>
                            <select id="template-mode">
                                <option value="auto">Auto (sends correct template per coach)</option>
                                <option value="manual">Choose specific template</option>
                            </select>
                        </div>
                        <div id="template-select-wrapper" style="display:none" class="form-group">
                            <label>Select Template</label>
                            <select id="template-select"></select>
                        </div>
                        <p class="text-sm text-muted mb-4">Sends to coaches not emailed recently. Each coach gets the next email in sequence.</p>
                        <div style="display:flex;gap:10px;flex-wrap:wrap;">
                            <button class="btn btn-secondary" onclick="previewEmail()">Preview</button>
                            <button class="btn btn-secondary" onclick="sendTestEmail()">Test Email</button>
                            <button class="btn btn-success" onclick="sendEmails()">SEND EMAILS</button>
                        </div>
                        <div id="email-log" class="mt-4 text-sm text-muted"></div>
                    </div>

                    <div class="card">
                        <div class="card-header">Templates</div>
                        <div id="email-templates"></div>
                        <button class="btn btn-secondary btn-sm mt-4" onclick="openCreateTemplate('email')">+ New Template</button>

                        <div class="card-header mt-4" style="display:flex;justify-content:space-between;align-items:center;">
                            Template Performance
                            <button class="btn btn-secondary btn-sm" onclick="loadTemplatePerformance()">Refresh</button>
                        </div>
                        <div id="template-performance" style="font-size:12px;">
                            <p class="text-muted text-sm">Loading performance data...</p>
                        </div>
                    </div>
                </div>

                <div class="card">
                    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                        AI Email Generator
                        <button class="btn btn-secondary btn-sm" onclick="loadAIEmailStatus()">Refresh</button>
                    </div>
                    <div id="ai-email-status" class="mb-4" style="background:var(--bg3);border:1px solid var(--border);padding:14px;font-family:monospace;font-size:11px;overflow:hidden;">
                        <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(180px, 1fr));gap:8px;">
                            <div><span class="text-muted">Schools in sheet:</span> <span id="ai-total-schools">-</span></div>
                            <div><span class="text-muted">With AI emails:</span> <span id="ai-with-emails" style="color:var(--success);">-</span></div>
                            <div><span class="text-muted">Needing AI:</span> <span id="ai-needing-emails" style="color:var(--warn);">-</span></div>
                            <div><span class="text-muted">API remaining:</span> <span id="ai-api-remaining">-</span></div>
                        </div>
                    </div>
                    <p class="text-sm text-muted mb-4">Generate personalized AI emails using Ollama + Google Search.</p>
                    <div class="form-group">
                        <label>Schools to Generate (max per run)</label>
                        <input type="number" id="ai-email-limit" value="5" min="1" max="20">
                    </div>
                    <div style="display:flex;gap:10px;margin-bottom:16px;">
                        <button class="btn btn-secondary" onclick="loadAIEmailSchools()">View Schools</button>
                        <button class="btn btn-success" onclick="generateAIEmails()">GENERATE AI</button>
                    </div>
                    <div id="ai-email-schools" style="max-height:300px;overflow-y:auto;font-size:12px;"></div>
                </div>

                <div class="card">
                    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                        Cloud Email Storage
                        <button class="btn btn-secondary btn-sm" onclick="loadCloudEmailStats()">Refresh</button>
                    </div>
                    <p class="text-sm text-muted mb-4">Sync AI emails to Google Sheets for Railway deployment.</p>
                    <div id="cloud-email-stats" class="mb-4" style="background:var(--bg3);border:1px solid var(--border);padding:14px;font-family:monospace;font-size:11px;overflow:hidden;">
                        <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(180px, 1fr));gap:8px;">
                            <div><span class="text-muted">Total in cloud:</span> <span id="cloud-total">-</span></div>
                            <div><span class="text-muted">Pending:</span> <span id="cloud-pending" style="color:var(--accent);">-</span></div>
                            <div><span class="text-muted">Sent:</span> <span id="cloud-sent" style="color:var(--success);">-</span></div>
                            <div><span class="text-muted">Open rate:</span> <span id="cloud-open-rate">-</span></div>
                            <div><span class="text-muted">Response rate:</span> <span id="cloud-response-rate">-</span></div>
                            <div><span class="text-muted">Successful:</span> <span id="cloud-successful" style="color:var(--success);">-</span></div>
                        </div>
                    </div>
                    <div style="display:flex;gap:10px;flex-wrap:wrap;">
                        <button class="btn btn-success" onclick="syncEmailsToCloud()">UPLOAD TO CLOUD</button>
                        <button class="btn btn-secondary" onclick="viewCloudEmails()">View Emails</button>
                        <button class="btn btn-secondary" onclick="viewSuccessfulEmails()">View Successful</button>
                    </div>
                    <div id="cloud-email-log" class="mt-4 text-sm text-muted"></div>
                </div>

                <!-- Pause Controls Footer -->
                <div id="email-pause-footer" style="margin-top:20px;padding:14px 18px;background:var(--bg3);border:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
                    <span id="email-mode-status" style="font-family:monospace;font-size:12px;color:var(--muted);">Auto-send active</span>
                    <div style="display:flex;gap:10px;align-items:center;">
                        <input type="date" id="pause-until-date" style="padding:8px 12px;font-size:12px;">
                        <button class="btn btn-secondary btn-sm" onclick="setPauseDate()">Pause</button>
                        <button class="btn btn-success btn-sm" id="resume-btn" onclick="resumeEmails()" style="display:none;">Resume</button>
                    </div>
                </div>
            </div>

            <!-- DMS PAGE -->
            <div id="page-dms" class="page">
                <!-- DM Stats -->
                <div class="stats" style="grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));margin-bottom:20px;">
                    <div class="stat">
                        <div class="stat-value highlight" id="dm-queue">0</div>
                        <div class="stat-label">In Queue</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value cyan" id="dm-sent">0</div>
                        <div class="stat-label">DMs Sent</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value success" id="dm-replied">0</div>
                        <div class="stat-label">Replied</div>
                    </div>
                    <div class="stat">
                        <div class="stat-value" style="color:var(--muted);" id="dm-no-handle">0</div>
                        <div class="stat-label">No Twitter</div>
                    </div>
                </div>

                <!-- Current Coach Card -->
                <div class="card mb-4" id="current-dm-card" style="border-left:3px solid var(--accent);">
                    <div class="card-header">Active Session</div>
                    <div id="current-coach-info" style="padding:20px 0;">
                        <p class="text-muted">Click "Start DM Session" to begin outreach</p>
                    </div>
                    <div id="dm-message-preview" style="background:var(--bg3);border:1px solid var(--border);padding:16px;margin-bottom:16px;display:none;">
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                            <div style="font-family:monospace;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;">Message (copied)</div>
                            <button class="btn btn-secondary btn-sm" onclick="reCopyMessage()">Re-copy</button>
                        </div>
                        <div id="dm-message-text" style="white-space:pre-wrap;font-size:13px;line-height:1.5;"></div>
                    </div>
                    <div id="keyboard-shortcuts" style="background:var(--bg);border:1px solid var(--border);padding:12px 16px;margin-bottom:16px;display:none;">
                        <div style="font-size:12px;"><strong style="color:var(--accent);">SHORTCUTS:</strong> <kbd>M</kbd> Messaged <kbd>F</kbd> Followed <kbd>S</kbd> Skip <kbd>W</kbd> Wrong <kbd>C</kbd> Re-copy</div>
                    </div>
                    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
                        <button class="btn btn-primary" id="btn-start-dm" onclick="startDMSession()">START SESSION</button>
                        <button class="btn btn-secondary" id="btn-end-dm" onclick="endDMSession()" style="display:none;">END SESSION</button>
                        <button class="btn btn-success" id="btn-followed-messaged" onclick="markDM('messaged')" style="display:none;">MESSAGED (M)</button>
                        <button class="btn btn-secondary" id="btn-followed-only" onclick="markDM('followed')" style="display:none;">FOLLOWED (F)</button>
                        <button class="btn btn-secondary" id="btn-skip" onclick="markDM('skipped')" style="display:none;">SKIP (S)</button>
                        <button class="btn" id="btn-wrong-twitter" onclick="markWrongTwitter()" style="display:none;background:var(--err);color:var(--bg);">WRONG (W)</button>
                        <label id="auto-advance-label" style="display:none;margin-left:12px;font-size:12px;cursor:pointer;color:var(--muted);">
                            <input type="checkbox" id="auto-advance-toggle" style="margin-right:6px;"> Auto-advance (8s)
                        </label>
                    </div>
                    <div id="dm-progress" class="mt-4" style="display:none;font-family:monospace;font-size:12px;color:var(--muted);"></div>
                </div>

                <div class="grid-2">
                    <div class="card">
                        <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                            DM Queue
                            <button class="btn btn-secondary btn-sm" onclick="refreshDMQueue()">Refresh</button>
                        </div>
                        <div id="dm-queue-list" style="max-height:320px;overflow-y:auto;">
                            <div class="loading-state">
                                <div class="spinner"></div>
                                <span>Loading coaches...</span>
                            </div>
                        </div>
                    </div>

                    <div class="card">
                        <div class="card-header">DM Templates</div>
                        <div id="dm-templates"></div>
                        <button class="btn btn-secondary btn-sm mt-4" onclick="openCreateTemplate('dm')">+ New Template</button>

                        <div class="card-header mt-4">Keyboard Reference</div>
                        <div style="font-size:12px;line-height:2;">
                            <div><kbd>M</kbd> Followed & Messaged</div>
                            <div><kbd>F</kbd> Followed Only</div>
                            <div><kbd>S</kbd> or <kbd>→</kbd> Skip to Next</div>
                            <div><kbd>C</kbd> Re-copy Message</div>
                            <div><kbd>W</kbd> Mark Wrong Handle</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- ADMIN PAGE (admin only) -->
            {% if is_admin %}
            <div id="page-admin" class="page">
                <div class="card">
                    <div class="card-header" style="display:flex;justify-content:space-between;align-items:center;">
                        Athletes
                        <button class="btn btn-primary btn-sm" onclick="showCreateAthlete()">+ NEW ATHLETE</button>
                    </div>
                    <div id="athletes-list" style="padding:12px;">Loading...</div>
                </div>
                <div class="card">
                    <div class="card-header">Missing Coach Data Alerts</div>
                    <div id="missing-coaches" style="padding:12px;">Loading...</div>
                </div>
            </div>
            {% endif %}

            {% if is_admin %}
            <!-- Create Athlete Modal -->
            <div class="modal-overlay" id="create-athlete-modal">
                <div class="modal">
                    <div class="modal-header">
                        <span class="modal-title">Create Athlete Account</span>
                        <button class="modal-close" onclick="closeModal('create-athlete-modal')">&times;</button>
                    </div>
                    <form onsubmit="createAthlete(event)" style="padding:20px;">
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
                            <div><label style="font-size:12px;color:var(--muted);">Name *</label><input id="ca-name" required style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Email *</label><input id="ca-email" type="email" required style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Password *</label><input id="ca-pw" type="password" required minlength="8" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Phone</label><input id="ca-phone" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Grad Year</label><input id="ca-year" placeholder="2026" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Position</label><input id="ca-pos" placeholder="OL" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Height</label><input id="ca-ht" placeholder="6'3" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Weight</label><input id="ca-wt" placeholder="295" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">GPA</label><input id="ca-gpa" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">High School</label><input id="ca-school" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">State</label><input id="ca-state" placeholder="FL" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Hudl/Highlight Link</label><input id="ca-hudl" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                        </div>
                        <div style="border-top:1px solid var(--border);margin-top:16px;padding-top:16px;">
                            <div style="font-weight:600;color:var(--accent);margin-bottom:12px;">Gmail Credentials (optional - add later)</div>
                            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
                                <div><label style="font-size:12px;color:var(--muted);">Gmail Email</label><input id="ca-gmail" type="email" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                                <div><label style="font-size:12px;color:var(--muted);">Client ID</label><input id="ca-cid" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                                <div><label style="font-size:12px;color:var(--muted);">Client Secret</label><input id="ca-csec" type="password" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                                <div><label style="font-size:12px;color:var(--muted);">Refresh Token</label><input id="ca-rtok" type="password" style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            </div>
                        </div>
                        <button type="submit" class="btn btn-primary" style="margin-top:16px;width:100%;">CREATE ACCOUNT</button>
                    </form>
                </div>
            </div>

            <!-- Edit Credentials Modal -->
            <div class="modal-overlay" id="edit-creds-modal">
                <div class="modal">
                    <div class="modal-header">
                        <span class="modal-title">Edit Gmail Credentials</span>
                        <button class="modal-close" onclick="closeModal('edit-creds-modal')">&times;</button>
                    </div>
                    <form onsubmit="saveCreds(event)" style="padding:20px;">
                        <input type="hidden" id="ec-aid">
                        <div style="display:grid;gap:12px;">
                            <div><label style="font-size:12px;color:var(--muted);">Gmail Email</label><input id="ec-gmail" type="email" required style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Client ID</label><input id="ec-cid" required style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Client Secret</label><input id="ec-csec" type="password" required style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                            <div><label style="font-size:12px;color:var(--muted);">Refresh Token</label><input id="ec-rtok" type="password" required style="width:100%;padding:8px;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;"></div>
                        </div>
                        <button type="submit" class="btn btn-primary" style="margin-top:16px;width:100%;">SAVE CREDENTIALS</button>
                    </form>
                </div>
            </div>
            {% endif %}

        </main>
    </div>

    <!-- Settings Modal -->
    <div class="modal-overlay" id="settings-modal">
        <div class="modal">
            <div class="modal-header">
                <span class="modal-title">Settings</span>
                <button class="modal-close" onclick="closeSettings()">&times;</button>
            </div>

            <div style="font-size:12px;font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:16px;">Athlete Profile</div>
            <div class="grid-2 mb-4">
                <div class="form-group"><label>Name</label><input type="text" id="s-name"></div>
                <div class="form-group"><label>Grad Year</label><input type="text" id="s-year" value="2026"></div>
                <div class="form-group"><label>Position</label><input type="text" id="s-position" placeholder="OL"></div>
                <div class="form-group"><label>High School</label><input type="text" id="s-school"></div>
                <div class="form-group"><label>Height</label><input type="text" id="s-height" placeholder="6'3&quot;"></div>
                <div class="form-group"><label>Weight</label><input type="text" id="s-weight" placeholder="295"></div>
                <div class="form-group"><label>GPA</label><input type="text" id="s-gpa"></div>
                <div class="form-group"><label>Hudl / Film Link</label><input type="text" id="s-hudl"></div>
                <div class="form-group"><label>Phone</label><input type="text" id="s-phone"></div>
                <div class="form-group"><label>Email</label><input type="email" id="s-email"></div>
            </div>

            <!-- Railway Banner -->
            <div id="railway-banner" style="display:none;background:var(--bg3);border:1px solid var(--accent);border-radius:8px;padding:16px;margin-bottom:20px;">
                <div style="display:flex;align-items:center;gap:14px;">
                    <div style="width:36px;height:36px;background:var(--accent);border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;color:white;">RW</div>
                    <div>
                        <div style="font-weight:600;color:var(--accent);">Running on Railway</div>
                        <div style="font-size:12px;color:var(--muted);">Credentials managed via environment variables</div>
                    </div>
                </div>
            </div>

            <!-- Credentials Section -->
            <div id="credentials-section">
                <div style="font-size:12px;font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:0.5px;margin:24px 0 16px;">Email Settings</div>
                <div class="grid-2 mb-4">
                    <div class="form-group"><label>Gmail Address</label><input type="email" id="s-gmail"></div>
                    <div class="form-group"><label>App Password</label><input type="password" id="s-gmail-pass"></div>
                </div>

                <div style="font-size:12px;font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:0.5px;margin:24px 0 16px;">Google Sheets</div>
                <div id="sheets-status" class="mb-4" style="padding:12px 16px;background:var(--bg3);border:1px solid var(--border);font-family:monospace;font-size:12px;">
                    <span id="sheets-connection-text">Checking...</span>
                </div>
                <div class="form-group">
                    <label>Spreadsheet Name or ID</label>
                    <input type="text" id="s-sheet" value="bardeen" placeholder="bardeen or spreadsheet ID">
                    <p class="text-sm text-muted mt-2">Enter the spreadsheet name or ID from URL</p>
                </div>
                <div class="form-group">
                    <label>Credentials JSON</label>
                    <p class="text-sm text-muted mb-4" style="line-height:1.6;">
                        1. Go to <a href="https://console.cloud.google.com" target="_blank" style="color:var(--accent);">Google Cloud Console</a><br>
                        2. Create project → Enable Sheets API<br>
                        3. Create Service Account → Download JSON<br>
                        4. Share spreadsheet with service account email
                    </p>
                    <input type="file" id="credentials-file" accept=".json" style="padding:10px;">
                    <button class="btn btn-secondary btn-sm mt-2" onclick="uploadCredentials()">Upload Credentials</button>
                </div>
                <button class="btn btn-secondary mb-4" onclick="testSheetConnection()">Test Connection</button>
            </div>

            <div style="font-size:12px;font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:0.5px;margin:24px 0 16px;">Response Tracking</div>
            <div class="grid-2 mb-4">
                <div class="form-group"><label>Gmail for Replies</label><input type="email" id="s-inbox-email" placeholder="Same or different Gmail"></div>
                <div class="form-group"><label>App Password</label><input type="password" id="s-inbox-pass" placeholder="For IMAP access"></div>
            </div>
            <button class="btn btn-secondary mb-4" onclick="testInboxConnection()">Test Inbox</button>

            <div style="border-top:1px solid var(--border);padding-top:20px;margin-top:20px;">
                <button class="btn btn-primary" onclick="saveSettings()">SAVE SETTINGS</button>
            </div>
        </div>
    </div>

    <!-- Create Template Modal -->
    <div class="modal-overlay" id="template-modal">
        <div class="modal">
            <div class="modal-header">
                <span class="modal-title">Email Template</span>
                <button class="modal-close" onclick="closeTemplateModal()">&times;</button>
            </div>
            <div class="form-group">
                <label>Template Type</label>
                <select id="new-tpl-type">
                    <option value="rc">Recruiting Coordinator</option>
                    <option value="ol">O-Line Coach</option>
                    <option value="followup">Follow-up</option>
                    <option value="dm">Twitter DM</option>
                </select>
            </div>
            <div class="form-group"><label>Template Name</label><input type="text" id="new-tpl-name" placeholder="My Template"></div>
            <div class="form-group" id="tpl-subject-group"><label>Subject Line</label><input type="text" id="new-tpl-subject" placeholder="{grad_year} {position} - {athlete_name}"></div>
            <div class="form-group"><label>Body</label><textarea id="new-tpl-body" rows="10" placeholder="Coach {coach_name},..."></textarea></div>
            <div style="background:var(--bg3);border:1px solid var(--border);padding:12px;margin-bottom:20px;font-family:monospace;font-size:11px;color:var(--muted);">
                <strong style="color:var(--accent);">Variables:</strong> {coach_name}, {school}, {athlete_name}, {position}, {grad_year}, {height}, {weight}, {gpa}, {hudl_link}, {phone}, {email}
            </div>
            <button class="btn btn-primary" id="tpl-save-btn" onclick="createTemplate()">SAVE TEMPLATE</button>
        </div>
    </div>

    <div id="toast" class="toast" style="display:none"></div>

    <script>
        // State
        let settings = {};
        let templates = [];
        let dmQueue = [];
        let hudlUrl = '';
        
        // Tab switching
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById('page-' + tab.dataset.page).classList.add('active');
                loadPageData(tab.dataset.page);
            });
        });
        
        // Load page data
        function loadPageData(page) {
            if (page === 'home') loadDashboard();
            if (page === 'find') initSchoolSearch();
            if (page === 'email') { loadEmailPage(); loadTemplates('email'); loadEmailQueueStatus(); loadTemplatePerformance(); loadAIEmailStatus(); }
            if (page === 'dms') { loadDMQueue(); loadTemplates('dm'); }
            if (page === 'track') loadTrackStats();
            if (page === 'admin') loadAdminPanel();
        }
        
        // Dashboard
        async function loadDashboard() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();
                document.getElementById('stat-sent').textContent = data.emails_sent || 0;
                document.getElementById('stat-responses').textContent = data.responses || 0;
                document.getElementById('stat-rate').textContent = (data.response_rate || 0) + '%';
                document.getElementById('stat-followups').textContent = data.followups_due || 0;

                // Load responses
                loadRecentResponses();
                loadHotLeads();
                loadHudlViews();
                loadTrackingStats();
                loadTomorrowPreview();
                loadRepliedCount();
                loadEmailModeStatus();  // Load email pause/holiday status for home banner
            } catch(e) { console.error(e); }
        }

        async function loadTrackingStats() {
            try {
                const res = await fetch('/api/tracking/stats');
                const data = await res.json();

                // Show backfill notice if tracking is empty but sheet has contacts
                const backfillNotice = document.getElementById('backfill-notice');
                if (backfillNotice && data.total_sent === 0) {
                    backfillNotice.style.display = 'block';
                } else if (backfillNotice) {
                    backfillNotice.style.display = 'none';
                }

                // Update open rate stat
                document.getElementById('stat-opens').textContent = (data.open_rate || 0) + '%';

                // Update email performance stats
                document.getElementById('perf-sent').textContent = data.total_sent || 0;
                document.getElementById('perf-opened').textContent = data.total_opened || 0;

                // Update recent opens
                const el = document.getElementById('recent-opens');
                if (data.recent_opens && data.recent_opens.length) {
                    el.innerHTML = data.recent_opens.slice(0, 8).map(o => `
                        <div style="padding:8px 0;border-bottom:1px solid var(--border);">
                            <div style="display:flex;justify-content:space-between;align-items:center;">
                                <div>
                                    <strong style="color:var(--text);">${o.school || 'Unknown'}</strong>
                                    <span class="text-muted"> - ${o.coach || ''}</span>
                                </div>
                                <span class="text-muted text-sm">${new Date(o.opened_at).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</span>
                            </div>
                        </div>
                    `).join('');
                } else {
                    el.innerHTML = `
                        <div class="empty-state" style="padding:24px;">
                            <div class="empty-state-icon" style="font-size:32px;color:var(--accent);">—</div>
                            <div class="empty-state-title">No opens yet</div>
                            <div class="empty-state-text">When coaches open your emails, you'll see it here in real-time.</div>
                        </div>
                    `;
                }

                // Get smart times for best time display
                const timesRes = await fetch('/api/tracking/smart-times');
                const timesData = await timesRes.json();
                const bestTimeEl = document.getElementById('perf-best-time');
                if (timesData.best_hours && timesData.best_hours.length) {
                    const times = timesData.best_hours.slice(0, 2).map(h => {
                        const hour = h.hour;
                        const ampm = hour >= 12 ? 'PM' : 'AM';
                        const displayHour = hour % 12 || 12;
                        return displayHour + ' ' + ampm;
                    }).join(' & ');
                    bestTimeEl.innerHTML = `Best send times: <strong style="color:var(--accent);">${times}</strong>`;
                } else {
                    bestTimeEl.innerHTML = 'Send more emails to discover best times';
                }
            } catch(e) { console.error(e); }
        }

        async function backfillTracking() {
            if (!confirm('This will import all contacted coaches from your sheet into tracking. Continue?')) {
                return;
            }
            showToast('Importing sent emails...');
            try {
                const res = await fetch('/api/tracking/backfill', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast(`Imported ${data.backfilled} emails! Total tracked: ${data.total_tracked}`, 'success');
                    document.getElementById('backfill-notice').style.display = 'none';
                    loadTrackingStats();
                    loadDashboard();
                } else {
                    showToast(data.error || 'Import failed', 'error');
                }
            } catch(e) {
                showToast('Import failed: ' + e.message, 'error');
            }
        }

        async function loadTomorrowPreview() {
            try {
                // First check if emails are paused
                const pauseRes = await fetch('/api/email/pause');
                const pauseData = await pauseRes.json();

                if (pauseData.is_paused) {
                    // Show paused state
                    document.getElementById('tomorrow-count').textContent = 'PAUSED';
                    document.getElementById('tomorrow-count').style.fontSize = '28px';
                    document.getElementById('optimal-time').textContent = 'PAUSED';
                    document.getElementById('tomorrow-breakdown').textContent = `Until ${pauseData.paused_until}`;
                    return;
                }

                // Reset font size if not paused
                document.getElementById('tomorrow-count').style.fontSize = '32px';

                const res = await fetch('/api/auto-send/tomorrow-preview');
                const data = await res.json();

                if (data.success) {
                    document.getElementById('tomorrow-count').textContent = data.total || 0;
                    document.getElementById('optimal-time').textContent = data.optimal_time || '9:00 AM';

                    // Build breakdown text
                    const parts = [];
                    if (data.intro > 0) parts.push(data.intro + ' intro');
                    if (data.followup1 > 0) parts.push(data.followup1 + ' follow-up 1');
                    if (data.followup2 > 0) parts.push(data.followup2 + ' follow-up 2');
                    if (data.restart > 0) parts.push(data.restart + ' restart');

                    document.getElementById('tomorrow-breakdown').textContent =
                        parts.length ? parts.join(' • ') : 'No emails scheduled';

                    // Show total available if capped
                    if (data.total_available > data.total) {
                        document.getElementById('tomorrow-breakdown').textContent +=
                            ` (${data.total_available} available, capped at ${data.total})`;
                    }
                }
            } catch(e) {
                console.error(e);
                document.getElementById('tomorrow-breakdown').textContent = 'Error loading preview';
            }
        }

        async function loadRepliedCount() {
            try {
                const res = await fetch('/api/responses/recent');
                const data = await res.json();
                document.getElementById('perf-replied').textContent =
                    (data.responses && data.responses.length) || 0;
            } catch(e) { console.error(e); }
        }

        async function loadHudlViews() {
            try {
                const res = await fetch('/api/hudl/views');
                const data = await res.json();
                const el = document.getElementById('stat-hudl-views');
                if (!el) return;
                if (data.success) {
                    el.textContent = data.views;
                    hudlUrl = data.url;
                } else {
                    el.textContent = '-';
                    el.title = data.error || 'No Hudl link';
                }
            } catch(e) {
                const el = document.getElementById('stat-hudl-views');
                if (el) el.textContent = '-';
            }
        }

        async function loadRecentResponses() {
            try {
                const res = await fetch('/api/responses/recent');
                const data = await res.json();
                const el = document.getElementById('recent-responses');
                if (data.responses && data.responses.length) {
                    // Analyze sentiment for each response
                    const responsesWithSentiment = await Promise.all(data.responses.slice(0, 5).map(async r => {
                        try {
                            const sentimentRes = await fetch('/api/responses/analyze-sentiment', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({text: r.snippet || r.subject || ''})
                            });
                            const sentiment = await sentimentRes.json();
                            return {...r, sentiment};
                        } catch {
                            return {...r, sentiment: {label: '❓ Review', color: '#888'}};
                        }
                    }));

                    el.innerHTML = responsesWithSentiment.map(r => {
                        // Parse date to show relative time
                        let timeAgo = '';
                        if (r.date) {
                            const d = new Date(r.date);
                            const now = new Date();
                            const diffDays = Math.floor((now - d) / (1000 * 60 * 60 * 24));
                            if (diffDays === 0) timeAgo = 'Today';
                            else if (diffDays === 1) timeAgo = 'Yesterday';
                            else if (diffDays < 7) timeAgo = diffDays + ' days ago';
                            else timeAgo = d.toLocaleDateString();
                        }
                        const sentimentBadge = r.sentiment ?
                            `<span class="sentiment-badge" style="background:${r.sentiment.color};color:white;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;">${r.sentiment.label}</span>` : '';
                        return `
                            <div class="response-item">
                                <div class="response-avatar" style="background:${r.sentiment?.color || 'var(--success)'};">${(r.school || '?')[0]}</div>
                                <div class="response-content">
                                    <div style="display:flex;justify-content:space-between;align-items:center;">
                                        <div class="response-school">${r.school || r.email}</div>
                                        <div style="display:flex;gap:8px;align-items:center;">
                                            ${sentimentBadge}
                                            <span class="text-sm text-muted">${timeAgo}</span>
                                        </div>
                                    </div>
                                    <div class="response-snippet" style="margin-top:4px;">${r.snippet || r.subject || ''}</div>
                                </div>
                            </div>
                        `;
                    }).join('');
                } else {
                    el.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon" style="font-size:32px;color:var(--accent);">—</div>
                            <div class="empty-state-title">No responses yet</div>
                            <div class="empty-state-text">Click "Check Inbox" to scan for coach replies.</div>
                        </div>
                    `;
                }
            } catch(e) { console.error(e); }
        }
        
        async function loadHotLeads() {
            try {
                const res = await fetch('/api/responses/hot-leads');
                const data = await res.json();
                const el = document.getElementById('hot-leads');
                if (!el) return;  // Element doesn't exist
                if (data.leads && data.leads.length) {
                    el.innerHTML = data.leads.slice(0, 5).map(l => `
                        <div class="lead-item">
                            <div class="lead-info">
                                <div class="lead-school">${l.school}</div>
                                <div class="lead-coach">${l.coach_name}</div>
                            </div>
                            <span class="lead-badge">${l.times_contacted}x</span>
                        </div>
                    `).join('');
                } else {
                    el.innerHTML = '<p class="text-muted text-sm">Send some emails first</p>';
                }
            } catch(e) { console.error(e); }
        }
        
        async function loadDivisionStats() {
            try {
                const res = await fetch('/api/responses/by-division');
                const data = await res.json();
                const container = document.getElementById('division-stats');
                if (!container) return;  // Element doesn't exist
                if (data.divisions) {
                    container.innerHTML = ['FBS','FCS','D2','D3','NAIA','JUCO'].map(div => {
                        const stat = data.divisions[div] || {rate: 0};
                        return `<div class="div-stat"><div class="div-name">${div}</div><div class="div-rate">${stat.rate || 0}%</div></div>`;
                    }).join('');
                }
            } catch(e) { console.error(e); }
        }
        
        async function loadEmailQueueStatus() {
            try {
                const res = await fetch('/api/email/queue-status');
                const data = await res.json();
                if (data.success) {
                    document.getElementById('email-ready').textContent = data.ready || 0;
                    document.getElementById('email-followups').textContent = data.followups_due || 0;
                    document.getElementById('email-responded').textContent = data.responded || 0;
                    document.getElementById('email-queue-summary').textContent = data.summary || '';
                }
            } catch(e) { console.error(e); }
        }
        
        async function scanPastResponses() {
            showToast('Scanning past 2 weeks for responses...');
            try {
                const res = await fetch('/api/email/scan-past-responses', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message, 'success');
                    loadDashboard();
                    loadEmailQueueStatus();
                } else {
                    showToast(data.error || 'Scan failed', 'error');
                }
            } catch(e) { showToast('Error scanning responses', 'error'); }
        }
        
        async function cleanupSheet() {
            showToast('Cleaning up sheet...');
            try {
                const res = await fetch('/api/sheet/cleanup', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message, 'success');
                } else {
                    showToast(data.error || 'Cleanup failed', 'error');
                }
            } catch(e) { showToast('Error cleaning sheet', 'error'); }
        }
        
        async function checkInbox() {
            showToast('Checking inbox...');
            try {
                const res = await fetch('/api/email/check-responses', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast(`Found ${data.new_count || 0} new responses`, 'success');
                    loadDashboard();
                } else {
                    showToast(data.error || 'Failed to check inbox', 'error');
                }
            } catch(e) { showToast('Error checking inbox', 'error'); }
        }
        
        async function testResponseTracking() {
            showToast('Testing response tracking...');
            try {
                const res = await fetch('/api/email/test-tracking');
                const data = await res.json();
                
                let msg = 'Response Tracking Test:\\n\\n';
                msg += `Gmail API Connected: ${data.gmail_connected ? 'Yes' : 'No'}\\n`;
                msg += `Can Check Inbox: ${data.imap_working ? 'Yes' : 'No'}\\n`;
                msg += `Emails Sent (from sheet): ${data.emails_sent || 0}\\n`;
                msg += `Responses Found: ${data.responses_found || 0}\\n`;
                
                if (data.error) msg += `\\nError: ${data.error}`;
                if (data.gmail_api_error) msg += `\\nGmail API Error: ${data.gmail_api_error}`;
                
                alert(msg);
            } catch(e) { 
                alert('Test failed: ' + e.message);
            }
        }
        
        async function toggleAutoSend(enabled) {
            const count = parseInt(document.getElementById('auto-send-count').value) || 100;
            
            try {
                const res = await fetch('/api/auto-send/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled, count })
                });
                const data = await res.json();
                
                if (data.success) {
                    showToast(enabled ? 'Auto-send enabled - will send daily when app is running' : 'Auto-send disabled', 'success');
                } else {
                    showToast('Failed to toggle auto-send', 'error');
                }
            } catch(e) {
                showToast('Error: ' + e.message, 'error');
            }
        }
        
        async function runAutoSendNow() {
            showToast('Starting auto-send...', 'info');
            try {
                const res = await fetch('/api/auto-send/run-now', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    showToast('Auto-send started in background', 'success');
                    // Check status after a few seconds
                    setTimeout(checkAutoSendStatus, 5000);
                } else {
                    showToast(data.error || 'Failed to start', 'error');
                }
            } catch(e) {
                showToast('Error: ' + e.message, 'error');
            }
        }
        
        async function checkAutoSendStatus() {
            try {
                const res = await fetch('/api/auto-send/status');
                const data = await res.json();
                if (data.last_result && data.last_result.sent > 0) {
                    showToast(`Auto-send complete: ${data.last_result.sent} emails sent`, 'success');
                    loadDashboard();
                }
            } catch(e) {}
        }
        
        async function toggleNotifications(enabled) {
            const channel = document.getElementById('notify-channel').value || 'keelan-coach-outreach';
            try {
                await fetch('/api/notifications/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled, channel })
                });
                showToast(enabled ? 'Phone notifications enabled' : 'Notifications disabled', 'success');
            } catch(e) {
                showToast('Error: ' + e.message, 'error');
            }
        }
        
        async function saveNotifyChannel(channel) {
            try {
                await fetch('/api/notifications/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled: document.getElementById('notify-toggle').checked, channel })
                });
            } catch(e) {}
        }
        
        async function testNotification() {
            const channel = document.getElementById('notify-channel').value;
            if (!channel) {
                showToast('Enter a channel name first', 'error');
                return;
            }
            showToast('Sending test notification...', 'info');
            try {
                const res = await fetch('/api/notifications/test', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ channel })
                });
                const data = await res.json();
                if (data.success) {
                    showToast('Test notification sent! Check your phone.', 'success');
                } else {
                    showToast(data.error || 'Failed to send', 'error');
                }
            } catch(e) {
                showToast('Error: ' + e.message, 'error');
            }
        }
        
        // School search
        async function initSchoolSearch() {
            // Populate states
            const states = ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY'];
            const sel = document.getElementById('state-filter');
            sel.innerHTML = '<option value="">All States</option>' + states.map(s => `<option value="${s}">${s}</option>`).join('');
            loadMySchools();
        }
        
        async function searchSchools() {
            const query = document.getElementById('school-search').value;
            const division = document.getElementById('division-filter').value;
            const state = document.getElementById('state-filter').value;
            
            try {
                const res = await fetch('/api/schools/search', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ query, division, state })
                });
                const data = await res.json();
                const tbody = document.getElementById('schools-body');
                
                if (data.schools && data.schools.length) {
                    tbody.innerHTML = data.schools.slice(0, 50).map(s => `
                        <tr>
                            <td>${s.name}</td>
                            <td>${s.division || '-'}</td>
                            <td>${s.state || '-'}</td>
                            <td>${s.conference || '-'}</td>
                            <td>
                                <button class="btn btn-sm btn-primary" onclick="addSchoolToMyList('${s.id}','${s.name.replace(/'/g, "\\'")}')">+ My List</button>
                                <button class="btn btn-sm" onclick="findCoaches('${s.name.replace(/'/g, "\\'")}')">Find Coaches</button>
                            </td>
                        </tr>
                    `).join('');
                } else {
                    tbody.innerHTML = `<tr><td colspan="5">
                        <div class="empty-state">
                            <div class="empty-state-icon" style="font-size:32px;color:var(--accent);">—</div>
                            <div class="empty-state-title">No schools found</div>
                            <div class="empty-state-text">Try a different search term or adjust your filters.</div>
                        </div>
                    </td></tr>`;
                }
            } catch(e) { console.error(e); }
        }

        async function addToSheet(schoolName) {
            try {
                await fetch('/api/schools/add-to-sheet', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ schools: [schoolName] })
                });
                showToast('Added to sheet', 'success');
            } catch(e) { showToast('Failed to add', 'error'); }
        }
        
        // Email
        async function loadEmailPage() {
            try {
                const res = await fetch('/api/spreadsheet');
                const data = await res.json();
                document.getElementById('email-ready').textContent = data.ready_to_send || 0;
                document.getElementById('email-today').textContent = data.sent_today || 0;
                document.getElementById('email-followups').textContent = data.followups_due || 0;
            } catch(e) {}
            // Also load followup queue
            loadFollowupQueue();
            // Load email mode status
            loadEmailModeStatus();
        }

        // ========== HOLIDAY MODE & PAUSE CONTROLS ==========

        async function loadEmailModeStatus() {
            console.log('loadEmailModeStatus called');
            try {
                // Load holiday mode
                const holidayRes = await fetch('/api/email/holiday-mode');
                const holidayData = await holidayRes.json();
                console.log('Holiday data:', holidayData);
                const toggle = document.getElementById('holiday-mode-toggle');
                if (toggle) toggle.checked = holidayData.holiday_mode || false;

                // Load pause status
                const pauseRes = await fetch('/api/email/pause');
                const pauseData = await pauseRes.json();
                console.log('Pause data:', pauseData);

                const statusEl = document.getElementById('email-mode-status');
                const resumeBtn = document.getElementById('resume-btn');
                const footer = document.getElementById('email-pause-footer');

                // Also update home page banner
                const homeStatus = document.getElementById('home-email-status');
                const homeStatusText = document.getElementById('home-email-status-text');
                const homeResumeBtn = document.getElementById('home-resume-btn');
                console.log('Elements found:', {homeStatus: !!homeStatus, homeStatusText: !!homeStatusText});

                const homeIcon = document.getElementById('home-email-status-icon');

                if (pauseData.is_paused) {
                    if (footer) footer.style.background = 'linear-gradient(135deg, #e74c3c 0%, #c0392b 100%)';
                    if (statusEl) statusEl.innerHTML = `PAUSED until ${pauseData.paused_until}`;
                    if (resumeBtn) resumeBtn.style.display = '';
                    // Home page - smaller indicator
                    if (homeStatus) homeStatus.style.background = 'linear-gradient(135deg, #e74c3c, #c0392b)';
                    if (homeIcon) homeIcon.textContent = 'II';
                    if (homeStatusText) homeStatusText.textContent = `Paused until ${pauseData.paused_until}`;
                    if (homeResumeBtn) homeResumeBtn.style.display = '';
                } else if (holidayData.holiday_mode) {
                    if (footer) footer.style.background = 'linear-gradient(135deg, #27ae60 0%, #1e8449 100%)';
                    if (statusEl) statusEl.innerHTML = 'HOLIDAY MODE ACTIVE';
                    if (resumeBtn) resumeBtn.style.display = 'none';
                    // Home page
                    if (homeStatus) homeStatus.style.background = 'linear-gradient(135deg, #f39c12, #e67e22)';
                    if (homeIcon) homeIcon.textContent = 'H';
                    if (homeStatusText) homeStatusText.textContent = 'Holiday Mode';
                    if (homeResumeBtn) homeResumeBtn.style.display = 'none';
                } else {
                    if (footer) footer.style.background = 'var(--bg3)';
                    if (statusEl) statusEl.innerHTML = 'AUTO-SEND ACTIVE';
                    if (resumeBtn) resumeBtn.style.display = 'none';
                    // Home page
                    if (homeStatus) homeStatus.style.background = 'linear-gradient(135deg, #27ae60, #1e8449)';
                    if (homeIcon) homeIcon.textContent = 'ON';
                    if (homeStatusText) homeStatusText.textContent = 'Emails active';
                    if (homeResumeBtn) homeResumeBtn.style.display = 'none';
                }
            } catch(e) { console.error(e); }
        }

        async function toggleHolidayMode(enabled) {
            try {
                const res = await fetch('/api/email/holiday-mode', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(enabled ? 'Holiday mode ON - no follow-ups' : 'Holiday mode OFF', 'success');
                    loadEmailModeStatus();
                }
            } catch(e) { showToast('Error', 'error'); }
        }

        async function setPauseDate() {
            const dateInput = document.getElementById('pause-until-date');
            const date = dateInput.value;
            if (!date) {
                showToast('Select a date first', 'error');
                return;
            }

            try {
                const res = await fetch('/api/email/pause', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ until: date })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(`Emails paused until ${date}`, 'success');
                    loadEmailModeStatus();
                } else {
                    showToast(data.error || 'Error setting pause', 'error');
                }
            } catch(e) { showToast('Error', 'error'); }
        }

        async function resumeEmails() {
            try {
                const res = await fetch('/api/email/pause', { method: 'DELETE' });
                const data = await res.json();
                if (data.success) {
                    showToast('Emails resumed!', 'success');
                    document.getElementById('pause-until-date').value = '';
                    loadEmailModeStatus();
                }
            } catch(e) { showToast('Error', 'error'); }
        }

        async function loadTemplatePerformance() {
            try {
                const res = await fetch('/api/templates/performance');
                const data = await res.json();
                const el = document.getElementById('template-performance');

                if (data.success && data.templates && data.templates.length > 0) {
                    el.innerHTML = `
                        <table style="width:100%;">
                            <thead>
                                <tr>
                                    <th style="text-align:left;">Template</th>
                                    <th>Sent</th>
                                    <th>Opens</th>
                                    <th>Responses</th>
                                    <th>Rate</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${data.templates.map((t, i) => `
                                    <tr style="${i === 0 ? 'background:rgba(34,197,94,0.1);' : ''}">
                                        <td style="text-align:left;">
                                            ${i === 0 ? '🏆 ' : ''}${t.template_name || t.template_id}
                                        </td>
                                        <td style="text-align:center;">${t.sent}</td>
                                        <td style="text-align:center;">${t.opened} <span class="text-muted">(${t.open_rate}%)</span></td>
                                        <td style="text-align:center;">${t.responded}</td>
                                        <td style="text-align:center;font-weight:bold;color:${t.response_rate >= 10 ? 'var(--success)' : t.response_rate >= 5 ? 'var(--warn)' : 'var(--muted)'};">
                                            ${t.response_rate}%
                                        </td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                        <p class="text-muted text-sm mt-2">Top template is highlighted. Use high-performing templates more often!</p>
                    `;
                } else {
                    el.innerHTML = '<p class="text-muted text-sm">Send more emails to see performance data. Template response rates will be tracked.</p>';
                }
            } catch(e) {
                console.error(e);
                document.getElementById('template-performance').innerHTML = '<p class="text-muted text-sm">Error loading performance data</p>';
            }
        }

        async function loadTemplates(type) {
            try {
                const res = await fetch('/api/templates');
                const data = await res.json();
                templates = data.templates || [];
                
                const emailTypes = ['rc', 'ol', 'followup'];
                const dmTypes = ['dm'];
                const filterTypes = type === 'dm' ? dmTypes : emailTypes;
                
                const filtered = templates.filter(t => filterTypes.includes(t.template_type));
                const container = document.getElementById(type === 'dm' ? 'dm-templates' : 'email-templates');
                
                container.innerHTML = filtered.map(t => `
                    <div class="template-item">
                        <div class="template-info">
                            <div class="template-name">${t.name}</div>
                            <div class="template-type">${t.template_type.toUpperCase()} ${t.category === 'user' ? '(Custom)' : ''}</div>
                        </div>
                        <label class="toggle">
                            <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTemplate('${t.id}', this.checked)">
                            <span class="toggle-slider"></span>
                        </label>
                        <button class="btn btn-sm btn-outline" style="margin-left:8px" onclick="editTemplate('${t.id}')">✎</button>
                        ${t.category === 'user' ? `<button class="btn btn-sm btn-outline" style="margin-left:4px" onclick="deleteTemplate('${t.id}')">×</button>` : ''}
                    </div>
                `).join('');
                
                // Populate template select
                const sel = document.getElementById('template-select');
                sel.innerHTML = filtered.filter(t => t.enabled).map(t => `<option value="${t.id}">${t.name}</option>`).join('');
            } catch(e) { console.error(e); }
        }
        
        async function editTemplate(id) {
            const t = templates.find(x => x.id === id);
            if (!t) return;
            
            document.getElementById('template-modal').classList.add('active');
            document.getElementById('new-tpl-type').value = t.template_type;
            document.getElementById('new-tpl-name').value = t.name;
            document.getElementById('new-tpl-subject').value = t.subject || '';
            document.getElementById('new-tpl-body').value = t.body || '';
            
            // Store editing ID
            document.getElementById('template-modal').dataset.editId = id;
            document.querySelector('#template-modal .modal-title').textContent = 'Edit Template';
            document.getElementById('tpl-save-btn').textContent = 'Save Changes';
            
            // Show/hide subject based on type
            document.getElementById('tpl-subject-group').style.display = t.template_type === 'dm' ? 'none' : 'block';
        }
        
        async function toggleTemplate(id, enabled) {
            try {
                await fetch('/api/templates/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ id, enabled })
                });
            } catch(e) { showToast('Failed to toggle', 'error'); }
        }
        
        async function deleteTemplate(id) {
            if (!confirm('Delete this template?')) return;
            try {
                await fetch('/api/templates/' + id, { method: 'DELETE' });
                loadTemplates('email');
                showToast('Deleted', 'success');
            } catch(e) { showToast('Failed to delete', 'error'); }
        }
        
        document.getElementById('template-mode').addEventListener('change', (e) => {
            document.getElementById('template-select-wrapper').style.display = e.target.value === 'manual' ? 'block' : 'none';
        });
        
        async function sendEmails() {
            const limit = document.getElementById('email-limit').value;
            const mode = document.getElementById('template-mode').value;
            const templateId = mode === 'manual' ? document.getElementById('template-select').value : null;
            
            // Confirmation dialog (Fix #9)
            if (!confirm(`Send up to ${limit} emails now?\\n\\nThis will email coaches from your sheet who haven't been contacted yet.`)) {
                return;
            }
            
            document.getElementById('email-log').innerHTML = 'Sending...';
            
            try {
                const res = await fetch('/api/email/send', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ limit: parseInt(limit), template_id: templateId })
                });
                const data = await res.json();
                if (data.success) {
                    document.getElementById('email-log').innerHTML = `Sent: ${data.sent || 0}, Errors: ${data.errors || 0}`;
                } else {
                    document.getElementById('email-log').innerHTML = `Error: ${data.error || 'Unknown error'}`;
                }
                loadEmailPage();
                loadFollowupQueue();
            } catch(e) { 
                document.getElementById('email-log').innerHTML = 'Error sending emails';
            }
        }
        
        // DMs
        async function loadDMQueue() {
            try {
                const res = await fetch('/api/dm/queue');
                const data = await res.json();
                dmQueue = data.queue || [];

                const queueEl = document.getElementById('dm-queue');
                const sentEl = document.getElementById('dm-sent');
                const needHandleEl = document.getElementById('dm-need-handle');
                if (queueEl) queueEl.textContent = dmQueue.length;
                if (sentEl) sentEl.textContent = data.sent || 0;
                if (needHandleEl) needHandleEl.textContent = data.no_handle || 0;

                const container = document.getElementById('dm-queue-list');
                if (!container) return;  // Element doesn't exist
                const isMobile = window.innerWidth <= 768;

                if (dmQueue.length) {
                    if (isMobile) {
                        // Mobile: show one coach at a time with bigger buttons
                        const c = dmQueue[0];
                        container.innerHTML = `
                            <div class="dm-card-mobile" data-index="0">
                                <div class="dm-school" style="font-size:18px;font-weight:bold;">${c.school}</div>
                                <div class="dm-coach" style="font-size:14px;color:var(--muted);margin-bottom:8px;">
                                    ${c.coach_name} • <a href="https://x.com/${c.twitter}" target="_blank" style="color:var(--accent);">@${c.twitter}</a>
                                </div>
                                <textarea class="dm-textarea" id="dm-text-0" oninput="updateCharCount(0)" style="min-height:100px;font-size:14px;">${getDMText(c)}</textarea>
                                <div class="char-count" id="char-count-0">0/500</div>
                                <div style="display:flex;flex-direction:column;gap:10px;margin-top:12px;">
                                    <button class="btn btn-primary" onclick="quickDM(0)" style="padding:16px;font-size:16px;font-weight:bold;">
                                        COPY + OPEN TWITTER
                                    </button>
                                    <div style="display:flex;gap:10px;">
                                        <button class="btn btn-success" onclick="markDMSentMobile(0)" style="flex:1;padding:14px;">
                                            SENT
                                        </button>
                                        <button class="btn btn-outline" onclick="skipDM()" style="flex:1;padding:14px;">
                                            SKIP
                                        </button>
                                    </div>
                                </div>
                                <div class="text-sm text-muted mt-2" style="text-align:center;">
                                    ${dmQueue.length - 1} more coaches in queue
                                </div>
                            </div>
                        `;
                    } else {
                        // Desktop: show list of 5
                        container.innerHTML = dmQueue.slice(0, 5).map((c, i) => `
                            <div class="dm-card" data-index="${i}">
                                <div class="dm-header">
                                    <div>
                                        <div class="dm-school">${c.school}</div>
                                        <div class="dm-coach">${c.coach_name} • @${c.twitter}</div>
                                    </div>
                                </div>
                                <textarea class="dm-textarea" id="dm-text-${i}" oninput="updateCharCount(${i})">${getDMText(c)}</textarea>
                                <div class="char-count" id="char-count-${i}">0/500</div>
                                <div class="dm-actions">
                                    <button class="btn btn-sm" onclick="copyAndOpen(${i})">COPY & OPEN</button>
                                    <button class="btn btn-sm btn-success" onclick="markDMSent(${i})">MARK SENT</button>
                                </div>
                            </div>
                        `).join('');
                    }
                    const count = isMobile ? 1 : Math.min(5, dmQueue.length);
                    for (let i = 0; i < count; i++) updateCharCount(i);
                } else {
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon">✉️</div>
                            <div class="empty-state-title">All caught up!</div>
                            <div class="empty-state-text">No coaches waiting for DMs. Find more coaches with Twitter handles to add to your queue.</div>
                            <button class="btn btn-sm" onclick="showPage('find')">Find Coaches</button>
                        </div>
                    `;
                }
            } catch(e) { console.error(e); }
        }

        function quickDM(i) {
            // Mobile: copy text and open Twitter in one tap
            const text = document.getElementById('dm-text-' + i).value;
            if (text.length > 500) {
                showToast('DM too long! Max 500 chars', 'error');
                return;
            }
            navigator.clipboard.writeText(text);
            const coach = dmQueue[i];
            window.open('https://x.com/' + coach.twitter, '_blank');
            showToast('Message copied! Paste in Twitter DM', 'success');
        }

        async function markDMSentMobile(i) {
            const coach = dmQueue[i];
            try {
                await fetch('/api/twitter/mark-dm-sent', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ email: coach.email, school: coach.school })
                });
                showToast('Marked sent - loading next...', 'success');
                // Auto-advance to next coach
                loadDMQueue();
            } catch(e) { showToast('Error', 'error'); }
        }

        function skipDM() {
            // Move first coach to end of queue and refresh display
            if (dmQueue.length > 1) {
                const skipped = dmQueue.shift();
                dmQueue.push(skipped);
                loadDMQueue();
                showToast('Skipped to next coach', 'info');
            }
        }
        
        function getDMText(coach) {
            const s = settings.athlete || {};
            return `Coach, I'm ${s.name || '[Name]'}, ${s.graduation_year || '2026'} ${s.positions || '[Position]'} (${s.height || '[Height]'}/${s.weight || '[Weight]'}). Interested in ${coach.school}. Film: ${s.highlight_url || '[Hudl]'}`;
        }
        
        function updateCharCount(i) {
            const textarea = document.getElementById('dm-text-' + i);
            const text = textarea.value;
            const el = document.getElementById('char-count-' + i);
            el.textContent = text.length + '/500';
            el.classList.toggle('over', text.length > 500);
            
            // Enforce limit (Fix #15)
            if (text.length > 500) {
                textarea.value = text.substring(0, 500);
                el.textContent = '500/500';
            }
        }
        
        function copyAndOpen(i) {
            const text = document.getElementById('dm-text-' + i).value;
            if (text.length > 500) {
                showToast('DM too long! Max 500 chars', 'error');
                return;
            }
            navigator.clipboard.writeText(text);
            const coach = dmQueue[i];
            // Use x.com (Twitter's new domain) - works better for DMs
            window.open('https://x.com/' + coach.twitter, '_blank');
            showToast('Copied! Opening Twitter...', 'success');
        }
        
        async function markDMSent(i) {
            const coach = dmQueue[i];
            try {
                await fetch('/api/twitter/mark-dm-sent', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ email: coach.email, school: coach.school })
                });
                showToast('Marked as sent', 'success');
                loadDMQueue();
            } catch(e) { showToast('Error', 'error'); }
        }
        
        // Track Stats Dashboard
        async function loadTrackStats() {
            try {
                // Load tracking stats
                const trackRes = await fetch('/api/tracking/stats');
                const trackData = await trackRes.json();

                // Load pipeline stats
                const statsRes = await fetch('/api/stats');
                const statsData = await statsRes.json();

                // Update key metrics
                const totalSent = trackData.total_sent || statsData.emails_sent || 0;
                const totalOpened = trackData.total_opened || 0;

                const tts = document.getElementById('track-total-sent');
                if (tts) tts.textContent = totalSent;
                const to = document.getElementById('track-opened');
                if (to) to.textContent = totalOpened;
                const tor = document.getElementById('track-open-rate');
                if (tor) tor.textContent = (trackData.open_rate || 0) + '%';
                const trr = document.getElementById('track-response-rate');
                if (trr) trr.textContent = (statsData.response_rate || 0) + '%';

                // Recent opens
                const opensEl = document.getElementById('track-recent-opens');
                if (opensEl && trackData.recent_opens && trackData.recent_opens.length) {
                    opensEl.innerHTML = trackData.recent_opens.slice(0, 10).map(o => `
                        <div style="padding:8px 0;border-bottom:1px solid var(--border);">
                            <div style="font-weight:500;">${o.school || 'Unknown'}</div>
                            <div style="display:flex;justify-content:space-between;">
                                <span class="text-muted">${o.coach || ''}</span>
                                <span class="text-muted">${o.opened_at ? new Date(o.opened_at).toLocaleDateString() : ''}</span>
                            </div>
                        </div>
                    `).join('');
                } else if (opensEl) {
                    opensEl.innerHTML = '<div class="text-muted">No opens tracked yet</div>';
                }

                // Recent responses
                const responsesEl = document.getElementById('track-recent-responses');
                const responsesRes = await fetch('/api/responses/recent');
                const responsesData = await responsesRes.json();
                if (responsesEl && responsesData.responses && responsesData.responses.length) {
                    responsesEl.innerHTML = responsesData.responses.slice(0, 10).map(r => `
                        <div style="padding:8px 0;border-bottom:1px solid var(--border);">
                            <div style="font-weight:500;">${r.school || 'Unknown'}</div>
                            <div style="display:flex;justify-content:space-between;">
                                <span class="text-muted">${r.coach || ''}</span>
                                <span style="color:${r.sentiment === 'positive' ? '#27ae60' : r.sentiment === 'negative' ? '#e74c3c' : 'var(--muted)'};">${r.sentiment || ''}</span>
                            </div>
                        </div>
                    `).join('');
                } else if (responsesEl) {
                    responsesEl.innerHTML = '<div class="text-muted">No responses yet</div>';
                }

            } catch(e) {
                console.error('Track stats error:', e);
            }
        }
        
        // Settings
        async function loadSettings() {
            try {
                const res = await fetch('/api/settings');
                settings = await res.json();
                
                const a = settings.athlete || {};
                document.getElementById('s-name').value = a.name || '';
                document.getElementById('s-year').value = a.graduation_year || '2026';
                document.getElementById('s-position').value = a.positions || '';
                document.getElementById('s-school').value = a.high_school || '';
                document.getElementById('s-height').value = a.height || '';
                document.getElementById('s-weight').value = a.weight || '';
                document.getElementById('s-gpa').value = a.gpa || '';
                document.getElementById('s-hudl').value = a.highlight_url || '';
                document.getElementById('s-phone').value = a.phone || '';
                document.getElementById('s-email').value = a.email || '';
                
                const e = settings.email || {};
                document.getElementById('s-gmail').value = e.email_address || '';
                document.getElementById('s-gmail-pass').value = e.app_password || '';
                
                document.getElementById('s-sheet').value = (settings.sheets || {}).spreadsheet_name || 'bardeen';
                
                // Load toggle states
                const autoSendToggle = document.getElementById('auto-send-toggle');
                if (autoSendToggle) autoSendToggle.checked = e.auto_send_enabled || false;
                
                const notifyToggle = document.getElementById('notify-toggle');
                if (notifyToggle) notifyToggle.checked = (settings.notifications || {}).enabled || false;
                
                // Update header with athlete info
                document.getElementById('header-name').textContent = (a.name || 'ATHLETE').toUpperCase();
                const headerInfo = document.getElementById('header-info');
                if (headerInfo) headerInfo.textContent = `${a.graduation_year || '2026'} ${a.positions || 'OL'}`;
                
                // Update connection status
                const connected = e.email_address && e.app_password;
                document.getElementById('connection-status').textContent = connected ? 'ONLINE' : 'SETUP';
            } catch(e) { console.error(e); }
        }
        
        async function saveSettings() {
            settings.athlete = {
                name: document.getElementById('s-name').value,
                graduation_year: document.getElementById('s-year').value,
                positions: document.getElementById('s-position').value,
                high_school: document.getElementById('s-school').value,
                height: document.getElementById('s-height').value,
                weight: document.getElementById('s-weight').value,
                gpa: document.getElementById('s-gpa').value,
                highlight_url: document.getElementById('s-hudl').value,
                phone: document.getElementById('s-phone').value,
                email: document.getElementById('s-email').value,
            };
            
            // Only update password if user actually changed it (not masked value)
            const newPassword = document.getElementById('s-gmail-pass').value;
            settings.email = {
                ...settings.email,
                email_address: document.getElementById('s-gmail').value,
            };
            // Only send password if it's not the masked placeholder
            if (newPassword && newPassword !== '********') {
                settings.email.app_password = newPassword;
            }
            
            settings.sheets = {
                ...settings.sheets,
                spreadsheet_name: document.getElementById('s-sheet').value,
            };
            
            try {
                await fetch('/api/settings', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(settings)
                });
                showToast('Settings saved', 'success');
                closeSettings();
                loadSettings();
            } catch(e) { showToast('Failed to save', 'error'); }
        }
        
        // openSettings is defined below with extra functionality
        function closeSettings() { document.getElementById('settings-modal').classList.remove('active'); }
        
        // Template modal
        function openCreateTemplate(type) {
            const modal = document.getElementById('template-modal');
            modal.classList.add('active');
            modal.dataset.editId = '';  // Clear edit state
            document.querySelector('#template-modal .modal-title').textContent = 'Create Template';
            document.getElementById('tpl-save-btn').textContent = 'Create Template';
            document.getElementById('new-tpl-type').value = type === 'dm' ? 'dm' : 'rc';
            document.getElementById('new-tpl-name').value = '';
            document.getElementById('new-tpl-subject').value = '';
            document.getElementById('new-tpl-body').value = '';
            document.getElementById('tpl-subject-group').style.display = type === 'dm' ? 'none' : 'block';
        }
        function closeTemplateModal() { 
            const modal = document.getElementById('template-modal');
            modal.classList.remove('active'); 
            modal.dataset.editId = '';
        }
        
        document.getElementById('new-tpl-type').addEventListener('change', (e) => {
            document.getElementById('tpl-subject-group').style.display = e.target.value === 'dm' ? 'none' : 'block';
        });
        
        async function createTemplate() {
            const modal = document.getElementById('template-modal');
            const editId = modal.dataset.editId;
            const type = document.getElementById('new-tpl-type').value;
            const name = document.getElementById('new-tpl-name').value;
            const subject = document.getElementById('new-tpl-subject').value;
            const body = document.getElementById('new-tpl-body').value;
            
            if (!name || !body) { showToast('Name and body required', 'error'); return; }
            
            try {
                if (editId) {
                    // Update existing template
                    await fetch('/api/templates/' + editId, {
                        method: 'PUT',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ name, subject, body })
                    });
                    showToast('Template updated', 'success');
                } else {
                    // Create new template
                    await fetch('/api/templates', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ template_type: type, name, subject, body })
                    });
                    showToast('Template created', 'success');
                }
                closeTemplateModal();
                loadTemplates(type === 'dm' ? 'dm' : 'email');
            } catch(e) { showToast('Failed to save', 'error'); }
        }
        
        // Find coaches for a school (Fix #3)
        async function findCoaches(schoolName) {
            showToast('Finding coaches for ' + schoolName + '...');
            try {
                // First add school to sheet, then scrape
                await fetch('/api/schools/add-to-sheet', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ schools: [schoolName] })
                });
                
                // Trigger scraper for this school
                const res = await fetch('/api/run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ task: 'scrape_emails', school: schoolName })
                });
                const data = await res.json();
                if (data.success) {
                    showToast('Scraping started - check Email tab soon', 'success');
                } else {
                    showToast('Added to sheet. Run scraper manually.', 'success');
                }
            } catch(e) { 
                showToast('Added to sheet', 'success');
            }
        }
        
        // Open contact in pipeline (Fix #4)
        function openContact(contactId) {
            // For now, show contact details in alert - could make modal later
            fetch('/api/crm/contacts/' + contactId)
                .then(r => r.json())
                .then(data => {
                    if (data.contact) {
                        const c = data.contact;
                        alert(`${c.coach_name || c.name}\\n${c.school_name || c.school}\\n${c.email || ''}\\n\\nStage: ${c.stage}\\nNotes: ${c.notes || 'None'}`);
                    }
                })
                .catch(() => showToast('Could not load contact', 'error'));
        }
        
        // Add coach to pipeline manually (Fix #14)
        async function addToPipeline() {
            const school = prompt('School name:');
            if (!school) return;
            const coach = prompt('Coach name:');
            if (!coach) return;
            const email = prompt('Coach email (optional):') || '';
            
            try {
                await fetch('/api/crm/contacts', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ school_name: school, coach_name: coach, email: email, stage: 'prospect' })
                });
                showToast('Added to pipeline', 'success');
            } catch(e) { showToast('Failed to add', 'error'); }
        }
        
        // Mark coach response
        async function markCoachResponse() {
            const school = document.getElementById('response-school').value.trim();
            const type = document.getElementById('response-type').value;
            
            if (!school) {
                showToast('Enter school name', 'error');
                return;
            }
            
            try {
                const res = await fetch('/api/coach/response', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ school, response_type: type })
                });
                const data = await res.json();
                
                if (data.success) {
                    const labels = {
                        'dm_reply': 'DM Reply',
                        'email_reply': 'Email Reply', 
                        'interested': 'Interested',
                        'not_interested': 'Not Interested'
                    };
                    document.getElementById('response-result').innerHTML = 
                        '<span style="color:var(--success)">Marked ' + school + ' as: ' + labels[type] + '</span>';
                    document.getElementById('response-school').value = '';
                    showToast('Response recorded!', 'success');
                } else {
                    document.getElementById('response-result').innerHTML = 
                        '<span style="color:var(--err)">Error: ' + (data.error || 'School not found') + '</span>';
                }
            } catch(e) {
                showToast('Failed to record response', 'error');
            }
        }
        
        // Preview email before sending (Fix #8)
        async function previewEmail() {
            const templateId = document.getElementById('template-select').value;
            try {
                const res = await fetch('/api/email/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ template_id: templateId })
                });
                const data = await res.json();
                if (data.success) {
                    alert('Subject: ' + data.subject + '\\n\\n' + data.body);
                } else {
                    showToast('Could not preview', 'error');
                }
            } catch(e) { showToast('Preview error', 'error'); }
        }
        
        // Load followup queue (Fix #13)
        async function loadFollowupQueue() {
            try {
                const res = await fetch('/api/followups/due');
                const data = await res.json();
                const el = document.getElementById('followup-queue');
                if (data.followups && data.followups.length) {
                    el.innerHTML = data.followups.map(f => `
                        <div class="lead-item">
                            <div class="lead-info">
                                <div class="lead-school">${f.school}</div>
                                <div class="lead-coach">${f.coach_name} - ${f.followup_type}</div>
                            </div>
                            <button class="btn btn-sm" onclick="sendFollowup('${f.id}')">Send</button>
                        </div>
                    `).join('');
                } else {
                    el.innerHTML = '<p class="text-muted text-sm">No follow-ups due</p>';
                }
            } catch(e) { console.error(e); }
        }
        
        async function sendFollowup(id) {
            try {
                await fetch('/api/followups/send', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ followup_id: id })
                });
                showToast('Follow-up sent', 'success');
                loadFollowupQueue();
                loadEmailPage();
            } catch(e) { showToast('Failed to send', 'error'); }
        }

        // ========== AI EMAIL GENERATOR FUNCTIONS ==========

        async function loadAIEmailStatus() {
            try {
                // Load schools from spreadsheet
                const schoolsRes = await fetch('/api/ai-emails/schools');
                const schoolsData = await schoolsRes.json();

                if (schoolsData.success) {
                    document.getElementById('ai-total-schools').textContent = schoolsData.total || 0;
                    document.getElementById('ai-with-emails').textContent = schoolsData.with_ai_emails || 0;
                    document.getElementById('ai-needing-emails').textContent = schoolsData.needing_ai_emails || 0;
                }

                // Load API status
                const statusRes = await fetch('/api/ai-emails/status');
                const statusData = await statusRes.json();

                if (statusData.success && statusData.api_usage) {
                    document.getElementById('ai-api-remaining').textContent = statusData.api_usage.remaining_schools || 0;
                }
            } catch(e) { console.error('AI email status error:', e); }
        }

        async function loadAIEmailSchools() {
            const el = document.getElementById('ai-email-schools');
            el.innerHTML = '<p class="text-muted">Loading schools from spreadsheet...</p>';

            try {
                const res = await fetch('/api/ai-emails/schools');
                const data = await res.json();

                if (!data.success) {
                    el.innerHTML = `<p class="text-danger">${data.error}</p>`;
                    return;
                }

                if (!data.schools || data.schools.length === 0) {
                    el.innerHTML = '<p class="text-muted">No schools with email addresses in spreadsheet</p>';
                    return;
                }

                el.innerHTML = `
                    <table style="width:100%;font-size:12px;">
                        <tr style="background:var(--bg3);"><th style="padding:6px;">School</th><th>Coach</th><th>AI Email</th><th></th></tr>
                        ${data.schools.slice(0, 50).map(s => `
                            <tr style="border-bottom:1px solid var(--border);">
                                <td style="padding:6px;">${s.school}</td>
                                <td>${s.rc_name || s.ol_name || 'Coach'}</td>
                                <td>${s.has_ai_email ? '<span style="color:var(--success);">Yes</span>' : '<span style="color:var(--warning);">No</span>'}</td>
                                <td>
                                    ${s.has_ai_email
                                        ? `<button class="btn btn-sm btn-outline" onclick="previewAIEmail('${s.school.replace(/'/g, "\\'")}')">Preview</button>`
                                        : `<button class="btn btn-sm" onclick="generateOneAIEmail('${s.school.replace(/'/g, "\\'")}')">Generate</button>`
                                    }
                                </td>
                            </tr>
                        `).join('')}
                    </table>
                    ${data.schools.length > 50 ? `<p class="text-muted mt-2">Showing 50 of ${data.schools.length} schools</p>` : ''}
                `;
            } catch(e) {
                el.innerHTML = `<p class="text-danger">Error: ${e.message}</p>`;
            }
        }

        async function generateAIEmails() {
            const limit = parseInt(document.getElementById('ai-email-limit').value) || 5;
            showToast(`Generating AI emails for ${limit} schools...`);

            try {
                const res = await fetch('/api/ai-emails/generate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ limit })
                });
                const data = await res.json();

                if (data.success) {
                    if (data.count > 0) {
                        showToast(`Generated AI emails for ${data.count} schools!`, 'success');
                    } else {
                        showToast('No new schools to generate emails for', 'info');
                    }
                    loadAIEmailStatus();
                    loadAIEmailSchools();
                } else {
                    showToast(data.error || 'Generation failed', 'error');
                }
            } catch(e) {
                showToast('Error: ' + e.message, 'error');
            }
        }

        async function generateOneAIEmail(school) {
            showToast(`Generating AI email for ${school}...`);

            try {
                const res = await fetch('/api/ai-emails/generate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ school, limit: 1 })
                });
                const data = await res.json();

                if (data.success && data.count > 0) {
                    showToast(`Generated AI email for ${school}!`, 'success');
                    loadAIEmailStatus();
                    loadAIEmailSchools();
                } else {
                    showToast(data.error || 'Generation failed', 'error');
                }
            } catch(e) {
                showToast('Error: ' + e.message, 'error');
            }
        }

        async function previewAIEmail(school) {
            try {
                const res = await fetch(`/api/ai-emails/preview/${encodeURIComponent(school)}`);
                const data = await res.json();

                if (data.success && data.emails && data.emails.length > 0) {
                    const email = data.emails[0];
                    alert(`AI Email Preview for ${school}\\n\\n${email.content}\\n\\n---\\nResearch: ${email.research_used ? 'Yes' : 'No'}\\nGenerated: ${email.generated_at}`);
                } else {
                    showToast(data.error || 'No preview available', 'error');
                }
            } catch(e) {
                showToast('Preview error: ' + e.message, 'error');
            }
        }

        // Refresh DM queue (Fix #12)
        function refreshDMQueue() {
            loadDMQueue();
            showToast('Queue refreshed', 'success');
        }

        // ========== CLOUD EMAIL FUNCTIONS ==========
        async function loadCloudEmailStats() {
            try {
                const res = await fetch('/api/cloud-emails/stats');
                const data = await res.json();

                if (data.success) {
                    document.getElementById('cloud-total').textContent = data.total_emails || 0;
                    document.getElementById('cloud-pending').textContent = data.pending || 0;
                    document.getElementById('cloud-sent').textContent = data.sent || 0;
                    document.getElementById('cloud-open-rate').textContent = (data.open_rate || 0) + '%';
                    document.getElementById('cloud-response-rate').textContent = (data.response_rate || 0) + '%';
                    document.getElementById('cloud-successful').textContent = data.responses || 0;
                } else {
                    document.getElementById('cloud-email-log').innerHTML = '<span style="color:var(--warning);">Could not load cloud stats: ' + (data.error || 'Unknown error') + '</span>';
                }
            } catch(e) {
                document.getElementById('cloud-email-log').innerHTML = '<span style="color:var(--error);">Error: ' + e.message + '</span>';
            }
        }

        async function syncEmailsToCloud() {
            const log = document.getElementById('cloud-email-log');
            log.innerHTML = '<span class="spinner" style="width:12px;height:12px;border-width:2px;"></span> Syncing emails to cloud...';

            try {
                const res = await fetch('/api/cloud-emails/sync', { method: 'POST' });
                const data = await res.json();

                if (data.success) {
                    log.innerHTML = '<span style="color:var(--success);">Uploaded ' + data.uploaded + ' emails to cloud!</span>';
                    showToast('Synced ' + data.uploaded + ' emails to cloud', 'success');
                    loadCloudEmailStats();
                } else {
                    log.innerHTML = '<span style="color:var(--error);">Sync failed: ' + (data.error || 'Unknown error') + '</span>';
                }
            } catch(e) {
                log.innerHTML = '<span style="color:var(--error);">Error: ' + e.message + '</span>';
            }
        }

        async function viewCloudEmails() {
            const log = document.getElementById('cloud-email-log');
            log.innerHTML = '<span class="spinner" style="width:12px;height:12px;border-width:2px;"></span> Loading cloud emails...';

            try {
                const res = await fetch('/api/cloud-emails/pending');
                const data = await res.json();

                if (data.success && data.emails) {
                    if (data.emails.length === 0) {
                        log.innerHTML = '<em>No pending emails in cloud</em>';
                        return;
                    }

                    let html = '<div style="max-height:300px;overflow-y:auto;">';
                    html += '<strong>' + data.count + ' pending emails:</strong><br><br>';
                    data.emails.slice(0, 20).forEach(e => {
                        html += '<div style="padding:8px;margin-bottom:8px;background:var(--bg2);border-radius:4px;">';
                        html += '<strong>' + e.school + '</strong> - ' + e.email_type + '<br>';
                        html += '<small style="color:var(--muted);">' + e.coach_name + ' (' + e.coach_email + ')</small>';
                        html += '</div>';
                    });
                    if (data.emails.length > 20) {
                        html += '<em>... and ' + (data.emails.length - 20) + ' more</em>';
                    }
                    html += '</div>';
                    log.innerHTML = html;
                } else {
                    log.innerHTML = '<span style="color:var(--error);">Error: ' + (data.error || 'Unknown') + '</span>';
                }
            } catch(e) {
                log.innerHTML = '<span style="color:var(--error);">Error: ' + e.message + '</span>';
            }
        }

        async function viewSuccessfulEmails() {
            const log = document.getElementById('cloud-email-log');
            log.innerHTML = '<span class="spinner" style="width:12px;height:12px;border-width:2px;"></span> Loading successful emails...';

            try {
                const res = await fetch('/api/cloud-emails/successful');
                const data = await res.json();

                if (data.success && data.emails) {
                    if (data.emails.length === 0) {
                        log.innerHTML = '<em>No successful emails yet. Keep sending and tracking responses!</em>';
                        return;
                    }

                    let html = '<div style="max-height:400px;overflow-y:auto;">';
                    html += '<strong>' + data.count + ' emails that got responses:</strong><br><br>';
                    data.emails.forEach(e => {
                        html += '<div style="padding:8px;margin-bottom:8px;background:var(--bg2);border-radius:4px;border-left:3px solid var(--success);">';
                        html += '<strong>' + e.school + '</strong> - ' + e.email_type;
                        if (e.sentiment) html += ' <span style="color:var(--success);">(' + e.sentiment + ')</span>';
                        html += '<br>';
                        html += '<small style="color:var(--muted);white-space:pre-wrap;">' + (e.body || '').substring(0, 200) + '...</small>';
                        html += '</div>';
                    });
                    html += '</div>';
                    log.innerHTML = html;
                } else {
                    log.innerHTML = '<span style="color:var(--error);">Error: ' + (data.error || 'Unknown') + '</span>';
                }
            } catch(e) {
                log.innerHTML = '<span style="color:var(--error);">Error: ' + e.message + '</span>';
            }
        }

        // Load cloud stats on page load
        setTimeout(loadCloudEmailStats, 2000);

        // Tomorrow's email preview
        async function loadTomorrowPreview() {
            try {
                const res = await fetch('/api/email/tomorrow-preview');
                const data = await res.json();

                if (data.success) {
                    document.getElementById('tomorrow-total').textContent = data.total;
                    document.getElementById('tomorrow-ai').textContent = data.will_send_ai + ' / ' + data.ai;
                    document.getElementById('tomorrow-template').textContent = data.will_send_template + ' / ' + data.template;
                    document.getElementById('tomorrow-limit').textContent = data.limit;
                } else {
                    console.error('Tomorrow preview error:', data.error);
                }
            } catch(e) {
                console.error('Tomorrow preview error:', e);
            }
        }

        // Load tomorrow preview on page load
        setTimeout(loadTomorrowPreview, 1500);

        // ========== NEW SCRAPER FUNCTIONS ==========
        let scraperRunning = false;
        
        async function startScraper() {
            const type = document.getElementById('scrape-type').value;
            const scope = document.getElementById('scrape-scope').value;
            const batch = document.getElementById('scrape-batch').value;
            const school = document.getElementById('scrape-school').value;
            
            scraperRunning = true;
            document.getElementById('scraper-status').innerHTML = '<span style="color:var(--success)">● Running...</span>';
            document.getElementById('scraper-log').innerHTML = 'Starting scraper...';
            
            try {
                const res = await fetch('/api/scraper/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ type, scope, batch: parseInt(batch), school })
                });
                const data = await res.json();
                
                if (data.success) {
                    showToast('Scraper started', 'success');
                    pollScraperStatus();
                } else {
                    document.getElementById('scraper-status').innerHTML = '<span style="color:var(--err)">● Error</span>';
                    document.getElementById('scraper-log').innerHTML += 'Error: ' + (data.error || 'Unknown error');
                    scraperRunning = false;
                }
            } catch(e) {
                showToast('Failed to start scraper', 'error');
                scraperRunning = false;
            }
        }
        
        async function stopScraper() {
            scraperRunning = false;
            try {
                await fetch('/api/scraper/stop', { method: 'POST' });
                document.getElementById('scraper-status').innerHTML = '<span style="color:var(--warn)">● Stopped</span>';
                showToast('Scraper stopped', 'success');
            } catch(e) {}
        }
        
        async function pollScraperStatus() {
            if (!scraperRunning) return;
            
            try {
                const res = await fetch('/api/scraper/status');
                const data = await res.json();
                
                if (data.log) {
                    document.getElementById('scraper-log').innerHTML = data.log.join('<br>');
                }
                
                if (data.running) {
                    document.getElementById('scraper-status').innerHTML = 
                        `<span style="color:var(--success)">● Running</span> - ${data.processed || 0}/${data.total || '?'} schools`;
                    setTimeout(pollScraperStatus, 2000);
                } else {
                    document.getElementById('scraper-status').innerHTML = 
                        `<span style="color:var(--muted)">● Complete</span> - ${data.processed || 0} schools processed`;
                    scraperRunning = false;
                    showToast('Scraping complete', 'success');
                }
            } catch(e) {
                setTimeout(pollScraperStatus, 3000);
            }
        }
        
        async function scrapeTwitterHandles() {
            document.getElementById('scrape-type').value = 'twitter';
            document.getElementById('scrape-scope').value = 'missing';
            startScraper();
        }
        
        // ========== TEST EMAIL FUNCTION ==========
        async function sendTestEmail() {
            const templateId = document.getElementById('template-select').value;
            
            if (!confirm('Send a test email to yourself using the selected template?')) return;
            
            try {
                const res = await fetch('/api/email/test', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ template_id: templateId })
                });
                const data = await res.json();
                
                if (data.success) {
                    showToast('Test email sent to ' + data.sent_to, 'success');
                } else {
                    showToast('Failed: ' + (data.error || 'Unknown error'), 'error');
                }
            } catch(e) {
                showToast('Failed to send test email', 'error');
            }
        }
        
        // ========== TWITTER/DM FUNCTIONS ==========
        dmQueue = [];  // Reset queue
        let dmCurrentIndex = -1;
        let dmSessionActive = false;
        
        async function refreshDMQueue() {
            try {
                const res = await fetch('/api/dm/queue');
                const data = await res.json();
                dmQueue = data.queue || [];
                
                document.getElementById('dm-queue').textContent = dmQueue.length;
                document.getElementById('dm-sent').textContent = data.sent || 0;
                document.getElementById('dm-replied').textContent = data.replied || 0;
                document.getElementById('dm-no-handle').textContent = data.no_handle || 0;
                
                const list = document.getElementById('dm-queue-list');
                if (dmQueue.length === 0) {
                    list.innerHTML = '<p class="text-muted text-sm">No coaches in queue yet.</p>';
                } else {
                    list.innerHTML = dmQueue.map((c, i) => `
                        <div class="flex" style="justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);">
                            <div>
                                <strong>${c.coach_name}</strong><br>
                                <span class="text-sm text-muted">${c.school} - @${c.twitter}</span>
                            </div>
                            <span class="text-sm ${i === dmCurrentIndex ? 'text-success' : 'text-muted'}">${i === dmCurrentIndex ? 'CURRENT' : ''}</span>
                        </div>
                    `).join('');
                }
            } catch(e) {
                console.error(e);
            }
        }
        
        async function startDMSession() {
            await refreshDMQueue();
            if (dmQueue.length === 0) {
                showToast('No coaches in DM queue', 'error');
                return;
            }
            dmSessionActive = true;
            dmCurrentIndex = 0;
            showCurrentCoach();
            
            document.getElementById('btn-start-dm').style.display = 'none';
            document.getElementById('btn-end-dm').style.display = '';
            document.getElementById('btn-followed-messaged').style.display = '';
            document.getElementById('btn-followed-only').style.display = '';
            document.getElementById('btn-skip').style.display = '';
            document.getElementById('btn-wrong-twitter').style.display = '';
            document.getElementById('dm-progress').style.display = '';
            document.getElementById('keyboard-shortcuts').style.display = '';
            document.getElementById('auto-advance-label').style.display = '';
        }

        // Auto-advance timer
        let autoAdvanceTimer = null;

        function startAutoAdvanceTimer() {
            if (autoAdvanceTimer) clearTimeout(autoAdvanceTimer);
            if (!document.getElementById('auto-advance-toggle').checked) return;

            autoAdvanceTimer = setTimeout(() => {
                if (dmSessionActive) {
                    markDM('messaged');
                }
            }, 8000);  // 8 seconds
        }

        function clearAutoAdvanceTimer() {
            if (autoAdvanceTimer) {
                clearTimeout(autoAdvanceTimer);
                autoAdvanceTimer = null;
            }
        }
        
        // Store the Twitter popup window reference
        let twitterPopup = null;
        
        async function showCurrentCoach() {
            if (dmCurrentIndex >= dmQueue.length) {
                endDMSession();
                return;
            }
            
            const coach = dmQueue[dmCurrentIndex];
            const twitterUrl = `https://x.com/${coach.twitter}`;
            
            const info = document.getElementById('current-coach-info');
            info.innerHTML = `
                <div style="font-size:1.5em;font-weight:bold;margin-bottom:8px;">${coach.coach_name}</div>
                <div class="text-muted">${coach.school}</div>
                <div style="margin-top:12px;">
                    <button class="btn btn-sm btn-outline" onclick="openTwitterPopup('${twitterUrl}')">
                        Re-open @${coach.twitter} ↗
                    </button>
                </div>
            `;
            
            // Get DM message and copy to clipboard FIRST (before opening Twitter)
            try {
                const res = await fetch('/api/dm/message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ coach_name: coach.coach_name, school: coach.school })
                });
                const data = await res.json();
                
                if (data.message) {
                    document.getElementById('dm-message-text').textContent = data.message;
                    document.getElementById('dm-message-preview').style.display = 'block';
                    
                    // Copy to clipboard BEFORE opening popup
                    await copyToClipboard(data.message);
                    showToast('Message copied! Opening Twitter...', 'success');

                    // Now open Twitter in a popup window (positioned to the right)
                    setTimeout(() => {
                        openTwitterPopup(twitterUrl);
                        startAutoAdvanceTimer();  // Start auto-advance if enabled
                    }, 300);
                }
            } catch(e) {
                console.error(e);
                // Still open Twitter even if message fails
                openTwitterPopup(twitterUrl);
            }
            
            // Update progress
            document.getElementById('dm-progress').textContent = `Coach ${dmCurrentIndex + 1} of ${dmQueue.length}`;
            
            // Refresh queue display to show current marker
            refreshDMQueue();
        }
        
        async function copyToClipboard(text) {
            try {
                await navigator.clipboard.writeText(text);
                return true;
            } catch(e) {
                // Fallback method
                const textarea = document.createElement('textarea');
                textarea.value = text;
                textarea.style.cssText = 'position:fixed;left:-9999px;top:0;';
                document.body.appendChild(textarea);
                textarea.focus();
                textarea.select();
                try {
                    document.execCommand('copy');
                } catch(e2) {
                    console.error('Copy failed:', e2);
                }
                document.body.removeChild(textarea);
                return true;
            }
        }
        
        function openTwitterPopup(url) {
            // Open in a popup window positioned to the right side of screen
            const width = 500;
            const height = 700;
            const left = window.screen.width - width - 50;
            const top = 50;
            
            twitterPopup = window.open(
                url, 
                'twitter_dm',
                `width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`
            );
            
            // Focus back to main window after a short delay so keyboard shortcuts work
            setTimeout(() => {
                window.focus();
            }, 500);
        }
        
        async function markDM(status) {
            if (!dmSessionActive || dmCurrentIndex >= dmQueue.length) return;
            clearAutoAdvanceTimer();  // Clear timer when user acts

            const coach = dmQueue[dmCurrentIndex];
            
            try {
                await fetch('/api/dm/mark', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ 
                        coach_name: coach.coach_name,
                        school: coach.school,
                        twitter: coach.twitter,
                        status: status  // 'messaged', 'followed', 'skipped'
                    })
                });
                
                if (status === 'messaged') {
                    showToast('Marked as messaged', 'success');
                } else if (status === 'followed') {
                    showToast('Marked as followed only', 'success');
                }
            } catch(e) {
                console.error(e);
            }
            
            // Close the Twitter popup if it's open
            if (twitterPopup && !twitterPopup.closed) {
                twitterPopup.close();
            }
            
            // Move to next
            dmCurrentIndex++;
            showCurrentCoach();
        }
        
        function endDMSession() {
            dmSessionActive = false;
            dmCurrentIndex = -1;
            clearAutoAdvanceTimer();  // Clear any pending timer

            // Close popup if open
            if (twitterPopup && !twitterPopup.closed) {
                twitterPopup.close();
            }

            document.getElementById('current-coach-info').innerHTML = '<p class="text-success">Session ended.</p>';
            document.getElementById('dm-message-preview').style.display = 'none';
            document.getElementById('btn-start-dm').style.display = '';
            document.getElementById('btn-end-dm').style.display = 'none';
            document.getElementById('btn-followed-messaged').style.display = 'none';
            document.getElementById('btn-followed-only').style.display = 'none';
            document.getElementById('btn-skip').style.display = 'none';
            document.getElementById('btn-wrong-twitter').style.display = 'none';
            document.getElementById('keyboard-shortcuts').style.display = 'none';
            document.getElementById('dm-progress').style.display = 'none';
            document.getElementById('auto-advance-label').style.display = 'none';
            
            refreshDMQueue();
            showToast('DM session ended', 'success');
        }
        
        // Keyboard shortcuts for DM session
        document.addEventListener('keydown', (e) => {
            if (!dmSessionActive) return;
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            
            if (e.key === 'm' || e.key === 'M') {
                markDM('messaged');
            } else if (e.key === 'f' || e.key === 'F') {
                markDM('followed');
            } else if (e.key === 's' || e.key === 'S' || e.key === 'ArrowRight') {
                markDM('skipped');
            } else if (e.key === 'c' || e.key === 'C') {
                reCopyMessage();
            } else if (e.key === 'w' || e.key === 'W') {
                markTwitterWrong();
            }
        });
        
        async function reCopyMessage() {
            const msgText = document.getElementById('dm-message-text').textContent;
            if (!msgText) {
                showToast('No message to copy', 'error');
                return;
            }
            try {
                await navigator.clipboard.writeText(msgText);
                showToast('Message re-copied!', 'success');
            } catch(e) {
                const textarea = document.createElement('textarea');
                textarea.value = msgText;
                textarea.style.position = 'fixed';
                textarea.style.left = '-9999px';
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                document.body.removeChild(textarea);
                showToast('Message re-copied!', 'success');
            }
        }
        
        async function markTwitterWrong() {
            if (!dmSessionActive || dmCurrentIndex >= dmQueue.length) return;
            
            const coach = dmQueue[dmCurrentIndex];
            try {
                await fetch('/api/twitter/mark-wrong', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ 
                        school: coach.school,
                        twitter: coach.twitter,
                        coach_name: coach.coach_name
                    })
                });
                showToast('Marked as wrong handle - will retry next scrape', 'success');
                
                // Move to next
                dmCurrentIndex++;
                showCurrentCoach();
            } catch(e) {
                showToast('Error marking', 'error');
            }
        }
        
        // ========== SHEETS CONNECTION FUNCTIONS ==========
        async function testSheetConnection() {
            showToast('Testing connection...', 'success');
            try {
                const res = await fetch('/api/sheets/test');
                const data = await res.json();
                
                const el = document.getElementById('sheets-connection-text');
                if (data.connected) {
                    el.innerHTML = '<span style="color:var(--success)">● Connected</span> - ' + data.rows + ' rows in sheet';
                    showToast('Connected to Google Sheets!', 'success');
                } else {
                    el.innerHTML = '<span style="color:var(--err)">● Not connected</span> - ' + (data.error || 'Check credentials');
                    showToast('Connection failed: ' + (data.error || 'Unknown'), 'error');
                }
            } catch(e) {
                showToast('Connection test failed', 'error');
            }
        }
        
        async function uploadCredentials() {
            const input = document.getElementById('credentials-file');
            if (!input.files.length) {
                showToast('Select a file first', 'error');
                return;
            }
            
            const file = input.files[0];
            const reader = new FileReader();
            reader.onload = async (e) => {
                try {
                    const content = e.target.result;
                    const res = await fetch('/api/sheets/credentials', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ credentials: content })
                    });
                    const data = await res.json();
                    if (data.success) {
                        showToast('Credentials saved! Test connection now.', 'success');
                    } else {
                        showToast(data.error || 'Failed to save', 'error');
                    }
                } catch(e) {
                    showToast('Failed to upload credentials', 'error');
                }
            };
            reader.readAsText(file);
        }
        
        async function testInboxConnection() {
            showToast('Testing inbox connection...', 'success');
            try {
                const res = await fetch('/api/inbox/test');
                const data = await res.json();
                if (data.success) {
                    showToast('Inbox connected! Found ' + data.count + ' recent emails', 'success');
                } else {
                    showToast('Failed: ' + (data.error || 'Check credentials'), 'error');
                }
            } catch(e) {
                showToast('Inbox test failed', 'error');
            }
        }
        
        // Check sheet status on settings open
        async function openSettings() { 
            document.getElementById('settings-modal').classList.add('active'); 
            
            // Check if running on Railway
            try {
                const res = await fetch('/api/deployment-info');
                const info = await res.json();
                
                if (info.on_railway) {
                    // Hide credentials section, show Railway banner
                    document.getElementById('credentials-section').style.display = 'none';
                    document.getElementById('railway-banner').style.display = 'block';
                } else {
                    // Show credentials section, hide Railway banner
                    document.getElementById('credentials-section').style.display = 'block';
                    document.getElementById('railway-banner').style.display = 'none';
                }
            } catch(e) {
                // Default to showing credentials section
                document.getElementById('credentials-section').style.display = 'block';
                document.getElementById('railway-banner').style.display = 'none';
            }
            
            testSheetConnection();
        }
        
        // Toast
        function showToast(msg, type = '') {
            const el = document.getElementById('toast');
            el.textContent = msg;
            el.className = 'toast ' + type;
            el.style.display = 'block';
            setTimeout(() => el.style.display = 'none', 3000);
        }
        
        // Check sheet connection and update header
        async function checkSheetConnection() {
            try {
                const res = await fetch('/api/sheets/test');
                const data = await res.json();
                const el = document.getElementById('connection-status');
                if (data.connected) {
                    el.textContent = 'ONLINE';
                } else {
                    el.textContent = 'OFFLINE';
                }
            } catch(e) {
                document.getElementById('connection-status').textContent = 'ERROR';
            }
        }
        
        // Auto-check inbox for responses (runs silently)
        async function autoCheckInbox() {
            try {
                const res = await fetch('/api/email/check-responses', { method: 'POST' });
                const data = await res.json();
                if (data.success && data.new_count > 0) {
                    showToast(`${data.new_count} coach(es) replied!`, 'success');
                    loadDashboard();
                }
            } catch(e) { /* silent fail */ }
        }
        
        async function loadAutoSendStatus() {
            try {
                const res = await fetch('/api/auto-send/status');
                const data = await res.json();
                
                document.getElementById('last-auto-send').textContent = 
                    data.last_run ? new Date(data.last_run).toLocaleString() : 'Never';
                document.getElementById('next-auto-send').textContent = 
                    data.next_run ? new Date(data.next_run).toLocaleString() : 
                    (data.enabled ? 'Today (random time)' : 'Auto-send disabled');
                    
                // Update toggle state
                const toggle = document.getElementById('auto-send-toggle');
                if (toggle) toggle.checked = data.enabled;
            } catch(e) { /* silent fail */ }
        }
        
        // Init with error handling
        console.log('Starting initialization...');

        // Test if fetch works
        fetch('/api/stats').then(r => r.json()).then(data => {
            console.log('API test succeeded:', data);
            document.getElementById('stat-sent').textContent = data.emails_sent || 0;
            document.getElementById('stat-responses').textContent = data.responses || 0;
            document.getElementById('stat-rate').textContent = (data.response_rate || 0) + '%';
            document.getElementById('connection-status').textContent = 'ONLINE';
        }).catch(e => {
            console.error('API test failed:', e);
            document.getElementById('connection-status').textContent = 'ERROR';
        });

        loadSettings().catch(e => console.error('loadSettings failed:', e));
        loadDashboard().catch(e => console.error('loadDashboard failed:', e));
        checkSheetConnection().catch(e => console.error('checkSheetConnection failed:', e));
        loadAutoSendStatus().catch(e => console.error('loadAutoSendStatus failed:', e));
        loadEmailQueueStatus().catch(e => console.error('loadEmailQueueStatus failed:', e));
        // Check for responses after 3 seconds, then every 5 minutes
        setTimeout(autoCheckInbox, 3000);
        setInterval(autoCheckInbox, 5 * 60 * 1000);  // Check every 5 minutes

        // Unregister service workers to fix caching issues
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.getRegistrations().then(regs => {
                regs.forEach(r => r.unregister());
            });
        }

        // ========== LOGOUT ==========
        async function doLogout() {
            await fetch('/logout', {method:'POST'});
            window.location.href = '/login';
        }

        // ========== MODAL HELPERS ==========
        function closeModal(id) { document.getElementById(id).classList.remove('active'); }
        function showCreateAthlete() { document.getElementById('create-athlete-modal').classList.add('active'); }

        // ========== ADMIN PANEL ==========
        async function loadAdminPanel() {
            await Promise.all([loadAthletesList(), loadMissingCoaches()]);
        }

        async function loadAthletesList() {
            try {
                const res = await fetch('/api/admin/athletes');
                const data = await res.json();
                const el = document.getElementById('athletes-list');
                if (!data.athletes || !data.athletes.length) {
                    el.innerHTML = '<div class="empty-state"><div class="empty-state-title">No athletes yet</div></div>';
                    return;
                }
                el.innerHTML = data.athletes.map(a => `
                    <div style="padding:12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">
                        <div>
                            <strong style="color:var(--text);">${a.name}</strong>
                            <span class="text-muted"> — ${a.email}</span>
                            ${a.is_admin ? '<span style="color:var(--accent);font-size:11px;margin-left:8px;">ADMIN</span>' : ''}
                        </div>
                        <div style="display:flex;gap:8px;align-items:center;">
                            <span class="text-muted text-sm">${a.stats.emails_sent || 0} sent, ${a.stats.schools_selected || 0} schools</span>
                            <span style="width:8px;height:8px;border-radius:50%;background:${a.stats.has_gmail ? 'var(--success)' : 'var(--danger)'};display:inline-block;" title="${a.stats.has_gmail ? 'Gmail configured' : 'No Gmail credentials'}"></span>
                            <button class="btn btn-sm" onclick="editAthleteCredentials('${a.id}','${a.email}')" style="font-size:11px;">CREDS</button>
                        </div>
                    </div>
                `).join('');
            } catch(e) { console.error(e); }
        }

        async function loadMissingCoaches() {
            try {
                const res = await fetch('/api/admin/missing-coaches');
                const data = await res.json();
                const el = document.getElementById('missing-coaches');
                if (!data.alerts || !data.alerts.length) {
                    el.innerHTML = '<div style="padding:12px;color:var(--success);">All good — no missing coach data.</div>';
                    return;
                }
                el.innerHTML = data.alerts.map(a => `
                    <div style="padding:8px 0;border-bottom:1px solid var(--border);">
                        <strong>${a.school_name}</strong> <span class="text-muted">(${a.athlete_name})</span>
                        — missing: ${a.missing.join(', ')}
                    </div>
                `).join('');
            } catch(e) { console.error(e); }
        }

        async function createAthlete(e) {
            e.preventDefault();
            const body = {
                name: document.getElementById('ca-name').value,
                email: document.getElementById('ca-email').value,
                password: document.getElementById('ca-pw').value,
                phone: document.getElementById('ca-phone').value,
                grad_year: document.getElementById('ca-year').value,
                position: document.getElementById('ca-pos').value,
                height: document.getElementById('ca-ht').value,
                weight: document.getElementById('ca-wt').value,
                gpa: document.getElementById('ca-gpa').value,
                high_school: document.getElementById('ca-school').value,
                state: document.getElementById('ca-state').value,
                hudl_link: document.getElementById('ca-hudl').value,
                gmail_email: document.getElementById('ca-gmail').value,
                gmail_client_id: document.getElementById('ca-cid').value,
                gmail_client_secret: document.getElementById('ca-csec').value,
                gmail_refresh_token: document.getElementById('ca-rtok').value,
            };
            try {
                const res = await fetch('/api/admin/athletes/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
                const data = await res.json();
                if (data.success) {
                    closeModal('create-athlete-modal');
                    e.target.reset();
                    loadAthletesList();
                    alert('Athlete created!');
                } else {
                    alert('Error: ' + (data.error || 'Unknown'));
                }
            } catch(err) { alert('Error: ' + err.message); }
        }

        function editAthleteCredentials(athleteId, email) {
            document.getElementById('ec-aid').value = athleteId;
            document.getElementById('ec-gmail').value = email;
            document.getElementById('ec-cid').value = '';
            document.getElementById('ec-csec').value = '';
            document.getElementById('ec-rtok').value = '';
            document.getElementById('edit-creds-modal').classList.add('active');
        }

        async function saveCreds(e) {
            e.preventDefault();
            const athleteId = document.getElementById('ec-aid').value;
            const body = {
                gmail_email: document.getElementById('ec-gmail').value,
                gmail_client_id: document.getElementById('ec-cid').value,
                gmail_client_secret: document.getElementById('ec-csec').value,
                gmail_refresh_token: document.getElementById('ec-rtok').value,
            };
            try {
                const res = await fetch(`/api/admin/athletes/${athleteId}/credentials`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
                const data = await res.json();
                if (data.success) {
                    closeModal('edit-creds-modal');
                    loadAthletesList();
                    alert('Credentials saved!');
                } else {
                    alert('Error: ' + (data.error || 'Unknown'));
                }
            } catch(err) { alert('Error: ' + err.message); }
        }

        // ========== SCHOOL SELECTION (MY SCHOOLS) ==========
        async function loadMySchools() {
            try {
                const res = await fetch('/api/athlete/schools');
                const data = await res.json();
                const el = document.getElementById('my-schools-list');
                if (!el) return;
                if (!data.schools || !data.schools.length) {
                    el.innerHTML = '<div class="empty-state"><div class="empty-state-title">No schools selected</div><div class="empty-state-text">Search above and add schools to your list.</div></div>';
                    return;
                }
                el.innerHTML = data.schools.map(s => `
                    <div style="padding:8px 12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">
                        <div>
                            <strong>${s.school_name || s.name}</strong>
                            <span class="text-muted text-sm"> — ${s.coach_preference || 'both'}</span>
                        </div>
                        <button class="btn btn-sm" onclick="removeMySchool('${s.school_id}')" style="font-size:11px;color:var(--danger);">REMOVE</button>
                    </div>
                `).join('');
            } catch(e) { console.error(e); }
        }

        async function addSchoolToMyList(schoolId, schoolName) {
            const pref = prompt('Coach preference for ' + schoolName + '?\\n\\n1) position_coach\\n2) rc (recruiting coordinator)\\n3) both (default)', 'both');
            const preference = (pref === '1') ? 'position_coach' : (pref === '2') ? 'rc' : 'both';
            try {
                const res = await fetch('/api/athlete/schools/add', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({school_id: schoolId, coach_preference: preference})});
                const data = await res.json();
                if (data.success) { loadMySchools(); } else { alert(data.error || 'Error'); }
            } catch(e) { alert(e.message); }
        }

        async function removeMySchool(schoolId) {
            if (!confirm('Remove this school from your list?')) return;
            try {
                const res = await fetch('/api/athlete/schools/remove', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({school_id: schoolId})});
                const data = await res.json();
                if (data.success) { loadMySchools(); }
            } catch(e) { console.error(e); }
        }
    </script>
</body>
</html>
'''


# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    """Login page and auth handler."""
    if request.method == 'GET':
        if 'athlete_id' in session:
            return redirect('/')
        return render_template_string(LOGIN_TEMPLATE)

    # POST: authenticate
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password', '')

    if not email or not password:
        return jsonify({'success': False, 'error': 'Email and password required'}), 400

    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not available'}), 500

    try:
        athlete = _supabase_db.authenticate_athlete(email, password)
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return jsonify({'success': False, 'error': 'Server error during login'}), 500
    if not athlete:
        return jsonify({'success': False, 'error': 'Invalid email or password'}), 401

    session.permanent = True
    session['athlete_id'] = athlete['id']
    session['is_admin'] = athlete.get('is_admin', False)
    session['athlete_name'] = athlete.get('name', '')
    session['athlete_email'] = athlete.get('email', '')
    logger.info(f"Login: {email} (admin={athlete.get('is_admin')})")
    return jsonify({'success': True, 'is_admin': athlete.get('is_admin', False)})


@app.route('/logout', methods=['POST', 'GET'])
def logout():
    """Logout."""
    session.clear()
    if request.method == 'GET':
        return redirect('/login')
    return jsonify({'success': True})


@app.route('/api/auth/status')
def auth_status():
    return jsonify({
        'logged_in': 'athlete_id' in session,
        'is_admin': session.get('is_admin', False),
        'athlete_name': session.get('athlete_name', ''),
    })


@app.route('/')
@login_required
def index():
    is_admin = session.get('is_admin', False)
    athlete_name = session.get('athlete_name', 'Athlete')
    response = make_response(render_template_string(
        HTML_TEMPLATE,
        is_admin=is_admin,
        athlete_name=athlete_name,
    ))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# ============================================================================
# PWA SUPPORT
# ============================================================================

@app.route('/manifest.json')
def manifest():
    """PWA manifest for installable app."""
    return jsonify({
        "name": "Coach Outreach Pro",
        "short_name": "Coach Outreach",
        "description": "College football recruiting outreach tool",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f0f14",
        "theme_color": "#6366f1",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    })

@app.route('/icon-192.png')
def icon_192():
    """Generate a simple icon for the PWA."""
    # Simple SVG icon encoded as PNG placeholder
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192" viewBox="0 0 192 192">
        <rect width="192" height="192" rx="32" fill="#6366f1"/>
        <text x="96" y="120" font-size="80" text-anchor="middle" fill="white" font-family="Arial" font-weight="bold">CO</text>
    </svg>'''
    import io
    return Response(svg, mimetype='image/svg+xml')

@app.route('/icon-512.png')
def icon_512():
    """Generate a simple icon for the PWA."""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
        <rect width="512" height="512" rx="64" fill="#6366f1"/>
        <text x="256" y="320" font-size="200" text-anchor="middle" fill="white" font-family="Arial" font-weight="bold">CO</text>
    </svg>'''
    return Response(svg, mimetype='image/svg+xml')

@app.route('/sw.js')
def service_worker():
    """Service worker - network first, no caching of HTML."""
    sw_code = '''
const CACHE_NAME = 'coach-outreach-v4';

self.addEventListener('install', event => {
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys => Promise.all(
            keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
        )).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', event => {
    // Always fetch from network for HTML
    if (event.request.mode === 'navigate' || event.request.url.endsWith('/')) {
        event.respondWith(fetch(event.request));
        return;
    }
    // For other requests, try network first
    event.respondWith(
        fetch(event.request).catch(() => caches.match(event.request))
    );
});
'''
    return Response(sw_code, mimetype='application/javascript')


# ============================================================================
# EMAIL TRACKING ENDPOINTS
# ============================================================================

# 1x1 transparent PNG pixel
TRACKING_PIXEL = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
)

@app.route('/api/track/open/<tracking_id>')
def track_open(tracking_id):
    """Track email opens via invisible pixel."""
    if tracking_id in email_tracking['sent']:
        open_event = {
            'opened_at': datetime.now().isoformat(),
            'ip': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', '')[:200]
        }
        if tracking_id not in email_tracking['opens']:
            email_tracking['opens'][tracking_id] = []
        is_first_open = len(email_tracking['opens'][tracking_id]) == 0
        email_tracking['opens'][tracking_id].append(open_event)
        save_tracking()

        info = email_tracking['sent'][tracking_id]
        logger.info(f"📬 Email OPENED: {info.get('school')} - {info.get('coach')}")

        # Update Supabase tracking
        if SUPABASE_AVAILABLE and _supabase_db:
            try:
                sb_tid = info.get('supabase_tracking_id')
                if sb_tid:
                    _supabase_db.track_open(sb_tid)
            except Exception as e:
                logger.warning(f"Supabase track_open error: {e}")

        # Send phone notification on FIRST open only
        if is_first_open:
            try:
                current_settings = load_settings()
                if current_settings.get('notifications', {}).get('enabled'):
                    send_phone_notification(
                        title="📬 Coach Opened Email!",
                        message=f"{info.get('coach', 'A coach')} at {info.get('school', 'Unknown')} just opened your email!"
                    )
            except Exception as e:
                logger.warning(f"Could not send open notification: {e}")

    return Response(TRACKING_PIXEL, mimetype='image/png', headers={
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'Pragma': 'no-cache'
    })


@app.route('/api/tracking/stats')
def tracking_stats():
    """Get email tracking statistics."""
    total_sent = len(email_tracking['sent'])
    total_opened = sum(1 for tid in email_tracking['opens'] if email_tracking['opens'][tid])

    # Get opens by school
    opens_by_school = {}
    for tid, info in email_tracking['sent'].items():
        school = info.get('school', 'Unknown')
        if school not in opens_by_school:
            opens_by_school[school] = {'sent': 0, 'opened': 0}
        opens_by_school[school]['sent'] += 1
        if email_tracking['opens'].get(tid):
            opens_by_school[school]['opened'] += 1

    # Recent opens (last 20)
    recent_opens = []
    for tid, opens in email_tracking['opens'].items():
        if opens:
            info = email_tracking['sent'].get(tid, {})
            for o in opens[-3:]:  # Last 3 opens per email
                recent_opens.append({
                    'school': info.get('school'),
                    'coach': info.get('coach'),
                    'opened_at': o.get('opened_at'),
                    'sent_at': info.get('sent_at')
                })
    recent_opens.sort(key=lambda x: x.get('opened_at', ''), reverse=True)

    # Merge Supabase stats if available
    supabase_stats = {}
    hot_leads = []
    if SUPABASE_AVAILABLE and _supabase_db:
        try:
            supabase_stats = _supabase_db.get_outreach_stats()
            hot_leads = _supabase_db.get_hot_leads(limit=10)
        except Exception as e:
            logger.warning(f"Could not fetch Supabase stats: {e}")

    return jsonify({
        'success': True,
        'total_sent': total_sent,
        'total_opened': total_opened,
        'open_rate': round(total_opened / total_sent * 100, 1) if total_sent > 0 else 0,
        'by_school': opens_by_school,
        'recent_opens': recent_opens[:20],
        'supabase': supabase_stats,
        'hot_leads': hot_leads,
    })


@app.route('/api/tracking/smart-times')
def smart_send_times():
    """Analyze email opens to suggest optimal send times."""
    from collections import defaultdict

    # Count opens by hour and day of week
    hour_counts = defaultdict(int)
    day_counts = defaultdict(int)
    day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    for tid, opens in email_tracking['opens'].items():
        for o in opens:
            try:
                opened_at = datetime.fromisoformat(o['opened_at'].replace('Z', '+00:00'))
                hour_counts[opened_at.hour] += 1
                day_counts[opened_at.weekday()] += 1
            except:
                pass

    # Find best hours (top 3)
    best_hours = sorted(hour_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    best_days = sorted(day_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    # Format for display
    best_hours_formatted = [
        {'hour': h, 'display': f"{h}:00 - {h+1}:00", 'opens': c}
        for h, c in best_hours
    ]
    best_days_formatted = [
        {'day': d, 'display': day_names[d], 'opens': c}
        for d, c in best_days
    ]

    # Generate recommendation
    if best_hours and best_days:
        top_day = day_names[best_days[0][0]]
        top_hour = best_hours[0][0]
        recommendation = f"Best time: {top_day}s around {top_hour}:00"
    else:
        recommendation = "Send more emails to gather data on optimal times"

    return jsonify({
        'success': True,
        'best_hours': best_hours_formatted,
        'best_days': best_days_formatted,
        'recommendation': recommendation,
        'total_opens_analyzed': sum(hour_counts.values())
    })


@app.route('/api/tracking/backfill', methods=['POST'])
def api_tracking_backfill():
    """Backfill tracking data from Supabase outreach records into local tracking."""
    try:
        if not _supabase_db:
            return jsonify({'success': False, 'error': 'Database not connected'})

        sent_outreach = _supabase_db.get_sent_outreach(limit=500)
        tracked_emails = {info.get('to', '').lower() for info in email_tracking['sent'].values()}

        backfilled = 0
        existing = 0

        for record in sent_outreach:
            coach_email = (record.get('coach_email') or '').strip().lower()
            if not coach_email:
                continue
            if coach_email in tracked_emails:
                existing += 1
                continue

            tracking_id = record.get('tracking_id') or generate_tracking_id(coach_email, record.get('school_name', ''))
            email_tracking['sent'][tracking_id] = {
                'to': coach_email,
                'school': record.get('school_name', ''),
                'coach': record.get('coach_name', ''),
                'subject': record.get('subject', 'Backfilled'),
                'sent_at': record.get('sent_at', ''),
                'template_id': 'backfill',
                'supabase_outreach_id': record.get('id'),
            }
            email_tracking['opens'][tracking_id] = []
            tracked_emails.add(coach_email)
            backfilled += 1

        save_tracking()

        return jsonify({
            'success': True,
            'backfilled': backfilled,
            'already_tracked': existing,
            'total_tracked': len(email_tracking['sent'])
        })

    except Exception as e:
        logger.error(f"Backfill error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/deployment-info')
def api_deployment_info():
    """Return info about deployment environment."""
    on_railway = is_railway_deployment()
    return jsonify({
        'on_railway': on_railway,
        'env_email_set': bool(ENV_EMAIL_ADDRESS),
        'env_password_set': bool(ENV_APP_PASSWORD),
        'env_google_creds_set': bool(ENV_GOOGLE_CREDENTIALS),
        'env_ntfy_set': bool(ENV_NTFY_CHANNEL),
    })


@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    global settings

    if request.method == 'POST':
        data = request.get_json()

        # Preserve existing password if masked value is sent back
        if 'email' in data and data['email'].get('app_password') in ['********', '', None]:
            # Keep the existing password, don't overwrite with masked value
            existing_password = settings.get('email', {}).get('app_password')
            if existing_password:
                data['email']['app_password'] = existing_password

        for key in data:
            if isinstance(data[key], dict) and key in settings:
                settings[key].update(data[key])
            else:
                settings[key] = data[key]
        save_settings(settings)
        return jsonify({'success': True})
    
    # Return settings but mask sensitive data for display
    import copy
    safe_settings = copy.deepcopy(settings)
    if 'email' in safe_settings and safe_settings['email'].get('app_password'):
        # Mask password for display - use string not boolean
        safe_settings['email']['app_password_set'] = True
        safe_settings['email']['app_password'] = '********'
    
    return jsonify(safe_settings)


@app.route('/api/hudl/views')
def api_hudl_views():
    """Get view count from Hudl highlight video."""
    try:
        import urllib.request
        import re

        hudl_url = settings.get('athlete', {}).get('highlight_url', '')
        if not hudl_url or 'hudl.com' not in hudl_url:
            return jsonify({'success': False, 'error': 'No Hudl link configured', 'views': 0})

        # Fetch the Hudl page
        req = urllib.request.Request(hudl_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode('utf-8')

        # Look for Hudl's metadata-views class: <div class="metadata-views">28 <span>views</span></div>
        view_match = re.search(r'metadata-views[^>]*>(\d[\d,]*)', html)
        if view_match:
            views = int(view_match.group(1).replace(',', ''))
            return jsonify({'success': True, 'views': views, 'url': hudl_url})

        # Fallback: look for "X views" pattern
        view_match = re.search(r'([\d,]+)\s*views?', html, re.IGNORECASE)
        if view_match:
            views = int(view_match.group(1).replace(',', ''))
            return jsonify({'success': True, 'views': views, 'url': hudl_url})

        return jsonify({'success': False, 'error': 'Could not find view count', 'views': 0})
    except Exception as e:
        logger.error(f"Hudl views error: {e}")
        return jsonify({'success': False, 'error': str(e), 'views': 0})


@app.route('/api/stats')
def api_stats():
    """Get dashboard stats from Supabase."""
    stats = {
        'success': True,
        'total_schools': 0,
        'emails_found': 0,
        'emails_sent': 0,
        'responses': 0,
        'response_rate': 0,
        'followups_due': 0,
        'dms_sent': 0
    }

    if _supabase_db:
        try:
            # School count
            schools = _supabase_db.client.table('schools').select('id', count='exact').execute()
            stats['total_schools'] = schools.count or 0

            # Coaches with emails
            coaches_with_email = _supabase_db.client.table('coaches').select('id', count='exact').not_.is_('email', 'null').execute()
            stats['emails_found'] = coaches_with_email.count or 0

            # Outreach stats
            outreach_stats = _supabase_db.get_outreach_stats()
            stats['emails_sent'] = outreach_stats.get('sent', 0)
            stats['responses'] = outreach_stats.get('replied', 0)
            stats['response_rate'] = outreach_stats.get('response_rate', 0)

            # DM stats
            dm_stats = _supabase_db.get_dm_stats()
            stats['dms_sent'] = dm_stats.get('messaged', 0)

        except Exception as e:
            logger.error(f"Stats error: {e}")

    return jsonify(stats)


@app.route('/api/sheet/debug')
def api_sheet_debug():
    """Debug endpoint to see Supabase table info."""
    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})

    try:
        tables = {}
        for table in ['schools', 'coaches', 'outreach', 'dm_queue', 'templates', 'settings']:
            try:
                r = _supabase_db.client.table(table).select('*', count='exact').limit(0).execute()
                tables[table] = r.count or 0
            except:
                tables[table] = 'error'

        sample_coaches = _supabase_db.client.table('coaches').select('*, schools(name)').limit(3).execute().data
        return jsonify({
            'success': True,
            'source': 'supabase',
            'tables': tables,
            'sample_coaches': sample_coaches,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/schools')
def api_schools():
    try:
        from data.schools import get_school_database
        db = get_school_database()
        schools = [
            {
                'name': s.name,
                'state': s.state,
                'division': s.division,
                'conference': s.conference,
                'public': s.public,
                'enrollment': s.enrollment,
                'region': s.region,
            }
            for s in db.schools
        ]
        return jsonify({'schools': schools})
    except Exception as e:
        logger.error(f"Schools API error: {e}")
        return jsonify({'schools': [], 'error': str(e)})


@app.route('/api/schools/search', methods=['POST'])
def api_schools_search():
    try:
        data = request.get_json()
        query = data.get('query', '')
        division = data.get('division', '')
        state = data.get('state', '')

        results = _supabase_db.search_schools(
            query=query or None,
            division=division or None,
            state=state or None,
            limit=50
        )

        schools = [
            {
                'id': s.get('id', ''),
                'name': s.get('name', ''),
                'state': s.get('state', ''),
                'division': s.get('division', ''),
                'conference': s.get('conference', ''),
            }
            for s in (results or [])
        ]

        return jsonify({'schools': schools, 'query': query})
    except Exception as e:
        logger.error(f"Search error: {e}")
        return jsonify({'schools': [], 'error': str(e)})


@app.route('/api/schools/add-to-sheet', methods=['POST'])
def api_add_schools_to_sheet():
    """Add schools to Supabase database."""
    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})

    try:
        from data.schools import get_school_database

        data = request.get_json()
        school_names = data.get('schools', [])

        db = get_school_database()
        added = 0

        for name in school_names:
            existing = _supabase_db.get_school(name)
            if not existing:
                school = next((s for s in db.schools if s.name == name), None)
                if school:
                    _supabase_db.add_school(
                        name=school.name,
                        division=getattr(school, 'division', None),
                        conference=getattr(school, 'conference', None),
                        state=getattr(school, 'state', None),
                    )
                    added += 1

        add_log(f"Added {added} schools to database", 'success')
        return jsonify({'success': True, 'added': added})
    except Exception as e:
        logger.error(f"Add schools error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/spreadsheet')
def api_spreadsheet():
    """Get coach data with ready_to_send and sent_today stats."""
    if not _supabase_db:
        return jsonify({'rows': [], 'ready_to_send': 0, 'sent_today': 0, 'followups_due': 0, 'error': 'Database not connected'})

    try:
        coaches = _supabase_db.get_all_coaches_with_schools()

        result = []
        ready_to_send = 0
        seen_schools = {}

        for coach in coaches:
            school_info = coach.get('schools') or {}
            school_name = school_info.get('name', '') if isinstance(school_info, dict) else ''
            email = (coach.get('email') or '').strip()
            contacted = coach.get('contacted_date', '')

            if email and '@' in email and not contacted:
                ready_to_send += 1

            # Group coaches by school
            if school_name not in seen_schools:
                seen_schools[school_name] = {
                    'school': school_name,
                    'url': school_info.get('staff_url', '') if isinstance(school_info, dict) else '',
                    'ol_coach': '', 'rc': '', 'ol_email': '', 'rc_email': '',
                }
            entry = seen_schools[school_name]
            role = coach.get('role', '')
            if role == 'ol':
                entry['ol_coach'] = coach.get('name', '')
                entry['ol_email'] = email
            elif role == 'rc':
                entry['rc'] = coach.get('name', '')
                entry['rc_email'] = email

        result = list(seen_schools.values())

        sent_today = 0
        followups_due = 0
        try:
            from enterprise.responses import get_response_tracker
            from datetime import date
            tracker = get_response_tracker()
            today = date.today().isoformat()
            sent_today = sum(1 for e in tracker.sent_emails if e.sent_at.startswith(today))
        except: pass

        try:
            from enterprise.followups import get_followup_manager
            fm = get_followup_manager()
            followups_due = len(fm.get_due_followups())
        except: pass
        
        return jsonify({
            'rows': result, 
            'ready_to_send': ready_to_send,
            'sent_today': sent_today,
            'followups_due': followups_due
        })
    except Exception as e:
        logger.error(f"Spreadsheet error: {e}")
        return jsonify({'rows': [], 'ready_to_send': 0, 'sent_today': 0, 'followups_due': 0, 'error': str(e)})


@app.route('/api/analytics')
def api_analytics():
    try:
        from outreach.email_sender import get_analytics
        analytics = get_analytics()
        return jsonify(analytics.get_stats())
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        return jsonify({'emails_sent': 0, 'schools_contacted': 0, 'responses_received': 0, 'response_rate': 0})


@app.route('/api/analytics/response', methods=['POST'])
def api_record_response():
    try:
        from outreach.email_sender import get_analytics
        
        data = request.get_json()
        school = data.get('school', '')
        response_type = data.get('type', 'response')
        
        analytics = get_analytics()
        
        if response_type == 'offer':
            analytics.record_offer(school)
        else:
            analytics.record_response(school)
        
        add_log(f"Recorded {response_type} from {school}", 'success')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/templates', methods=['GET', 'POST'])
def api_templates():
    """Get all templates or create new user template."""
    try:
        from enterprise.templates import get_template_manager
        manager = get_template_manager()
        
        if request.method == 'POST':
            data = request.get_json()
            template = manager.create_template(
                name=data.get('name', 'New Template'),
                template_type=data.get('template_type', 'rc'),
                subject=data.get('subject', ''),
                body=data.get('body', '')
            )
            # Sync to Supabase
            if SUPABASE_AVAILABLE and _supabase_db:
                try:
                    _supabase_db.create_template(
                        name=data.get('name', 'New Template'),
                        body=data.get('body', ''),
                        subject=data.get('subject'),
                        template_type='email',
                        coach_type=data.get('template_type', 'any'),
                    )
                except Exception as sb_e:
                    logger.warning(f"Supabase template sync error: {sb_e}")
            return jsonify({'success': True, 'template': template.to_dict()})

        templates = manager.get_all_templates()
        return jsonify({'success': True, 'templates': templates})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'templates': []})


@app.route('/api/templates/<template_id>', methods=['GET', 'PUT', 'DELETE'])
def api_template_single(template_id):
    """Get, update, or delete a single template."""
    try:
        from enterprise.templates import get_template_manager
        manager = get_template_manager()
        
        if request.method == 'GET':
            template = manager.get_template(template_id)
            if template:
                return jsonify({'success': True, 'template': template.to_dict()})
            return jsonify({'success': False, 'error': 'Template not found'})
        
        elif request.method == 'PUT':
            data = request.get_json() or {}
            success = manager.update_template(
                template_id,
                name=data.get('name'),
                subject=data.get('subject'),
                body=data.get('body')
            )
            if success:
                template = manager.get_template(template_id)
                # Sync update to Supabase
                if SUPABASE_AVAILABLE and _supabase_db:
                    try:
                        sb_templates = _supabase_db.get_templates()
                        for sbt in sb_templates:
                            if sbt['name'] == template.name:
                                update_fields = {}
                                if data.get('name'):
                                    update_fields['name'] = data['name']
                                if data.get('subject') is not None:
                                    update_fields['subject'] = data['subject']
                                if data.get('body') is not None:
                                    update_fields['body'] = data['body']
                                if update_fields:
                                    _supabase_db.update_template(sbt['id'], **update_fields)
                                break
                    except Exception as sb_e:
                        logger.warning(f"Supabase template update sync error: {sb_e}")
                return jsonify({'success': True, 'template': template.to_dict()})
            return jsonify({'success': False, 'error': 'Failed to update template'})

        elif request.method == 'DELETE':
            # Get template name before deleting for Supabase sync
            template = manager.get_template(template_id)
            template_name = template.name if template else None
            success = manager.delete_template(template_id)
            if success and SUPABASE_AVAILABLE and _supabase_db and template_name:
                try:
                    sb_templates = _supabase_db.get_templates()
                    for sbt in sb_templates:
                        if sbt['name'] == template_name:
                            _supabase_db.delete_template(sbt['id'])
                            break
                except Exception as sb_e:
                    logger.warning(f"Supabase template delete sync error: {sb_e}")
            return jsonify({'success': success})
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/twitter/connect', methods=['POST'])
def api_twitter_connect():
    try:
        from outreach.twitter_sender import get_twitter_sender
        sender = get_twitter_sender()
        if sender.open_login_page():
            return jsonify({'success': True, 'message': 'Browser opened for login'})
        return jsonify({'success': False, 'error': 'Failed to open browser'})
    except ImportError:
        return jsonify({'success': False, 'error': 'Twitter module not available. Install selenium.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/twitter/status')
def api_twitter_status():
    try:
        from outreach.twitter_sender import get_twitter_sender
        sender = get_twitter_sender()
        logged_in = sender.check_logged_in()
        stats = sender.get_stats()
        return jsonify({
            'logged_in': logged_in,
            'sent_today': stats.get('sent_today', 0),
            'daily_limit': stats.get('daily_limit', 20),
            'total_sent': stats.get('total_sent', 0)
        })
    except ImportError:
        return jsonify({'logged_in': False, 'error': 'Twitter module not available'})
    except Exception as e:
        return jsonify({'logged_in': False, 'error': str(e)})


@app.route('/api/twitter/coaches')
def api_twitter_coaches():
    """Get coaches with Twitter handles from Supabase."""
    if not _supabase_db:
        return jsonify({'coaches': []})

    try:
        all_coaches = _supabase_db.get_all_coaches_with_schools()
        coaches = []
        for c in all_coaches:
            twitter = (c.get('twitter') or '').strip()
            if not twitter:
                continue
            school_info = c.get('schools') or {}
            school_name = school_info.get('name', '') if isinstance(school_info, dict) else ''
            coaches.append({
                'school': school_name,
                'name': c.get('name', ''),
                'handle': twitter,
                'type': c.get('role', 'ol').upper(),
                'dm_sent': False
            })

        return jsonify({'coaches': coaches})
    except Exception as e:
        logger.error(f"Twitter coaches error: {e}")
        return jsonify({'coaches': [], 'error': str(e)})


# ============================================================================
# DM WORKFLOW ROUTES
# ============================================================================

@app.route('/api/dm/queue')
def api_dm_queue():
    """Get coaches who need DMs (have Twitter, not yet DM'd, haven't replied)."""
    if not _supabase_db:
        return jsonify({'queue': [], 'sent': 0, 'no_handle': 0, 'replied': 0})

    try:
        import re
        def clean_twitter_handle(handle):
            if not handle:
                return ''
            handle = handle.strip().lstrip('@')
            if 'twitter.com/' in handle or 'x.com/' in handle:
                match = re.search(r'(?:twitter\.com|x\.com)/(@?[A-Za-z0-9_]+)', handle, re.IGNORECASE)
                if match:
                    handle = match.group(1).lstrip('@')
            handle = handle.split('/')[0].split('?')[0].split('#')[0]
            handle = re.sub(r'[^\w].*$', '', handle)
            if handle and re.match(r'^[A-Za-z0-9_]{1,30}$', handle):
                return handle
            return ''

        all_coaches = _supabase_db.get_all_coaches_with_schools()
        dm_stats = _supabase_db.get_dm_stats()

        queue = []
        sent_count = 0
        no_handle = 0
        replied = 0
        followed_only = 0

        for coach in all_coaches:
            school_info = coach.get('schools') or {}
            school_name = school_info.get('name', '') if isinstance(school_info, dict) else ''
            twitter = clean_twitter_handle(coach.get('twitter', ''))
            name = coach.get('name', '')
            role = (coach.get('role') or 'ol').upper()

            # Check if responded
            if coach.get('responded'):
                replied += 1
                continue

            # Check DM history in Supabase
            if twitter and _supabase_db.was_coach_dmed(twitter):
                sent_count += 1
                continue

            if twitter:
                queue.append({
                    'school': school_name,
                    'coach_name': name or f'{role} Coach',
                    'twitter': twitter,
                    'type': role,
                })
            elif name:
                no_handle += 1

        return jsonify({
            'queue': queue,
            'sent': dm_stats.get('messaged', 0) + sent_count,
            'no_handle': no_handle,
            'replied': replied,
            'followed_only': dm_stats.get('followed', 0),
        })
    except Exception as e:
        logger.error(f"DM queue error: {e}")
        return jsonify({'queue': [], 'error': str(e)})


@app.route('/api/debug/twitter-handles')
def api_debug_twitter():
    """Debug endpoint to see raw Twitter handles from Supabase."""
    if not _supabase_db:
        return jsonify({'error': 'Database not connected'})

    try:
        coaches = _supabase_db.client.table('coaches').select('name, role, twitter, schools(name)').not_.is_('twitter', 'null').limit(10).execute().data
        debug_info = []
        for c in coaches:
            school_info = c.get('schools') or {}
            debug_info.append({
                'school': school_info.get('name', '') if isinstance(school_info, dict) else '',
                'name': c.get('name', ''),
                'role': c.get('role', ''),
                'twitter_raw': c.get('twitter', ''),
                'twitter_url': f"https://x.com/{(c.get('twitter') or '').lstrip('@')}" if c.get('twitter') else '',
            })
        return jsonify({'source': 'supabase', 'sample_coaches': debug_info})
    except Exception as e:
        return jsonify({'error': str(e)})


def clean_school_name(school):
    """Remove state suffix from school name for display to coaches.
    e.g., 'Lincoln University (MO)' -> 'Lincoln University'
    """
    if not school:
        return school
    # Remove (STATE) or (State Name) suffix
    import re
    return re.sub(r'\s*\([^)]+\)\s*$', '', school).strip()


@app.route('/api/dm/message', methods=['POST'])
def api_dm_message():
    """Generate DM message for a coach."""
    global settings
    data = request.get_json() or {}
    coach_name = data.get('coach_name', 'Coach')
    school = clean_school_name(data.get('school', ''))
    
    # Get coach's last name
    coach_last = coach_name.split()[-1] if coach_name else 'Coach'
    
    athlete = settings.get('athlete', {})
    
    # Keelan's preferred DM format
    message = f"""Coach {coach_last}, {athlete.get('name', 'Keelan Underwood')} '{athlete.get('graduation_year', '26')[-2:]} {athlete.get('positions', 'OL')} {athlete.get('height', "6'3")} {athlete.get('weight', '295')}lbs {athlete.get('gpa', '3.0')} GPA
{athlete.get('high_school', 'The Benjamin School')} ({athlete.get('state', 'FL')}). I'd love to be considered as a {school} recruit.
X post with my film is below and recruiting form is already completed.
Would love your take.
Thanks so much.
{athlete.get('name', 'Keelan Underwood')}
{athlete.get('phone', '9107471140')}
{athlete.get('highlight_url', 'https://x.com/underwoodkeelan/status/1995522905841746075?s=46')}"""
    
    return jsonify({'message': message.strip()})


@app.route('/api/dm/mark', methods=['POST'])
def api_dm_mark():
    """Mark a coach's DM status in Supabase."""
    data = request.get_json() or {}
    coach_name = data.get('coach_name', '')
    school = data.get('school', '')
    twitter = data.get('twitter', '').lower().strip().lstrip('@')
    status = data.get('status', 'messaged')

    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})

    try:
        from datetime import datetime
        timestamp = datetime.now().strftime('%m/%d')

        # Find or create DM record
        dm = _supabase_db.find_dm_by_coach_school(coach_name, school)
        if not dm and twitter:
            dm = _supabase_db.find_dm_by_twitter(twitter)
        if not dm:
            _supabase_db.add_to_dm_queue(coach_name, twitter, school)
            dm = _supabase_db.find_dm_by_coach_school(coach_name, school)

        if dm:
            note_map = {
                'messaged': f'DM sent {timestamp}',
                'followed': f'Followed only {timestamp}',
                'skipped': f'Skipped {timestamp}',
            }
            _supabase_db.mark_dm_status(dm['id'], status, notes=note_map.get(status))
            logger.info(f"Marked {coach_name} at {school} as {status}")
            return jsonify({'success': True, 'updated': status, 'school': school})

        return jsonify({'success': False, 'error': f'Could not create DM record for {school}'})
    except Exception as e:
        logger.error(f"DM mark error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/coach/response', methods=['POST'])
def api_coach_response():
    """Mark a coach's response in Supabase."""
    data = request.get_json() or {}
    school = data.get('school', '')
    response_type = data.get('response_type', 'dm_reply')

    if not school:
        return jsonify({'success': False, 'error': 'School name required'})

    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})

    try:
        labels = {
            'dm_reply': 'REPLIED to DM',
            'email_reply': 'REPLIED to email',
            'interested': 'INTERESTED!',
            'not_interested': 'Not interested'
        }
        label = labels.get(response_type, response_type)
        sentiment_map = {
            'interested': 'positive',
            'dm_reply': 'positive',
            'email_reply': 'neutral',
            'not_interested': 'negative',
        }

        # Find coaches for this school
        coaches = _supabase_db.get_coaches_for_school(school)
        if not coaches:
            return jsonify({'success': False, 'error': f'School "{school}" not found'})

        # Mark all coaches at this school as responded
        for coach in coaches:
            _supabase_db.mark_coach_responded(coach['id'], sentiment=sentiment_map.get(response_type, 'neutral'))
            coach_email = coach.get('email', '')
            if coach_email:
                _supabase_db.track_reply(coach_email, sentiment=sentiment_map.get(response_type, 'neutral'), snippet=label)

        return jsonify({'success': True, 'school': school, 'status': label})
    except Exception as e:
        logger.error(f"Coach response error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/connections/sheets', methods=['POST'])
def api_connections_sheets():
    global settings
    
    try:
        data = request.get_json()
        creds_json = data.get('credentials', '')
        sheet_name = data.get('spreadsheet_name', 'bardeen')
        
        if creds_json:
            creds = json.loads(creds_json)
            with open(CREDENTIALS_FILE, 'w') as f:
                json.dump(creds, f)
            
            settings['sheets']['credentials_configured'] = True
            settings['sheets']['spreadsheet_name'] = sheet_name
            save_settings(settings)
            
            return jsonify({'success': True})
        
        return jsonify({'success': False, 'error': 'No credentials provided'})
    except json.JSONDecodeError:
        return jsonify({'success': False, 'error': 'Invalid JSON'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/email/connection-test', methods=['POST'])
def api_email_connection_test():
    """Test email connection without sending"""
    try:
        from outreach.email_sender import test_email_connection
        
        data = request.get_json()
        email_addr = data.get('email', settings['email'].get('email_address', ''))
        password = data.get('password', settings['email'].get('app_password', ''))
        
        if not email_addr or not password:
            return jsonify({'success': False, 'error': 'Email and password required'})
        
        success, message = test_email_connection(email_addr, password)
        return jsonify({'success': success, 'message': message})
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/email/check-responses', methods=['POST'])
def api_check_responses():
    """Check Gmail inbox for responses from coaches using Gmail API"""
    try:
        # Use Gmail API if available
        if has_gmail_api():
            logger.info("Checking responses via Gmail API")

            # Get coaches we've emailed from Supabase
            if not SUPABASE_AVAILABLE or not _supabase_db:
                return jsonify({'success': False, 'error': 'Database not connected'})

            # Get all coaches that have outreach records (i.e. we've emailed them)
            try:
                sent_outreach = _supabase_db.get_sent_outreach()
            except Exception:
                sent_outreach = []

            coach_emails = []
            seen_emails = set()
            for o in sent_outreach:
                email = (o.get('coach_email') or '').strip()
                if email and '@' in email and email not in seen_emails:
                    seen_emails.add(email)
                    coach_emails.append({
                        'email': email,
                        'school': o.get('school_name', ''),
                        'type': o.get('coach_role', 'rc'),
                        'coach_name': o.get('coach_name', '')
                    })

            logger.info(f"Found {len(coach_emails)} coaches we've emailed")

            if not coach_emails:
                return jsonify({
                    'success': True,
                    'message': 'No coaches found with sent emails.',
                    'responses': [],
                    'total_checked': 0
                })
            
            logger.info(f"Found {len(coach_emails)} coaches we've emailed, checking inbox...")
            
            # Auto-reply patterns to filter out
            auto_reply_patterns = [
                'out of office', 'out-of-office', 'automatic reply', 'auto-reply', 'autoreply',
                'delivery status', 'delivery failed', 'undeliverable', 'returned mail',
                'mail delivery', 'failure notice', 'delayed:', 'could not be delivered',
                'away from', 'on vacation', 'currently out', 'be back', 'return on',
                'no longer at', 'no longer with', 'mailer-daemon', 'postmaster'
            ]

            def is_auto_reply(subject, snippet=''):
                text = (subject + ' ' + snippet).lower()
                return any(pattern in text for pattern in auto_reply_patterns)

            # Search inbox for replies from these coaches
            responses = []
            service = get_gmail_service()
            if service:
                for coach in coach_emails:
                    try:
                        # Search for emails from this coach
                        query = f"from:{coach['email']}"
                        results = service.users().messages().list(userId='me', q=query, maxResults=5).execute()
                        messages = results.get('messages', [])

                        if messages:
                            # Get the most recent message
                            msg = service.users().messages().get(userId='me', id=messages[0]['id'], format='metadata', metadataHeaders=['Subject', 'Date']).execute()
                            headers_dict = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                            subject = headers_dict.get('Subject', 'No subject')
                            snippet = msg.get('snippet', '')[:100]

                            # Skip auto-replies
                            if is_auto_reply(subject, snippet):
                                continue

                            responses.append({
                                'email': coach['email'],
                                'school': coach['school'],
                                'type': coach['type'],
                                'subject': subject,
                                'date': headers_dict.get('Date', ''),
                                'snippet': snippet
                            })
                            logger.info(f"Found response from {coach['email']} at {coach['school']}")

                            # Mark response in Supabase
                            try:
                                if SUPABASE_AVAILABLE and _supabase_db:
                                    c = _supabase_db.find_coach_by_email(coach['email'])
                                    if c:
                                        _supabase_db.mark_coach_responded(c['id'])
                                    _supabase_db.track_reply(coach['email'], coach['school'])
                            except Exception as e:
                                logger.warning(f"Could not mark response: {e}")

                    except Exception as e:
                        logger.warning(f"Error checking {coach['email']}: {e}")
            
            # Cache responses for display
            global cached_responses
            cached_responses = responses
            
            return jsonify({
                'success': True,
                'responses': responses,
                'total_checked': len(coach_emails),
                'total_responses': len(responses),
                'method': 'Gmail API'
            })
        
        else:
            # Fallback to IMAP (may not work on Railway)
            return jsonify({'success': False, 'error': 'Gmail API not configured. Add GMAIL_REFRESH_TOKEN to Railway.'})
    
    except Exception as e:
        logger.error(f"Response check error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/email/test-tracking')
def api_test_tracking():
    """Test if response tracking is working."""
    result = {
        'gmail_connected': False,
        'imap_working': False,
        'emails_sent': 0,
        'responses_found': 0,
        'error': None
    }
    
    try:
        # Check Gmail API first (works on Railway)
        if has_gmail_api():
            result['gmail_connected'] = True
            result['using_gmail_api'] = True
            
            service = get_gmail_service()
            if service:
                result['imap_working'] = True  # Gmail API is working
                
                # Count emails in inbox
                try:
                    results = service.users().messages().list(userId='me', maxResults=10).execute()
                    result['inbox_count'] = results.get('resultSizeEstimate', 0)
                except Exception as e:
                    result['gmail_api_error'] = str(e)
                
                # Get response count from cache
                global cached_responses
                if 'cached_responses' in globals() and cached_responses:
                    result['responses_found'] = len(cached_responses)
            else:
                result['error'] = 'Gmail API configured but could not connect'
        else:
            result['error'] = 'Gmail API not configured. Add GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN to Railway.'
            result['gmail_api_configured'] = False
        
        # Get sent count from Supabase
        if SUPABASE_AVAILABLE and _supabase_db:
            try:
                stats = _supabase_db.get_outreach_stats()
                result['emails_sent'] = stats.get('sent', 0)
            except Exception as e:
                result['db_error'] = str(e)
        
        return jsonify(result)
    
    except Exception as e:
        result['error'] = str(e)
        return jsonify(result)


# ============================================================================
# CLOUD EMAIL STORAGE API
# ============================================================================

@app.route('/api/cloud-emails/sync', methods=['POST'])
def api_cloud_emails_sync():
    """Cloud sync is no longer needed - Supabase is the single source of truth."""
    return jsonify({'success': True, 'message': 'Supabase is the data store - no sync needed', 'synced': 0})


@app.route('/api/cloud-emails/stats')
def api_cloud_emails_stats():
    """Get outreach statistics from Supabase."""
    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})
    try:
        stats = _supabase_db.get_outreach_stats()
        return jsonify({'success': True, **stats})
    except Exception as e:
        logger.error(f"Cloud stats error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/cloud-emails/successful')
def api_cloud_emails_successful():
    """Get emails that received positive responses."""
    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})
    try:
        responses = _supabase_db.get_recent_responses(limit=50)
        return jsonify({'success': True, 'emails': responses, 'count': len(responses)})
    except Exception as e:
        logger.error(f"Error getting successful emails: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/cloud-emails/pending')
def api_cloud_emails_pending():
    """Get pending outreach that hasn't been sent yet."""
    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})
    try:
        pending = _supabase_db.get_pending_outreach(limit=100)
        return jsonify({'success': True, 'emails': pending, 'count': len(pending)})
    except Exception as e:
        logger.error(f"Error getting pending emails: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/email/tomorrow-preview')
def api_email_tomorrow_preview():
    """Preview how many emails will be sent tomorrow."""
    try:
        if not _supabase_db:
            return jsonify({'success': False, 'error': 'Database not connected'})

        daily_limit = settings.get('email', {}).get('auto_send_count', 25)
        days_between = settings.get('email', {}).get('days_between_emails', 3)

        coaches_ready = _supabase_db.get_coaches_to_email(limit=500, days_between=days_between)
        total_ready = len(coaches_ready)

        # Check for pregenerated AI emails
        from pathlib import Path
        import json
        pregenerated_file = Path.home() / '.coach_outreach' / 'pregenerated_emails.json'
        pregenerated_schools = set()
        if pregenerated_file.exists():
            try:
                with open(pregenerated_file) as f:
                    pregenerated = json.load(f)
                pregenerated_schools = set(s.lower() for s in pregenerated.keys())
            except:
                pass

        ai_count = sum(1 for c in coaches_ready if c.get('school_name', '').lower() in pregenerated_schools)
        template_count = total_ready - ai_count

        will_send = min(total_ready, daily_limit)
        will_send_ai = min(ai_count, will_send)
        will_send_template = will_send - will_send_ai

        return jsonify({
            'success': True,
            'total': total_ready,
            'ai': ai_count,
            'template': template_count,
            'will_send': will_send,
            'will_send_ai': will_send_ai,
            'will_send_template': will_send_template,
            'limit': daily_limit
        })

    except Exception as e:
        logger.error(f"Tomorrow preview error: {e}")
        return jsonify({'success': False, 'error': str(e)})


def mark_coach_replied_in_sheet(sheet, coach_email: str, school_name: str, sentiment: str = 'positive'):
    """Mark a coach as REPLIED in Supabase and update all tracking systems."""
    try:
        # Track reply in Supabase
        if _supabase_db:
            try:
                _supabase_db.track_reply(coach_email, sentiment=sentiment, snippet=f"Reply from {school_name}")
                # Also mark coach as responded
                coach = _supabase_db.find_coach_by_email(coach_email)
                if coach:
                    _supabase_db.mark_coach_responded(coach['id'], sentiment=sentiment)
            except Exception as sb_e:
                logger.warning(f"Supabase track_reply error: {sb_e}")

        # Reset scheduler AI cycle for this coach
        try:
            from scheduler.email_scheduler import SchedulerState
            state = SchedulerState('email_scheduler_state.json')
            state.mark_response_received(coach_email)
        except Exception as e:
            logger.warning(f"Could not reset AI cycle: {e}")

        # Legacy sheet handling - skip if no sheet passed
        if not sheet:
            return
        try:
            all_data = sheet.get_all_values()
        except:
            return
        if len(all_data) < 2:
            return

        headers = all_data[0]

        def find_col(keywords):
            for i, h in enumerate(headers):
                h_lower = h.lower()
                for kw in keywords:
                    if kw in h_lower:
                        return i
            return -1

        school_col = find_col(['school'])
        rc_email_col = find_col(['rc email'])
        ol_email_col = find_col(['ol email', 'oc email'])
        rc_contacted_col = find_col(['rc contacted'])
        ol_contacted_col = find_col(['ol contacted', 'oc contacted'])
        rc_notes_col = find_col(['rc notes'])
        ol_notes_col = find_col(['ol notes', 'oc notes'])
        rc_responded_col = find_col(['rc responded'])
        ol_responded_col = find_col(['ol responded', 'oc responded'])

        logger.info(f"mark_coach_replied columns: school={school_col}, rc_email={rc_email_col}, ol_email={ol_email_col}, rc_responded={rc_responded_col}, ol_responded={ol_responded_col}")

        coach_email_lower = coach_email.lower().strip()
        school_name_lower = school_name.lower().strip() if school_name else ''

        def mark_row_replied(row_idx, contacted_col, notes_col, responded_col, coach_type, row_school):
            """Helper to mark a row as replied."""
            from datetime import datetime
            now_str = datetime.now().strftime('%m/%d/%Y')
            write_count = 0

            # Write to the dedicated "responded" column if it exists
            if responded_col >= 0:
                try:
                    sheet.update_cell(row_idx, responded_col + 1, f"Yes - {now_str}")
                    write_count += 1
                    logger.info(f"Wrote responded column (row {row_idx}, col {responded_col + 1}) for {row_school}")
                except Exception as e:
                    logger.error(f"FAILED to write responded column for {row_school}: {e}")

            # Also mark the contacted column with REPLIED
            if contacted_col >= 0:
                try:
                    current = sheet.cell(row_idx, contacted_col + 1).value or ''
                    if 'REPLIED' not in current.upper():
                        new_val = 'REPLIED' if not current else current + ', REPLIED'
                        sheet.update_cell(row_idx, contacted_col + 1, new_val)
                        write_count += 1
                        logger.info(f"Wrote contacted column (row {row_idx}, col {contacted_col + 1}) = '{new_val}' for {row_school}")
                except Exception as e:
                    logger.error(f"FAILED to write contacted column for {row_school}: {e}")

            if notes_col >= 0:
                try:
                    current = sheet.cell(row_idx, notes_col + 1).value or ''
                    note = f"Response received {now_str} ({sentiment})"
                    if 'response received' not in current.lower():
                        new_val = note if not current else note + '; ' + current
                        sheet.update_cell(row_idx, notes_col + 1, new_val)
                        write_count += 1
                        logger.info(f"Wrote notes column (row {row_idx}, col {notes_col + 1}) for {row_school}")
                except Exception as e:
                    logger.error(f"FAILED to write notes column for {row_school}: {e}")

            logger.info(f"Marked {coach_type} at {row_school} as REPLIED ({sentiment}) - {write_count} columns updated")

        # Pass 1: Try exact email match
        for row_idx, row in enumerate(all_data[1:], start=2):
            row_school = row[school_col].strip() if school_col >= 0 and school_col < len(row) else ''

            # Check RC email
            rc_email = row[rc_email_col].strip().lower() if rc_email_col >= 0 and rc_email_col < len(row) else ''
            if rc_email and rc_email == coach_email_lower:
                mark_row_replied(row_idx, rc_contacted_col, rc_notes_col, rc_responded_col, 'RC', row_school)
                return

            # Check OL email
            ol_email = row[ol_email_col].strip().lower() if ol_email_col >= 0 and ol_email_col < len(row) else ''
            if ol_email and ol_email == coach_email_lower:
                mark_row_replied(row_idx, ol_contacted_col, ol_notes_col, ol_responded_col, 'OL', row_school)
                return

        # Pass 2: Try fuzzy email match (handles hidden chars, encoding issues)
        for row_idx, row in enumerate(all_data[1:], start=2):
            row_school = row[school_col].strip() if school_col >= 0 and school_col < len(row) else ''

            rc_email = row[rc_email_col].strip().lower() if rc_email_col >= 0 and rc_email_col < len(row) else ''
            if rc_email and (coach_email_lower in rc_email or rc_email in coach_email_lower):
                logger.info(f"Fuzzy email match: '{coach_email_lower}' ~ '{rc_email}' at {row_school}")
                mark_row_replied(row_idx, rc_contacted_col, rc_notes_col, rc_responded_col, 'RC', row_school)
                return

            ol_email = row[ol_email_col].strip().lower() if ol_email_col >= 0 and ol_email_col < len(row) else ''
            if ol_email and (coach_email_lower in ol_email or ol_email in coach_email_lower):
                logger.info(f"Fuzzy email match: '{coach_email_lower}' ~ '{ol_email}' at {row_school}")
                mark_row_replied(row_idx, ol_contacted_col, ol_notes_col, ol_responded_col, 'OL', row_school)
                return

        # Pass 3: Match by school name as last resort
        if school_name_lower:
            for row_idx, row in enumerate(all_data[1:], start=2):
                row_school = row[school_col].strip() if school_col >= 0 and school_col < len(row) else ''
                if row_school.lower() == school_name_lower:
                    # Mark whichever coach column has an email
                    rc_email = row[rc_email_col].strip() if rc_email_col >= 0 and rc_email_col < len(row) else ''
                    ol_email = row[ol_email_col].strip() if ol_email_col >= 0 and ol_email_col < len(row) else ''
                    if rc_email and '@' in rc_email:
                        logger.info(f"School-name match for {school_name} (email: {coach_email} not found, marking RC)")
                        mark_row_replied(row_idx, rc_contacted_col, rc_notes_col, rc_responded_col, 'RC', row_school)
                    if ol_email and '@' in ol_email:
                        logger.info(f"School-name match for {school_name} (email: {coach_email} not found, marking OL)")
                        mark_row_replied(row_idx, ol_contacted_col, ol_notes_col, ol_responded_col, 'OL', row_school)
                    return

        logger.warning(f"Could not find {coach_email} ({school_name}) in sheet to mark as replied")
    except Exception as e:
        logger.error(f"Error marking coach replied: {e}")
        import traceback
        logger.error(traceback.format_exc())


@app.route('/api/email/settings', methods=['GET', 'POST'])
def api_email_settings():
    """Get or update email settings including enterprise features"""
    global settings
    
    if request.method == 'POST':
        data = request.get_json()
        
        if 'email' not in settings:
            settings['email'] = {}
        
        # Update email settings
        for key in ['email_address', 'app_password', 'max_per_day', 'delay_seconds',
                    'use_randomized_templates', 'enable_followups']:
            if key in data:
                settings['email'][key] = data[key]
        
        save_settings(settings)
        return jsonify({'success': True})
    
    # GET - return current settings (without password)
    email_settings = settings.get('email', {}).copy()
    email_settings['app_password'] = '********' if email_settings.get('app_password') else ''
    email_settings.setdefault('use_randomized_templates', True)
    email_settings.setdefault('enable_followups', True)
    
    return jsonify({'success': True, 'settings': email_settings})


@app.route('/api/run', methods=['POST'])
def api_run():
    global active_task, task_thread, stop_requested
    
    if active_task is not None:
        return jsonify({'error': 'Task already running'})
    
    data = request.get_json()
    tool = data.get('tool', 'staff')
    
    stop_requested = False
    active_task = tool
    
    task_thread = threading.Thread(target=run_task, args=(tool,), daemon=True)
    task_thread.start()
    
    return jsonify({'success': True, 'tool': tool})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    global stop_requested
    stop_requested = True
    add_log("Stop requested...", 'warning')
    return jsonify({'success': True})


@app.route('/api/events')
def api_events():
    def generate():
        while True:
            try:
                evt = event_queue.get(timeout=30)
                yield f"data: {json.dumps(evt)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


# ============================================================================
# TASK RUNNERS
# ============================================================================

def run_task(tool: str):
    global active_task, stop_requested
    
    try:
        event_queue.put({'type': 'progress', 'data': {'percent': 0, 'title': 'Starting...'}})
        
        if tool == 'staff':
            run_staff_scraper()
        elif tool == 'twitter':
            run_twitter_scraper()
        elif tool == 'email':
            run_email_scraper()
        elif tool == 'pipeline':
            run_pipeline()
        elif tool == 'email_send':
            run_email_send()
        else:
            add_log(f"Unknown tool: {tool}", 'error')
    except Exception as e:
        add_log(f"Error: {e}", 'error')
        logger.error(traceback.format_exc())
    finally:
        active_task = None
        event_queue.put({'type': 'done'})


def run_staff_scraper():
    global stop_requested
    
    if not HAS_SCRAPER:
        add_log("Staff scraper not available - install selenium", 'error')
        return
    
    add_log("Starting staff name scraper...")
    
    found = 0
    total = 0
    
    def callback(event, data):
        nonlocal found, total
        
        if stop_requested:
            raise KeyboardInterrupt()
        
        if event == 'schools_found':
            total = data.get('count', 0)
            add_log(f"Found {total} schools to process")
        elif event == 'processing':
            current = data.get('current', 0)
            school = data.get('school', '?')
            add_log(f"[{current}/{total}] {school}")
            if total > 0:
                event_queue.put({'type': 'progress', 'data': {'percent': int((current/total)*100)}})
        elif event == 'school_processed':
            if data.get('ol_found') or data.get('rc_found'):
                found += 1
                add_log(f"✓ {data.get('school', '?')}", 'success')
                # Sync scraped data to Supabase
                if SUPABASE_AVAILABLE and _supabase_db:
                    try:
                        school_name = data.get('school', '')
                        if school_name:
                            _supabase_db.add_school(name=school_name, staff_url=data.get('url'))
                        if data.get('rc_found') and data.get('rc_name'):
                            _supabase_db.add_coach(school_name, data['rc_name'], 'rc',
                                                   email=data.get('rc_email'), twitter=data.get('rc_twitter'))
                        if data.get('ol_found') and data.get('ol_name'):
                            _supabase_db.add_coach(school_name, data['ol_name'], 'ol',
                                                   email=data.get('ol_email'), twitter=data.get('ol_twitter'))
                    except Exception as sb_e:
                        logger.warning(f"Supabase scraper sync error: {sb_e}")

    try:
        config = ScraperConfig(reverse=settings['scraper'].get('start_from_bottom', False))
        scraper = CoachScraper(config)
        scraper.run(callback=callback)
        add_log(f"Done! Found {found} coaches", 'success')
    except KeyboardInterrupt:
        add_log("Stopped by user")
    except Exception as e:
        add_log(f"Scraper error: {e}", 'error')


def run_twitter_scraper():
    global stop_requested
    
    if not HAS_TWITTER_SCRAPER:
        add_log("Twitter scraper not available", 'error')
        return
    
    add_log("Starting Twitter handle scraper...")
    
    found = 0
    total = 0
    
    def callback(event, data):
        nonlocal found, total
        
        if stop_requested:
            raise KeyboardInterrupt()
        
        if event == 'schools_found':
            total = data.get('count', 0)
            add_log(f"Found {total} schools needing Twitter handles")
        elif event == 'processing':
            current = data.get('current', 0)
            if total > 0:
                event_queue.put({'type': 'progress', 'data': {'percent': int((current/total)*100)}})
        elif event == 'found':
            found += 1
            add_log(f"✓ {data.get('school')} - {data.get('type')}: {data.get('handle')}", 'success')
            # Sync twitter handle to Supabase coach record
            if SUPABASE_AVAILABLE and _supabase_db:
                try:
                    school_name = data.get('school', '')
                    handle = data.get('handle', '')
                    coach_type = data.get('type', '').lower()
                    role = 'rc' if 'rc' in coach_type else 'ol'
                    coaches = _supabase_db.get_coaches_for_school(school_name)
                    for c in coaches:
                        if c.get('role') == role and not c.get('twitter'):
                            _supabase_db.update_coach(c['id'], twitter=handle)
                            break
                except Exception as sb_e:
                    logger.warning(f"Supabase twitter sync error: {sb_e}")

    try:
        config = TwitterScraperConfig(reverse=settings['scraper'].get('start_from_bottom', False))
        scraper = TwitterScraper(config)
        scraper.run(callback=callback)
        add_log(f"Done! Found {found} handles", 'success')
    except KeyboardInterrupt:
        add_log("Stopped by user")
    except Exception as e:
        add_log(f"Twitter scraper error: {e}", 'error')


def run_email_scraper():
    global stop_requested
    
    if not HAS_EMAIL_SCRAPER:
        add_log("Email scraper not available", 'error')
        return
    
    add_log("Starting email scraper...")
    
    found = 0
    total = 0
    
    def callback(event, data):
        nonlocal found, total
        
        if stop_requested:
            raise KeyboardInterrupt()
        
        if event == 'schools_found':
            total = data.get('count', 0)
            add_log(f"Found {total} schools needing emails")
        elif event == 'processing':
            current = data.get('current', 0)
            if total > 0:
                event_queue.put({'type': 'progress', 'data': {'percent': int((current/total)*100)}})
        elif event == 'found':
            found += 1
            add_log(f"✓ {data.get('school')} - {data.get('email')}", 'success')
    
    try:
        config = EmailScraperConfig(reverse=settings['scraper'].get('start_from_bottom', False))
        scraper = EmailScraper(config)
        scraper.run(callback=callback)
        add_log(f"Done! Found {found} emails", 'success')
    except KeyboardInterrupt:
        add_log("Stopped by user")
    except Exception as e:
        add_log(f"Email scraper error: {e}", 'error')


def run_pipeline():
    add_log("Starting full pipeline...")
    run_staff_scraper()
    if not stop_requested:
        run_email_scraper()
    if not stop_requested:
        run_twitter_scraper()
    add_log("Pipeline complete!", 'success')


def run_email_send():
    global stop_requested

    add_log("Starting email campaign...")

    try:
        # Use the /api/email/send endpoint which already uses Supabase
        with app.test_client() as client:
            limit = settings['email'].get('max_per_day', 50)
            response = client.post('/api/email/send', json={'limit': limit}, content_type='application/json')
            result = response.get_json()

            if result.get('success') or result.get('sent', 0) > 0:
                add_log(f"Done! Sent: {result.get('sent', 0)}, Errors: {result.get('errors', 0)}", 'success')
            elif result.get('error'):
                add_log(f"Error: {result['error']}", 'error')
            else:
                add_log("No coaches to email", 'warning')

    except Exception as e:
        add_log(f"Email error: {e}", 'error')
        logger.error(traceback.format_exc())


# ============================================================================
# NEW STREAMLINED API ROUTES
# ============================================================================

@app.route('/api/responses/recent')
def api_responses_recent():
    """Get recent responses - returns cached responses from last check."""
    try:
        # Return cached responses if available
        global cached_responses
        if 'cached_responses' not in globals():
            cached_responses = []
        
        if cached_responses:
            return jsonify({'success': True, 'responses': cached_responses})
        
        # If no cache, return empty and suggest checking
        return jsonify({'success': True, 'responses': [], 'message': 'Click "Check for Responses" to scan inbox'})
    except Exception as e:
        logger.error(f"Responses error: {e}")
        return jsonify({'success': False, 'error': str(e), 'responses': []})


@app.route('/api/email/queue-status')
def api_email_queue_status():
    """Get detailed email queue status from Supabase."""
    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})

    try:
        counts = _supabase_db.get_email_queue_status()
        new_coaches = counts.get('new', 0)
        followup_count = counts.get('followup_1', 0) + counts.get('followup_2', 0)
        ready_count = new_coaches + followup_count
        responded_count = counts.get('replied', 0)

        return jsonify({
            'success': True,
            'ready': ready_count,
            'new_coaches': new_coaches,
            'followups_due': followup_count,
            'responded': responded_count,
            'total_coaches': counts.get('total_with_email', 0),
            'summary': f"{ready_count} coaches ready ({new_coaches} new intros, {followup_count} follow-ups). {responded_count} have responded."
        })
    except Exception as e:
        logger.error(f"Queue status error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/email/scan-past-responses', methods=['POST'])
def api_scan_past_responses():
    """Scan Gmail for past responses and mark them in Supabase."""
    try:
        if not has_gmail_api():
            return jsonify({'success': False, 'error': 'Gmail API not configured. Add GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN to Railway.'})

        if not _supabase_db:
            return jsonify({'success': False, 'error': 'Database not connected'})

        # Get all coaches with emails who haven't responded yet
        all_coaches = _supabase_db.get_all_coaches_with_schools()
        coach_emails = []
        for coach in all_coaches:
            email = (coach.get('email') or '').strip().lower()
            if not email or '@' not in email:
                continue
            if coach.get('responded'):
                continue
            school_info = coach.get('schools') or {}
            school_name = school_info.get('name', '') if isinstance(school_info, dict) else ''
            coach_emails.append({
                'email': email,
                'school': school_name,
                'type': (coach.get('role') or 'ol').upper(),
                'coach_id': coach['id'],
            })

        logger.info(f"Scanning responses for {len(coach_emails)} coach emails")
        
        service = get_gmail_service()
        if not service:
            return jsonify({'success': False, 'error': 'Could not connect to Gmail API'})
        
        responses_found = []
        marked_count = 0
        checked_count = 0

        # Auto-reply patterns to filter out
        auto_reply_patterns = [
            'out of office', 'out-of-office', 'automatic reply', 'auto-reply', 'autoreply',
            'delivery status', 'delivery failed', 'undeliverable', 'returned mail',
            'mail delivery', 'failure notice', 'delayed:', 'could not be delivered',
            'away from', 'on vacation', 'currently out', 'be back', 'return on',
            'no longer at', 'no longer with', 'mailer-daemon', 'postmaster'
        ]

        def is_auto_reply(subject, snippet=''):
            """Check if email is an auto-reply or bounce."""
            text = (subject + ' ' + snippet).lower()
            return any(pattern in text for pattern in auto_reply_patterns)

        for coach in coach_emails:
            try:
                checked_count += 1
                # Search for emails FROM this coach (they replied to us) - search past 90 days
                query = f"from:{coach['email']} newer_than:90d"
                results = service.users().messages().list(userId='me', q=query, maxResults=5).execute()
                messages = results.get('messages', [])

                if messages:
                    # Get message details with snippet
                    msg = service.users().messages().get(userId='me', id=messages[0]['id'], format='metadata', metadataHeaders=['Subject', 'Date']).execute()
                    headers_dict = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                    snippet = msg.get('snippet', '')[:200]
                    subject = headers_dict.get('Subject', '')

                    # Skip auto-replies and bounces
                    if is_auto_reply(subject, snippet):
                        logger.info(f"Skipping auto-reply from {coach['school']}: {subject[:50]}")
                        continue

                    responses_found.append({
                        'school': coach['school'],
                        'email': coach['email'],
                        'type': coach['type'],
                        'subject': subject,
                        'snippet': snippet,
                        'date': headers_dict.get('Date', '')
                    })
                    
                    # Mark in Supabase
                    try:
                        _supabase_db.mark_coach_responded(coach['coach_id'], sentiment='positive')
                        _supabase_db.track_reply(coach['email'], sentiment='positive', snippet=snippet[:200] if snippet else subject[:200])
                        marked_count += 1
                        logger.info(f"Marked response from {coach['school']} ({coach['type']})")
                    except Exception as e:
                        logger.warning(f"Could not mark response for {coach['school']}: {e}")
                        
            except Exception as e:
                logger.warning(f"Error checking {coach['email']}: {e}")
        
        global cached_responses
        cached_responses = responses_found
        
        logger.info(f"Scan complete: checked {checked_count}, found {len(responses_found)} responses, marked {marked_count}")
        
        return jsonify({
            'success': True,
            'checked': checked_count,
            'responses_found': len(responses_found),
            'marked_in_sheet': marked_count,
            'responses': responses_found,
            'message': f"Checked {checked_count} coaches. Found {len(responses_found)} responses, marked {marked_count} new in sheet."
        })
    except Exception as e:
        logger.error(f"Scan past responses error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/sheet/cleanup', methods=['POST'])
def api_sheet_cleanup():
    """Clean up coach notes - remove duplicates."""
    if not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})

    try:
        coaches = _supabase_db.client.table('coaches').select('id, notes').not_.is_('notes', 'null').execute().data
        fixes_made = 0

        for coach in coaches:
            notes = coach.get('notes', '')
            if notes and ';' in notes:
                parts = [p.strip() for p in notes.split(';') if p.strip()]
                seen = set()
                unique_parts = []
                for p in parts:
                    p_lower = p.lower()
                    if p_lower not in seen:
                        seen.add(p_lower)
                        unique_parts.append(p)
                new_notes = '; '.join(unique_parts)
                if new_notes != notes:
                    _supabase_db.client.table('coaches').update({'notes': new_notes}).eq('id', coach['id']).execute()
                    fixes_made += 1

        return jsonify({
            'success': True,
            'fixes_made': fixes_made,
            'message': f"Cleaned up {fixes_made} coach notes"
        })
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/responses/hot-leads')
def api_responses_hot_leads():
    """Get hot leads."""
    try:
        from enterprise.responses import get_response_tracker
        tracker = get_response_tracker()
        leads = tracker.get_hot_leads(10)
        return jsonify({'success': True, 'leads': leads})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'leads': []})


@app.route('/api/responses/by-division')
def api_responses_by_division():
    """Get response stats by division."""
    try:
        from enterprise.responses import get_response_tracker
        tracker = get_response_tracker()
        divisions = tracker.get_stats_by_division()
        return jsonify({'success': True, 'divisions': divisions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'divisions': {}})


@app.route('/api/responses/analyze-sentiment', methods=['POST'])
def api_analyze_sentiment():
    """Analyze sentiment of a response text."""
    data = request.get_json() or {}
    text = data.get('text', '')
    result = analyze_response_sentiment(text)
    return jsonify({'success': True, **result})


@app.route('/api/templates/performance')
def api_templates_performance():
    """Get A/B testing performance stats for templates."""
    try:
        global email_tracking

        # Count emails sent per template and responses received
        template_stats = {}

        for tracking_id, sent_data in email_tracking.get('sent', {}).items():
            template_id = sent_data.get('template_id', 'default')
            if template_id not in template_stats:
                template_stats[template_id] = {
                    'sent': 0,
                    'opened': 0,
                    'responded': 0,
                    'template_name': template_id
                }
            template_stats[template_id]['sent'] += 1

            # Check if opened
            if tracking_id in email_tracking.get('opens', {}) and email_tracking['opens'][tracking_id]:
                template_stats[template_id]['opened'] += 1

            # Check if responded
            if tracking_id in email_tracking.get('responses', {}):
                template_stats[template_id]['responded'] += 1

        # Calculate rates and format for display
        results = []
        for template_id, stats in template_stats.items():
            open_rate = round((stats['opened'] / stats['sent'] * 100) if stats['sent'] > 0 else 0, 1)
            response_rate = round((stats['responded'] / stats['sent'] * 100) if stats['sent'] > 0 else 0, 1)
            results.append({
                'template_id': template_id,
                'template_name': stats['template_name'],
                'sent': stats['sent'],
                'opened': stats['opened'],
                'responded': stats['responded'],
                'open_rate': open_rate,
                'response_rate': response_rate
            })

        # Sort by response rate (best performing first)
        results.sort(key=lambda x: x['response_rate'], reverse=True)

        return jsonify({
            'success': True,
            'templates': results,
            'best_template': results[0]['template_id'] if results else None
        })
    except Exception as e:
        logger.error(f"Template performance error: {e}")
        return jsonify({'success': False, 'error': str(e), 'templates': []})


@app.route('/api/templates/toggle', methods=['POST'])
def api_templates_toggle():
    """Toggle template enabled/disabled."""
    try:
        from enterprise.templates import get_template_manager
        data = request.get_json()
        manager = get_template_manager()
        success = manager.toggle_template(data['id'], data['enabled'])
        # Sync toggle to Supabase
        if success and SUPABASE_AVAILABLE and _supabase_db:
            try:
                t = manager.get_template(data['id'])
                if t:
                    sb_templates = _supabase_db.get_templates()
                    for sbt in sb_templates:
                        if sbt['name'] == t.name:
                            _supabase_db.toggle_template(sbt['id'], data['enabled'])
                            break
            except Exception as sb_e:
                logger.warning(f"Supabase template toggle sync error: {sb_e}")
        return jsonify({'success': success})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/templates/<template_id>', methods=['DELETE'])
def api_templates_delete(template_id):
    """Delete a user template."""
    try:
        from enterprise.templates import get_template_manager
        manager = get_template_manager()
        t = manager.get_template(template_id)
        t_name = t.name if t else None
        success = manager.delete_template(template_id)
        if success and SUPABASE_AVAILABLE and _supabase_db and t_name:
            try:
                sb_templates = _supabase_db.get_templates()
                for sbt in sb_templates:
                    if sbt['name'] == t_name:
                        _supabase_db.delete_template(sbt['id'])
                        break
            except Exception as sb_e:
                logger.warning(f"Supabase template delete sync error: {sb_e}")
        return jsonify({'success': success})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/templates/<template_id>', methods=['PUT'])
def api_templates_update(template_id):
    """Update a template."""
    try:
        from enterprise.templates import get_template_manager
        manager = get_template_manager()
        data = request.get_json() or {}
        
        template = manager.get_template(template_id)
        if not template:
            return jsonify({'success': False, 'error': 'Template not found'})
        
        # Update the template
        if data.get('name'):
            template.name = data['name']
        if 'subject' in data:
            template.subject = data['subject']
        if data.get('body'):
            template.body = data['body']
        
        manager._save()
        # Sync to Supabase
        if SUPABASE_AVAILABLE and _supabase_db:
            try:
                sb_templates = _supabase_db.get_templates()
                old_name = template.name  # name before update
                for sbt in sb_templates:
                    if sbt['name'] == old_name or sbt['name'] == data.get('name', old_name):
                        update_fields = {}
                        if data.get('name'):
                            update_fields['name'] = data['name']
                        if 'subject' in data:
                            update_fields['subject'] = data['subject']
                        if data.get('body'):
                            update_fields['body'] = data['body']
                        if update_fields:
                            _supabase_db.update_template(sbt['id'], **update_fields)
                        break
            except Exception as sb_e:
                logger.warning(f"Supabase template update sync error: {sb_e}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/email/send', methods=['POST'])
def api_email_send():
    """Send emails to coaches from sheet - handles intro and follow-up campaigns."""
    # Reload settings fresh to avoid stale/corrupted data
    current_settings = load_settings()

    data = request.get_json() or {}
    limit = data.get('limit', 10)
    template_id = data.get('template_id')
    campaign_type = data.get('campaign_type', 'intro')  # 'intro', 'followup_1', 'followup_2', 'smart'

    # =========================================================================
    # PAUSE & HOLIDAY MODE CHECKS
    # =========================================================================
    from datetime import date, datetime
    today = date.today()
    email_settings = current_settings.get('email', {})

    # Check if emails are completely paused
    paused_until = email_settings.get('paused_until')
    if paused_until:
        try:
            pause_date = datetime.strptime(paused_until, '%Y-%m-%d').date()
            if today < pause_date:
                days_left = (pause_date - today).days
                return jsonify({
                    'success': False,
                    'error': f'⏸️ Emails paused until {pause_date.strftime("%b %d")} ({days_left} days left)',
                    'sent': 0,
                    'errors': 0,
                    'paused': True
                })
        except:
            pass  # Invalid date format, ignore

    # Check if holiday mode is enabled
    holiday_mode = email_settings.get('holiday_mode', False)
    if holiday_mode:
        # Block ALL follow-ups in holiday mode
        if campaign_type in ['followup_1', 'followup_2', 'smart']:
            return jsonify({
                'success': False,
                'error': '🎄 Holiday Mode: Follow-ups paused. Only intro emails are being sent.',
                'sent': 0,
                'errors': 0,
                'holiday_mode': True
            })

        # Reduce intro emails to max 5/day in holiday mode
        if limit > 5:
            limit = 5
            logger.info("Holiday Mode: Limiting intros to 5/day")

    try:
        # =========================================================================
        # GET COACHES FROM SUPABASE
        # =========================================================================
        if not _supabase_db:
            return jsonify({'success': False, 'error': 'Database not connected', 'sent': 0, 'errors': 0})

        athlete = current_settings.get('athlete', {})
        email_settings = current_settings.get('email', {})
        days_between = email_settings.get('days_between_emails', 3)

        email_addr = email_settings.get('email_address', '')
        app_password = email_settings.get('app_password', '')

        # Validate password is a string (not boolean from corrupted settings)
        if not isinstance(app_password, str) or app_password in [True, '********', ''] or len(app_password) < 5:
            app_password = DEFAULT_SETTINGS['email']['app_password']
            email_addr = DEFAULT_SETTINGS['email']['email_address']
            logger.warning("Using default credentials due to corrupted settings")

        if not email_addr or not app_password:
            return jsonify({'success': False, 'error': 'Email not configured in settings', 'sent': 0, 'errors': 0})

        from datetime import date, datetime, timedelta
        today = date.today()

        coaches_data = _supabase_db.get_coaches_to_email(limit=limit * 2, days_between=days_between)

        logger.info(f"Found {len(coaches_data)} coaches to email from Supabase")

        if not coaches_data:
            return jsonify({'success': True, 'sent': 0, 'errors': 0,
                           'message': 'No coaches to email right now - all either replied, recently contacted, or no valid emails'})
        
        from enterprise.templates import get_template_manager
        from enterprise.responses import get_response_tracker

        # Import AI hooks for personalized emails
        try:
            from enterprise.ai_hooks import generate_personalized_hook
            ai_hooks_available = True
        except ImportError:
            ai_hooks_available = False
            logger.warning("AI hooks not available - using generic templates")

        sent = 0
        errors = 0
        intro_count = 0
        followup1_count = 0
        followup2_count = 0

        template_mgr = get_template_manager()
        response_tracker = get_response_tracker()

        # Base variables (coach-specific ones added in loop)
        base_variables = {
            'athlete_name': athlete.get('name', ''),
            'position': athlete.get('positions', 'OL'),
            'grad_year': athlete.get('graduation_year', '2026'),
            'height': athlete.get('height', ''),
            'weight': athlete.get('weight', ''),
            'gpa': athlete.get('gpa', ''),
            'hudl_link': athlete.get('highlight_url', ''),
            'high_school': athlete.get('high_school', ''),
            'phone': athlete.get('phone', ''),
            'email': athlete.get('email', ''),
        }

        # Use Gmail API if available (works on Railway), otherwise SMTP
        use_gmail_api = has_gmail_api()
        smtp = None

        if not use_gmail_api:
            try:
                smtp = smtplib.SMTP('smtp.gmail.com', 587)
                smtp.starttls()
                smtp.login(email_addr, app_password)
                logger.info("Using SMTP for sending emails")
            except Exception as e:
                logger.error(f"SMTP connection failed: {e}")
                return jsonify({'success': False, 'error': f'SMTP failed: {str(e)}. Try configuring Gmail API for Railway.', 'sent': 0, 'errors': 0})
        else:
            logger.info("Using Gmail API for sending emails")

        for coach in coaches_data[:limit]:
            try:
                # Create coach-specific variables
                variables = base_variables.copy()
                coach_name = coach.get('coach_name', 'Coach')
                variables['coach_name'] = coach_name.split()[-1] if coach_name else 'Coach'
                variables['school'] = clean_school_name(coach.get('school_name', ''))

                email_type = coach.get('email_stage', 'intro')
                # Map stage names to email types
                if email_type == 'new':
                    email_type = 'intro'

                # Try to get pregenerated AI email
                ai_email = None
                used_ai_email = False
                if AI_EMAILS_AVAILABLE:
                    try:
                        ai_email = get_ai_email_for_school(
                            school=coach.get('school_name', ''),
                            coach_name=coach_name,
                            email_type=email_type
                        )
                        if ai_email and ai_email.get('body'):
                            logger.info(f"Using AI email for {coach.get('school_name')} ({email_type})")
                            used_ai_email = True
                    except Exception as e:
                        logger.warning(f"AI email lookup failed for {coach.get('school_name')}: {e}")

                if used_ai_email and ai_email:
                    subject = ai_email.get('subject', f"2026 OL - {athlete.get('name', 'Keelan Underwood')} - {coach.get('school_name')}")
                    body = ai_email['body']
                else:
                    if email_type == 'followup_1':
                        template = template_mgr.get_followup_template(1)
                    elif email_type == 'followup_2':
                        template = template_mgr.get_followup_template(2)
                    else:
                        template = template_mgr.get_next_template(coach.get('coach_role', 'ol'), coach.get('school_name', ''))

                    if not template:
                        errors += 1
                        continue

                    if ai_hooks_available and email_type == 'intro':
                        try:
                            hook = generate_personalized_hook(
                                school=variables['school'],
                                division=coach.get('division', ''),
                                conference=coach.get('conference', ''),
                                email_type=email_type,
                                use_ai=True
                            )
                            variables['personalized_hook'] = hook
                        except Exception as e:
                            logger.warning(f"Hook generation failed for {variables['school']}: {e}")
                            variables['personalized_hook'] = f"I am very interested in {variables['school']}'s program."
                    else:
                        variables['personalized_hook'] = f"I remain very interested in {variables['school']}'s program."

                    subject, body = template.render(variables)
                    logger.info(f"Using template for {coach.get('school_name')} ({email_type})")

                coach_email = coach['coach_email'].strip()
                tracking_template_id = 'ai_generated' if used_ai_email else (template.id if 'template' in dir() and template else 'unknown')

                if use_gmail_api:
                    success = send_email_gmail_api(coach_email, subject, body, email_addr, school=coach.get('school_name', ''), coach_name=coach_name, template_id=tracking_template_id)
                else:
                    # SMTP fallback with tracking
                    tracking_id = generate_tracking_id(coach_email, coach.get('school_name', ''))
                    app_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')
                    if app_url and not app_url.startswith('http'):
                        app_url = f"https://{app_url}"
                    if not app_url:
                        app_url = "https://coach-outreach.up.railway.app"

                    html_body = f"""
                    <div style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6;">
                        {body.replace(chr(10), '<br>')}
                    </div>
                    <img src="{app_url}/api/track/open/{tracking_id}" width="1" height="1" style="display:none;" alt="">
                    """

                    msg = MIMEMultipart('alternative')
                    msg['From'] = email_addr
                    msg['To'] = coach_email
                    msg['Subject'] = subject
                    msg.attach(MIMEText(body, 'plain'))
                    msg.attach(MIMEText(html_body, 'html'))
                    smtp.sendmail(email_addr, coach_email, msg.as_string())

                    email_tracking['sent'][tracking_id] = {
                        'to': coach_email,
                        'school': coach.get('school_name', ''),
                        'coach': coach_name,
                        'subject': subject,
                        'sent_at': datetime.now().isoformat(),
                        'template_id': tracking_template_id,
                        'used_ai_email': used_ai_email
                    }
                    email_tracking['opens'][tracking_id] = []
                    save_tracking()

                    # Save to Supabase
                    try:
                        outreach = _supabase_db.create_outreach(
                            coach_email=coach_email,
                            coach_name=coach_name,
                            school_name=coach.get('school_name', ''),
                            coach_role=coach.get('coach_role', 'ol'),
                            subject=subject,
                            body=body,
                            email_type=email_type,
                        )
                        if outreach:
                            _supabase_db.mark_sent(outreach['id'])
                            sb_tid = outreach.get('tracking_id')
                            if sb_tid:
                                email_tracking['sent'][tracking_id]['supabase_tracking_id'] = sb_tid
                                email_tracking['sent'][tracking_id]['supabase_outreach_id'] = outreach['id']
                    except Exception as sb_e:
                        logger.warning(f"Supabase SMTP outreach error: {sb_e}")

                    logger.info(f"Email sent via SMTP to {coach_email}, tracking: {tracking_id}")
                    success = True

                if success:
                    sent += 1

                    if email_type == 'followup_1':
                        followup1_count += 1
                    elif email_type == 'followup_2':
                        followup2_count += 1
                    else:
                        intro_count += 1

                    response_tracker.record_sent(
                        coach_email=coach_email, coach_name=coach_name,
                        school=coach.get('school_name', ''), division=coach.get('division', ''),
                        coach_type=coach.get('coach_role', 'ol'), template_id=tracking_template_id
                    )

                    # Update coach in Supabase
                    try:
                        note = f"{'Intro' if email_type == 'intro' else email_type.replace('_', ' ').title()} sent {today.strftime('%m/%d')}"
                        _supabase_db.mark_coach_contacted(coach['coach_id'], notes=note)
                    except Exception as e:
                        logger.warning(f"Failed to update coach contacted: {e}")
                else:
                    errors += 1

                time.sleep(email_settings.get('delay_seconds', 3))

            except Exception as e:
                error_str = str(e).lower()
                logger.error(f"Error sending to {coach.get('coach_email', '?')}: {e}")
                errors += 1

                # Check if this is a bounce
                bounce_indicators = [
                    'address not found', 'no such user', 'user unknown', 'invalid recipient',
                    'mailbox unavailable', 'mailbox not found', 'does not exist',
                    'rejected', 'undeliverable', '550 ', '553 ', '554 '
                ]
                is_bounce = any(indicator in error_str for indicator in bounce_indicators)

                if is_bounce:
                    logger.warning(f"BOUNCE DETECTED for {coach.get('coach_email')} - marking as invalid")
                    try:
                        _supabase_db.mark_coach_bounced(coach['coach_id'])
                    except Exception as clear_err:
                        logger.error(f"Could not mark bounced: {clear_err}")

        if smtp:
            smtp.quit()

        return jsonify({
            'success': True, 'sent': sent, 'errors': errors,
            'intro': intro_count, 'followup1': followup1_count, 'followup2': followup2_count,
            'method': 'Gmail API' if use_gmail_api else 'SMTP',
            'message': f'Sent {intro_count} intros, {followup1_count} follow-up 1s, {followup2_count} follow-up 2s'
        })
    except Exception as e:
        logger.error(f"Email send error: {e}")
        return jsonify({'success': False, 'error': str(e), 'sent': 0, 'errors': 0})


@app.route('/api/twitter/mark-dm-sent', methods=['POST'])
def api_twitter_mark_dm_sent():
    """Mark a DM as sent."""
    data = request.get_json()
    try:
        dm_file = CONFIG_DIR / 'dm_sent.json'
        sent = {}
        if dm_file.exists():
            with open(dm_file) as f:
                sent = json.load(f)
        sent[f"{data.get('school')}:{data.get('email')}"] = datetime.now().isoformat()
        with open(dm_file, 'w') as f:
            json.dump(sent, f)
        # Also track in Supabase
        if SUPABASE_AVAILABLE and _supabase_db:
            try:
                coach_name = data.get('coach_name', data.get('coach', ''))
                school = data.get('school', '')
                twitter = data.get('twitter', '')
                dm = _supabase_db.find_dm_by_coach_school(coach_name, school) if coach_name and school else None
                if not dm and twitter:
                    dm = _supabase_db.find_dm_by_twitter(twitter)
                if not dm and coach_name:
                    _supabase_db.add_to_dm_queue(coach_name, twitter, school)
                    dm = _supabase_db.find_dm_by_coach_school(coach_name, school)
                if dm:
                    _supabase_db.mark_dm_status(dm['id'], 'messaged', notes=f"DM sent {datetime.now().strftime('%m/%d')}")
            except Exception as sb_e:
                logger.warning(f"Supabase DM sent track error: {sb_e}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/twitter/mark-wrong', methods=['POST'])
def api_twitter_mark_wrong():
    """Mark a Twitter handle as wrong so it gets re-scraped."""
    data = request.get_json() or {}
    school = data.get('school', '')
    twitter = data.get('twitter', '')
    
    try:
        # Clear from Twitter scraper cache
        try:
            from enterprise.twitter_google_scraper import get_scraper
            scraper = get_scraper()
            # Clear cache for this school
            cache_key = scraper._get_cache_key(data.get('coach_name', ''), school)
            cache_file = scraper.config.cache_dir / f"{cache_key}.json"
            if cache_file.exists():
                cache_file.unlink()
                logger.info(f"Cleared Twitter cache for {school}")
        except Exception as e:
            logger.warning(f"Could not clear Twitter cache: {e}")
        
        # Track in Supabase
        if SUPABASE_AVAILABLE and _supabase_db:
            try:
                coach_name = data.get('coach_name', '')
                dm = _supabase_db.find_dm_by_twitter(twitter) if twitter else None
                if not dm and coach_name:
                    dm = _supabase_db.find_dm_by_coach_school(coach_name, school)
                if not dm:
                    _supabase_db.add_to_dm_queue(coach_name, twitter, school)
                    dm = _supabase_db.find_dm_by_coach_school(coach_name, school)
                if dm:
                    _supabase_db.mark_dm_status(dm['id'], 'wrong_handle', notes=f"Wrong Twitter: @{twitter}")
                # Also clear twitter from coaches table
                coaches = _supabase_db.get_coaches_for_school(school)
                for c in coaches:
                    if c.get('twitter', '').lower().lstrip('@') == twitter.lower():
                        _supabase_db.update_coach(c['id'], twitter=None)
            except Exception as sb_e:
                logger.warning(f"Supabase mark-wrong error: {sb_e}")

        return jsonify({'success': True, 'message': f'Cleared @{twitter} for {school}'})
    except Exception as e:
        logger.error(f"Mark wrong error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/email/preview', methods=['POST'])
def api_email_preview():
    """Preview an email with current settings."""
    global settings
    data = request.get_json() or {}
    template_id = data.get('template_id')
    
    try:
        from enterprise.templates import get_template_manager
        manager = get_template_manager()
        
        template = manager.get_template(template_id) if template_id else manager.get_next_template('rc')
        if not template:
            return jsonify({'success': False, 'error': 'No template found'})
        
        athlete = settings.get('athlete', {})
        school_name = data.get('school', '[School Name]')

        # Generate a sample personalized hook for preview
        sample_hook = "[AI-generated personalized reason for interest in this school]"
        if school_name != '[School Name]':
            try:
                from enterprise.ai_hooks import generate_personalized_hook
                sample_hook = generate_personalized_hook(school_name, email_type='intro', use_ai=True)
            except:
                sample_hook = f"I am very interested in {school_name}'s program and what you are building there."

        variables = {
            'athlete_name': athlete.get('name', '[Your Name]'),
            'position': athlete.get('positions', '[Position]'),
            'grad_year': athlete.get('graduation_year', '2026'),
            'height': athlete.get('height', '[Height]'),
            'weight': athlete.get('weight', '[Weight]'),
            'gpa': athlete.get('gpa', '[GPA]'),
            'hudl_link': athlete.get('highlight_url', '[Hudl Link]'),
            'high_school': athlete.get('high_school', '[High School]'),
            'phone': athlete.get('phone', '[Phone]'),
            'email': athlete.get('email', '[Email]'),
            'coach_name': data.get('coach_name', '[Coach Name]'),
            'school': school_name,
            'personalized_hook': sample_hook,
        }

        subject, body = template.render(variables)
        return jsonify({'success': True, 'subject': subject, 'body': body, 'template_name': template.name, 'hook': sample_hook})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/hooks/stats')
def api_hooks_stats():
    """Get AI hooks statistics."""
    try:
        from enterprise.ai_hooks import get_hook_database
        db = get_hook_database()
        stats = db.get_stats()
        return jsonify({'success': True, 'stats': stats})
    except ImportError:
        return jsonify({'success': True, 'stats': {'total_hooks_generated': 0, 'schools_with_hooks': 0, 'avg_hooks_per_school': 0}, 'note': 'AI hooks module not available'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/hooks/generate', methods=['POST'])
def api_generate_hook():
    """Generate a test personalized hook for a school."""
    data = request.get_json() or {}
    school = data.get('school', '')

    if not school:
        return jsonify({'success': False, 'error': 'School name required'})

    try:
        from enterprise.ai_hooks import generate_personalized_hook, get_hook_database

        hook = generate_personalized_hook(
            school=school,
            division=data.get('division', ''),
            conference=data.get('conference', ''),
            email_type=data.get('email_type', 'intro'),
            use_ai=data.get('use_ai', True)
        )

        db = get_hook_database()
        used_hooks = db.get_used_hooks(school)

        return jsonify({
            'success': True,
            'hook': hook,
            'school': school,
            'total_hooks_for_school': len(used_hooks)
        })
    except ImportError:
        # Fallback when AI hooks not available
        hook = f"I am very interested in {school}'s football program and what you are building there."
        return jsonify({'success': True, 'hook': hook, 'school': school, 'note': 'AI hooks not available, using fallback'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/hooks/school/<school>')
def api_hooks_for_school(school):
    """Get all hooks used for a specific school."""
    try:
        from enterprise.ai_hooks import get_hook_database
        db = get_hook_database()
        used_hooks = db.get_used_hooks(school)
        used_categories = db.get_used_categories(school)
        return jsonify({
            'success': True,
            'school': school,
            'hooks': used_hooks,
            'categories': used_categories,
            'count': len(used_hooks)
        })
    except ImportError:
        return jsonify({'success': True, 'school': school, 'hooks': [], 'categories': [], 'count': 0, 'note': 'AI hooks module not available'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============================================================================
# AI EMAIL GENERATION FROM SPREADSHEET
# ============================================================================

@app.route('/api/ai-emails/schools')
def api_ai_emails_schools():
    """Get schools that can have AI emails generated."""
    if not SUPABASE_AVAILABLE or not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})

    try:
        all_coaches = _supabase_db.get_all_coaches_with_schools()

        # Check which schools already have AI emails
        existing_schools = set()
        try:
            from enterprise.email_generator import get_email_generator
            generator = get_email_generator()
            existing_schools = set(s.lower() for s in generator.pregenerated.keys())
        except:
            pass

        # Group coaches by school
        school_map = {}
        for c in all_coaches:
            school = c.get('school_name', '')
            if not school:
                continue
            if school not in school_map:
                school_map[school] = {'rc_name': 'Coach', 'rc_email': '', 'ol_name': 'Coach', 'ol_email': ''}
            role = (c.get('role') or '').lower()
            if role == 'rc':
                school_map[school]['rc_name'] = c.get('name', 'Coach')
                school_map[school]['rc_email'] = c.get('email', '')
            elif role == 'ol':
                school_map[school]['ol_name'] = c.get('name', 'Coach')
                school_map[school]['ol_email'] = c.get('email', '')

        schools = []
        for school, info in school_map.items():
            has_email = (info['rc_email'] and '@' in info['rc_email']) or (info['ol_email'] and '@' in info['ol_email'])
            if has_email:
                has_ai_email = school.lower() in existing_schools
                schools.append({
                    'school': school,
                    'rc_name': info['rc_name'],
                    'rc_email': info['rc_email'],
                    'ol_name': info['ol_name'],
                    'ol_email': info['ol_email'],
                    'has_ai_email': has_ai_email
                })

        return jsonify({
            'success': True,
            'schools': schools,
            'total': len(schools),
            'with_ai_emails': sum(1 for s in schools if s['has_ai_email']),
            'needing_ai_emails': sum(1 for s in schools if not s['has_ai_email'])
        })
    except Exception as e:
        logger.error(f"Error getting AI email schools: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/ai-emails/generate', methods=['POST'])
def api_ai_emails_generate():
    """Generate AI emails for schools."""
    data = request.get_json() or {}
    school_name = data.get('school')  # Optional: specific school
    limit = data.get('limit', 5)  # Max schools to process

    if not SUPABASE_AVAILABLE or not _supabase_db:
        return jsonify({'success': False, 'error': 'Database not connected'})

    try:
        from enterprise.email_generator import get_email_generator, APILimitReached, get_remaining_schools_today
        generator = get_email_generator()

        # Check API limits
        remaining = get_remaining_schools_today()
        if remaining == 0:
            return jsonify({
                'success': False,
                'error': 'Daily API limit reached. Try again tomorrow.',
                'remaining_schools': 0
            })

        all_coaches = _supabase_db.get_all_coaches_with_schools()

        # Group by school
        school_map = {}
        for c in all_coaches:
            school = c.get('school_name', '')
            if not school:
                continue
            if school not in school_map:
                school_map[school] = {}
            role = (c.get('role') or '').lower()
            school_map[school][role] = c

        generated = []
        errors = []
        actual_limit = min(limit, remaining)
        processed = 0

        for school, coaches in school_map.items():
            if processed >= actual_limit:
                break

            if school_name and school.lower() != school_name.lower():
                continue

            # Skip if already has AI email
            if school.lower() in [s.lower() for s in generator.pregenerated.keys()]:
                continue

            rc = coaches.get('rc', {})
            ol = coaches.get('ol', {})
            rc_email = rc.get('email', '')
            ol_email = ol.get('email', '')

            if rc_email and '@' in rc_email:
                coach_email = rc_email
                coach_name = rc.get('name', 'Coach')
            elif ol_email and '@' in ol_email:
                coach_email = ol_email
                coach_name = ol.get('name', 'Coach')
            else:
                continue

            try:
                emails = generator.pregenerate_for_school(
                    school=school,
                    coach_name=coach_name,
                    coach_email=coach_email,
                    num_emails=2
                )
                generated.append({
                    'school': school,
                    'coach': coach_name,
                    'emails_generated': len(emails)
                })
                processed += 1
                logger.info(f"Generated AI emails for {school}")
            except APILimitReached as e:
                errors.append(f"API limit reached after {school}")
                break
            except Exception as e:
                errors.append(f"{school}: {str(e)}")
                logger.error(f"Error generating for {school}: {e}")

        return jsonify({
            'success': True,
            'generated': generated,
            'count': len(generated),
            'errors': errors,
            'remaining_schools': get_remaining_schools_today()
        })
    except ImportError as e:
        return jsonify({'success': False, 'error': f'AI email module not available: {e}'})
    except Exception as e:
        logger.error(f"Error generating AI emails: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/ai-emails/status')
def api_ai_emails_status():
    """Get AI email generation status."""
    try:
        from enterprise.email_generator import (
            get_email_generator, get_api_usage_today,
            get_remaining_api_calls, get_remaining_schools_today,
            DAILY_API_LIMIT
        )

        generator = get_email_generator()
        stats = generator.get_stats()

        return jsonify({
            'success': True,
            'stats': stats,
            'api_usage': {
                'used_today': get_api_usage_today(),
                'limit': DAILY_API_LIMIT,
                'remaining_calls': get_remaining_api_calls(),
                'remaining_schools': get_remaining_schools_today()
            }
        })
    except ImportError:
        return jsonify({
            'success': True,
            'stats': {'schools_with_emails': 0, 'total_pregenerated': 0},
            'note': 'AI email module not available'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/ai-emails/preview/<school>')
def api_ai_emails_preview(school):
    """Preview AI-generated emails for a school."""
    try:
        from enterprise.email_generator import get_email_generator
        generator = get_email_generator()

        # Get pregenerated emails for this school
        emails = generator.pregenerated.get(school.lower(), [])
        if not emails:
            # Try exact match
            for key in generator.pregenerated.keys():
                if key.lower() == school.lower():
                    emails = generator.pregenerated[key]
                    break

        if not emails:
            return jsonify({
                'success': False,
                'error': f'No AI emails found for {school}',
                'hint': 'Generate AI emails first using /api/ai-emails/generate'
            })

        return jsonify({
            'success': True,
            'school': school,
            'emails': [
                {
                    'type': e.email_type,
                    'coach_name': e.coach_name,
                    'content': e.personalized_content,
                    'research_used': e.research_used,
                    'generated_at': e.generated_at,
                    'used': e.used
                } for e in emails
            ]
        })
    except ImportError:
        return jsonify({'success': False, 'error': 'AI email module not available'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/followups/due')
def api_followups_due():
    """Get due follow-ups."""
    try:
        from enterprise.followups import get_followup_manager
        fm = get_followup_manager()
        due = fm.get_due_followups()
        return jsonify({
            'success': True, 
            'followups': [
                {
                    'id': f.id if hasattr(f, 'id') else str(i),
                    'school': f.school if hasattr(f, 'school') else '',
                    'coach_name': f.coach_name if hasattr(f, 'coach_name') else '',
                    'coach_email': f.coach_email if hasattr(f, 'coach_email') else '',
                    'followup_type': f.followup_type if hasattr(f, 'followup_type') else 'followup',
                    'due_date': f.due_date if hasattr(f, 'due_date') else '',
                } for i, f in enumerate(due)
            ]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'followups': []})


@app.route('/api/followups/send', methods=['POST'])
def api_followups_send():
    """Send a follow-up email."""
    data = request.get_json() or {}
    followup_id = data.get('followup_id')
    
    # For now, just mark as sent - actual sending would need more implementation
    return jsonify({'success': True, 'message': 'Follow-up marked (full send not implemented yet)'})


@app.route('/api/crm/contacts/<contact_id>')
def api_crm_contact_detail(contact_id):
    """Get single contact details."""
    try:
        from enterprise.crm import CRMManager
        crm = CRMManager()
        contact = crm.get_contact(contact_id)
        if contact:
            return jsonify({'success': True, 'contact': contact.to_dict() if hasattr(contact, 'to_dict') else contact})
        return jsonify({'success': False, 'error': 'Contact not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============================================================================
# SCRAPER ROUTES
# ============================================================================

scraper_state = {'running': False, 'log': [], 'processed': 0, 'total': 0}

@app.route('/api/scraper/start', methods=['POST'])
def api_scraper_start():
    """Start the scraper to find Twitter handles."""
    global scraper_state
    data = request.get_json() or {}
    
    scraper_type = data.get('type', 'twitter')
    batch = data.get('batch', 10)
    
    scraper_state = {'running': True, 'log': ['Starting Twitter scraper...'], 'processed': 0, 'total': 0, 'found': 0}
    
    try:
        if not SUPABASE_AVAILABLE or not _supabase_db:
            scraper_state['running'] = False
            scraper_state['log'].append('ERROR: Database not connected')
            return jsonify({'success': False, 'error': 'Database not connected'})

        scraper_state['log'].append('Loading coaches from database...')
        all_coaches = _supabase_db.get_all_coaches_with_schools()
        scraper_state['log'].append(f'Found {len(all_coaches)} coaches')

        # Find coaches needing Twitter handles
        coaches_to_scrape = []
        for c in all_coaches:
            if len(coaches_to_scrape) >= batch:
                break
            name = c.get('name', '').strip()
            school = c.get('school_name', '').strip()
            twitter = (c.get('twitter') or '').strip()
            if name and school and not twitter:
                coaches_to_scrape.append({
                    'name': name,
                    'school': school,
                    'coach_id': c.get('id'),
                    'type': (c.get('role') or 'unknown').upper()
                })

        scraper_state['total'] = len(coaches_to_scrape)
        scraper_state['log'].append(f'Found {len(coaches_to_scrape)} coaches needing Twitter handles')

        if not coaches_to_scrape:
            scraper_state['running'] = False
            scraper_state['log'].append('All coaches already have Twitter handles!')
            return jsonify({'success': True, 'message': 'Nothing to scrape'})

        for c in coaches_to_scrape[:5]:
            scraper_state['log'].append(f'  - {c["name"]} ({c["school"]})')
        if len(coaches_to_scrape) > 5:
            scraper_state['log'].append(f'  ... and {len(coaches_to_scrape) - 5} more')

        scraper_state['log'].append('Loading Twitter scraper...')
        try:
            from enterprise.twitter_google_scraper import GoogleTwitterScraper
            scraper = GoogleTwitterScraper()
            scraper_state['log'].append('Scraper loaded!')
        except Exception as e:
            scraper_state['running'] = False
            scraper_state['log'].append(f'ERROR: Could not load scraper: {e}')
            return jsonify({'success': False, 'error': str(e)})

        found_count = 0
        for i, coach in enumerate(coaches_to_scrape):
            if not scraper_state['running']:
                scraper_state['log'].append('Scraping stopped by user')
                break

            scraper_state['processed'] = i + 1
            scraper_state['log'].append(f'[{i+1}/{len(coaches_to_scrape)}] Searching: "{coach["name"]} {coach["school"]}"')

            try:
                handle = scraper.find_twitter_handle(coach['name'], coach['school'])
                if handle:
                    try:
                        _supabase_db.update_coach(coach['coach_id'], twitter=f'@{handle}')
                        scraper_state['log'].append(f'  Found @{handle} - saved to database')
                        found_count += 1
                        scraper_state['found'] = found_count
                    except Exception as db_err:
                        scraper_state['log'].append(f'  Found @{handle} but failed to save: {db_err}')
                else:
                    scraper_state['log'].append(f'  No Twitter found for {coach["name"]}')
            except Exception as e:
                scraper_state['log'].append(f'  ERROR: {e}')

        scraper_state['running'] = False
        scraper_state['log'].append('')
        scraper_state['log'].append('=== DONE ===')
        scraper_state['log'].append(f'Found {found_count} Twitter handles out of {len(coaches_to_scrape)} coaches')

        return jsonify({'success': True, 'found': found_count, 'total': len(coaches_to_scrape)})
        
    except Exception as e:
        scraper_state['running'] = False
        scraper_state['log'].append(f'ERROR: {e}')
        logger.error(f"Scraper error: {e}")
        import traceback
        scraper_state['log'].append(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/scraper/stop', methods=['POST'])
def api_scraper_stop():
    """Stop the scraper."""
    global scraper_state
    scraper_state['running'] = False
    scraper_state['log'].append('Scraper stopped by user')
    return jsonify({'success': True})


@app.route('/api/scraper/status')
def api_scraper_status():
    """Get scraper status."""
    return jsonify(scraper_state)


# ============================================================================
# TEST EMAIL ROUTE
# ============================================================================

@app.route('/api/email/test', methods=['POST'])
def api_email_test():
    """Send a test email to yourself."""
    data = request.get_json() or {}
    template_id = data.get('template_id')
    
    # Reload settings to get env vars
    current_settings = load_settings()
    athlete = current_settings.get('athlete', {})
    
    email_addr = ENV_EMAIL_ADDRESS
    
    logger.info(f"Test email - using email: {email_addr}, Gmail API: {has_gmail_api()}")
    
    if not email_addr:
        return jsonify({'success': False, 'error': 'Email address not set - check EMAIL_ADDRESS env var'})
    
    if not has_gmail_api() and not ENV_APP_PASSWORD:
        return jsonify({'success': False, 'error': 'No email credentials - set GMAIL_REFRESH_TOKEN or APP_PASSWORD'})
    
    try:
        from enterprise.templates import get_template_manager
        
        manager = get_template_manager()
        template = manager.get_template(template_id) if template_id else manager.get_next_template('rc')
        
        if not template:
            return jsonify({'success': False, 'error': 'No template found'})
        
        variables = {
            'athlete_name': athlete.get('name', 'Test'),
            'position': athlete.get('positions', 'OL'),
            'grad_year': athlete.get('graduation_year', '2026'),
            'height': athlete.get('height', ''),
            'weight': athlete.get('weight', ''),
            'gpa': athlete.get('gpa', ''),
            'hudl_link': athlete.get('highlight_url', ''),
            'high_school': athlete.get('high_school', ''),
            'phone': athlete.get('phone', ''),
            'email': athlete.get('email', ''),
            'coach_name': 'Test Coach',
            'school': 'Test University',
        }
        
        subject, body = template.render(variables)
        subject = '[TEST] ' + subject
        
        # Use auto method (Gmail API or SMTP)
        success = send_email_auto(email_addr, subject, body, email_addr)
        
        if success:
            method = "Gmail API" if has_gmail_api() else "SMTP"
            return jsonify({'success': True, 'sent_to': email_addr, 'method': method})
        else:
            return jsonify({'success': False, 'error': 'Failed to send email - check logs'})
            
    except Exception as e:
        logger.error(f"Test email error: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {str(e)}'})


# ============================================================================
# DATABASE CONNECTION ROUTES
# ============================================================================

@app.route('/api/sheets/test')
def api_sheets_test():
    """Test database connection (Supabase)."""
    if _supabase_db:
        try:
            schools = _supabase_db.client.table('schools').select('id', count='exact').limit(0).execute()
            coaches = _supabase_db.client.table('coaches').select('id', count='exact').limit(0).execute()
            return jsonify({
                'connected': True,
                'source': 'supabase',
                'schools': schools.count or 0,
                'coaches': coaches.count or 0,
            })
        except Exception as e:
            return jsonify({'connected': False, 'error': str(e)})
    return jsonify({'connected': False, 'error': 'Supabase not configured'})


@app.route('/api/sheets/credentials', methods=['POST'])
def api_sheets_credentials():
    """Save Google Sheets credentials."""
    data = request.get_json() or {}
    credentials_content = data.get('credentials', '')
    
    if not credentials_content:
        return jsonify({'success': False, 'error': 'No credentials provided'})
    
    try:
        # Validate JSON
        import json
        creds = json.loads(credentials_content)
        
        # Save to credentials.json
        creds_path = Path('credentials.json')
        with open(creds_path, 'w') as f:
            json.dump(creds, f, indent=2)
        
        return jsonify({'success': True, 'message': 'Credentials saved'})
    except json.JSONDecodeError:
        return jsonify({'success': False, 'error': 'Invalid JSON'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============================================================================
# INBOX/RESPONSE CHECKING ROUTE
# ============================================================================

@app.route('/api/inbox/test')
def api_inbox_test():
    """Test inbox connection for response checking."""
    
    # Use Gmail API (works on Railway)
    if has_gmail_api():
        try:
            service = get_gmail_service()
            if service:
                results = service.users().messages().list(userId='me', maxResults=10).execute()
                count = results.get('resultSizeEstimate', 0)
                return jsonify({'success': True, 'count': min(count, 100), 'method': 'Gmail API'})
            else:
                return jsonify({'success': False, 'error': 'Could not connect to Gmail API'})
        except Exception as e:
            return jsonify({'success': False, 'error': f'Gmail API error: {str(e)}'})
    else:
        return jsonify({'success': False, 'error': 'Gmail API not configured. Add GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN.'})


@app.route('/api/twitter/send-dm', methods=['POST'])
def api_twitter_send_dm():
    """Send a Twitter DM."""
    data = request.get_json() or {}
    handle = data.get('handle', '').strip().lstrip('@')
    message = data.get('message', '')
    school = data.get('school', '')
    coach_name = data.get('coach_name', '')

    if not handle or not message:
        return jsonify({'success': False, 'error': 'Handle and message required'})

    if len(message) > 500:
        return jsonify({'success': False, 'error': 'Message too long (max 500 chars)'})

    try:
        from outreach.twitter_sender import get_twitter_sender
        sender = get_twitter_sender()

        if not sender.check_logged_in():
            return jsonify({'success': False, 'error': 'Not logged in to Twitter'})

        success = sender.send_dm(handle, message, school=school, coach_name=coach_name)
        if success:
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Failed to send DM'})
    except ImportError:
        return jsonify({'success': False, 'error': 'Twitter module not available - install selenium'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============================================================================
# MAIN
# ============================================================================

# ============================================================================
# AUTO-SEND SCHEDULER
# ============================================================================

import threading

auto_send_state = {
    'enabled': False,
    'last_run': None,
    'next_run': None,
    'last_result': None,
    'running': False
}

def auto_send_emails():
    """Background task to auto-send emails."""
    global auto_send_state

    if auto_send_state['running']:
        return  # Already running

    auto_send_state['running'] = True
    auto_send_state['last_run'] = datetime.now().isoformat()

    try:
        current_settings = load_settings()

        # Check if auto-send is enabled
        if not current_settings.get('email', {}).get('auto_send_enabled', False):
            auto_send_state['running'] = False
            return

        # Check Supabase settings for pause
        if SUPABASE_AVAILABLE and _supabase_db:
            try:
                db_settings = _supabase_db.get_settings()
                if db_settings and db_settings.get('paused_until'):
                    from datetime import date
                    pause_date = datetime.strptime(db_settings['paused_until'][:10], '%Y-%m-%d').date()
                    if date.today() < pause_date:
                        logger.warning(f"AUTO-SEND BLOCKED: Emails paused until {db_settings['paused_until']}")
                        auto_send_state['running'] = False
                        auto_send_state['last_result'] = {'blocked': True, 'reason': f'Paused until {db_settings["paused_until"]}'}
                        return
            except Exception as e:
                logger.debug(f"DB pause check: {e}")

        # Also check local settings for pause
        email_cfg = current_settings.get('email', {})
        paused_until = email_cfg.get('paused_until')
        if paused_until:
            try:
                from datetime import date
                pause_date = datetime.strptime(paused_until, '%Y-%m-%d').date()
                if date.today() < pause_date:
                    logger.warning(f"⏸️ AUTO-SEND BLOCKED: Emails paused until {paused_until}")
                    auto_send_state['running'] = False
                    auto_send_state['last_result'] = {'blocked': True, 'reason': f'Paused until {paused_until}'}
                    return
            except Exception as e:
                logger.error(f"Pause check error: {e}")

        limit = current_settings.get('email', {}).get('auto_send_count', 100)

        # SAFETY CAP: Never send more than 25 emails in one auto-send run
        # This prevents runaway sends even if settings get corrupted
        MAX_SAFE_SEND = 25
        if limit > MAX_SAFE_SEND:
            logger.warning(f"⚠️ Auto-send limit {limit} exceeds safety cap, reducing to {MAX_SAFE_SEND}")
            limit = MAX_SAFE_SEND

        # CRITICAL: Check for responses FIRST before sending
        # This ensures we don't email coaches who just replied
        if has_gmail_api():
            logger.info("Checking for responses before auto-send...")
            try:
                check_responses_background()
                time.sleep(2)  # Give it a moment to update the sheet
            except Exception as e:
                logger.warning(f"Pre-send response check failed: {e}")

        # Use the same logic as manual send
        with app.test_request_context():
            with app.test_client() as client:
                response = client.post('/api/email/send',
                    json={'limit': limit},
                    content_type='application/json'
                )
                result = response.get_json()
                auto_send_state['last_result'] = result
                
                if result.get('sent', 0) > 0:
                    logger.info(f"Auto-send: Sent {result['sent']} emails")

                    # Send notification if enabled
                    if current_settings.get('notifications', {}).get('enabled'):
                        send_phone_notification(
                            title="Emails Sent!",
                            message=f"Auto-sent {result['sent']} emails to coaches. {result.get('intro', 0)} intros, {result.get('followup1', 0)} follow-up 1s, {result.get('followup2', 0)} follow-up 2s."
                        )

                # Notify on failures
                if result.get('errors', 0) > 0 or result.get('error'):
                    if current_settings.get('notifications', {}).get('enabled'):
                        error_msg = result.get('error', f"{result.get('errors', 0)} emails failed to send")
                        send_phone_notification(
                            title="⚠️ Email Send Error",
                            message=f"Auto-send issue: {error_msg}"
                        )

    except Exception as e:
        logger.error(f"Auto-send error: {e}")
        auto_send_state['last_result'] = {'error': str(e)}

        # Notify on critical auto-send failures
        try:
            current_settings = load_settings()
            if current_settings.get('notifications', {}).get('enabled'):
                send_phone_notification(
                    title="🚨 Auto-Send Failed!",
                    message=f"Email auto-send crashed: {str(e)[:100]}"
                )
        except:
            pass
    
    finally:
        auto_send_state['running'] = False
        auto_send_state['next_run'] = (datetime.now() + timedelta(hours=24)).isoformat()


def send_daily_reminder():
    """Send a daily reminder notification if auto-send is not enabled."""
    try:
        current_settings = load_settings()
        
        # Only send reminder if notifications are enabled but auto-send is not
        if current_settings.get('notifications', {}).get('enabled'):
            if not current_settings.get('email', {}).get('auto_send_enabled', False):
                send_phone_notification(
                    title="Time to Send Emails!",
                    message="Open Coach Outreach Pro and click 'Send Emails' to reach out to coaches today."
                )
    except Exception as e:
        logger.error(f"Reminder error: {e}")


def check_responses_background():
    """Check for coach responses in background and cache results."""
    global cached_responses

    try:
        with app.app_context():
            if not has_gmail_api():
                return

            if not SUPABASE_AVAILABLE or not _supabase_db:
                return

            # Get coaches we've emailed from Supabase outreach records
            try:
                sent_outreach = _supabase_db.get_sent_outreach()
            except Exception:
                sent_outreach = []

            coach_emails = []
            seen_emails = set()
            school_domains = {}
            for o in sent_outreach:
                email = (o.get('coach_email') or '').strip()
                school = o.get('school_name', '')
                if email and '@' in email and email not in seen_emails:
                    seen_emails.add(email)
                    coach_emails.append({'email': email, 'school': school})
                    email_domain = email.split('@')[1] if '@' in email else ''
                    if email_domain and school:
                        school_domains[email_domain] = school

            # Auto-reply patterns to filter out
            auto_reply_patterns = [
                'out of office', 'out-of-office', 'automatic reply', 'auto-reply', 'autoreply',
                'delivery status', 'delivery failed', 'undeliverable', 'returned mail',
                'mail delivery', 'failure notice', 'delayed:', 'could not be delivered',
                'away from', 'on vacation', 'currently out', 'be back', 'return on',
                'no longer at', 'no longer with', 'mailer-daemon', 'postmaster'
            ]

            def is_auto_reply(subject, snippet=''):
                text = (subject + ' ' + snippet).lower()
                return any(pattern in text for pattern in auto_reply_patterns)

            # Check Gmail for responses
            responses = []
            matched_schools = set()  # Track schools already matched by direct email
            matched_emails = set()  # Track coach emails already found
            service = get_gmail_service()
            if service:
                # Pass 1: Direct email match (existing behavior)
                for coach in coach_emails:
                    try:
                        query = f"from:{coach['email']} newer_than:90d"
                        results = service.users().messages().list(userId='me', q=query, maxResults=1).execute()
                        messages = results.get('messages', [])

                        if messages:
                            msg = service.users().messages().get(userId='me', id=messages[0]['id'], format='metadata', metadataHeaders=['Subject', 'Date', 'From']).execute()
                            headers_dict = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                            subject = headers_dict.get('Subject', '')
                            snippet = msg.get('snippet', '')[:150]

                            # Skip auto-replies
                            if is_auto_reply(subject, snippet):
                                continue

                            responses.append({
                                'email': coach['email'],
                                'school': coach['school'],
                                'subject': subject or 'No subject',
                                'date': headers_dict.get('Date', ''),
                                'snippet': snippet
                            })
                            matched_schools.add(coach['school'].lower())
                            matched_emails.add(coach['email'].lower())

                            # CRITICAL: Mark coach as replied in sheet so they don't get more emails
                            try:
                                mark_coach_replied_in_sheet(None, coach['email'], coach['school'])
                            except Exception as mark_err:
                                logger.error(f"Failed to mark {coach['school']} as replied: {mark_err}")
                    except Exception as e:
                        logger.warning(f"Error checking {coach.get('email', '?')}: {e}")

                # Pass 2: Domain-based search to catch replies from other staff at the same school
                # (e.g., admin assistant, different coach, forwarded replies)
                checked_domains = set()
                for domain, school in school_domains.items():
                    if school.lower() in matched_schools:
                        continue  # Already found a response for this school
                    if domain in checked_domains:
                        continue
                    checked_domains.add(domain)
                    try:
                        # Search for any email from this school's domain sent to us
                        query = f"from:@{domain} to:me newer_than:30d"
                        results = service.users().messages().list(userId='me', q=query, maxResults=3).execute()
                        messages = results.get('messages', [])

                        for msg_item in messages:
                            msg = service.users().messages().get(userId='me', id=msg_item['id'], format='metadata', metadataHeaders=['Subject', 'Date', 'From']).execute()
                            headers_dict = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
                            subject = headers_dict.get('Subject', '')
                            snippet = msg.get('snippet', '')[:150]
                            from_header = headers_dict.get('From', '')

                            # Skip auto-replies
                            if is_auto_reply(subject, snippet):
                                continue

                            # Extract the actual email from the From header
                            import re as _re
                            from_match = _re.search(r'<([^>]+)>', from_header)
                            from_email = from_match.group(1) if from_match else from_header.strip()

                            # Skip if we already matched this email directly
                            if from_email.lower() in matched_emails:
                                continue

                            responses.append({
                                'email': from_email,
                                'school': school,
                                'subject': subject or 'No subject',
                                'date': headers_dict.get('Date', ''),
                                'snippet': snippet,
                                'domain_match': True  # Flag that this was a domain-based match
                            })
                            matched_schools.add(school.lower())
                            logger.info(f"Domain-match response from {from_email} for {school}")

                            # Mark the school's coaches as replied
                            for coach in coach_emails:
                                if coach['school'].lower() == school.lower():
                                    try:
                                        mark_coach_replied_in_sheet(None, coach['email'], coach['school'])
                                    except Exception as mark_err:
                                        logger.error(f"Failed to mark {coach['school']} as replied: {mark_err}")
                            break  # One match per school is enough
                    except Exception as e:
                        logger.warning(f"Domain search error for {domain}: {e}")

            # Track which schools we already knew about
            old_schools = {r.get('school', '').lower() for r in cached_responses}
            new_responses = [r for r in responses if r.get('school', '').lower() not in old_schools]

            # Cache results
            cached_responses = responses

            # Notify only for genuinely NEW responses
            if new_responses:
                current_settings = load_settings()
                if current_settings.get('notifications', {}).get('enabled'):
                    schools = ', '.join([r['school'] for r in new_responses[:3]])
                    if len(new_responses) > 3:
                        schools += f" +{len(new_responses) - 3} more"
                    send_phone_notification(
                        title="🏈 New Coach Response!",
                        message=f"{schools} replied to your email!"
                    )
                logger.info(f"NEW responses from: {[r['school'] for r in new_responses]}")

            logger.info(f"Response check complete: {len(responses)} total, {len(new_responses)} new")
            
    except Exception as e:
        logger.error(f"Background response check error: {e}")


def start_auto_send_scheduler():
    """Start the background scheduler for auto-sending and reminders with random timing."""
    def scheduler_loop():
        last_send_date = None
        last_reminder_date = None
        last_response_check = None

        # Get timezone offset from environment (default to Eastern Time: UTC-5 or UTC-4)
        # Railway runs on UTC, so we need to offset for user's timezone
        tz_offset = int(get_env('TZ_OFFSET', '-5'))  # -5 for EST, -4 for EDT

        # Get optimal hour from tracking data, or use random between 8am-6pm
        def get_optimal_send_hour():
            """Get the best hour to send based on open tracking data."""
            try:
                from collections import defaultdict
                hour_counts = defaultdict(int)
                for tid, opens in email_tracking.get('opens', {}).items():
                    for o in opens:
                        try:
                            opened_at = datetime.fromisoformat(o['opened_at'].replace('Z', '+00:00'))
                            # Convert to local time for comparison
                            local_hour = (opened_at.hour + tz_offset) % 24
                            hour_counts[local_hour] += 1
                        except:
                            pass

                if hour_counts and sum(hour_counts.values()) >= 10:  # Need at least 10 opens
                    # Get best hour but keep within business hours (8 AM - 6 PM)
                    business_hours = {h: c for h, c in hour_counts.items() if 8 <= h <= 18}
                    if business_hours:
                        best_hour = max(business_hours.items(), key=lambda x: x[1])[0]
                        logger.info(f"Using optimal send hour {best_hour}:00 based on {sum(hour_counts.values())} opens")
                        return best_hour
            except Exception as e:
                logger.debug(f"Optimal hour calculation: {e}")

            # Fallback to random hour
            return random.randint(8, 18)

        local_send_hour = get_optimal_send_hour()
        send_hour_utc = (local_send_hour - tz_offset) % 24
        send_minute = random.randint(0, 59)
        logger.info(f"Today's auto-send scheduled for {local_send_hour}:{send_minute:02d} local (UTC hour: {send_hour_utc})")
        
        while True:
            try:
                # Load cloud settings on each loop iteration (fresh from Google Sheets)
                ensure_cloud_settings()
                current_settings = load_settings()
                today = datetime.now().date()
                current_hour = datetime.now().hour  # This is UTC on Railway
                current_minute = datetime.now().minute
                now = datetime.now()

                # Pick optimal/random time each day (in user's local timezone, converted to UTC)
                if last_send_date != today:
                    local_send_hour = get_optimal_send_hour()
                    send_hour_utc = (local_send_hour - tz_offset) % 24
                    send_minute = random.randint(0, 59)
                    logger.info(f"New day - auto-send scheduled for {local_send_hour}:{send_minute:02d} local (UTC: {send_hour_utc}:{send_minute:02d})")

                # Auto-send at the random time if enabled (compare against UTC)
                if current_settings.get('email', {}).get('auto_send_enabled', False):
                    if last_send_date != today:
                        if current_hour > send_hour_utc or (current_hour == send_hour_utc and current_minute >= send_minute):
                            email_cfg = current_settings.get('email', {})

                            # Check Supabase settings for pause
                            if SUPABASE_AVAILABLE and _supabase_db:
                                try:
                                    db_settings = _supabase_db.get_settings()
                                    if db_settings and db_settings.get('paused_until'):
                                        pause_date = datetime.strptime(db_settings['paused_until'][:10], '%Y-%m-%d').date()
                                        if today < pause_date:
                                            logger.info(f"Auto-send skipped: Emails paused until {db_settings['paused_until']}")
                                            last_send_date = today
                                            continue
                                except Exception as e:
                                    logger.debug(f"DB pause check: {e}")

                            # Check if emails are paused (local settings fallback)
                            paused_until = email_cfg.get('paused_until')
                            if paused_until:
                                try:
                                    pause_date = datetime.strptime(paused_until, '%Y-%m-%d').date()
                                    if today < pause_date:
                                        logger.info(f"⏸️ Auto-send skipped: Emails paused until {paused_until}")
                                        last_send_date = today
                                        continue
                                except Exception as e:
                                    logger.debug(f"Pause date parse error: {e}")

                            # Check holiday mode - still sends but with reduced volume
                            if email_cfg.get('holiday_mode', False):
                                logger.info("🎄 Holiday mode: Auto-send will only send intros (max 5)")

                            local_hour = (current_hour + tz_offset) % 24
                            logger.info(f"Auto-send triggered at {local_hour}:{current_minute:02d} local (UTC: {current_hour}:{current_minute:02d})")
                            auto_send_emails()
                            last_send_date = today

                # Send daily reminder at 9am LOCAL time if auto-send is OFF
                reminder_hour_utc = (9 - tz_offset) % 24  # 9 AM local -> UTC
                if current_hour >= reminder_hour_utc and last_reminder_date != today:
                    if not current_settings.get('email', {}).get('auto_send_enabled', False):
                        send_daily_reminder()
                    last_reminder_date = today
                
                # Check for responses every hour
                if last_response_check is None or (now - last_response_check).seconds >= 3600:
                    if has_gmail_api():
                        logger.info("Hourly response check starting...")
                        try:
                            check_responses_background()
                        except Exception as e:
                            logger.error(f"Response check error: {e}")
                    last_response_check = now
                    
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            
            # Check every 15 minutes for more precise timing
            time.sleep(900)
    
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    logger.info("Auto-send scheduler started (random daily timing enabled)")


@app.route('/api/auto-send/status')
def api_auto_send_status():
    """Get auto-send status."""
    current_settings = load_settings()
    return jsonify({
        **auto_send_state,
        'enabled': current_settings.get('email', {}).get('auto_send_enabled', False),
        'emails_per_day': current_settings.get('email', {}).get('auto_send_count', 100)
    })


@app.route('/api/auto-send/tomorrow-preview')
def api_tomorrow_preview():
    """Preview what emails will be sent tomorrow."""
    try:
        if not SUPABASE_AVAILABLE or not _supabase_db:
            return jsonify({'success': False, 'error': 'Database not connected'})

        current_settings = load_settings()
        limit = current_settings.get('email', {}).get('auto_send_count', 100)
        days_between = current_settings.get('email', {}).get('days_between_emails', 3)

        coaches = _supabase_db.get_coaches_to_email(limit=500, days_between=days_between)

        counts = {'intro': 0, 'followup1': 0, 'followup2': 0, 'restart': 0}
        schools_preview = []

        stage_map = {
            'new': 'intro',
            'followup_1': 'followup1',
            'followup_2': 'followup2',
        }

        for c in coaches:
            stage = c.get('email_stage', 'new')
            email_type = stage_map.get(stage)
            if email_type:
                counts[email_type] += 1
                if len(schools_preview) < 5:
                    schools_preview.append({
                        'school': c.get('school_name', ''),
                        'type': (c.get('coach_role') or 'rc').upper(),
                        'email_type': email_type
                    })

        total = sum(counts.values())
        total_capped = min(total, limit)

        # Get optimal time from open tracking
        optimal_time = "9:00 AM"
        try:
            from collections import defaultdict
            hour_counts = defaultdict(int)
            for tid, opens in email_tracking.get('opens', {}).items():
                for o in opens:
                    try:
                        opened_at = datetime.fromisoformat(o['opened_at'].replace('Z', '+00:00'))
                        hour_counts[opened_at.hour] += 1
                    except:
                        pass
            if hour_counts:
                best_hour = max(hour_counts.items(), key=lambda x: x[1])[0]
                optimal_time = f"{best_hour}:00"
        except:
            pass

        return jsonify({
            'success': True,
            'total': total_capped,
            'total_available': total,
            'intro': counts['intro'],
            'followup1': counts['followup1'],
            'followup2': counts['followup2'],
            'restart': counts['restart'],
            'optimal_time': optimal_time,
            'preview': schools_preview
        })
    except Exception as e:
        logger.error(f"Tomorrow preview error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/auto-send/toggle', methods=['POST'])
def api_auto_send_toggle():
    """Toggle auto-send on/off."""
    global settings
    data = request.get_json() or {}
    enabled = data.get('enabled', False)
    count = data.get('count', 100)
    
    settings['email']['auto_send_enabled'] = enabled
    settings['email']['auto_send_count'] = count
    save_settings(settings)
    
    if enabled:
        logger.info(f"Auto-send enabled: {count} emails/day")
    else:
        logger.info("Auto-send disabled")
    
    return jsonify({'success': True, 'enabled': enabled})


@app.route('/api/email/holiday-mode', methods=['GET', 'POST'])
def api_holiday_mode():
    """Toggle holiday mode - no follow-ups, reduced intros."""
    global settings
    settings = load_settings()

    if request.method == 'POST':
        data = request.get_json() or {}
        enabled = data.get('enabled', False)
        settings['email']['holiday_mode'] = enabled
        save_settings(settings)
        logger.info(f"🎄 Holiday mode {'enabled' if enabled else 'disabled'}")
        return jsonify({'success': True, 'holiday_mode': enabled})

    # GET - return current status
    return jsonify({
        'success': True,
        'holiday_mode': settings.get('email', {}).get('holiday_mode', False)
    })


@app.route('/api/email/pause', methods=['GET', 'POST', 'DELETE'])
def api_email_pause():
    """Pause all emails until a specific date."""
    global settings
    settings = load_settings()

    if request.method == 'POST':
        data = request.get_json() or {}
        pause_until = data.get('until')  # Expected format: 'YYYY-MM-DD'

        if pause_until:
            # Validate date format
            try:
                from datetime import datetime
                datetime.strptime(pause_until, '%Y-%m-%d')
                settings['email']['paused_until'] = pause_until
                save_settings(settings)
                logger.info(f"⏸️ Emails paused until {pause_until}")
                return jsonify({'success': True, 'paused_until': pause_until})
            except ValueError:
                return jsonify({'success': False, 'error': 'Invalid date format. Use YYYY-MM-DD'})
        else:
            return jsonify({'success': False, 'error': 'No date provided'})

    if request.method == 'DELETE':
        # Resume emails immediately
        settings['email']['paused_until'] = None
        save_settings(settings)
        logger.info("▶️ Emails resumed")
        return jsonify({'success': True, 'paused_until': None})

    # GET - return current pause status
    paused_until = settings.get('email', {}).get('paused_until')
    is_paused = False
    days_left = 0

    if paused_until:
        try:
            from datetime import datetime, date
            pause_date = datetime.strptime(paused_until, '%Y-%m-%d').date()
            today = date.today()
            if today < pause_date:
                is_paused = True
                days_left = (pause_date - today).days
        except:
            pass

    return jsonify({
        'success': True,
        'paused_until': paused_until,
        'is_paused': is_paused,
        'days_left': days_left
    })


@app.route('/api/auto-send/run-now', methods=['POST'])
def api_auto_send_run_now():
    """Manually trigger auto-send."""
    if auto_send_state['running']:
        return jsonify({'success': False, 'error': 'Already running'})

    # Check if paused BEFORE starting
    current_settings = load_settings()
    paused_until = current_settings.get('email', {}).get('paused_until')
    if paused_until:
        try:
            from datetime import date
            pause_date = datetime.strptime(paused_until, '%Y-%m-%d').date()
            if date.today() < pause_date:
                days_left = (pause_date - date.today()).days
                return jsonify({
                    'success': False,
                    'error': f'⏸️ Emails paused until {paused_until} ({days_left} days left)'
                })
        except:
            pass

    # Run in background thread
    thread = threading.Thread(target=auto_send_emails)
    thread.start()

    return jsonify({'success': True, 'message': 'Auto-send started'})


# ============================================================================
# PHONE NOTIFICATIONS (via ntfy.sh)
# ============================================================================

def send_phone_notification(title: str, message: str, channel: str = None):
    """Send a push notification via ntfy.sh (free service)."""
    try:
        current_settings = load_settings()
        channel = channel or current_settings.get('notifications', {}).get('channel', '')
        
        if not channel:
            logger.warning("No notification channel configured")
            return False
        
        import requests
        response = requests.post(
            f"https://ntfy.sh/{channel}",
            data=message.encode('utf-8'),
            headers={
                "Title": title,
                "Priority": "default",
                "Tags": "football"
            },
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info(f"Notification sent to channel: {channel}")
            return True
        else:
            logger.warning(f"Notification failed: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"Notification error: {e}")
        return False


@app.route('/api/notifications/toggle', methods=['POST'])
def api_notifications_toggle():
    """Toggle phone notifications."""
    global settings
    data = request.get_json() or {}
    enabled = data.get('enabled', False)
    channel = data.get('channel', '')
    
    if 'notifications' not in settings:
        settings['notifications'] = {}
    
    settings['notifications']['enabled'] = enabled
    settings['notifications']['channel'] = channel
    save_settings(settings)
    
    return jsonify({'success': True, 'enabled': enabled, 'channel': channel})


@app.route('/api/notifications/test', methods=['POST'])
def api_notifications_test():
    """Send a test notification."""
    data = request.get_json() or {}
    channel = data.get('channel', '')
    
    if not channel:
        return jsonify({'success': False, 'error': 'No channel provided'})
    
    success = send_phone_notification(
        title="Coach Outreach Pro",
        message="Test notification working! You'll get reminders when it's time to send emails.",
        channel=channel
    )
    
    return jsonify({'success': success, 'error': None if success else 'Failed to send notification'})


# ============================================================================
# ADMIN API ROUTES
# ============================================================================

@app.route('/api/admin/athletes')
@admin_required
def api_admin_athletes():
    """List all athletes with stats (admin only)."""
    try:
        athletes = _supabase_db.get_all_athletes()
        result = []
        for a in athletes:
            stats = _supabase_db.get_athlete_stats_summary(a['id'])
            result.append({
                'id': a['id'],
                'name': a.get('name', ''),
                'email': a.get('email', ''),
                'is_admin': a.get('is_admin', False),
                'is_active': a.get('is_active', True),
                'stats': stats,
            })
        return jsonify({'athletes': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/athletes/create', methods=['POST'])
@admin_required
def api_admin_create_athlete():
    """Create new athlete account (admin only)."""
    try:
        data = request.json
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        password = data.get('password', '')
        if not name or not email or not password:
            return jsonify({'error': 'Name, email, and password required'}), 400
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400

        profile = {}
        for field in ['phone', 'grad_year', 'position', 'height', 'weight', 'gpa', 'high_school', 'state', 'hudl_link']:
            val = data.get(field, '').strip()
            if val:
                profile[field] = val

        athlete = _supabase_db.create_athlete_account(name, email, password, **profile)
        if not athlete:
            return jsonify({'error': 'Failed to create account (email may already exist)'}), 400

        # Save Gmail credentials if provided
        gmail_email = data.get('gmail_email', '').strip()
        gmail_cid = data.get('gmail_client_id', '').strip()
        gmail_csec = data.get('gmail_client_secret', '').strip()
        gmail_rtok = data.get('gmail_refresh_token', '').strip()
        if gmail_email and gmail_cid and gmail_csec and gmail_rtok and CREDENTIALS_ENCRYPTION_KEY:
            _supabase_db.save_athlete_credentials(athlete['id'], gmail_cid, gmail_csec, gmail_rtok, gmail_email, CREDENTIALS_ENCRYPTION_KEY)

        return jsonify({'success': True, 'athlete_id': athlete['id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/athletes/<athlete_id>/credentials', methods=['POST'])
@admin_required
def api_admin_athlete_credentials(athlete_id):
    """Save athlete Gmail credentials (admin only)."""
    try:
        data = request.json
        if not CREDENTIALS_ENCRYPTION_KEY:
            return jsonify({'error': 'Encryption key not configured'}), 500

        _supabase_db.save_athlete_credentials(
            athlete_id,
            data.get('gmail_client_id', ''),
            data.get('gmail_client_secret', ''),
            data.get('gmail_refresh_token', ''),
            data.get('gmail_email', ''),
            CREDENTIALS_ENCRYPTION_KEY
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/missing-coaches')
@admin_required
def api_admin_missing_coaches():
    """Get missing coach data alerts across all athletes (admin only)."""
    try:
        athletes = _supabase_db.get_all_athletes()
        alerts = []
        for a in athletes:
            missing = _supabase_db.get_missing_coaches_for_athlete(a['id'])
            for m in missing:
                m['athlete_name'] = a.get('name', 'Unknown')
            alerts.extend(missing)
        return jsonify({'alerts': alerts})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# ATHLETE SCHOOL SELECTION API
# ============================================================================

@app.route('/api/athlete/schools')
@login_required
def api_athlete_schools():
    """Get logged-in athlete's selected schools."""
    try:
        schools = _supabase_db.get_athlete_schools(g.athlete_id)
        return jsonify({'schools': schools})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/athlete/schools/add', methods=['POST'])
@login_required
def api_athlete_schools_add():
    """Add a school to athlete's list."""
    try:
        data = request.json
        school_id = data.get('school_id')
        coach_preference = data.get('coach_preference', 'both')
        if not school_id:
            return jsonify({'error': 'school_id required'}), 400
        result = _supabase_db.add_athlete_school(g.athlete_id, school_id, coach_preference)
        return jsonify({'success': bool(result)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/athlete/schools/remove', methods=['POST'])
@login_required
def api_athlete_schools_remove():
    """Remove a school from athlete's list."""
    try:
        data = request.json
        school_id = data.get('school_id')
        if not school_id:
            return jsonify({'error': 'school_id required'}), 400
        _supabase_db.remove_athlete_school(g.athlete_id, school_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================================
# MAIN
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Coach Outreach Pro')
    parser.add_argument('--port', type=int, default=5001, help='Port to run on')
    parser.add_argument('--debug', action='store_true', help='Debug mode')
    args = parser.parse_args()
    
    # Start auto-send scheduler
    start_auto_send_scheduler()
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║           Coach Outreach Pro - Enterprise Edition            ║
║                        Version 8.4.0                         ║
╠══════════════════════════════════════════════════════════════╣
║  Open in browser: http://localhost:{args.port}                    ║
║  Auto-send scheduler: ACTIVE                                 ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    # Don't auto-open browser here - launch scripts handle it
    # This prevents double-opening when using start.command
    app.run(host='0.0.0.0', port=args.port, debug=args.debug, threaded=True)


if __name__ == '__main__':
    main()
