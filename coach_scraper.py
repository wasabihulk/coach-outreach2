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
import random
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from urllib.parse import urljoin, urlparse

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
from bs4 import BeautifulSoup

# Selenium imports for browser-based search
try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    print("Warning: Selenium not available. Install with: pip install selenium undetected-chromedriver")

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG = {
    # Ollama settings
    'OLLAMA_URL': os.environ.get('OLLAMA_URL', 'http://localhost:11434/api/generate'),
    'OLLAMA_MODEL': os.environ.get('OLLAMA_MODEL', 'llama3.2:3b'),  # Smaller and faster
    'OLLAMA_TIMEOUT': 180,  # seconds - longer for large HTML pages

    # Google API settings
    'GOOGLE_API_KEY': os.environ.get('GOOGLE_API_KEY', 'AIzaSyBSEzp2OF4lsFWgC-2goTfrZdRoKV_VyfA'),
    'GOOGLE_CSE_ID': os.environ.get('GOOGLE_CSE_ID', 'a37e7aad7fd3c4c7a'),  # Custom Search Engine ID
    'GOOGLE_CREDENTIALS_PATH': os.environ.get('GOOGLE_CREDENTIALS_PATH', '/Users/keelanunderwood/Desktop/credentials.json'),

    # Google Sheet settings
    'SHEET_NAME': 'bardeen',
    'ERRORS_SHEET_NAME': 'Errors',
    'TEST_SHEET_NAME': 'Test Results',  # Write results here for verification
    'TEST_MODE': False,  # Write directly to main sheet

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

    # Limit (set to 0 for unlimited)
    'LIMIT': 0,  # Process all schools
}

# Column mapping (0-indexed) - matches actual spreadsheet structure
# Headers: School, URL, recruiting coordinator name, Oline Coach, RC twitter, OC twitter, RC email, OC email
COLUMNS = {
    'school': 0,
    'school_url': 1,
    'rc_name': 2,
    'oc_name': 3,
    'rc_twitter': 4,
    'oc_twitter': 5,
    'rc_email': 6,
    'oc_email': 7,
    'rc_contacted': 8,
    'oc_contacted': 9,
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
        sheet.update(range_name='A1:E1', values=[['Timestamp', 'School', 'Step', 'Error', 'Details']])
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
                parsed = json.loads(json_match.group())
                # Ensure we always return a dict, not a list
                if isinstance(parsed, list):
                    # If it's a list, try to find a dict in it or return None
                    for item in parsed:
                        if isinstance(item, dict):
                            return item
                    return None
                return parsed
            except json.JSONDecodeError:
                pass

        # Try parsing the whole response
        try:
            # Remove markdown code blocks if present
            cleaned = re.sub(r'```json\s*', '', response_text)
            cleaned = re.sub(r'```\s*', '', cleaned)
            cleaned = cleaned.strip()
            parsed = json.loads(cleaned)
            # Ensure we always return a dict, not a list
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        return item
                return None
            return parsed
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


def find_head_coach_from_page(html: str, base_url: str) -> Optional[Dict]:
    """Find the Head Coach's info from the coaches page as a fallback."""
    soup = BeautifulSoup(html, 'html.parser')

    # Look for "Head Coach" text patterns
    head_coach_patterns = ['head coach', 'head football coach', 'hc']

    # Find text containing "Head Coach"
    for pattern in head_coach_patterns:
        elements = soup.find_all(string=re.compile(pattern, re.IGNORECASE))
        for element in elements:
            # Get parent context
            parent = element.find_parent()
            if parent:
                # Look for a link nearby (profile link)
                link = parent.find('a', href=True)
                if link:
                    href = link['href']
                    if not href.startswith('http'):
                        href = urljoin(base_url, href)
                    name = link.get_text(strip=True)
                    if name and len(name) < 50:
                        return {'name': name, 'url': href}

                # Look for name in same section
                name_text = parent.get_text(separator=' ', strip=True)
                # Try to extract a name (2-3 capitalized words before/after "Head Coach")
                match = re.search(r'([A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', name_text)
                if match:
                    return {'name': match.group(1), 'url': None}

    return None


def find_coaches_from_directory(html: str, school_name: str) -> Dict[str, Optional[str]]:
    """Find Recruiting Coordinator and O-Line Coach names from a coaches directory page.

    This is used when coach names are missing from the spreadsheet - we need to identify them first.

    Returns:
        dict with 'rc_name' and 'oc_name' (both may be None if not found)
    """
    result = {'rc_name': None, 'oc_name': None}

    if not html:
        return result

    # Clean HTML for AI
    clean_html = clean_html_for_ollama(html, 25000)

    prompt = f"""Analyze this football coaching staff page and identify TWO specific coaches:

1. RECRUITING COORDINATOR (RC) - The coach responsible for recruiting. May have titles like:
   - Recruiting Coordinator
   - Director of Player Personnel
   - Director of Recruiting
   - Assistant Coach/Recruiting

2. OFFENSIVE LINE COACH (OC) - The coach who coaches the offensive line. May have titles like:
   - Offensive Line Coach
   - O-Line Coach
   - OL Coach
   - Offensive Line/Run Game Coordinator

COACHING STAFF PAGE FOR {school_name}:
{clean_html}

Return ONLY a JSON object with the full names of these coaches:
{{"rc_name": "First Last" or null, "oc_name": "First Last" or null}}

IMPORTANT:
- Return the FULL NAME (first and last name)
- If a coach has multiple roles, still include them
- If you can't find one, return null for that position
- Do NOT include titles, just the name"""

    try:
        response = query_ollama(prompt)
        if response:
            # Try to parse JSON from response
            json_match = re.search(r'\{[^}]+\}', response)
            if json_match:
                data = json.loads(json_match.group())
                if data.get('rc_name'):
                    result['rc_name'] = data['rc_name'].strip()
                    logger.info(f"  ✓ Found RC: {result['rc_name']}")
                if data.get('oc_name'):
                    result['oc_name'] = data['oc_name'].strip()
                    logger.info(f"  ✓ Found OC: {result['oc_name']}")
    except Exception as e:
        logger.warning(f"  Error finding coaches from directory: {e}")

    return result


def find_coach_profile_urls(html: str, base_url: str, rc_name: str, oc_name: str, find_head_coach: bool = False) -> Dict[str, Optional[str]]:
    """Find coach profile URLs by extracting links and having AI select from them."""
    urls = {'rc_profile_url': None, 'oc_profile_url': None, 'hc_profile_url': None, 'hc_name': None}

    # First, extract all links from the page
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(strip=True)
        if text and len(text) < 100:
            # Make URL absolute
            if not href.startswith('http'):
                href = urljoin(base_url, href)
            # Filter to likely coach profile links
            href_lower = href.lower()
            if any(x in href_lower for x in ['coach', 'staff', 'roster', 'directory', 'bio']):
                links.append({'url': href, 'text': text})

    if not links:
        return urls

    # Create numbered list of links for AI to choose from
    link_list = "\n".join([f"{i+1}. [{link['text']}] -> {link['url']}" for i, link in enumerate(links[:50])])

    # Get name parts for matching
    rc_parts = rc_name.lower().split() if rc_name else []
    oc_parts = oc_name.lower().split() if oc_name else []

    # First try simple name matching (no AI needed)
    for link in links:
        text_lower = link['text'].lower()
        url_lower = link['url'].lower()

        # Check for RC match
        if rc_name and not urls['rc_profile_url']:
            if any(part in text_lower or part in url_lower for part in rc_parts if len(part) > 2):
                urls['rc_profile_url'] = link['url']

        # Check for OC match
        if oc_name and not urls['oc_profile_url']:
            if any(part in text_lower or part in url_lower for part in oc_parts if len(part) > 2):
                urls['oc_profile_url'] = link['url']

        # Check for Head Coach if requested
        if find_head_coach and not urls['hc_profile_url']:
            if 'head coach' in text_lower or 'head football' in text_lower:
                urls['hc_profile_url'] = link['url']
                # Try to extract the name
                name_match = re.search(r'([A-Z][a-z]+ [A-Z][a-z]+)', link['text'])
                if name_match:
                    urls['hc_name'] = name_match.group(1)

    # If simple matching didn't find coaches, use AI
    need_ai = ((rc_name and not urls['rc_profile_url']) or
               (oc_name and not urls['oc_profile_url']) or
               (find_head_coach and not urls['hc_profile_url']))

    if need_ai:
        hc_instruction = "\nHC (Head Coach): Look for 'Head Coach' or 'Head Football Coach' title" if find_head_coach else ""

        prompt = f"""Find the profile links for these football coaches from the list below.
Look for names that match OR job titles that match.

COACHES TO FIND:
- RC (Recruiting Coordinator): {rc_name if rc_name else 'Not specified - look for title containing Recruiting'}
- OC (Offensive Line Coach): {oc_name if oc_name else 'Not specified - look for title containing O-Line or Offensive Line'}{hc_instruction}

AVAILABLE LINKS (pick by number only):
{link_list}

IMPORTANT:
- Match by NAME first (look for last name matches)
- If name not found, match by JOB TITLE (Recruiting Coordinator, Offensive Line, Head Coach)
- Titles may be abbreviated: RC, OC, OL Coach, Recruiting, etc.

Return JSON with LINK NUMBERS only:
{{"rc_link_number": number or null, "oc_link_number": number or null{', "hc_link_number": number or null' if find_head_coach else ''}}}"""

        result = query_ollama(prompt)
        if result:
            # Get RC URL by number
            if not urls['rc_profile_url'] and result.get('rc_link_number'):
                try:
                    idx = int(result['rc_link_number']) - 1
                    if 0 <= idx < len(links):
                        urls['rc_profile_url'] = links[idx]['url']
                except (ValueError, TypeError):
                    pass

            # Get OC URL by number
            if not urls['oc_profile_url'] and result.get('oc_link_number'):
                try:
                    idx = int(result['oc_link_number']) - 1
                    if 0 <= idx < len(links):
                        urls['oc_profile_url'] = links[idx]['url']
                except (ValueError, TypeError):
                    pass

            # Get HC URL by number
            if find_head_coach and not urls['hc_profile_url'] and result.get('hc_link_number'):
                try:
                    idx = int(result['hc_link_number']) - 1
                    if 0 <= idx < len(links):
                        urls['hc_profile_url'] = links[idx]['url']
                        urls['hc_name'] = links[idx]['text']
                except (ValueError, TypeError):
                    pass

    return urls


def find_school_football_twitter(html: str, school_name: str) -> Optional[str]:
    """Find the school's official football Twitter handle from the athletics page.

    Returns the handle without @ prefix, or None if not found.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, 'html.parser')
    school_lower = school_name.lower()

    # Common patterns for school football Twitter handles
    # Look for Twitter links that look institutional (contain school name or 'football')
    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        if 'twitter.com' in href or 'x.com' in href:
            handle = extract_twitter_handle(a['href'])
            if handle:
                handle_lower = handle.lower()
                # Check if this looks like a football account
                if 'football' in handle_lower or 'fb' in handle_lower:
                    return handle
                # Check if it matches school name patterns
                school_parts = school_lower.replace('university', '').replace('college', '').replace('state', '').split()
                for part in school_parts:
                    if len(part) > 3 and part in handle_lower:
                        # Likely an institutional account
                        return handle

    # Also check for common patterns in text
    text = soup.get_text()
    patterns = [
        r'(?:follow\s+(?:us\s+)?(?:on\s+)?(?:twitter|x)[:\s]+)?@([a-zA-Z0-9_]*(?:football|fb)[a-zA-Z0-9_]*)',
        r'@([a-zA-Z0-9_]*(?:football|fb)[a-zA-Z0-9_]*)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text, re.I)
        for match in matches:
            if len(match) > 3:
                return match

    return None


def search_school_twitter_following(school_twitter: str, coach_name: str, school_name: str) -> Optional[str]:
    """Search for a coach by looking at the school's Twitter following/interactions.

    Strategy:
    1. Search Google for the school Twitter + coach name to find interactions
    2. Look for the coach's Twitter handle in search results
    """
    if not school_twitter or not coach_name:
        return None

    name_parts = coach_name.split()
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[-1] if len(name_parts) > 1 else ''

    # Clean school twitter handle
    school_handle = school_twitter.lstrip('@')

    if not SELENIUM_AVAILABLE:
        return None

    # Search queries that leverage the school's Twitter presence
    search_queries = [
        # Search for coach mentioned with school Twitter
        f'site:twitter.com "{coach_name}" @{school_handle}',
        f'site:x.com "{coach_name}" @{school_handle}',
        # Search for coach + school football
        f'site:twitter.com {first_name} {last_name} {school_handle}',
        # Look for school Twitter mentioning coach
        f'site:twitter.com from:{school_handle} {last_name}',
        f'site:twitter.com @{school_handle} {last_name} coach',
        # Search for coach following/interacting with school
        f'site:twitter.com {last_name} coach {school_handle}',
    ]

    found_handles = []

    for query in search_queries[:4]:  # Limit queries
        try:
            results = search_google_selenium(query, num_results=10)

            for url in results:
                # Extract Twitter handles from URLs
                twitter_match = re.match(
                    r'https?://(?:www\.)?(twitter\.com|x\.com)/([a-zA-Z0-9_]+)/?(?:\?.*)?$',
                    url
                )
                if twitter_match:
                    handle = twitter_match.group(2)
                    # Skip the school's own handle
                    if handle.lower() != school_handle.lower():
                        # Check if it could be the coach
                        if is_valid_twitter_handle(handle, first_name, last_name, school_name):
                            found_handles.append(handle)

            if found_handles:
                # Return first valid handle
                return f"@{found_handles[0]}"

            human_like_delay(0.8, 1.5)

        except Exception as e:
            logger.debug(f"School Twitter search failed: {e}")
            continue

    return None


def extract_twitter_from_coaches_page(html: str, rc_name: str, oc_name: str, school_name: str = '') -> Dict[str, Optional[str]]:
    """Try to extract coach Twitter handles directly from the coaches listing page.

    IMPORTANT: Only returns personal coach handles, not institutional/team accounts.
    """
    results = {'rc_twitter': None, 'oc_twitter': None}

    # Find all Twitter/X links on the page
    soup = BeautifulSoup(html, 'html.parser')

    # Get name parts for matching
    rc_parts = rc_name.lower().split() if rc_name else []
    oc_parts = oc_name.lower().split() if oc_name else []
    rc_first = rc_parts[0] if rc_parts else ''
    oc_first = oc_parts[0] if oc_parts else ''
    rc_last = rc_parts[-1] if len(rc_parts) > 1 else (rc_parts[0] if rc_parts else '')
    oc_last = oc_parts[-1] if len(oc_parts) > 1 else (oc_parts[0] if oc_parts else '')

    # Method 1: Find all links that look like Twitter/X
    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        if 'twitter.com' in href or 'x.com' in href:
            # Get surrounding context (parent text) - expand search radius
            parent_text = ''
            for parent in a.parents:
                parent_text = parent.get_text(separator=' ', strip=True).lower()
                if len(parent_text) > 20:
                    break

            # Also check siblings and nearby elements
            container = a.find_parent(['div', 'section', 'article', 'li', 'tr'])
            if container:
                parent_text = container.get_text(separator=' ', strip=True).lower()

            handle = extract_twitter_handle(a['href'])
            if not handle or handle.lower() in ['https', 'http', 'www', 'twitter', 'x', 'share', 'intent']:
                continue

            # Check if this Twitter link is near RC name AND handle is valid (not institutional)
            if rc_name and not results['rc_twitter']:
                if any(part in parent_text for part in rc_parts if len(part) > 2):
                    # Validate it's a personal handle, not institutional
                    if is_valid_twitter_handle(handle, rc_first, rc_last, school_name):
                        results['rc_twitter'] = handle

            # Check if this Twitter link is near OC name AND handle is valid
            if oc_name and not results['oc_twitter']:
                if any(part in parent_text for part in oc_parts if len(part) > 2):
                    # Validate it's a personal handle, not institutional
                    if is_valid_twitter_handle(handle, oc_first, oc_last, school_name):
                        results['oc_twitter'] = handle

    # Method 2: Look for @handles in text near coach names
    if not results['rc_twitter'] or not results['oc_twitter']:
        text = soup.get_text(separator=' ')
        handle_pattern = r'@([a-zA-Z0-9_]{3,15})'

        # Find all @handles and their positions
        for match in re.finditer(handle_pattern, text):
            handle = match.group(1)
            if handle.lower() in ['twitter', 'x', 'facebook', 'instagram', 'youtube']:
                continue

            # Get context around the handle
            start = max(0, match.start() - 200)
            end = min(len(text), match.end() + 200)
            context = text[start:end].lower()

            # Check for RC - must validate handle is personal, not institutional
            if rc_name and not results['rc_twitter']:
                if any(part in context for part in rc_parts if len(part) > 2):
                    if is_valid_twitter_handle(handle, rc_first, rc_last, school_name):
                        results['rc_twitter'] = handle

            # Check for OC - must validate handle is personal, not institutional
            if oc_name and not results['oc_twitter']:
                if any(part in context for part in oc_parts if len(part) > 2):
                    if is_valid_twitter_handle(handle, oc_first, oc_last, school_name):
                        results['oc_twitter'] = handle

    return results


def extract_emails_from_coaches_page(html: str, rc_name: str, oc_name: str) -> Dict[str, Optional[str]]:
    """Try to extract coach emails directly from the coaches listing page."""
    results = {'rc_email': None, 'oc_email': None}

    # Parse HTML to find emails with context
    soup = BeautifulSoup(html, 'html.parser')

    # Find all emails on the page
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

    # Filter out generic emails
    excluded = ['info@', 'tickets@', 'support@', 'webmaster@', 'admin@', 'noreply@', 'athletics@', 'compliance@', 'recruiting@']

    # Get name parts
    rc_parts = [p.lower() for p in rc_name.split() if len(p) > 2] if rc_name else []
    oc_parts = [p.lower() for p in oc_name.split() if len(p) > 2] if oc_name else []
    rc_first = rc_parts[0] if rc_parts else ''
    rc_last = rc_parts[-1] if len(rc_parts) > 1 else rc_parts[0] if rc_parts else ''
    oc_first = oc_parts[0] if oc_parts else ''
    oc_last = oc_parts[-1] if len(oc_parts) > 1 else oc_parts[0] if oc_parts else ''

    # Method 1: Find emails near coach names using HTML structure
    for element in soup.find_all(string=re.compile(email_pattern)):
        email_match = re.search(email_pattern, element)
        if not email_match:
            continue
        email = email_match.group()
        if any(x in email.lower() for x in excluded):
            continue

        # Get surrounding text (parent elements)
        context = ''
        for parent in element.parents:
            context = parent.get_text(separator=' ', strip=True).lower()
            if len(context) > 50:
                break

        # Check if RC name is near this email
        if rc_name and not results['rc_email']:
            if rc_last in context or rc_first in context:
                results['rc_email'] = email
                continue

        # Check if OC name is near this email
        if oc_name and not results['oc_email']:
            if oc_last in context or oc_first in context:
                results['oc_email'] = email
                continue

    # Method 2: Match by email local part (fallback)
    if not results['rc_email'] or not results['oc_email']:
        all_emails = re.findall(email_pattern, html)
        valid_emails = [e for e in all_emails if not any(x in e.lower() for x in excluded) and is_valid_email(e)]

        for email in valid_emails:
            local_part = email.lower().split('@')[0]

            # Check for RC match (try multiple patterns)
            if rc_name and not results['rc_email']:
                # Full name match, last name, first initial + last name
                if (rc_last in local_part or
                    (rc_first and rc_first[0] + rc_last in local_part) or
                    (rc_first in local_part and len(rc_first) > 3)):
                    results['rc_email'] = email

            # Check for OC match
            if oc_name and not results['oc_email']:
                if (oc_last in local_part or
                    (oc_first and oc_first[0] + oc_last in local_part) or
                    (oc_first in local_part and len(oc_first) > 3)):
                    results['oc_email'] = email

    return results


def is_valid_email(email: str) -> bool:
    """Check if a string is a valid email (not a Twitter handle or other format)."""
    if not email:
        return False
    # Must contain @ and have a proper domain
    if '@' not in email:
        return False
    parts = email.split('@')
    if len(parts) != 2:
        return False
    local, domain = parts
    # Domain must have at least one dot (e.g., .edu, .com)
    if '.' not in domain:
        return False
    # Must not start with @ (Twitter handles like @username)
    if email.startswith('@'):
        return False
    # Domain should be at least 4 chars (x.co)
    if len(domain) < 4:
        return False
    return True


def extract_email_from_profile(html: str, coach_name: str) -> Optional[str]:
    """Use Ollama to extract email from coach profile page."""
    # First try regex for common email patterns
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(email_pattern, html)

    # Filter out common non-coach emails and invalid formats
    excluded = ['info@', 'tickets@', 'support@', 'webmaster@', 'admin@', 'noreply@', 'athletics@', 'compliance@']
    valid_emails = [e for e in emails if not any(x in e.lower() for x in excluded) and is_valid_email(e)]

    # Try name matching first
    name_parts = coach_name.lower().split()
    for email in valid_emails:
        local_part = email.lower().split('@')[0]
        for part in name_parts:
            if len(part) > 2 and part in local_part:
                return email

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
    if result and result.get('email'):
        ai_email = str(result.get('email', ''))
        if is_valid_email(ai_email):
            return ai_email

    # Return first valid email if found
    if valid_emails:
        return valid_emails[0]

    return None

# =============================================================================
# SELENIUM BROWSER MANAGER - PRODUCTION READY WITH STEALTH
# =============================================================================

# Global browser instance (reused across searches)
_browser = None
_browser_initialized = False
_search_count = 0  # Track searches for rotation


def get_browser(force_new: bool = False):
    """Get or create a Selenium browser instance with stealth settings."""
    global _browser, _browser_initialized, _search_count

    if not SELENIUM_AVAILABLE:
        return None

    # Rotate browser every 20 searches to avoid detection
    if _search_count >= 20:
        close_browser()
        _search_count = 0
        force_new = True

    if _browser is not None and _browser_initialized and not force_new:
        return _browser

    try:
        logger.info("Starting stealth Chrome browser...")
        options = uc.ChromeOptions()

        # Essential options for stability
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')

        # Randomize window size slightly
        width = random.randint(1200, 1920)
        height = random.randint(800, 1080)
        options.add_argument(f'--window-size={width},{height}')

        # Headless mode (undetected_chromedriver handles stealth automatically)
        options.add_argument('--headless=new')

        # Create browser - undetected_chromedriver handles most stealth features automatically
        _browser = uc.Chrome(options=options)

        _browser_initialized = True
        logger.info("Stealth Chrome browser started successfully")
        return _browser
    except Exception as e:
        logger.error(f"Failed to start Chrome browser: {e}")
        return None


def close_browser():
    """Close the Selenium browser."""
    global _browser, _browser_initialized
    if _browser is not None:
        try:
            _browser.quit()
        except:
            pass
        _browser = None
        _browser_initialized = False


def human_like_delay(min_sec: float = 1.0, max_sec: float = 3.0):
    """Add human-like random delay."""
    time.sleep(random.uniform(min_sec, max_sec))


def simulate_human_behavior(browser):
    """Simulate human-like behavior on the page."""
    try:
        # Random scroll
        scroll_amount = random.randint(100, 400)
        browser.execute_script(f"window.scrollBy(0, {scroll_amount})")
        time.sleep(random.uniform(0.3, 0.8))

        # Maybe scroll back up a bit
        if random.random() > 0.5:
            browser.execute_script(f"window.scrollBy(0, -{random.randint(50, 150)})")
            time.sleep(random.uniform(0.2, 0.5))
    except:
        pass


def search_google_selenium(query: str, num_results: int = 10) -> List[str]:
    """Search Google using Selenium with human-like behavior."""
    global _search_count
    browser = get_browser()
    if not browser:
        return []

    results = []
    try:
        _search_count += 1

        # Navigate to Google first (not directly to search)
        if random.random() > 0.7:  # 30% of time go to google.com first
            browser.get("https://www.google.com")
            human_like_delay(1, 2)

        # Now search
        search_url = f"https://www.google.com/search?q={requests.utils.quote(query)}&num={num_results}"
        browser.get(search_url)

        # Human-like wait
        human_like_delay(2, 4)

        # Handle consent popup if it appears
        try:
            consent_selectors = [
                "//button[contains(., 'Accept')]",
                "//button[contains(., 'I agree')]",
                "//button[contains(., 'Accept all')]",
                "//div[contains(@class, 'consent')]//button",
            ]
            for selector in consent_selectors:
                try:
                    consent_button = browser.find_element(By.XPATH, selector)
                    consent_button.click()
                    human_like_delay(1, 2)
                    break
                except:
                    continue
        except:
            pass

        # Simulate human scrolling
        simulate_human_behavior(browser)

        # Extract result URLs with multiple selectors
        selectors = [
            'div.g a[href^="http"]',
            'div.yuRUbf a[href^="http"]',
            'a[jsname][href^="http"]',
            'div[data-hveid] a[href^="http"]',
        ]

        for selector in selectors:
            try:
                links = browser.find_elements(By.CSS_SELECTOR, selector)
                for link in links[:num_results * 2]:
                    try:
                        href = link.get_attribute('href')
                        if href and 'google.com' not in href and href not in results:
                            results.append(href)
                    except:
                        continue
                if len(results) >= num_results:
                    break
            except:
                continue

        # Fallback: get all http links
        if len(results) < 3:
            try:
                all_links = browser.find_elements(By.XPATH, "//a[starts-with(@href, 'http')]")
                for link in all_links:
                    try:
                        href = link.get_attribute('href')
                        if (href and 'google.com' not in href and
                            'gstatic.com' not in href and href not in results):
                            results.append(href)
                            if len(results) >= num_results:
                                break
                    except:
                        continue
            except:
                pass

        # Small delay before next action
        human_like_delay(0.5, 1.5)

    except Exception as e:
        logger.debug(f"Google Selenium search error: {e}")

    return results[:num_results]


# =============================================================================
# TWITTER SEARCH FUNCTIONS - PRODUCTION READY
# =============================================================================

def get_random_user_agent() -> str:
    """Return a random browser user agent to avoid detection."""
    user_agents = [
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
    ]
    return random.choice(user_agents)


def is_valid_twitter_handle(handle: str, first_name: str, last_name: str, school_name: str = '', lenient: bool = False) -> bool:
    """Check if a Twitter handle likely belongs to the coach (not the school/team).

    Args:
        handle: The Twitter handle to validate
        first_name: Coach's first name
        last_name: Coach's last name
        school_name: School name to filter out institutional accounts
        lenient: If True, accept handles that look football-related even without name match
    """
    if not handle:
        return False

    handle_clean = handle.lower().replace('_', '').replace('-', '').replace(' ', '')

    # Skip obvious non-profile handles
    invalid_handles = [
        'home', 'search', 'explore', 'messages', 'settings', 'i', 'intent',
        'share', 'https', 'http', 'www', 'twitter', 'x', 'status', 'hashtag',
        'login', 'signup', 'compose', 'notifications', 'lists', 'help',
        'tos', 'privacy', 'about', 'jobs', 'blog', 'developers'
    ]
    if handle_clean in invalid_handles:
        return False

    # IMPORTANT: Filter out institutional/team accounts
    # These are school Twitter accounts, not personal coach accounts
    institutional_patterns = [
        'athletics', 'sports', 'football', 'university', 'college', 'state',
        'tigers', 'lions', 'bears', 'eagles', 'hawks', 'wolves', 'panthers',
        'bulldogs', 'chargers', 'warriors', 'knights', 'rams', 'mustangs',
        'cardinals', 'falcons', 'hornets', 'bobcats', 'cougars', 'huskies',
        'marauders', 'mavericks', 'pioneers', 'griffons', 'bluejays'
    ]

    # Check if handle looks like a school/team account
    if school_name:
        school_parts = school_name.lower().replace('-', '').split()
        for part in school_parts:
            if len(part) > 3 and part in handle_clean:
                # Handle matches school name - likely institutional
                return False

    # Check for common institutional patterns
    # Only reject if handle is SHORT and matches pattern (longer handles with these words might be personal)
    if len(handle_clean) < 15:
        for pattern in institutional_patterns:
            if handle_clean == pattern or handle_clean.endswith(pattern):
                return False

    # Reject handles that look auto-generated (ending in many numbers)
    if re.search(r'\d{6,}$', handle):
        return False

    # Must have some connection to the name or be coach-related
    first_clean = first_name.lower().replace('-', '') if first_name else ''
    last_clean = last_name.lower().replace('-', '') if last_name else ''

    # Check various matching criteria - REQUIRE name match for personal accounts
    matches = [
        # Name-based matches (primary criteria)
        first_clean and len(first_clean) > 2 and first_clean in handle_clean,
        last_clean and len(last_clean) > 2 and last_clean in handle_clean,
        # Coach + name pattern (e.g., CoachSmith, Coach_Jones)
        'coach' in handle_clean and last_clean and len(last_clean) > 2 and last_clean in handle_clean,
        'coach' in handle_clean and first_clean and len(first_clean) > 2 and first_clean in handle_clean,
        # Check for initials + last name pattern (e.g., jsmith)
        first_clean and last_clean and len(last_clean) > 2 and (first_clean[0] + last_clean) in handle_clean,
        # Check for last name + initials (e.g., smithj)
        first_clean and last_clean and len(last_clean) > 2 and (last_clean + first_clean[0]) in handle_clean,
    ]

    if any(matches):
        return True

    # "Coach" alone is not enough - must have name component
    # This prevents matching generic coach accounts

    return False


def extract_twitter_from_html(html: str, first_name: str, last_name: str) -> List[str]:
    """Extract potential Twitter handles from HTML content."""
    handles = []

    # Multiple patterns to catch Twitter/X URLs
    patterns = [
        r'https?://(?:www\.)?twitter\.com/([a-zA-Z0-9_]+)',
        r'https?://(?:www\.)?x\.com/([a-zA-Z0-9_]+)',
        r'twitter\.com/([a-zA-Z0-9_]+)',
        r'x\.com/([a-zA-Z0-9_]+)',
        r'@([a-zA-Z0-9_]{1,15})(?:\s|$|["\'])',  # @handle format
    ]

    for pattern in patterns:
        matches = re.findall(pattern, html, re.IGNORECASE)
        for match in matches:
            if is_valid_twitter_handle(match, first_name, last_name):
                handles.append(match)

    return list(set(handles))  # Remove duplicates


def search_twitter_duckduckgo(coach_name: str, school_name: str) -> Optional[str]:
    """Search DuckDuckGo for coach's Twitter profile (no API limits)."""
    name_parts = coach_name.split()
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[-1] if len(name_parts) > 1 else ''

    # More comprehensive search queries
    search_queries = [
        f'"{coach_name}" twitter football',
        f'"{coach_name}" x.com coach',
        f'{coach_name} {school_name} twitter',
        f'coach {last_name} twitter football',
        f'site:twitter.com {coach_name} coach',
        f'site:x.com {coach_name} football',
    ]

    headers = {
        'User-Agent': get_random_user_agent(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

    for query in search_queries:
        try:
            time.sleep(random.uniform(1.5, 3))
            url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
            response = requests.get(url, headers=headers, timeout=15)

            if response.status_code == 200:
                handles = extract_twitter_from_html(response.text, first_name, last_name)
                if handles:
                    return f"@{handles[0]}"

        except Exception as e:
            logger.debug(f"DuckDuckGo search failed: {e}")
            continue

    return None


def search_twitter_bing(coach_name: str, school_name: str) -> Optional[str]:
    """Search Bing for coach's Twitter profile as backup."""
    name_parts = coach_name.split()
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[-1] if len(name_parts) > 1 else ''

    queries = [
        f'"{coach_name}" twitter football coach',
        f'{coach_name} {school_name} twitter',
        f'site:twitter.com {coach_name}',
    ]

    headers = {
        'User-Agent': get_random_user_agent(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }

    for query in queries:
        try:
            time.sleep(random.uniform(1, 2.5))
            url = f"https://www.bing.com/search?q={requests.utils.quote(query)}"
            response = requests.get(url, headers=headers, timeout=15)

            if response.status_code == 200:
                handles = extract_twitter_from_html(response.text, first_name, last_name)
                if handles:
                    return f"@{handles[0]}"

        except Exception as e:
            logger.debug(f"Bing search failed: {e}")

    return None


def extract_twitter_from_profile_page(profile_url: str, coach_name: str, school_name: str = '') -> Optional[str]:
    """Extract Twitter handle directly from coach's profile page.

    Comprehensive extraction using multiple methods to find Twitter handles.
    Only returns personal coach handles, filters out institutional accounts.
    """
    if not profile_url:
        return None

    name_parts = coach_name.split()
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[-1] if len(name_parts) > 1 else ''

    try:
        html = fetch_url(profile_url)
        if not html:
            return None

        soup = BeautifulSoup(html, 'html.parser')
        found_handles = []  # Collect all potential handles

        # Method 1: Find all links to Twitter/X (most reliable)
        for a in soup.find_all('a', href=True):
            href = a['href']
            href_lower = href.lower()
            if 'twitter.com' in href_lower or 'x.com' in href_lower:
                handle = extract_twitter_handle(href)
                if handle and handle.lower() not in ['share', 'intent', 'home', 'search']:
                    found_handles.append(handle)

        # Method 2: Look for Twitter icons (fa-twitter, icon-twitter, etc.)
        icon_patterns = [
            'fa-twitter', 'fa-x-twitter', 'icon-twitter', 'twitter-icon',
            'bi-twitter', 'fab-twitter', 'social-twitter', 'twitter'
        ]
        for pattern in icon_patterns:
            # Find elements with Twitter icon classes
            icons = soup.find_all(class_=re.compile(pattern, re.I))
            for icon in icons:
                # Check parent link
                parent_link = icon.find_parent('a', href=True)
                if parent_link:
                    handle = extract_twitter_handle(parent_link['href'])
                    if handle:
                        found_handles.append(handle)

        # Method 3: Check aria-label attributes
        twitter_aria = soup.find_all(attrs={'aria-label': re.compile(r'twitter|tweet', re.I)})
        for elem in twitter_aria:
            if elem.name == 'a' and elem.get('href'):
                handle = extract_twitter_handle(elem['href'])
                if handle:
                    found_handles.append(handle)
            # Check for parent link
            parent_link = elem.find_parent('a', href=True)
            if parent_link:
                handle = extract_twitter_handle(parent_link['href'])
                if handle:
                    found_handles.append(handle)

        # Method 4: Look for social media containers (Sidearm, common athletic site patterns)
        social_container_patterns = [
            'sidearm-social', 'social-links', 'social-media', 'social-icons',
            'staff-social', 'coach-social', 'bio-social', 'profile-social',
            'c-social', 's-social-links', 'connect-links'
        ]
        for pattern in social_container_patterns:
            containers = soup.find_all(class_=re.compile(pattern, re.I))
            for container in containers:
                # Find all links in container
                for a in container.find_all('a', href=True):
                    href_lower = a['href'].lower()
                    if 'twitter.com' in href_lower or 'x.com' in href_lower:
                        handle = extract_twitter_handle(a['href'])
                        if handle:
                            found_handles.append(handle)

        # Method 5: Check data- attributes for Twitter URLs
        for elem in soup.find_all(attrs={'data-url': re.compile(r'twitter\.com|x\.com', re.I)}):
            handle = extract_twitter_handle(elem.get('data-url', ''))
            if handle:
                found_handles.append(handle)
        for elem in soup.find_all(attrs={'data-href': re.compile(r'twitter\.com|x\.com', re.I)}):
            handle = extract_twitter_handle(elem.get('data-href', ''))
            if handle:
                found_handles.append(handle)

        # Method 6: Look for @handle patterns in bio/text sections
        bio_patterns = ['bio', 'about', 'profile', 'description', 'info', 'staff-bio']
        for pattern in bio_patterns:
            bio_sections = soup.find_all(class_=re.compile(pattern, re.I))
            for section in bio_sections:
                text = section.get_text()
                handle_matches = re.findall(r'@([a-zA-Z0-9_]{3,15})', text)
                for match in handle_matches:
                    if match.lower() not in ['gmail', 'yahoo', 'hotmail', 'outlook', 'email']:
                        found_handles.append(match)

        # Method 7: Scan entire page for @handle near coach name
        full_text = soup.get_text()
        # Look for patterns like "Twitter: @handle" or "Follow @handle"
        twitter_patterns = [
            r'(?:twitter|tweet|follow\s+(?:me\s+)?(?:on\s+)?(?:twitter)?)\s*[:\-]?\s*@?([a-zA-Z0-9_]{3,15})',
            r'@([a-zA-Z0-9_]{3,15})\s*(?:on\s+)?(?:twitter|x\.com)',
        ]
        for pattern in twitter_patterns:
            matches = re.findall(pattern, full_text, re.I)
            for match in matches:
                found_handles.append(match)

        # Validate and return the best handle
        # First, try handles that match coach name (most reliable)
        for handle in found_handles:
            if is_valid_twitter_handle(handle, first_name, last_name, school_name):
                return f"@{handle}"

        # If we found handles on the coach's profile page but none match name,
        # accept the first non-institutional one (it's likely correct since it's on their bio page)
        if found_handles:
            for handle in found_handles:
                handle_lower = handle.lower()
                # Skip obvious institutional handles
                skip_patterns = ['athletics', 'sports', 'football', 'team', 'official']
                if not any(p in handle_lower for p in skip_patterns):
                    # Skip if handle matches school name
                    school_lower = school_name.lower().replace(' ', '')
                    if school_lower not in handle_lower.replace('_', ''):
                        logger.info(f"    Found Twitter @{handle} on profile page (accepting without name match)")
                        return f"@{handle}"

    except Exception as e:
        logger.debug(f"Error extracting Twitter from profile: {e}")

    return None


def google_search_twitter(coach_name: str, school_name: str, profile_url: str = None, role: str = '', school_twitter: str = None) -> Optional[str]:
    """
    Production-ready Twitter search with multiple fallbacks.

    Strategy:
    1. Try extracting from coach's profile page first (fastest, most accurate)
    2. Search using school's football Twitter to find coach interactions
    3. Use Selenium Google search with multiple query variations
    4. Fall back to DuckDuckGo
    5. Fall back to Bing

    Args:
        coach_name: Full name of the coach
        school_name: Name of the school
        profile_url: Optional URL to coach's profile page
        role: Coach's role (e.g., "Recruiting Coordinator", "Offensive Line Coach")
        school_twitter: Optional school football Twitter handle
    """
    name_parts = coach_name.split()
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[-1] if len(name_parts) > 1 else ''
    # Handle middle names/initials
    middle_parts = name_parts[1:-1] if len(name_parts) > 2 else []

    # Strategy 1: Extract from profile page (if available)
    if profile_url:
        logger.debug(f"Checking profile page for Twitter: {profile_url}")
        result = extract_twitter_from_profile_page(profile_url, coach_name, school_name)
        if result:
            logger.info(f"  ✓ Found Twitter on profile page: {result}")
            return result

    # Strategy 2: Search using school's football Twitter
    if school_twitter and SELENIUM_AVAILABLE:
        logger.debug(f"Searching via school Twitter @{school_twitter}...")
        result = search_school_twitter_following(school_twitter, coach_name, school_name)
        if result:
            logger.info(f"  ✓ Found Twitter via school account: {result}")
            return result

    # Strategy 3: Selenium Google search with multiple variations
    if SELENIUM_AVAILABLE:
        # Clean school name for search (remove common suffixes)
        school_clean = school_name.replace('University', '').replace('College', '').replace('State', '').strip()
        school_short = school_clean.split()[0] if school_clean else ''  # First word only

        # Build role-specific queries
        role_terms = []
        if role:
            role_terms.append(role.lower())
        if 'recruiting' in role.lower() or 'rc' in role.lower():
            role_terms.extend(['recruiting coordinator', 'recruiting', 'RC'])
        if 'offensive' in role.lower() or 'oline' in role.lower() or 'oc' in role.lower():
            role_terms.extend(['offensive line coach', 'OL coach', 'offensive line', 'o-line'])

        # Prioritized search queries (most specific first)
        search_queries = [
            # Exact name + Twitter (highest priority)
            f'"{coach_name}" twitter football coach',
            f'"{coach_name}" twitter {school_name}',
            f'"{first_name} {last_name}" twitter football',
            # Site-specific searches (most reliable)
            f'site:twitter.com "{coach_name}" football',
            f'site:twitter.com {first_name} {last_name} coach',
            f'site:x.com "{coach_name}" football',
            f'site:x.com {first_name} {last_name} coach',
            # Name variations with school
            f'{first_name} {last_name} football coach twitter',
            f'coach {last_name} {school_name} twitter',
            f'{coach_name} {school_clean} football twitter',
            f'"{last_name}" {school_short} football twitter',
            # Handle search patterns
            f'"@{last_name}" football twitter',
            f'"coach{last_name}" twitter',
            f'"coach_{last_name}" twitter',
            f'"@coach{last_name}" football',
            # X.com direct searches
            f'site:x.com {coach_name} {school_short}',
            f'site:x.com coach {last_name} football',
        ]

        # Add role-specific queries
        for role_term in role_terms[:2]:  # Limit to 2 role terms
            search_queries.extend([
                f'"{coach_name}" {role_term} twitter',
                f'{last_name} {role_term} twitter {school_short}',
                f'site:twitter.com {last_name} {role_term}',
            ])

        # Add initial-based queries (e.g., "J Smith" or "JSmith")
        if first_name and last_name:
            search_queries.extend([
                f'site:twitter.com {first_name[0]} {last_name} football coach',
                f'site:x.com {first_name[0]}{last_name} football',
            ])

        found_handles = []  # Collect potential handles

        for i, query in enumerate(search_queries[:12]):  # Try up to 12 queries
            try:
                logger.debug(f"Twitter search ({i+1}/12): {query[:50]}...")
                results = search_google_selenium(query, num_results=15)

                for url in results:
                    # Check for Twitter/X profile URLs
                    twitter_match = re.match(
                        r'https?://(?:www\.)?(twitter\.com|x\.com)/([a-zA-Z0-9_]+)/?(?:\?.*)?$',
                        url
                    )
                    if twitter_match:
                        handle = twitter_match.group(2)
                        if is_valid_twitter_handle(handle, first_name, last_name, school_name):
                            found_handles.append(handle)

                # If we found good handles, use the most common one
                if len(found_handles) >= 2:
                    # Return most frequently found handle
                    from collections import Counter
                    most_common = Counter(found_handles).most_common(1)
                    if most_common:
                        return f"@{most_common[0][0]}"

                # Rate limit between searches
                if i < 11:
                    human_like_delay(0.8, 1.5)

            except Exception as e:
                logger.debug(f"Google search failed for query: {e}")
                continue

        # Return first valid handle if we found any
        if found_handles:
            return f"@{found_handles[0]}"

        # Try lenient mode for remaining queries if nothing found
        logger.debug("Trying lenient search mode...")
        for query in search_queries[12:]:
            try:
                results = search_google_selenium(query, num_results=10)
                for url in results:
                    twitter_match = re.match(
                        r'https?://(?:www\.)?(twitter\.com|x\.com)/([a-zA-Z0-9_]+)/?(?:\?.*)?$',
                        url
                    )
                    if twitter_match:
                        handle = twitter_match.group(2)
                        # Use lenient mode for broader searches (still filter institutional)
                        if is_valid_twitter_handle(handle, first_name, last_name, school_name, lenient=True):
                            return f"@{handle}"
                human_like_delay(1.0, 2.0)
            except:
                continue

    # Strategy 3: DuckDuckGo fallback
    logger.debug("Trying DuckDuckGo fallback...")
    result = search_twitter_duckduckgo(coach_name, school_name)
    if result:
        return result

    # Strategy 4: Bing fallback
    logger.debug("Trying Bing fallback...")
    result = search_twitter_bing(coach_name, school_name)
    if result:
        return result

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
    """Verify Twitter profile belongs to the correct coach using name matching."""
    handle = extract_twitter_handle(twitter_url)
    if not handle:
        return {'verified': False, 'handle': None, 'confidence': 'low'}

    # Clean up names for comparison
    coach_name_lower = coach_name.lower()
    handle_lower = handle.lower().replace('_', '').replace('coach', '')

    # Extract name parts
    name_parts = coach_name_lower.split()
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[-1] if len(name_parts) > 1 else ''

    # Check for name matches in handle
    first_in_handle = first_name and first_name in handle_lower
    last_in_handle = last_name and last_name in handle_lower

    # Determine confidence based on name matching
    if first_in_handle and last_in_handle:
        confidence = 'high'
        verified = True
    elif last_in_handle:
        confidence = 'medium'
        verified = True
    elif first_in_handle:
        confidence = 'medium'
        verified = True
    elif 'coach' in handle.lower():
        # Handle contains 'coach' - might be worth accepting with low confidence
        confidence = 'low'
        verified = False
    else:
        confidence = 'low'
        verified = False

    # Try to fetch and verify with AI only if name matching failed
    if not verified:
        try:
            html = fetch_url(twitter_url)
            if html and len(html) > 1000:  # Got meaningful content
                prompt = f"""Is this Twitter profile for {coach_name}, a football coach at {school_name}?

PROFILE: {clean_html_for_ollama(html, 5000)}

Return JSON: {{"verified": true/false, "confidence": "high"/"medium"/"low"}}"""

                result = query_ollama(prompt)
                if result:
                    if result.get('verified'):
                        return {'verified': True, 'handle': handle, 'confidence': result.get('confidence', 'medium')}
                    elif result.get('confidence') in ['high', 'medium']:
                        return {'verified': True, 'handle': handle, 'confidence': result.get('confidence')}
        except:
            pass  # Fall through to name-based result

    return {'verified': verified, 'handle': handle, 'confidence': confidence}

# =============================================================================
# AI VERIFICATION FOR TWITTER HANDLES
# =============================================================================

def ai_verify_twitter_handle(handle: str, coach_name: str, school_name: str, role: str = 'coach') -> Dict[str, Any]:
    """
    Use AI to verify that a Twitter handle belongs to the specified coach.

    Returns:
        dict with 'verified' (bool), 'confidence' (str), 'reason' (str)
    """
    if not handle:
        return {'verified': False, 'confidence': 'none', 'reason': 'No handle provided'}

    handle_clean = handle.lstrip('@')
    name_parts = coach_name.split()
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[-1] if len(name_parts) > 1 else ''

    # Quick check: if handle clearly contains name parts, high confidence
    handle_lower = handle_clean.lower().replace('_', '')
    first_lower = first_name.lower() if first_name else ''
    last_lower = last_name.lower() if last_name else ''

    # Strong name match = auto-verify
    if last_lower and len(last_lower) > 3 and last_lower in handle_lower:
        if first_lower and first_lower in handle_lower:
            return {'verified': True, 'confidence': 'high', 'reason': f'Handle contains both {first_name} and {last_name}'}
        if 'coach' in handle_lower:
            return {'verified': True, 'confidence': 'high', 'reason': f'Handle contains "coach" and {last_name}'}
        return {'verified': True, 'confidence': 'medium', 'reason': f'Handle contains {last_name}'}

    if first_lower and len(first_lower) > 3 and first_lower in handle_lower:
        return {'verified': True, 'confidence': 'medium', 'reason': f'Handle contains {first_name}'}

    # For handles without clear name match, use AI to verify via web search
    logger.info(f"    AI verifying @{handle_clean} for {coach_name}...")

    # Try to fetch Twitter/X profile to verify
    twitter_urls = [
        f"https://x.com/{handle_clean}",
        f"https://twitter.com/{handle_clean}"
    ]

    for url in twitter_urls:
        try:
            html = fetch_url(url)
            if html and len(html) > 500:
                # Use AI to check if this profile matches the coach
                prompt = f"""Analyze this Twitter/X profile and determine if it belongs to {coach_name},
who is a {role} for {school_name} football.

PROFILE CONTENT:
{clean_html_for_ollama(html, 8000)}

Check for:
1. Does the name match or is similar to {coach_name}?
2. Is there any mention of {school_name} or football coaching?
3. Does the bio/content suggest this is a football coach?

Return JSON:
{{"verified": true/false, "confidence": "high"/"medium"/"low", "reason": "brief explanation"}}"""

                result = query_ollama(prompt)
                if result:
                    verified = result.get('verified', False)
                    confidence = result.get('confidence', 'low')
                    reason = result.get('reason', 'AI verification')

                    if verified and confidence in ['high', 'medium']:
                        return {'verified': True, 'confidence': confidence, 'reason': reason}
                    elif not verified:
                        return {'verified': False, 'confidence': confidence, 'reason': reason}

        except Exception as e:
            logger.debug(f"    Could not fetch {url}: {e}")
            continue

    # If we couldn't verify via profile, reject it
    return {'verified': False, 'confidence': 'low', 'reason': 'Could not verify - no name match and profile unavailable'}


# =============================================================================
# MAIN SCRAPING LOGIC
# =============================================================================

def scrape_school(row_data: List[str], row_num: int, school_name: str) -> Dict[str, Any]:
    """Scrape coach information for a single school."""
    results = {
        'rc_email': None,
        'oc_email': None,
        'rc_twitter': None,
        'oc_twitter': None,
        # Status tracking
        'status': {
            'rc_email': 'skipped',  # skipped, found, not_found, error
            'oc_email': 'skipped',
            'rc_twitter': 'skipped',
            'oc_twitter': 'skipped',
        }
    }

    # Get existing data
    school_url = row_data[COLUMNS['school_url']] if len(row_data) > COLUMNS['school_url'] else ''
    rc_name = row_data[COLUMNS['rc_name']] if len(row_data) > COLUMNS['rc_name'] else ''
    oc_name = row_data[COLUMNS['oc_name']] if len(row_data) > COLUMNS['oc_name'] else ''
    existing_rc_email = row_data[COLUMNS['rc_email']] if len(row_data) > COLUMNS['rc_email'] else ''
    existing_oc_email = row_data[COLUMNS['oc_email']] if len(row_data) > COLUMNS['oc_email'] else ''
    existing_rc_twitter = row_data[COLUMNS['rc_twitter']] if len(row_data) > COLUMNS['rc_twitter'] else ''
    existing_oc_twitter = row_data[COLUMNS['oc_twitter']] if len(row_data) > COLUMNS['oc_twitter'] else ''

    # If URL doesn't have protocol, add https://
    if school_url and not school_url.startswith('http'):
        school_url = 'https://' + school_url

    # ===== STEP 0: FIND COACH NAMES IF MISSING =====
    # If we don't have coach names, we need to find them from the directory first
    if not rc_name and not oc_name and school_url:
        logger.info(f"  No coach names found, searching directory...")

        # Check if URL already points to a coaches/staff page
        url_lower = school_url.lower()
        is_coaches_page = any(x in url_lower for x in ['coach', 'staff', 'directory'])

        directory_html_for_names = None
        if is_coaches_page:
            directory_html_for_names = fetch_url(school_url)
        else:
            # Need to find the staff directory from the main page
            main_html = fetch_url(school_url)
            if main_html:
                time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])
                directory_url = find_staff_directory_url(main_html, school_url)
                if directory_url:
                    time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
                    directory_html_for_names = fetch_url(directory_url)

        if directory_html_for_names:
            # Use AI to find coach names from the directory
            found_coaches = find_coaches_from_directory(directory_html_for_names, school_name)
            if found_coaches.get('rc_name'):
                rc_name = found_coaches['rc_name']
                results['rc_name'] = rc_name  # Store for updating sheet
            if found_coaches.get('oc_name'):
                oc_name = found_coaches['oc_name']
                results['oc_name'] = oc_name  # Store for updating sheet

        if not rc_name and not oc_name:
            logger.warning(f"  Could not find any coach names from directory")
            return results

    # Determine what we need to find
    need_rc_email = rc_name and (not existing_rc_email or '@' not in existing_rc_email)
    need_oc_email = oc_name and (not existing_oc_email or '@' not in existing_oc_email)
    need_rc_twitter = rc_name and not existing_rc_twitter
    need_oc_twitter = oc_name and not existing_oc_twitter

    logger.info(f"  Need: RC email={need_rc_email}, OC email={need_oc_email}, RC twitter={need_rc_twitter}, OC twitter={need_oc_twitter}")

    # Initialize profile_urls (will be populated during email extraction)
    profile_urls = {}
    directory_html = None

    if not school_url:
        logger.warning(f"  No valid URL for {school_name}, skipping email scrape")
    else:
        # ===== EMAIL EXTRACTION =====
        if need_rc_email or need_oc_email:
            # Check if URL already points to a coaches/staff page
            url_lower = school_url.lower()
            is_coaches_page = any(x in url_lower for x in ['coach', 'staff', 'directory'])

            if is_coaches_page:
                # URL is already a coaches page, use it directly
                logger.info(f"  URL is already a coaches page, using directly...")
                directory_url = school_url
                directory_html = fetch_url(directory_url)
            else:
                # Need to find the staff directory from the main page
                logger.info("  Step 1: Fetching main athletics page...")
                main_html = fetch_url(school_url)

                directory_url = None
                directory_html = None
                if main_html:
                    time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])

                    logger.info("  Step 2: Finding staff directory URL...")
                    directory_url = find_staff_directory_url(main_html, school_url)

                    if directory_url:
                        time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
                        directory_html = fetch_url(directory_url)

            if directory_url and directory_html:
                logger.info(f"  Using directory: {directory_url}")

                # FIRST: Try to extract emails directly from the coaches listing page
                direct_emails = extract_emails_from_coaches_page(directory_html, rc_name, oc_name)
                if direct_emails.get('rc_email'):
                    results['rc_email'] = direct_emails['rc_email']
                    results['status']['rc_email'] = 'found'
                    logger.info(f"  ✓ Found RC email directly: {direct_emails['rc_email']}")
                    need_rc_email = False
                if direct_emails.get('oc_email'):
                    results['oc_email'] = direct_emails['oc_email']
                    results['status']['oc_email'] = 'found'
                    logger.info(f"  ✓ Found OC email directly: {direct_emails['oc_email']}")
                    need_oc_email = False

                # SECOND: If we still need emails, find profile pages
                # Also look for Head Coach as fallback if RC not found
                if need_rc_email or need_oc_email:
                    time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])
                    logger.info("  Finding coach profile URLs...")
                    # Pass find_head_coach=True to get HC as fallback for RC
                    profile_urls = find_coach_profile_urls(directory_html, directory_url, rc_name, oc_name, find_head_coach=need_rc_email)
                else:
                    profile_urls = {}

                # Fetch RC profile (with Head Coach fallback)
                if need_rc_email:
                    rc_profile_url = profile_urls.get('rc_profile_url')
                    fallback_to_hc = False
                    coach_name_for_search = rc_name

                    # If no RC profile found, try Head Coach as fallback
                    if not rc_profile_url and profile_urls.get('hc_profile_url'):
                        logger.info(f"  RC not found, using Head Coach as fallback...")
                        rc_profile_url = profile_urls['hc_profile_url']
                        fallback_to_hc = True
                        if profile_urls.get('hc_name'):
                            coach_name_for_search = profile_urls['hc_name']

                    if rc_profile_url:
                        logger.info(f"  Fetching {'HC' if fallback_to_hc else 'RC'} profile...")
                        time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
                        rc_html = fetch_url(rc_profile_url)
                        if rc_html:
                            time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])
                            email = extract_email_from_profile(rc_html, coach_name_for_search)
                            if email:
                                results['rc_email'] = email
                                results['status']['rc_email'] = 'found'
                                logger.info(f"  ✓ Found {'HC' if fallback_to_hc else 'RC'} email: {email}")
                            else:
                                results['status']['rc_email'] = 'not_found'
                                logger.info(f"  ✗ {'HC' if fallback_to_hc else 'RC'} email not found on profile")
                        else:
                            results['status']['rc_email'] = 'error'
                    else:
                        results['status']['rc_email'] = 'not_found'
                        logger.info(f"  ✗ RC profile URL not found (no HC fallback available)")

                # Fetch OC profile
                if need_oc_email:
                    if profile_urls.get('oc_profile_url'):
                        logger.info(f"  Fetching OC profile...")
                        time.sleep(CONFIG['DELAY_BETWEEN_REQUESTS'])
                        oc_html = fetch_url(profile_urls['oc_profile_url'])
                        if oc_html:
                            time.sleep(CONFIG['DELAY_BETWEEN_OLLAMA'])
                            email = extract_email_from_profile(oc_html, oc_name)
                            if email:
                                results['oc_email'] = email
                                results['status']['oc_email'] = 'found'
                                logger.info(f"  ✓ Found OC email: {email}")
                            else:
                                results['status']['oc_email'] = 'not_found'
                                logger.info(f"  ✗ OC email not found on profile")
                        else:
                            results['status']['oc_email'] = 'error'
                    else:
                        results['status']['oc_email'] = 'not_found'
                        logger.info(f"  ✗ OC profile URL not found")
            else:
                logger.warning(f"  Could not fetch coaches page: {school_url}")

    # ===== TWITTER SEARCH =====
    # First try to extract Twitter from the coaches page if we have it
    school_football_twitter = None  # Will store the school's football Twitter handle

    if school_url and (need_rc_twitter or need_oc_twitter):
        try:
            # Fetch the coaches page if we haven't already
            try:
                page_html = directory_html if directory_html else fetch_url(school_url)
            except:
                page_html = fetch_url(school_url)
            if page_html:
                # Find the school's football Twitter for later use
                school_football_twitter = find_school_football_twitter(page_html, school_name)
                if school_football_twitter:
                    logger.info(f"  Found school football Twitter: @{school_football_twitter}")

                direct_twitter = extract_twitter_from_coaches_page(page_html, rc_name, oc_name, school_name)
                if direct_twitter.get('rc_twitter') and need_rc_twitter:
                    handle = direct_twitter['rc_twitter']
                    # Verify before accepting
                    verification = ai_verify_twitter_handle(handle, rc_name, school_name, role='Recruiting Coordinator')
                    if verification['verified']:
                        results['rc_twitter'] = handle
                        results['status']['rc_twitter'] = 'found'
                        logger.info(f"  ✓ Found RC Twitter on page: @{handle} (verified: {verification['confidence']})")
                        need_rc_twitter = False
                    else:
                        logger.info(f"  ✗ RC Twitter @{handle} from page failed verification: {verification['reason']}")

                if direct_twitter.get('oc_twitter') and need_oc_twitter:
                    handle = direct_twitter['oc_twitter']
                    # Verify before accepting
                    verification = ai_verify_twitter_handle(handle, oc_name, school_name, role='Offensive Line Coach')
                    if verification['verified']:
                        results['oc_twitter'] = handle
                        results['status']['oc_twitter'] = 'found'
                        logger.info(f"  ✓ Found OC Twitter on page: @{handle} (verified: {verification['confidence']})")
                        need_oc_twitter = False
                    else:
                        logger.info(f"  ✗ OC Twitter @{handle} from page failed verification: {verification['reason']}")
        except:
            pass

    # Get profile URLs for Twitter extraction (set during email extraction)
    rc_profile_url = profile_urls.get('rc_profile_url')
    oc_profile_url = profile_urls.get('oc_profile_url')

    if need_rc_twitter and rc_name:
        logger.info("  Searching for RC Twitter (multi-strategy)...")
        try:
            # Use production-ready search with profile URL and school Twitter if available
            twitter_handle = google_search_twitter(rc_name, school_name, profile_url=rc_profile_url, role='Recruiting Coordinator', school_twitter=school_football_twitter)

            if twitter_handle:
                # Clean up handle (remove @ if present for storage)
                handle = twitter_handle.lstrip('@')

                # AI Verification step
                verification = ai_verify_twitter_handle(handle, rc_name, school_name, role='Recruiting Coordinator')
                if verification['verified']:
                    results['rc_twitter'] = handle
                    results['status']['rc_twitter'] = 'found'
                    logger.info(f"  ✓ Found RC Twitter: @{handle} (verified: {verification['confidence']})")
                else:
                    results['status']['rc_twitter'] = 'not_found'
                    logger.info(f"  ✗ RC Twitter @{handle} failed verification: {verification['reason']}")
            else:
                results['status']['rc_twitter'] = 'not_found'
                logger.info(f"  ✗ RC Twitter not found after all search strategies")
        except Exception as e:
            results['status']['rc_twitter'] = 'error'
            logger.warning(f"  ✗ RC Twitter search error: {e}")

    if need_oc_twitter and oc_name:
        logger.info("  Searching for OC Twitter (multi-strategy)...")
        try:
            # Use production-ready search with profile URL and school Twitter if available
            twitter_handle = google_search_twitter(oc_name, school_name, profile_url=oc_profile_url, role='Offensive Line Coach', school_twitter=school_football_twitter)

            if twitter_handle:
                # Clean up handle (remove @ if present for storage)
                handle = twitter_handle.lstrip('@')

                # AI Verification step
                verification = ai_verify_twitter_handle(handle, oc_name, school_name, role='Offensive Line Coach')
                if verification['verified']:
                    results['oc_twitter'] = handle
                    results['status']['oc_twitter'] = 'found'
                    logger.info(f"  ✓ Found OC Twitter: @{handle} (verified: {verification['confidence']})")
                else:
                    results['status']['oc_twitter'] = 'not_found'
                    logger.info(f"  ✗ OC Twitter @{handle} failed verification: {verification['reason']}")
            else:
                results['status']['oc_twitter'] = 'not_found'
                logger.info(f"  ✗ OC Twitter not found after all search strategies")
        except Exception as e:
            results['status']['oc_twitter'] = 'error'
            logger.warning(f"  ✗ OC Twitter search error: {e}")

    return results


def update_sheet_row(sheet, row_num: int, results: Dict[str, Optional[str]], row_data: List[str]):
    """Update a row in the sheet with found data (only fill missing values)."""
    updates = []

    # Update coach names if found (when they were missing from the sheet)
    if results.get('rc_name'):
        existing = row_data[COLUMNS['rc_name']] if len(row_data) > COLUMNS['rc_name'] else ''
        if not existing:
            col_letter = chr(ord('A') + COLUMNS['rc_name'])
            updates.append((f'{col_letter}{row_num}', results['rc_name']))
            logger.info(f"  ✓ Saving RC name to sheet: {results['rc_name']}")

    if results.get('oc_name'):
        existing = row_data[COLUMNS['oc_name']] if len(row_data) > COLUMNS['oc_name'] else ''
        if not existing:
            col_letter = chr(ord('A') + COLUMNS['oc_name'])
            updates.append((f'{col_letter}{row_num}', results['oc_name']))
            logger.info(f"  ✓ Saving OC name to sheet: {results['oc_name']}")

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
            # gspread v6+ requires values first (list of lists), then range_name as keyword
            sheet.update([[value]], range_name=cell)
            logger.debug(f"Updated {cell} = {value}")
        except Exception as e:
            logger.warning(f"Could not update {cell}: {e}")

    return len(updates)


def should_process_row(row_data: List[str]) -> Tuple[bool, str]:
    """Determine if a row should be processed."""
    # Get school URL
    school_url = row_data[COLUMNS['school_url']] if len(row_data) > COLUMNS['school_url'] else ''

    # Check if we have coach names
    rc_name = row_data[COLUMNS['rc_name']] if len(row_data) > COLUMNS['rc_name'] else ''
    oc_name = row_data[COLUMNS['oc_name']] if len(row_data) > COLUMNS['oc_name'] else ''

    # If no coach names AND no URL, we can't do anything - skip
    if not rc_name and not oc_name and not school_url:
        return False, "No coach names or URL"

    # If no coach names but we HAVE a URL, we should process to find coach names
    if not rc_name and not oc_name and school_url:
        return True, "Need to find coach names from directory"

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

        # Create or get test results sheet if in test mode
        test_sheet = None
        if CONFIG.get('TEST_MODE', False):
            test_sheet_name = CONFIG.get('TEST_SHEET_NAME', 'Test Results')
            try:
                test_sheet = spreadsheet.worksheet(test_sheet_name)
                # Clear existing data
                test_sheet.clear()
                logger.info(f"Cleared existing test sheet: {test_sheet_name}")
            except:
                test_sheet = spreadsheet.add_worksheet(title=test_sheet_name, rows=100, cols=12)
                logger.info(f"Created test sheet: {test_sheet_name}")
            # Add headers
            test_headers = ['School', 'URL', 'RC Name', 'OC Name', 'RC Email (Found)', 'OC Email (Found)',
                          'RC Twitter (Found)', 'OC Twitter (Found)', 'Status', 'Notes']
            test_sheet.update(range_name='A1:J1', values=[test_headers])
            logger.info("TEST MODE: Results will be written to test sheet for verification")
    except Exception as e:
        logger.error(f"Could not open spreadsheet '{CONFIG['SHEET_NAME']}': {e}")
        return

    # Load all data
    logger.info("Loading data from sheet...")
    all_data = sheet.get_all_values()
    if len(all_data) < 2:
        logger.error("Sheet is empty or has only headers")
        return

    # Extract hyperlinks from URL column (column B = index 1)
    logger.info("Extracting hyperlinks from URL column...")
    try:
        # Get the hyperlinks using the spreadsheet API
        spreadsheet_id = spreadsheet.id
        url_col = COLUMNS['school_url']  # Column index for URLs
        url_col_letter = chr(ord('A') + url_col)  # Convert to letter (B)
        range_notation = f"'{sheet.title}'!{url_col_letter}2:{url_col_letter}{len(all_data)}"

        # Use the sheets API to get hyperlinks
        result = spreadsheet.values_get(
            range_notation,
            params={'valueRenderOption': 'FORMULA'}
        )
        formula_values = result.get('values', [])

        # Extract URLs from HYPERLINK formulas or plain text
        hyperlink_urls = []
        for row in formula_values:
            if row:
                cell_value = row[0]
                # Check if it's a HYPERLINK formula: =HYPERLINK("url", "text")
                if cell_value.startswith('=HYPERLINK('):
                    # Extract URL from formula
                    match = re.search(r'=HYPERLINK\s*\(\s*"([^"]+)"', cell_value)
                    if match:
                        hyperlink_urls.append(match.group(1))
                    else:
                        hyperlink_urls.append('')
                else:
                    hyperlink_urls.append(cell_value)
            else:
                hyperlink_urls.append('')

        # Update all_data with hyperlink URLs
        for i, url in enumerate(hyperlink_urls):
            row_idx = i + 1  # +1 because all_data includes header
            if row_idx < len(all_data) and url:
                all_data[row_idx][url_col] = url

        logger.info(f"Extracted {len([u for u in hyperlink_urls if u])} hyperlink URLs")
    except Exception as e:
        logger.warning(f"Could not extract hyperlinks (will use display text): {e}")

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
        # Detailed status tracking
        'rc_email': {'found': 0, 'not_found': 0, 'skipped': 0, 'error': 0},
        'oc_email': {'found': 0, 'not_found': 0, 'skipped': 0, 'error': 0},
        'rc_twitter': {'found': 0, 'not_found': 0, 'skipped': 0, 'error': 0},
        'oc_twitter': {'found': 0, 'not_found': 0, 'skipped': 0, 'error': 0},
    }

    # Apply limit if set
    limit = CONFIG.get('LIMIT', 0)
    if limit > 0:
        logger.info(f"LIMIT set to {limit} schools (for testing)")

    # Process each row
    processed_count = 0
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

        # Check if we've hit the limit
        if limit > 0 and processed_count >= limit:
            logger.info(f"\nReached LIMIT of {limit} schools, stopping...")
            break

        logger.info(f"\n[{processed_count + 1}/{limit if limit > 0 else len(rows)}] Processing: {school_name}")

        try:
            # Scrape this school
            results = scrape_school(row_data, row_num, school_name)

            # Track status for each field
            for field in ['rc_email', 'oc_email', 'rc_twitter', 'oc_twitter']:
                status = results.get('status', {}).get(field, 'skipped')
                stats[field][status] += 1

            # Write to test sheet or update main sheet
            if CONFIG.get('TEST_MODE', False) and test_sheet:
                # Write to test sheet for verification
                school_url = row_data[COLUMNS['school_url']] if len(row_data) > COLUMNS['school_url'] else ''
                rc_name = row_data[COLUMNS['rc_name']] if len(row_data) > COLUMNS['rc_name'] else ''
                oc_name = row_data[COLUMNS['oc_name']] if len(row_data) > COLUMNS['oc_name'] else ''

                # Build status summary
                status_parts = []
                for field in ['rc_email', 'oc_email', 'rc_twitter', 'oc_twitter']:
                    s = results.get('status', {}).get(field, 'skipped')
                    if s == 'found':
                        status_parts.append(f"{field}:✓")
                    elif s == 'not_found':
                        status_parts.append(f"{field}:✗")
                status_summary = ', '.join(status_parts) if status_parts else 'No searches'

                test_row = [
                    school_name,
                    school_url,
                    rc_name,
                    oc_name,
                    results.get('rc_email', ''),
                    results.get('oc_email', ''),
                    results.get('rc_twitter', ''),
                    results.get('oc_twitter', ''),
                    status_summary,
                    ''  # Notes column for manual review
                ]
                test_row_num = processed_count + 2  # +2 for header and 1-indexing
                test_sheet.update(range_name=f'A{test_row_num}:J{test_row_num}', values=[test_row])
                logger.info(f"  Wrote results to test sheet row {test_row_num}")
                updates = sum(1 for v in [results.get('rc_email'), results.get('oc_email'),
                                          results.get('rc_twitter'), results.get('oc_twitter')] if v)
            else:
                # Update main sheet directly
                updates = update_sheet_row(sheet, row_num, results, row_data)

            if updates > 0:
                stats['updated'] += 1
                logger.info(f"  Found {updates} new values")

            stats['processed'] += 1
            processed_count += 1
            completed_rows.add(row_num)

        except Exception as e:
            logger.error(f"  Error processing {school_name}: {e}")
            log_error_to_sheet(errors_sheet, school_name, "scrape_school", str(e))
            stats['errors'] += 1
            processed_count += 1

        # Save progress after each school
        progress['completed_rows'] = list(completed_rows)
        progress['last_row'] = row_num
        save_progress(progress)

        # Delay between schools
        if idx < len(rows) - 1 and (limit == 0 or processed_count < limit):
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
    print("=" * 60)
    print("\n  DETAILED STATUS BREAKDOWN:")
    print("-" * 60)
    print(f"  {'Field':<15} {'Found':>8} {'Not Found':>12} {'Skipped':>10} {'Error':>8}")
    print("-" * 60)
    for field in ['rc_email', 'oc_email', 'rc_twitter', 'oc_twitter']:
        s = stats[field]
        display_name = field.replace('_', ' ').upper()
        print(f"  {display_name:<15} {s['found']:>8} {s['not_found']:>12} {s['skipped']:>10} {s['error']:>8}")
    print("-" * 60)

    # Calculate success rate
    total_attempts = sum(stats['rc_email'][k] + stats['oc_email'][k] +
                        stats['rc_twitter'][k] + stats['oc_twitter'][k]
                        for k in ['found', 'not_found', 'error'])
    total_found = (stats['rc_email']['found'] + stats['oc_email']['found'] +
                   stats['rc_twitter']['found'] + stats['oc_twitter']['found'])
    if total_attempts > 0:
        success_rate = (total_found / total_attempts) * 100
        print(f"\n  Success Rate: {total_found}/{total_attempts} ({success_rate:.1f}%)")
    print("=" * 60 + "\n")

    # Clear progress file on successful completion
    if stats['errors'] == 0 and (limit == 0 or processed_count >= limit):
        clear_progress()
        logger.info("All done! Progress file cleared.")
    else:
        logger.info(f"Completed with {stats['errors']} errors. Run again to retry failed rows.")


if __name__ == '__main__':
    try:
        main()
    finally:
        # Clean up Selenium browser
        close_browser()
