"""
scheduler/email_scheduler.py - Automated Email Scheduling System
============================================================================
Schedules and sends daily emails to coaches automatically.

Features:
- Daily scheduling with configurable time
- Rate limiting to avoid spam filters
- Tracks sent emails to prevent duplicates
- Retry logic for failed sends
- Detailed logging and status reporting

Usage:
    from scheduler.email_scheduler import EmailScheduler
    scheduler = EmailScheduler()
    scheduler.start()  # Runs in background

Author: Coach Outreach System
Version: 3.0.0
============================================================================
"""

import os
import sys
import json
import time
import smtplib
import logging
import threading
import schedule
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, List, Dict, Any, Callable, Tuple
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sheets.manager import SheetsManager

logger = logging.getLogger(__name__)

# Tracking file for email opens
TRACKING_FILE = Path.home() / '.coach_outreach' / 'email_tracking.json'


def load_tracking_data() -> Dict[str, Any]:
    """Load email tracking data to analyze open times."""
    if TRACKING_FILE.exists():
        try:
            with open(TRACKING_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'sent': {}, 'opens': {}}


def save_tracking_data(data: Dict[str, Any]):
    """Save email tracking data."""
    try:
        with open(TRACKING_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save tracking data: {e}")


def generate_tracking_id(to_email: str, school: str) -> str:
    """Generate unique tracking ID for an email."""
    unique_str = f"{to_email}-{school}-{datetime.now().isoformat()}-{uuid.uuid4().hex[:8]}"
    return hashlib.sha256(unique_str.encode()).hexdigest()[:16]


def record_email_sent(tracking_id: str, to_email: str, school: str, coach: str, subject: str):
    """Record a sent email in tracking data."""
    tracking = load_tracking_data()
    tracking['sent'][tracking_id] = {
        'to': to_email,
        'school': school,
        'coach': coach,
        'subject': subject,
        'sent_at': datetime.now().isoformat()
    }
    tracking['opens'][tracking_id] = []
    save_tracking_data(tracking)


def get_optimal_send_hour() -> int:
    """
    Calculate the optimal hour to send emails based on open tracking data.
    Returns hour (0-23) when coaches are most likely to open emails.
    Sends 1 hour BEFORE peak to catch them at the right time.
    """
    tracking = load_tracking_data()
    opens = tracking.get('opens', {})

    if not opens:
        # Default: 9 AM if no data
        return 9

    # Count opens by hour
    hour_counts = {}
    for tid, open_list in opens.items():
        for o in open_list:
            try:
                opened_at = datetime.fromisoformat(o.get('opened_at', '').replace('Z', '+00:00'))
                hour = opened_at.hour
                hour_counts[hour] = hour_counts.get(hour, 0) + 1
            except:
                pass

    if not hour_counts:
        return 9  # Default

    # Find peak hour
    peak_hour = max(hour_counts, key=hour_counts.get)

    # Send 1 hour before peak to land in inbox before they check
    optimal_hour = (peak_hour - 1) % 24

    # Clamp to reasonable hours (7 AM - 6 PM)
    if optimal_hour < 7:
        optimal_hour = 7
    elif optimal_hour > 18:
        optimal_hour = 18

    logger.info(f"Smart timing: Peak opens at {peak_hour}:00, sending at {optimal_hour}:00")
    return optimal_hour


def get_ai_email_for_school(school: str, coach_name: str, email_type: str = 'intro') -> Optional[Dict]:
    """
    AI email generation has been removed. Always returns None - use templates instead.
    """
    return None


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class EmailSchedulerConfig:
    """Configuration for email scheduling."""
    # SMTP Settings
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    email_address: str = ""
    email_password: str = ""  # Gmail App Password
    
    # Schedule
    send_time: str = "09:00"  # 24-hour format
    enabled: bool = True
    
    # Rate Limiting
    max_emails_per_day: int = 50
    delay_between_emails: float = 5.0  # seconds
    
    # Retry
    max_retries: int = 3
    retry_delay: float = 60.0  # seconds
    
    # Athlete Info (for templates)
    athlete_name: str = "Keelan Underwood"
    graduation_year: str = "2026"
    height: str = "6'3\""
    weight: str = "295 lbs"
    positions: str = "Center, Guard, and Tackle"
    highlight_url: str = ""
    phone: str = ""
    high_school: str = ""
    city_state: str = ""
    gpa: str = ""
    
    # Persistence
    state_file: str = "email_scheduler_state.json"

    # Per-Coach Settings
    days_between_emails: int = 2  # Wait 2 days between emails to same coach

    # Smart Timing - automatically calculated from email open tracking data
    use_smart_timing: bool = True  # Use tracking data to determine optimal send time


# ============================================================================
# EMAIL TEMPLATES
# ============================================================================

RC_SUBJECT = "Recruiting Inquiry - {grad_year} OL - {athlete_name}"

RC_TEMPLATE = """Dear Coach {last_name},

My name is {athlete_name}, and I am a {grad_year} offensive lineman from {high_school} in {city_state}.

I am very interested in {school}'s football program and would love the opportunity to be recruited by your team.

Here are my stats:
• Height: {height}
• Weight: {weight}
• Positions: {positions}
• GPA: {gpa}

You can view my highlight film here: {highlight_url}

I would greatly appreciate any information about {school}'s football program and what it takes to be recruited. I am committed to both my academic and athletic development.

Thank you for your time and consideration. I look forward to hearing from you.

Respectfully,

{athlete_name}
{phone}
"""

OL_SUBJECT = "OL Recruiting Inquiry - {grad_year} - {athlete_name}"

OL_TEMPLATE = """Dear Coach {last_name},

My name is {athlete_name}, and I'm a {grad_year} offensive lineman from {high_school} in {city_state}.

I'm reaching out because I am very interested in playing for {school} and learning from your offensive line coaching.

My stats:
• Height: {height}
• Weight: {weight}
• Positions: {positions}

Here's my film: {highlight_url}

I would love the chance to speak with you about the program and how I can contribute to {school} football.

Thank you for your time.

Best regards,

{athlete_name}
{phone}
"""


# ============================================================================
# STATE MANAGER
# ============================================================================

class SchedulerState:
    """Manages persistent state for the scheduler with per-coach tracking."""

    def __init__(self, state_file: str):
        self.state_file = state_file
        self.daily_count = 0
        self.last_reset = datetime.now().date().isoformat()
        self.errors = []
        # Per-coach tracking: {email: {last_sent, ai_emails_sent: [list of types], last_type}}
        self.coach_history = {}
        self._load()

    def _load(self):
        """Load state from file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.daily_count = data.get('daily_count', 0)
                    self.last_reset = data.get('last_reset', datetime.now().date().isoformat())
                    self.errors = data.get('errors', [])[-100:]
                    self.coach_history = data.get('coach_history', {})
                    # Migrate old format if needed
                    if 'sent_emails' in data and not self.coach_history:
                        for email, timestamp in data['sent_emails'].items():
                            self.coach_history[email.lower()] = {
                                'last_sent': timestamp,
                                'ai_emails_sent': ['intro'],
                                'last_type': 'intro',
                                'template_count': 0
                            }
            except:
                pass

    def save(self):
        """Save state to file."""
        try:
            with open(self.state_file, 'w') as f:
                json.dump({
                    'daily_count': self.daily_count,
                    'last_reset': self.last_reset,
                    'errors': self.errors[-100:],
                    'coach_history': self.coach_history,
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def reset_daily_if_needed(self):
        """Reset daily count if it's a new day."""
        today = datetime.now().date().isoformat()
        if today != self.last_reset:
            self.daily_count = 0
            self.last_reset = today
            self.save()

    def get_coach_history(self, email: str) -> Dict:
        """Get history for a specific coach."""
        return self.coach_history.get(email.lower(), {
            'last_sent': None,
            'ai_emails_sent': [],
            'last_type': None,
            'template_count': 0
        })

    def days_since_last_email(self, email: str) -> int:
        """Get days since last email to this coach. Returns 999 if never emailed."""
        history = self.get_coach_history(email)
        if not history.get('last_sent'):
            return 999  # Never emailed

        try:
            last_sent = datetime.fromisoformat(history['last_sent'])
            return (datetime.now() - last_sent).days
        except:
            return 999

    def can_email_coach(self, email: str, min_days: int = 2) -> bool:
        """Check if enough days have passed to email this coach again."""
        return self.days_since_last_email(email) >= min_days

    def get_next_ai_email_type(self, email: str) -> str:
        """Get the next AI email type to send to this coach."""
        history = self.get_coach_history(email)
        sent_types = history.get('ai_emails_sent', [])

        # Auto-reset AI cycle after 30 days for a fresh start
        days_since = self.days_since_last_email(email)
        if days_since >= 30 and sent_types:
            logger.info(f"Auto-resetting AI cycle for {email} (30+ days since last email)")
            self.reset_coach_ai_cycle(email)
            sent_types = []  # Fresh start

        # Order: intro -> followup_1 -> followup_2
        if 'intro' not in sent_types:
            return 'intro'
        elif 'followup_1' not in sent_types:
            return 'followup_1'
        elif 'followup_2' not in sent_types:
            return 'followup_2'
        else:
            return None  # All AI emails sent, use templates

    def mark_email_sent(self, email: str, email_type: str, used_ai: bool):
        """Record that an email was sent to this coach."""
        email_lower = email.lower()
        history = self.get_coach_history(email_lower)

        history['last_sent'] = datetime.now().isoformat()
        history['last_type'] = email_type

        if used_ai:
            if email_type not in history.get('ai_emails_sent', []):
                history.setdefault('ai_emails_sent', []).append(email_type)
        else:
            history['template_count'] = history.get('template_count', 0) + 1

        self.coach_history[email_lower] = history
        self.daily_count += 1
        self.save()

    def reset_coach_ai_cycle(self, email: str):
        """Reset AI cycle for a coach (when new AI emails are generated)."""
        email_lower = email.lower()
        if email_lower in self.coach_history:
            self.coach_history[email_lower]['ai_emails_sent'] = []
            self.coach_history[email_lower]['template_count'] = 0
            self.save()

    def mark_response_received(self, email: str):
        """Mark that a coach responded - resets AI cycle for fresh outreach."""
        email_lower = email.lower()
        history = self.get_coach_history(email_lower)
        history['last_response'] = datetime.now().isoformat()
        history['response_count'] = history.get('response_count', 0) + 1
        # Reset AI cycle so we can send fresh personalized emails
        history['ai_emails_sent'] = []
        history['template_count'] = 0
        self.coach_history[email_lower] = history
        self.save()
        logger.info(f"Response received from {email} - AI cycle reset")

    def add_error(self, email: str, error: str):
        """Record an error."""
        self.errors.append({
            'email': email,
            'error': error,
            'time': datetime.now().isoformat()
        })
        self.save()


# ============================================================================
# EMAIL SENDER
# ============================================================================

class EmailSender:
    """Handles SMTP email sending."""
    
    def __init__(self, config: EmailSchedulerConfig):
        self.config = config
        self._connection = None
    
    def connect(self) -> bool:
        """Connect to SMTP server."""
        try:
            self._connection = smtplib.SMTP(self.config.smtp_server, self.config.smtp_port)
            self._connection.starttls()
            self._connection.login(self.config.email_address, self.config.email_password)
            logger.info("Connected to SMTP server")
            return True
        except Exception as e:
            logger.error(f"SMTP connection failed: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from SMTP server."""
        if self._connection:
            try:
                self._connection.quit()
            except:
                pass
            self._connection = None
    
    def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        reply_to: str = None,
        school: str = '',
        coach_name: str = ''
    ) -> Tuple[bool, str]:
        """
        Send an email with tracking pixel.

        Returns:
            Tuple of (success, error_message)
        """
        try:
            if not self._connection:
                if not self.connect():
                    return False, "Not connected to SMTP"

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
            msg = MIMEMultipart('alternative')
            msg['From'] = self.config.email_address
            msg['To'] = to_email
            msg['Subject'] = subject
            if reply_to:
                msg['Reply-To'] = reply_to

            # Attach plain text first, then HTML (email clients prefer the last one)
            msg.attach(MIMEText(body, 'plain'))
            msg.attach(MIMEText(html_body, 'html'))

            self._connection.sendmail(
                self.config.email_address,
                to_email,
                msg.as_string()
            )

            # Record in tracking data
            record_email_sent(tracking_id, to_email, school, coach_name, subject)
            logger.info(f"Email sent to {to_email}, tracking: {tracking_id}")

            return True, ""

        except smtplib.SMTPException as e:
            error = str(e)
            logger.error(f"SMTP error sending to {to_email}: {error}")
            # Try to reconnect for next send
            self.disconnect()
            return False, error
        except Exception as e:
            error = str(e)
            logger.error(f"Error sending to {to_email}: {error}")
            return False, error


# ============================================================================
# EMAIL SCHEDULER
# ============================================================================

class EmailScheduler:
    """
    Automated email scheduling system.
    
    Sends emails to coaches daily at a configured time.
    Tracks sent emails to prevent duplicates.
    Respects rate limits.
    """
    
    def __init__(self, config: Optional[EmailSchedulerConfig] = None):
        self.config = config or EmailSchedulerConfig()
        self.state = SchedulerState(self.config.state_file)
        self.sender = EmailSender(self.config)
        self.sheets = SheetsManager()
        
        self._running = False
        self._thread = None
        self._callbacks = []
        
        self.logger = logging.getLogger(__name__)

    def _load_settings(self) -> Dict:
        """Load settings from the settings file."""
        settings_file = Path.home() / '.coach_outreach' / 'settings.json'
        if settings_file.exists():
            try:
                with open(settings_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def add_callback(self, callback: Callable[[str, Dict], None]):
        """Add a callback for events."""
        self._callbacks.append(callback)
    
    def start(self) -> bool:
        """Start the scheduler in background."""
        if self._running:
            return True

        self._running = True

        # Calculate optimal send time from tracking data
        optimal_hour = get_optimal_send_hour()
        send_time = f"{optimal_hour:02d}:00"

        # Schedule daily job at optimal time
        schedule.every().day.at(send_time).do(self._run_daily_job)

        # Also schedule a daily recalculation of optimal time at midnight
        schedule.every().day.at("00:01").do(self._update_schedule_time)

        # Start background thread
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()

        self._emit('started', {'time': send_time, 'smart_timing': True})
        self.logger.info(f"Scheduler started with smart timing, sending at {send_time} (based on open data)")

        return True

    def _update_schedule_time(self):
        """Recalculate optimal send time daily based on new tracking data."""
        # Clear existing schedule and reschedule with updated optimal time
        optimal_hour = get_optimal_send_hour()
        new_send_time = f"{optimal_hour:02d}:00"

        # Remove old job and add new one
        schedule.clear('daily_email')
        schedule.every().day.at(new_send_time).do(self._run_daily_job).tag('daily_email')

        self.logger.info(f"Updated send time to {new_send_time} based on tracking data")
    
    def stop(self):
        """Stop the scheduler."""
        self._running = False
        schedule.clear()
        self.sender.disconnect()
        self._emit('stopped', {})
        self.logger.info("Scheduler stopped")
    
    def run_now(self) -> Dict[str, int]:
        """Run email sending immediately (for testing)."""
        return self._run_daily_job()
    
    def get_status(self) -> Dict[str, Any]:
        """Get scheduler status."""
        self.state.reset_daily_if_needed()

        # Get smart timing info
        optimal_hour = get_optimal_send_hour()
        tracking_data = load_tracking_data()
        total_opens = sum(len(opens) for opens in tracking_data.get('opens', {}).values())

        return {
            'enabled': self.config.enabled,
            'running': self._running,
            'send_time': f"{optimal_hour:02d}:00",
            'smart_timing': {
                'enabled': self.config.use_smart_timing,
                'optimal_hour': optimal_hour,
                'total_opens_tracked': total_opens,
                'status': 'Using tracked data' if total_opens > 0 else 'Using default (9 AM) - send more emails to gather data'
            },
            'daily_sent': self.state.daily_count,
            'daily_limit': self.config.max_emails_per_day,
            'total_coaches_contacted': len(self.state.coach_history),
            'recent_errors': self.state.errors[-5:],
            'next_run': schedule.next_run().isoformat() if schedule.jobs else None,
            'days_between_emails': self.config.days_between_emails,
        }
    
    def get_pending_emails(self) -> List[Dict]:
        """Get coaches who are ready to receive an email (respects 2-day gap)."""
        if not self.sheets.connect():
            return []

        try:
            data = self.sheets.get_all_data()
            if len(data) < 2:
                return []

            headers = data[0]
            rows = data[1:]

            def find_col(keywords):
                for i, h in enumerate(headers):
                    h_lower = h.lower()
                    for kw in keywords:
                        if kw in h_lower:
                            return i
                return -1

            school_col = find_col(['school'])
            ol_name_col = find_col(['oline', 'ol coach'])
            rc_name_col = find_col(['recruiting'])
            ol_email_col = find_col(['oc email', 'ol email'])
            rc_email_col = find_col(['rc email'])
            ol_contacted_col = find_col(['ol contacted'])
            rc_contacted_col = find_col(['rc contacted'])

            pending = []

            for row_idx, row in enumerate(rows):
                school = row[school_col] if school_col >= 0 and school_col < len(row) else ''
                ol_name = row[ol_name_col] if ol_name_col >= 0 and ol_name_col < len(row) else ''
                rc_name = row[rc_name_col] if rc_name_col >= 0 and rc_name_col < len(row) else ''
                ol_email = row[ol_email_col] if ol_email_col >= 0 and ol_email_col < len(row) else ''
                rc_email = row[rc_email_col] if rc_email_col >= 0 and rc_email_col < len(row) else ''

                # Check OL coach - ready if 2+ days since last email OR never emailed
                if ol_email and self.state.can_email_coach(ol_email, self.config.days_between_emails):
                    pending.append({
                        'row': row_idx + 2,
                        'school': school,
                        'name': ol_name,
                        'email': ol_email,
                        'type': 'OL',
                        'contacted_col': ol_contacted_col + 1,
                    })

                # Check RC - ready if 2+ days since last email OR never emailed
                if rc_email and self.state.can_email_coach(rc_email, self.config.days_between_emails):
                    pending.append({
                        'row': row_idx + 2,
                        'school': school,
                        'name': rc_name,
                        'email': rc_email,
                        'type': 'RC',
                        'contacted_col': rc_contacted_col + 1,
                    })

            return pending

        finally:
            self.sheets.disconnect()
    
    def _scheduler_loop(self):
        """Background scheduler loop."""
        while self._running:
            schedule.run_pending()
            time.sleep(30)  # Check every 30 seconds
    
    def _run_daily_job(self) -> Dict[str, int]:
        """Execute the daily email job."""
        if not self.config.enabled:
            self.logger.info("Scheduler disabled, skipping")
            return {'sent': 0, 'errors': 0}

        # Check holiday mode and pause settings
        settings = self._load_settings()
        email_settings = settings.get('email', {})

        if email_settings.get('holiday_mode', False):
            self.logger.info("Holiday mode enabled - limiting to 5 intro emails only")
            # Will enforce this limit below

        paused_until = email_settings.get('paused_until')
        if paused_until:
            try:
                pause_date = datetime.fromisoformat(paused_until)
                if datetime.now() < pause_date:
                    self.logger.info(f"Scheduler paused until {paused_until}, skipping")
                    return {'sent': 0, 'errors': 0, 'paused': True}
            except:
                pass

        self.state.reset_daily_if_needed()
        
        self._emit('job_started', {})
        self.logger.info("Starting daily email job")
        
        sent = 0
        errors = 0
        
        try:
            pending = self.get_pending_emails()
            
            if not pending:
                self.logger.info("No pending emails")
                return {'sent': 0, 'errors': 0}
            
            # Connect
            if not self.sender.connect():
                self._emit('error', {'message': 'Failed to connect to SMTP'})
                return {'sent': 0, 'errors': 1}
            
            if not self.sheets.connect():
                self._emit('error', {'message': 'Failed to connect to Sheets'})
                return {'sent': 0, 'errors': 1}
            
            # Holiday mode limits
            holiday_mode = email_settings.get('holiday_mode', False)
            holiday_limit = 5 if holiday_mode else self.config.max_emails_per_day
            intro_only = holiday_mode  # Only send intros in holiday mode

            # Send emails
            for coach in pending:
                # Check daily limit (5 in holiday mode, normal limit otherwise)
                if self.state.daily_count >= holiday_limit:
                    self.logger.info(f"Daily limit reached ({holiday_limit})" + (" [holiday mode]" if holiday_mode else ""))
                    break

                # Prepare email
                last_name = coach['name'].split()[-1] if coach['name'] else 'Coach'
                used_ai = False
                email_type = 'intro'

                # Get the next AI email type for this coach (intro -> followup_1 -> followup_2)
                next_ai_type = self.state.get_next_ai_email_type(coach['email'])

                # In holiday mode, only send intro emails (skip followups)
                if intro_only and next_ai_type and next_ai_type != 'intro':
                    self.logger.info(f"Holiday mode: Skipping followup for {coach['school']}")
                    continue

                # Try to get AI email if there's a type available
                ai_email = None
                if next_ai_type:
                    ai_email = get_ai_email_for_school(coach['school'], coach['name'], next_ai_type)
                    if ai_email:
                        email_type = next_ai_type

                if ai_email:
                    # Use AI-generated email
                    subject = ai_email['subject']
                    body = ai_email['body']
                    used_ai = True
                    self.logger.info(f"Using AI email ({email_type}) for {coach['school']}")
                else:
                    # Fall back to template - AI emails exhausted for this coach
                    if coach['type'] == 'RC':
                        subject = RC_SUBJECT.format(
                            grad_year=self.config.graduation_year,
                            athlete_name=self.config.athlete_name
                        )
                        body = RC_TEMPLATE.format(
                            last_name=last_name,
                            athlete_name=self.config.athlete_name,
                            grad_year=self.config.graduation_year,
                            high_school=self.config.high_school or "[HIGH SCHOOL]",
                            city_state=self.config.city_state or "[CITY, STATE]",
                            school=coach['school'],
                            height=self.config.height,
                            weight=self.config.weight,
                            positions=self.config.positions,
                            gpa=self.config.gpa or "[GPA]",
                            highlight_url=self.config.highlight_url or "[HIGHLIGHT LINK]",
                            phone=self.config.phone or ""
                        )
                    else:
                        subject = OL_SUBJECT.format(
                            grad_year=self.config.graduation_year,
                            athlete_name=self.config.athlete_name
                        )
                        body = OL_TEMPLATE.format(
                            last_name=last_name,
                            athlete_name=self.config.athlete_name,
                            grad_year=self.config.graduation_year,
                            high_school=self.config.high_school or "[HIGH SCHOOL]",
                            city_state=self.config.city_state or "[CITY, STATE]",
                            school=coach['school'],
                            height=self.config.height,
                            weight=self.config.weight,
                            positions=self.config.positions,
                            highlight_url=self.config.highlight_url or "[HIGHLIGHT LINK]",
                            phone=self.config.phone or ""
                        )
                    email_type = 'template'
                    self.logger.info(f"Using template for {coach['school']} (AI exhausted)")
                
                # Send with tracking
                success, error = self.sender.send(
                    coach['email'], subject, body,
                    school=coach['school'],
                    coach_name=coach['name']
                )
                
                if success:
                    # Track this email for the coach (for 2-day gap and AI cycling)
                    self.state.mark_email_sent(coach['email'], email_type, used_ai)

                    # Mark as contacted in sheet
                    self.sheets.update_cell(
                        coach['row'],
                        coach['contacted_col'],
                        datetime.now().strftime('%Y-%m-%d')
                    )

                    sent += 1
                    self._emit('email_sent', {
                        'school': coach['school'],
                        'coach_type': coach['type'],
                        'email': coach['email'],
                        'email_type': email_type,
                        'used_ai': used_ai
                    })
                    self.logger.info(f"Sent to {coach['email']} ({email_type}, AI: {used_ai})")

                    # Short delay between emails to avoid spam filters
                    time.sleep(self.config.delay_between_emails)
                else:
                    self.state.add_error(coach['email'], error)
                    errors += 1
                    self._emit('email_error', {
                        'school': coach['school'],
                        'email': coach['email'],
                        'error': error
                    })

        except Exception as e:
            self.logger.error(f"Job error: {e}")
            errors += 1
        finally:
            self.sender.disconnect()
            self.sheets.disconnect()

        self._emit('job_completed', {'sent': sent, 'errors': errors})
        self.logger.info(f"Daily job complete: sent={sent}, errors={errors}")

        return {'sent': sent, 'errors': errors}
    
    def _emit(self, event: str, data: Dict):
        """Emit event to callbacks."""
        for callback in self._callbacks:
            try:
                callback(event, data)
            except:
                pass


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Email Scheduler')
    parser.add_argument('--run-now', action='store_true', help='Run immediately')
    parser.add_argument('--status', action='store_true', help='Show status')
    parser.add_argument('--pending', action='store_true', help='Show pending emails')
    args = parser.parse_args()
    
    scheduler = EmailScheduler()
    
    if args.status:
        status = scheduler.get_status()
        print(json.dumps(status, indent=2))
    elif args.pending:
        pending = scheduler.get_pending_emails()
        print(f"Pending emails: {len(pending)}")
        for p in pending[:10]:
            print(f"  {p['school']} - {p['type']}: {p['email']}")
    elif args.run_now:
        def callback(event, data):
            print(f"[{event}] {data}")
        scheduler.add_callback(callback)
        result = scheduler.run_now()
        print(f"Result: {result}")
    else:
        scheduler.start()
        print("Scheduler running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            scheduler.stop()


if __name__ == '__main__':
    main()
