#!/usr/bin/env python3
"""
Coach Contact Information Scraper
=================================
Scrapes coach emails and Twitter handles from school athletic websites using AI.

SETUP INSTRUCTIONS:
-------------------
1. Google Sheets Service Account:
   - Go to console.cloud.google.com
   - Create a project or select existing one
   - Enable Google Sheets API and Google Drive API
   - Create a Service Account (IAM & Admin > Service Accounts)
   - Download JSON credentials file
   - Share your Google Sheet with the service account email
   - Set GOOGLE_CREDENTIALS_PATH environment variable or place in same directory

2. Google Custom Search API (for Twitter lookups):
   - Go to console.cloud.google.com
   - Enable Custom Search API
   - Create API key (APIs & Services > Credentials)
   - Go to programmablesearchengine.google.com
   - Create a search engine (search whole web)
   - Get your Search Engine ID (cx)
   - Set GOOGLE_API_KEY and GOOGLE_CSE_ID environment variables

3. Ollama Setup:
   - Install Ollama: https://ollama.ai
   - Pull the model: ollama pull deepseek-r1:14b
   - Start server: ollama serve (runs on port 11434)

4. Install Dependencies:
   pip install gspread oauth2client requests beautifulsoup4

5. Run:
   python coach_scraper.py

Author: Coach Outreach Pro
Version: 1.0.0
"""

import os
import sys
import json
import time
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from urllib.parse import urljoin, urlparse

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
from bs4 import BeautifulSoup

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG = {
    # Ollama settings
    'OLLAMA_URL': os.environ.get('OLLAMA_URL', 'http://localhost:11434/api/generate'),
    'OLLAMA_MODEL': os.environ.get('OLLAMA_MODEL', 'deepseek-r1:14b'),
    'OLLAMA_TIMEOUT': 120,  # seconds

    # Google API settings
    'GOOGLE_API_KEY': os.environ.get('GOOGLE_API_KEY', ''),
    'GOOGLE_CSE_ID': os.environ.get('GOOGLE_CSE_ID', ''),  # Custom Search Engine ID
    'GOOGLE_CREDENTIALS_PATH': os.environ.get('GOOGLE_CREDENTIALS_PATH', 'google_credentials.json'),

    # Google Sheet settings
    'SHEET_NAME': 'bardeen',
    'ERRORS_SHEET_NAME': 'Errors',

    # Rate limiting
    'DELAY_BETWEEN_SCHOOLS': 3,  # seconds
    'DELAY_BETWEEN_REQUESTS': 1,  # seconds
    'DELAY_BETWEEN_OLLAMA': 0.5,  # seconds

    # Request settings
    'REQUEST_TIMEOUT': 15,  # seconds
    'MAX_RETRIES': 3,
    'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',

    # Progress tracking
    'PROGRESS_FILE': 'scraper_progress.json',

    # Logging
    'LOG_FILE': 'scraper.log',
    'LOG_LEVEL': logging.INFO,
}

# Column mapping (0-indexed)
COLUMNS = {
    'school_url': 0,
    'school': 0,  # Often same as URL or derived
    'rc_name': 1,
    'oc_name': 2,
    'rc_twitter': 3,
    'oc_twitter': 4,
    'rc_email': 5,
    'oc_email': 6,
    'rc_contacted': 7,
    'oc_contacted': 8,
    'rc_notes': 9,
    'oc_notes': 10,
}

# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logging():
    """Configure logging to both console and file."""
    logger = logging.getLogger('coach_scraper')
    logger.setLevel(CONFIG['LOG_LEVEL'])

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
    console_handler.setFormatter(console_format)

    # File handler
    file_handler = logging.FileHandler(CONFIG['LOG_FILE'])
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    file_handler.setFormatter(file_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger

logger = setup_logging()

# =============================================================================
# GOOGLE SHEETS FUNCTIONS
# =============================================================================

def get_sheets_client():
    """Initialize and return Google Sheets client."""
    try:
        scope = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive'
        ]

        creds_path = CONFIG['GOOGLE_CREDENTIALS_PATH']

        # Try environment variable with JSON content
        creds_json = os.environ.get('GOOGLE_CREDENTIALS', '')
        if creds_json:
            import json
            creds_dict = json.loads(creds_json)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        elif os.path.exists(creds_path):
            creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
        else:
            logger.error(f"Google credentials not found at {creds_path}")
            logger.error("Set GOOGLE_CREDENTIALS_PATH or GOOGLE_CREDENTIALS env var")
            return None

        client = gspread.authorize(creds)
        logger.info("Google Sheets client initialized successfully")
        return client

    except Exception as e:
        logger.error(f"Failed to initialize Google Sheets: {e}")
        return None


def get_or_create_errors_sheet(spreadsheet):
    """Get or create the Errors worksheet."""
    try:
        return spreadsheet.worksheet(CONFIG['ERRORS_SHEET_NAME'])
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=CONFIG['ERRORS_SHEET_NAME'], rows=1000, cols=5)
        sheet.update('A1:E1', [['Timestamp', 'School', 'Step', 'Error', 'Details']])
        logger.info("Created Errors worksheet")
        return sheet


def log_error_to_sheet(errors_sheet, school: str, step: str, error: str, details: str = ''):
    """Log an error to the Errors sheet."""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        errors_sheet.append_row([timestamp, school, step, str(error)[:500], str(details)[:500]])
    except Exception as e:
        logger.warning(f"Could not log error to sheet: {e}")

# =============================================================================
# PROGRESS TRACKING
# =============================================================================

def load_progress() -> Dict:
    """Load progress from file to enable resume."""
    try:
        if os.path.exists(CONFIG['PROGRESS_FILE']):
            with open(CONFIG['PROGRESS_FILE'], 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load progress: {e}")
    return {'completed_rows': [], 'last_row': 0}


def save_progress(progress: Dict):
    """Save progress to file."""
    try:
        with open(CONFIG['PROGRESS_FILE'], 'w') as f:
            json.dump(progress, f)
    except Exception as e:
        logger.warning(f"Could not save progress: {e}")


def clear_progress():
    """Clear progress file (for fresh start)."""
    if os.path.exists(CONFIG['PROGRESS_FILE']):
        os.remove(CONFIG['PROGRESS_FILE'])
        logger.info("Progress file cleared")

# =============================================================================
# HTTP REQUEST FUNCTIONS
# =============================================================================

def fetch_url(url: str, retries: int = None) -> Optional[str]:
    """Fetch URL content with retries and error handling."""
    if retries is None:
        retries = CONFIG['MAX_RETRIES']

    headers = {
        'User-Agent': CONFIG['USER_AGENT'],
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }

    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=CONFIG['REQUEST_TIMEOUT'],
                allow_redirects=True
            )
            response.raise_for_status()
            return response.text

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout fetching {url} (attempt {attempt + 1}/{retries})")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error fetching {url}: {e} (attempt {attempt + 1}/{retries})")

        if attempt < retries - 1:
            time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])

    return None


def clean_html_for_ollama(html: str, max_length: int = 50000) -> str:
    """Clean and truncate HTML for sending to Ollama."""
    try:
        soup = BeautifulSoup(html, 'html.parser')

        # Remove script and style elements
        for element in soup(['script', 'style', 'noscript', 'iframe', 'svg', 'path']):
            element.decompose()

        # Get text content with some structure
        text = soup.get_text(separator='\n', strip=True)

        # Also extract links (important for finding directories)
        links = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text_content = a.get_text(strip=True)
            if text_content and len(text_content) < 100:
                links.append(f"[{text_content}]({href})")

        # Combine text and relevant links
        result = text[:max_length // 2]
        if links:
            result += "\n\nLINKS FOUND:\n" + "\n".join(links[:100])

        return result[:max_length]

    except Exception as e:
        logger.warning(f"Error cleaning HTML: {e}")
        return html[:max_length]

# =============================================================================
# OLLAMA AI FUNCTIONS
# =============================================================================

def query_ollama(prompt: str, system_prompt: str = None) -> Optional[Dict]:
    """Send a query to Ollama and get JSON response."""
    try:
        # Build the full prompt
        full_prompt = ""
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n"
        full_prompt += prompt
        full_prompt += "\n\nRespond with ONLY valid JSON, no markdown, no explanation, no code blocks."

        payload = {
            'model': CONFIG['OLLAMA_MODEL'],
            'prompt': full_prompt,
            'stream': False,
            'options': {
                'temperature': 0.1,  # Low temperature for more deterministic responses
            }
        }

        response = requests.post(
            CONFIG['OLLAMA_URL'],
            json=payload,
            timeout=CONFIG['OLLAMA_TIMEOUT']
        )
        response.raise_for_status()

        result = response.json()
        response_text = result.get('response', '')

        # Try to extract JSON from response
        json_match = re.search(r'\{[^{}]*\}|\[[^\[\]]*\]', response_text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        # Try parsing the whole response
        try:
            # Remove markdown code blocks if present
            cleaned = re.sub(r'```json\s*', '', response_text)
            cleaned = re.sub(r'```\s*', '', cleaned)
            cleaned = cleaned.strip()
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning(f"Could not parse Ollama response as JSON: {response_text[:200]}")
            return None

    except requests.exceptions.Timeout:
        logger.error("Ollama request timed out")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Ollama request failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Ollama error: {e}")
        return None


def find_staff_directory_url(html: str, base_url: str) -> Optional[str]:
    """Use Ollama to find the staff directory URL from athletics page."""
    prompt = f"""Analyze this college athletics website content and find the URL to the football staff directory or coaching staff page.

BASE URL: {base_url}

WEBSITE CONTENT:
{clean_html_for_ollama(html, 30000)}

Find the link to the football coaching staff page. Look for links containing words like:
- "staff", "coaches", "directory", "football staff", "coaching staff"

Return JSON with:
- "directory_url": the full URL to the staff directory (or null if not found)
- "confidence": "high", "medium", or "low"
"""

    result = query_ollama(prompt)
    if result and result.get('directory_url'):
        url = result['directory_url']
        # Make absolute URL if relative
        if not url.startswith('http'):
            url = urljoin(base_url, url)
        return url
    return None


def find_coach_profile_urls(html: str, base_url: str, rc_name: str, oc_name: str) -> Dict[str, Optional[str]]:
    """Use Ollama to find individual coach profile URLs from staff directory."""
    prompt = f"""Analyze this football staff directory page and find profile URLs for these coaches:

1. Recruiting Coordinator: {rc_name}
2. Offensive Line Coach: {oc_name}

WEBSITE CONTENT:
{clean_html_for_ollama(html, 40000)}

Find the individual profile page URLs for each coach. Look for their names in the staff list.

Return JSON with:
{{
    "rc_profile_url": "full URL to recruiting coordinator's profile page or null",
    "oc_profile_url": "full URL to offensive line coach's profile page or null"
}}
"""

    result = query_ollama(prompt)
    urls = {'rc_profile_url': None, 'oc_profile_url': None}

    if result:
        if result.get('rc_profile_url'):
            url = result['rc_profile_url']
            if not url.startswith('http'):
                url = urljoin(base_url, url)
            urls['rc_profile_url'] = url

        if result.get('oc_profile_url'):
            url = result['oc_profile_url']
            if not url.startswith('http'):
                url = urljoin(base_url, url)
            urls['oc_profile_url'] = url

    return urls


def extract_email_from_profile(html: str, coach_name: str) -> Optional[str]:
    """Use Ollama to extract email from coach profile page."""
    # First try regex for common email patterns
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(email_pattern, html)

    # Filter out common non-coach emails
    excluded = ['info@', 'tickets@', 'support@', 'webmaster@', 'admin@', 'noreply@']
    valid_emails = [e for e in emails if not any(x in e.lower() for x in excluded)]

    if len(valid_emails) == 1:
        return valid_emails[0]

    # Use Ollama if multiple or no emails found
    prompt = f"""Extract the email address for coach {coach_name} from this profile page.

PROFILE CONTENT:
{clean_html_for_ollama(html, 20000)}

Find the coach's direct email address (not general athletics email).

Return JSON with:
{{
    "email": "coach's email address or null if not found",
    "confidence": "high", "medium", or "low"
}}
"""

    result = query_ollama(prompt)
    if result and result.get('email') and '@' in str(result.get('email', '')):
        return result['email']

    # Return first valid email if found
    if valid_emails:
        return valid_emails[0]

    return None

# =============================================================================
# TWITTER SEARCH FUNCTIONS
# =============================================================================

def google_search_twitter(coach_name: str, school_name: str) -> Optional[str]:
    """Search Google for coach's Twitter profile."""
    if not CONFIG['GOOGLE_API_KEY'] or not CONFIG['GOOGLE_CSE_ID']:
        logger.warning("Google API credentials not configured for Twitter search")
        return None

    try:
        query = f'"{coach_name}" "{school_name}" site:twitter.com OR site:x.com'

        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            'key': CONFIG['GOOGLE_API_KEY'],
            'cx': CONFIG['GOOGLE_CSE_ID'],
            'q': query,
            'num': 5
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        results = response.json()
        items = results.get('items', [])

        for item in items:
            link = item.get('link', '')
            # Check if it's a Twitter/X profile URL
            if re.match(r'https?://(twitter\.com|x\.com)/[a-zA-Z0-9_]+/?$', link):
                return link

        return None

    except Exception as e:
        logger.warning(f"Google search failed: {e}")
        return None


def extract_twitter_handle(url: str) -> Optional[str]:
    """Extract Twitter handle from URL."""
    if not url:
        return None
    match = re.search(r'(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)', url)
    if match:
        handle = match.group(1)
        # Filter out non-profile pages
        if handle.lower() not in ['home', 'search', 'explore', 'messages', 'settings', 'i']:
            return handle
    return None


def verify_twitter_profile(twitter_url: str, coach_name: str, school_name: str) -> Dict:
    """Fetch Twitter profile and verify it belongs to the correct coach."""
    try:
        html = fetch_url(twitter_url)
        if not html:
            return {'verified': False, 'handle': None, 'confidence': 'low'}

        prompt = f"""Analyze this Twitter/X profile page and determine if it belongs to:
Coach Name: {coach_name}
School: {school_name}

PROFILE CONTENT:
{clean_html_for_ollama(html, 15000)}

Check if the bio mentions:
- The coach's name or similar
- The school name or abbreviation
- Football coaching role

Return JSON with:
{{
    "verified": true or false,
    "confidence": "high", "medium", or "low",
    "handle": "the Twitter handle without @",
    "reason": "brief explanation"
}}
"""

        result = query_ollama(prompt)
        if result:
            return {
                'verified': result.get('verified', False),
                'handle': result.get('handle'),
                'confidence': result.get('confidence', 'low')
            }

        # Fallback: just extract handle
        handle = extract_twitter_handle(twitter_url)
        return {'verified': False, 'handle': handle, 'confidence': 'low'}

    except Exception as e:
        logger.warning(f"Twitter verification failed: {e}")
        return {'verified': False, 'handle': None, 'confidence': 'low'}

# =============================================================================
# MAIN SCRAPING LOGIC
# =============================================================================

def scrape_school(row_data: List[str], row_num: int, school_name: str) -> Dict[str, Optional[str]]:
    """Scrape coach information for a single school."""
    results = {
        'rc_email': None,
        'oc_email': None,
        'rc_twitter': None,
        'oc_twitter': None,
    }

    # Get existing data
    school_url = row_data[COLUMNS['school_url']] if len(row_data) > COLUMNS['school_url'] else ''
    rc_name = row_data[COLUMNS['rc_name']] if len(row_data) > COLUMNS['rc_name'] else ''
    oc_name = row_data[COLUMNS['oc_name']] if len(row_data) > COLUMNS['oc_name'] else ''
    existing_rc_email = row_data[COLUMNS['rc_email']] if len(row_data) > COLUMNS['rc_email'] else ''
    existing_oc_email = row_data[COLUMNS['oc_email']] if len(row_data) > COLUMNS['oc_email'] else ''
    existing_rc_twitter = row_data[COLUMNS['rc_twitter']] if len(row_data) > COLUMNS['rc_twitter'] else ''
    existing_oc_twitter = row_data[COLUMNS['oc_twitter']] if len(row_data) > COLUMNS['oc_twitter'] else ''

    # Determine what we need to find
    need_rc_email = not existing_rc_email or '@' not in existing_rc_email
    need_oc_email = not existing_oc_email or '@' not in existing_oc_email
    need_rc_twitter = not existing_rc_twitter
    need_oc_twitter = not existing_oc_twitter

    logger.info(f"  Need: RC email={need_rc_email}, OC email={need_oc_email}, RC twitter={need_rc_twitter}, OC twitter={need_oc_twitter}")

    # If no URL, try to construct one from school name
    if not school_url or not school_url.startswith('http'):
        logger.warning(f"  No valid URL for {school_name}, skipping email scrape")
    else:
        # ===== EMAIL EXTRACTION =====
        if need_rc_email or need_oc_email:
            logger.info("  Step 1: Fetching main athletics page...")
            main_html = fetch_url(school_url)

            if main_html:
                time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])

                logger.info("  Step 2: Finding staff directory URL...")
                directory_url = find_staff_directory_url(main_html, school_url)

                if directory_url:
                    logger.info(f"  Found directory: {directory_url}")
                    time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])

                    directory_html = fetch_url(directory_url)

                    if directory_html:
                        time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])

                        logger.info("  Step 3: Finding coach profile URLs...")
                        profile_urls = find_coach_profile_urls(directory_html, directory_url, rc_name, oc_name)

                        # Fetch RC profile
                        if need_rc_email and profile_urls.get('rc_profile_url'):
                            logger.info(f"  Step 4a: Fetching RC profile...")
                            time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
                            rc_html = fetch_url(profile_urls['rc_profile_url'])
                            if rc_html:
                                time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])
                                email = extract_email_from_profile(rc_html, rc_name)
                                if email:
                                    results['rc_email'] = email
                                    logger.info(f"  Found RC email: {email}")

                        # Fetch OC profile
                        if need_oc_email and profile_urls.get('oc_profile_url'):
                            logger.info(f"  Step 4b: Fetching OC profile...")
                            time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
                            oc_html = fetch_url(profile_urls['oc_profile_url'])
                            if oc_html:
                                time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])
                                email = extract_email_from_profile(oc_html, oc_name)
                                if email:
                                    results['oc_email'] = email
                                    logger.info(f"  Found OC email: {email}")
                else:
                    logger.warning("  Could not find staff directory URL")
            else:
                logger.warning(f"  Could not fetch main page: {school_url}")

    # ===== TWITTER SEARCH =====
    if need_rc_twitter and rc_name:
        logger.info("  Step 5a: Searching for RC Twitter...")
        time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
        twitter_url = google_search_twitter(rc_name, school_name)

        if twitter_url:
            logger.info(f"  Found potential Twitter: {twitter_url}")
            time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
            verification = verify_twitter_profile(twitter_url, rc_name, school_name)

            if verification.get('verified') or verification.get('confidence') in ['high', 'medium']:
                handle = verification.get('handle') or extract_twitter_handle(twitter_url)
                if handle:
                    results['rc_twitter'] = handle
                    logger.info(f"  Verified RC Twitter: @{handle}")
            else:
                logger.info(f"  Twitter not verified (confidence: {verification.get('confidence')})")

    if need_oc_twitter and oc_name:
        logger.info("  Step 5b: Searching for OC Twitter...")
        time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
        twitter_url = google_search_twitter(oc_name, school_name)

        if twitter_url:
            logger.info(f"  Found potential Twitter: {twitter_url}")
            time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
            verification = verify_twitter_profile(twitter_url, oc_name, school_name)

            if verification.get('verified') or verification.get('confidence') in ['high', 'medium']:
                handle = verification.get('handle') or extract_twitter_handle(twitter_url)
                if handle:
                    results['oc_twitter'] = handle
                    logger.info(f"  Verified OC Twitter: @{handle}")
            else:
                logger.info(f"  Twitter not verified (confidence: {verification.get('confidence')})")

    return results


def update_sheet_row(sheet, row_num: int, results: Dict[str, Optional[str]], row_data: List[str]):
    """Update a row in the sheet with found data (only fill missing values)."""
    updates = []

    # Check each result and only update if we found something AND cell is empty
    if results.get('rc_email'):
        existing = row_data[COLUMNS['rc_email']] if len(row_data) > COLUMNS['rc_email'] else ''
        if not existing or '@' not in existing:
            col_letter = chr(ord('A') + COLUMNS['rc_email'])
            updates.append((f'{col_letter}{row_num}', results['rc_email']))

    if results.get('oc_email'):
        existing = row_data[COLUMNS['oc_email']] if len(row_data) > COLUMNS['oc_email'] else ''
        if not existing or '@' not in existing:
            col_letter = chr(ord('A') + COLUMNS['oc_email'])
            updates.append((f'{col_letter}{row_num}', results['oc_email']))

    if results.get('rc_twitter'):
        existing = row_data[COLUMNS['rc_twitter']] if len(row_data) > COLUMNS['rc_twitter'] else ''
        if not existing:
            col_letter = chr(ord('A') + COLUMNS['rc_twitter'])
            updates.append((f'{col_letter}{row_num}', results['rc_twitter']))

    if results.get('oc_twitter'):
        existing = row_data[COLUMNS['oc_twitter']] if len(row_data) > COLUMNS['oc_twitter'] else ''
        if not existing:
            col_letter = chr(ord('A') + COLUMNS['oc_twitter'])
            updates.append((f'{col_letter}{row_num}', results['oc_twitter']))

    # Apply updates
    for cell, value in updates:
        try:
            sheet.update(cell, value)
            logger.debug(f"Updated {cell} = {value}")
        except Exception as e:
            logger.warning(f"Could not update {cell}: {e}")

    return len(updates)


def should_process_row(row_data: List[str]) -> Tuple[bool, str]:
    """Determine if a row should be processed."""
    # Check if we have coach names
    rc_name = row_data[COLUMNS['rc_name']] if len(row_data) > COLUMNS['rc_name'] else ''
    oc_name = row_data[COLUMNS['oc_name']] if len(row_data) > COLUMNS['oc_name'] else ''

    if not rc_name and not oc_name:
        return False, "No coach names"

    # Check if all data is already complete
    rc_email = row_data[COLUMNS['rc_email']] if len(row_data) > COLUMNS['rc_email'] else ''
    oc_email = row_data[COLUMNS['oc_email']] if len(row_data) > COLUMNS['oc_email'] else ''
    rc_twitter = row_data[COLUMNS['rc_twitter']] if len(row_data) > COLUMNS['rc_twitter'] else ''
    oc_twitter = row_data[COLUMNS['oc_twitter']] if len(row_data) > COLUMNS['oc_twitter'] else ''

    has_rc_email = rc_email and '@' in rc_email
    has_oc_email = oc_email and '@' in oc_email
    has_rc_twitter = bool(rc_twitter)
    has_oc_twitter = bool(oc_twitter)

    # If RC exists and all RC data is complete, and same for OC, skip
    rc_complete = not rc_name or (has_rc_email and has_rc_twitter)
    oc_complete = not oc_name or (has_oc_email and has_oc_twitter)

    if rc_complete and oc_complete:
        return False, "Data already complete"

    return True, "Needs data"

# =============================================================================
# MAIN FUNCTION
# =============================================================================

def main():
    """Main entry point for the scraper."""
    print("\n" + "=" * 60)
    print("  COACH CONTACT INFORMATION SCRAPER")
    print("=" * 60 + "\n")

    # Check Ollama is running
    logger.info("Checking Ollama connection...")
    try:
        test_response = requests.get(CONFIG['OLLAMA_URL'].replace('/api/generate', '/api/tags'), timeout=5)
        if test_response.status_code != 200:
            logger.error("Ollama is not responding. Make sure it's running: ollama serve")
            return
        logger.info("Ollama is running")
    except Exception as e:
        logger.error(f"Cannot connect to Ollama at {CONFIG['OLLAMA_URL']}: {e}")
        logger.error("Make sure Ollama is running: ollama serve")
        return

    # Initialize Google Sheets
    logger.info("Connecting to Google Sheets...")
    client = get_sheets_client()
    if not client:
        return

    try:
        spreadsheet = client.open(CONFIG['SHEET_NAME'])
        sheet = spreadsheet.sheet1  # Main sheet
        errors_sheet = get_or_create_errors_sheet(spreadsheet)
        logger.info(f"Connected to spreadsheet: {CONFIG['SHEET_NAME']}")
    except Exception as e:
        logger.error(f"Could not open spreadsheet '{CONFIG['SHEET_NAME']}': {e}")
        return

    # Load all data
    logger.info("Loading data from sheet...")
    all_data = sheet.get_all_values()
    if len(all_data) < 2:
        logger.error("Sheet is empty or has only headers")
        return

    headers = all_data[0]
    rows = all_data[1:]
    logger.info(f"Found {len(rows)} rows to process")

    # Load progress
    progress = load_progress()
    completed_rows = set(progress.get('completed_rows', []))

    # Stats
    stats = {
        'total': len(rows),
        'processed': 0,
        'skipped': 0,
        'updated': 0,
        'errors': 0,
    }

    # Process each row
    for idx, row_data in enumerate(rows):
        row_num = idx + 2  # Account for header row and 1-indexing

        # Skip if already completed in previous run
        if row_num in completed_rows:
            stats['skipped'] += 1
            continue

        # Get school name for display
        school_name = row_data[0] if row_data else f"Row {row_num}"

        # Check if row needs processing
        should_process, reason = should_process_row(row_data)
        if not should_process:
            logger.info(f"[{idx + 1}/{len(rows)}] Skipping {school_name}: {reason}")
            stats['skipped'] += 1
            completed_rows.add(row_num)
            continue

        logger.info(f"\n[{idx + 1}/{len(rows)}] Processing: {school_name}")

        try:
            # Scrape this school
            results = scrape_school(row_data, row_num, school_name)

            # Update sheet with results
            updates = update_sheet_row(sheet, row_num, results, row_data)

            if updates > 0:
                stats['updated'] += 1
                logger.info(f"  Updated {updates} fields")

            stats['processed'] += 1
            completed_rows.add(row_num)

        except Exception as e:
            logger.error(f"  Error processing {school_name}: {e}")
            log_error_to_sheet(errors_sheet, school_name, "scrape_school", str(e))
            stats['errors'] += 1

        # Save progress after each school
        progress['completed_rows'] = list(completed_rows)
        progress['last_row'] = row_num
        save_progress(progress)

        # Delay between schools
        if idx < len(rows) - 1:
            time.sleep(CONFIG['DELAY_BETWEEN_SCHOOLS'])

    # Print summary
    print("\n" + "=" * 60)
    print("  SCRAPING COMPLETE")
    print("=" * 60)
    print(f"  Total rows:     {stats['total']}")
    print(f"  Processed:      {stats['processed']}")
    print(f"  Skipped:        {stats['skipped']}")
    print(f"  Updated:        {stats['updated']}")
    print(f"  Errors:         {stats['errors']}")
    print("=" * 60 + "\n")

    # Clear progress file on successful completion
    if stats['errors'] == 0:
        clear_progress()
        logger.info("All done! Progress file cleared.")
    else:
        logger.info(f"Completed with {stats['errors']} errors. Run again to retry failed rows.")


if __name__ == '__main__':
    main()
