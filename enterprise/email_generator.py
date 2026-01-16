"""
AI Email Generator with Memory & Human-Like Writing
============================================================================
Generates personalized, human-sounding emails for each coach with:
- School/coach research with fact-checking
- Email memory (remembers what was sent before)
- Pre-generation in batches (run locally, use on Railway)
- Human-like writing style (no AI giveaways)

Author: Coach Outreach System
Version: 1.0.0
============================================================================
"""

import json
import os
import re
import time
import random
import logging
import requests
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from urllib.parse import quote_plus

# Browser manager for free Google scraping
try:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from browser.manager import BrowserManager, BrowserConfig
    from bs4 import BeautifulSoup
    HAS_BROWSER = True
except ImportError:
    HAS_BROWSER = False

logger = logging.getLogger(__name__)

# Configuration - API keys should be set via environment variables only
CONFIG = {
    'OLLAMA_URL': os.environ.get('OLLAMA_URL', 'http://localhost:11434/api/generate'),
    'OLLAMA_MODEL': os.environ.get('OLLAMA_MODEL', 'llama3.2:3b'),
    'OLLAMA_TIMEOUT': 120,  # Longer timeout for quality
    'GOOGLE_API_KEY': os.environ.get('GOOGLE_API_KEY', 'AIzaSyBSEzp2OF4lsFWgC-2goTfrZdRoKV_VyfA'),
    'GOOGLE_CSE_ID': os.environ.get('GOOGLE_CSE_ID', 'a37e7aad7fd3c4c7a'),
}

# Data directory
DATA_DIR = Path.home() / '.coach_outreach'
EMAIL_MEMORY_FILE = DATA_DIR / 'email_memory.json'
PREGENERATED_EMAILS_FILE = DATA_DIR / 'pregenerated_emails.json'
SCHOOL_RESEARCH_FILE = DATA_DIR / 'school_research.json'
API_USAGE_FILE = DATA_DIR / 'api_usage.json'

# Daily API limit (Google Custom Search free tier = 100/day)
DAILY_API_LIMIT = 100
SEARCHES_PER_SCHOOL = 3  # Each school research uses 3 searches (D2-optimized)


class APILimitReached(Exception):
    """Raised when daily API limit is reached."""
    pass


def get_api_usage_today() -> int:
    """Get the number of API calls made today."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    if API_USAGE_FILE.exists():
        try:
            with open(API_USAGE_FILE, 'r') as f:
                data = json.load(f)
                if data.get('date') == today:
                    return data.get('count', 0)
        except:
            pass
    return 0


def increment_api_usage() -> int:
    """Increment API usage counter. Returns new count."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    current = 0
    if API_USAGE_FILE.exists():
        try:
            with open(API_USAGE_FILE, 'r') as f:
                data = json.load(f)
                if data.get('date') == today:
                    current = data.get('count', 0)
        except:
            pass

    new_count = current + 1
    with open(API_USAGE_FILE, 'w') as f:
        json.dump({'date': today, 'count': new_count}, f)

    return new_count


def get_remaining_api_calls() -> int:
    """Get remaining API calls for today."""
    return max(0, DAILY_API_LIMIT - get_api_usage_today())


def get_remaining_schools_today() -> int:
    """Get how many schools we can still research today."""
    return get_remaining_api_calls() // SEARCHES_PER_SCHOOL


# ============================================================================
# HUMAN-LIKE WRITING SYSTEM PROMPT
# ============================================================================

HUMANIZE_SYSTEM_PROMPT = """You are Keelan Underwood, a 2026 offensive lineman from Florida (6'3", 295 lbs) writing emails to college coaches.

WRITE EXACTLY LIKE THIS EXAMPLE:
"Good Morning Coach Kelly, congrats on LSU's 10-1 season, I'm Keelan Underwood a class of 2026 offensive lineman from Florida, if you're looking to add offensive linemen for next season please check out my film, I think I'd be a great fit for LSU!"

KEY RULES:
1. Start with "Good Morning Coach [LAST NAME ONLY]," - NEVER use full names like "Coach John Smith", only "Coach Smith"
2. Immediately congratulate them on something SPECIFIC (record, recent win, championship, etc.)
3. Introduce yourself simply: "I'm Keelan Underwood a class of 2026 offensive lineman from Florida"
4. Make a simple ask: "if you're looking to add offensive linemen please check out my film"
5. End with confidence: "I think I'd be a great fit for [School]!"

CRITICAL GRAMMAR:
- Use "offensive linemen" (plural) NOT "offensive lineman" when referring to the position group
- Use "lineman" (singular) ONLY when referring to yourself specifically

NEVER USE:
- Full coach names like "Coach John Smith" - only use "Coach Smith"
- "I hope this email finds you well"
- "I am reaching out to"
- "I wanted to express my interest"
- "I've been following your program"
- "I believe I would be a great addition"
- Any formal/corporate language
- Multiple sentences about yourself
- Descriptions of your work ethic or technique

KEEP IT SHORT - 2-3 sentences max after the greeting. Simple. Direct. Like a real teenager texting.
DO NOT include any signature - the signature will be added automatically.
"""

# Standard email signature with Hudl link
EMAIL_SIGNATURE = """
Keelan Underwood
2026 OL | The Benjamin School
6'3" 295 lbs | 3.0 GPA
Film: https://www.hudl.com/profile/21702795/Keelan-Underwood
910-747-1140"""


def extract_last_name(full_name: str) -> str:
    """
    Extract the last name from a full name.
    'Brayden Long' -> 'Long'
    'John Smith Jr.' -> 'Smith'
    'Coach Davis' -> 'Davis'
    """
    if not full_name or not full_name.strip():
        return ""

    name = full_name.strip()

    # Remove common prefixes
    prefixes = ['Coach ', 'Dr. ', 'Mr. ', 'Mrs. ', 'Ms. ']
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):]

    # Remove common suffixes
    suffixes = [' Jr.', ' Jr', ' Sr.', ' Sr', ' III', ' II', ' IV']
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[:-len(suffix)]

    # Split and get last part
    parts = name.strip().split()
    if len(parts) == 0:
        return ""
    elif len(parts) == 1:
        return parts[0]
    else:
        return parts[-1]


def validate_coach_name(name: str) -> bool:
    """Check if a coach name is valid (not empty or placeholder)."""
    if not name or not name.strip():
        return False

    name_lower = name.lower().strip()
    invalid_names = ['coach', 'unknown', 'n/a', 'na', 'tbd', 'tba', '']

    return name_lower not in invalid_names and len(name.strip()) > 1

# ============================================================================
# EMAIL MEMORY SYSTEM
# ============================================================================

@dataclass
class EmailRecord:
    """Record of an email sent to a coach."""
    coach_email: str
    coach_name: str
    school: str
    email_type: str  # intro, followup_1, followup_2
    subject: str
    body: str
    personalized_content: str  # The AI-generated part
    sent_date: str
    template_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'EmailRecord':
        return cls(**data)


class EmailMemory:
    """Tracks all emails sent to each coach for context."""

    def __init__(self):
        self.memory: Dict[str, List[EmailRecord]] = {}  # coach_email -> list of emails
        self._load()

    def _load(self):
        """Load memory from disk."""
        if EMAIL_MEMORY_FILE.exists():
            try:
                with open(EMAIL_MEMORY_FILE, 'r') as f:
                    data = json.load(f)
                    for email, records in data.items():
                        self.memory[email] = [EmailRecord.from_dict(r) for r in records]
            except Exception as e:
                logger.warning(f"Error loading email memory: {e}")

    def _save(self):
        """Save memory to disk."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                email: [r.to_dict() for r in records]
                for email, records in self.memory.items()
            }
            with open(EMAIL_MEMORY_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving email memory: {e}")

    def get_history(self, coach_email: str) -> List[EmailRecord]:
        """Get email history for a coach."""
        return self.memory.get(coach_email.lower(), [])

    def get_last_email(self, coach_email: str) -> Optional[EmailRecord]:
        """Get the most recent email sent to a coach."""
        history = self.get_history(coach_email)
        return history[-1] if history else None

    def record_email(self, record: EmailRecord):
        """Record an email that was sent."""
        key = record.coach_email.lower()
        if key not in self.memory:
            self.memory[key] = []
        self.memory[key].append(record)
        self._save()

    def get_context_summary(self, coach_email: str) -> str:
        """Get a summary of previous emails for AI context."""
        history = self.get_history(coach_email)
        if not history:
            return "No previous emails sent."

        summary_parts = []
        for i, email in enumerate(history, 1):
            summary_parts.append(
                f"Email {i} ({email.email_type}, {email.sent_date}): {email.personalized_content[:200]}..."
            )
        return "\n".join(summary_parts)

    def get_stats(self) -> Dict:
        """Get memory statistics."""
        total_emails = sum(len(records) for records in self.memory.values())
        return {
            'coaches_contacted': len(self.memory),
            'total_emails_sent': total_emails,
        }


# Singleton
_email_memory = None

def get_email_memory() -> EmailMemory:
    global _email_memory
    if _email_memory is None:
        _email_memory = EmailMemory()
    return _email_memory


# ============================================================================
# SCHOOL RESEARCH
# ============================================================================

@dataclass
class SchoolResearch:
    """Research data about a school."""
    school_name: str
    division: str = ""
    conference: str = ""
    head_coach: str = ""
    ol_coach: str = ""
    recent_record: str = ""
    conference_standing: str = ""  # D2: conference champs, playoff appearance, etc.
    ol_depth_need: str = ""  # D2: graduating seniors, roster gaps
    notable_academics: str = ""  # D2: known majors, academic programs
    recent_news: List[str] = field(default_factory=list)
    coach_quotes: List[str] = field(default_factory=list)
    notable_facts: List[str] = field(default_factory=list)
    ol_nfl_players: List[str] = field(default_factory=list)
    verified: bool = False
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'SchoolResearch':
        return cls(**data)


def google_search(query: str, num_results: int = 5) -> List[Dict]:
    """Search Google for school information. Tracks API usage."""
    if not CONFIG['GOOGLE_API_KEY'] or not CONFIG['GOOGLE_CSE_ID']:
        return []

    # Check if we've hit daily limit
    current_usage = get_api_usage_today()
    if current_usage >= DAILY_API_LIMIT:
        logger.warning(f"Daily API limit reached ({current_usage}/{DAILY_API_LIMIT})")
        raise APILimitReached(f"Daily API limit of {DAILY_API_LIMIT} reached. Resets at midnight Pacific.")

    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            'key': CONFIG['GOOGLE_API_KEY'],
            'cx': CONFIG['GOOGLE_CSE_ID'],
            'q': query,
            'num': num_results,
        }
        response = requests.get(url, params=params, timeout=10)

        # Track usage (even on error, API call was made)
        new_count = increment_api_usage()

        # Handle rate limit from Google
        if response.status_code == 429:
            logger.warning(f"Google rate limited (429). Usage: {new_count}/{DAILY_API_LIMIT}")
            raise APILimitReached("Google API returned 429 - rate limited")

        response.raise_for_status()
        data = response.json()

        logger.debug(f"API call {new_count}/{DAILY_API_LIMIT}: {query[:50]}...")

        return [
            {'title': item.get('title', ''), 'snippet': item.get('snippet', ''), 'link': item.get('link', '')}
            for item in data.get('items', [])
        ]
    except APILimitReached:
        raise  # Re-raise limit errors
    except Exception as e:
        logger.warning(f"Google search error: {e}")
        return []


# Global browser instance for reuse
_browser_manager: Optional[BrowserManager] = None


def bing_search_free(query: str, num_results: int = 5) -> List[Dict]:
    """
    Free search using Bing (no CAPTCHA, scraper-friendly).
    No API limits - can do unlimited searches.
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        encoded_query = quote_plus(query)
        url = f"https://www.bing.com/search?q={encoded_query}&count={num_results + 5}"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }

        response = requests.get(url, headers=headers, timeout=15, verify=False)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'lxml')
        results = []

        # Bing uses 'b_algo' class for organic results
        for result in soup.find_all('li', class_='b_algo'):
            try:
                # Get title and link
                h2 = result.find('h2')
                if not h2:
                    continue

                link_elem = h2.find('a')
                if not link_elem:
                    continue

                title = link_elem.get_text(strip=True)
                link = link_elem.get('href', '')

                # Get snippet
                snippet_elem = result.find('p')
                snippet = snippet_elem.get_text(strip=True) if snippet_elem else ''

                if title and link and link.startswith('http'):
                    results.append({
                        'title': title,
                        'snippet': snippet,
                        'link': link
                    })

                    if len(results) >= num_results:
                        break

            except Exception:
                continue

        # Small delay between searches
        time.sleep(random.uniform(0.5, 1.5))

        logger.debug(f"Bing found {len(results)} results for: {query[:40]}...")
        return results

    except Exception as e:
        logger.warning(f"Bing search error: {e}")
        return []


def google_search_free(query: str, num_results: int = 5) -> List[Dict]:
    """
    Free search - uses Bing (Google blocks scrapers with CAPTCHA).
    No API limits - can do unlimited searches.
    """
    return bing_search_free(query, num_results)


def smart_search(query: str, num_results: int = 5, prefer_free: bool = True) -> List[Dict]:
    """
    Smart search that uses free headless scraping by default.
    Falls back to API if browser unavailable.

    Args:
        query: Search query
        num_results: Number of results to return
        prefer_free: If True, use free scraping first (unlimited). If False, use API first.
    """
    if prefer_free:
        # Try free scraping first (unlimited)
        results = google_search_free(query, num_results)
        if results:
            return results

        # Fall back to API
        logger.info("Free search failed, falling back to API")
        try:
            return google_search(query, num_results)
        except APILimitReached:
            logger.warning("API limit reached and free search unavailable")
            return []
    else:
        # Try API first
        try:
            results = google_search(query, num_results)
            if results:
                return results
        except APILimitReached:
            pass

        # Fall back to free scraping
        return google_search_free(query, num_results)


def cleanup_browser():
    """Clean up browser resources. Call when done with searches."""
    global _browser_manager
    if _browser_manager:
        _browser_manager.stop()
        _browser_manager = None


def query_ollama(prompt: str, system_prompt: str = None, temperature: float = 0.7) -> Optional[str]:
    """Query Ollama with optional system prompt."""
    try:
        full_prompt = ""
        if system_prompt:
            full_prompt = f"System: {system_prompt}\n\nUser: {prompt}"
        else:
            full_prompt = prompt

        payload = {
            'model': CONFIG['OLLAMA_MODEL'],
            'prompt': full_prompt,
            'stream': False,
            'options': {
                'temperature': temperature,
                'num_predict': 800,
            }
        }

        response = requests.post(
            CONFIG['OLLAMA_URL'],
            json=payload,
            timeout=CONFIG['OLLAMA_TIMEOUT']
        )
        response.raise_for_status()

        result = response.json()
        return result.get('response', '').strip()
    except requests.exceptions.ConnectionError:
        logger.debug("Ollama not available")
        return None
    except Exception as e:
        logger.warning(f"Ollama error: {e}")
        return None


def research_school_deep(school_name: str, coach_name: str = "") -> SchoolResearch:
    """
    Deep research on a school with fact-checking.
    Takes its time to gather accurate information.
    """
    research = SchoolResearch(school_name=school_name)

    logger.info(f"Researching {school_name}...")

    # Search queries optimized for D2/smaller schools
    queries = [
        f"{school_name} football 2024 2025 season record conference",  # Record + conference standing
        f"{school_name} football roster offensive line 2025",  # Roster/OL depth
        f"{school_name} university academics majors programs",  # Notable academics
    ]

    all_results = []
    for query in queries:
        # Use API (free scraping blocked by network filter)
        results = smart_search(query, num_results=3, prefer_free=False)
        all_results.extend(results)
        time.sleep(0.3)  # Small delay between searches

    if not all_results:
        logger.warning(f"No search results for {school_name}")
        return research

    # Combine snippets
    combined_text = "\n".join([
        f"- {r['title']}: {r['snippet']}"
        for r in all_results if r.get('snippet')
    ])[:4000]

    # Use AI to extract and verify information (optimized for D2/smaller schools)
    extract_prompt = f"""Analyze these search results about {school_name} football and extract VERIFIED facts only.

SEARCH RESULTS:
{combined_text}

Extract ONLY information that is clearly stated in the results. Do NOT make anything up.
If something is not mentioned, say "unknown".

Respond in this exact format:
DIVISION: [FBS/FCS/D2/D3/NAIA or unknown]
CONFERENCE: [conference name or unknown]
HEAD_COACH: [name or unknown]
OL_COACH: [offensive line coach name or unknown]
RECENT_RECORD: [2024 or recent record like "8-4" or unknown]
CONFERENCE_STANDING: [conference champs, playoff appearance, bowl game, or unknown]
OL_DEPTH_NEED: [number of senior OL graduating, OL roster size, or if they need OL help, or unknown]
NOTABLE_ACADEMICS: [what the school is known for academically - engineering, business, nursing, etc. or unknown]
NOTABLE_FACTS: [2-3 specific facts about the program, comma separated]
"""

    ai_response = query_ollama(extract_prompt, temperature=0.3)  # Low temp for accuracy

    if ai_response:
        # Parse response
        for line in ai_response.split('\n'):
            line = line.strip()
            if line.startswith('DIVISION:'):
                val = line.replace('DIVISION:', '').strip()
                if val.lower() != 'unknown':
                    research.division = val
            elif line.startswith('CONFERENCE:'):
                val = line.replace('CONFERENCE:', '').strip()
                if val.lower() != 'unknown':
                    research.conference = val
            elif line.startswith('HEAD_COACH:'):
                val = line.replace('HEAD_COACH:', '').strip()
                if val.lower() != 'unknown':
                    research.head_coach = val
            elif line.startswith('OL_COACH:'):
                val = line.replace('OL_COACH:', '').strip()
                if val.lower() != 'unknown':
                    research.ol_coach = val
            elif line.startswith('RECENT_RECORD:'):
                val = line.replace('RECENT_RECORD:', '').strip()
                if val.lower() != 'unknown':
                    research.recent_record = val
            elif line.startswith('CONFERENCE_STANDING:'):
                val = line.replace('CONFERENCE_STANDING:', '').strip()
                if val.lower() != 'unknown':
                    research.conference_standing = val
            elif line.startswith('OL_DEPTH_NEED:'):
                val = line.replace('OL_DEPTH_NEED:', '').strip()
                if val.lower() != 'unknown':
                    research.ol_depth_need = val
            elif line.startswith('NOTABLE_ACADEMICS:'):
                val = line.replace('NOTABLE_ACADEMICS:', '').strip()
                if val.lower() != 'unknown':
                    research.notable_academics = val
            elif line.startswith('NOTABLE_FACTS:'):
                val = line.replace('NOTABLE_FACTS:', '').strip()
                if val.lower() != 'unknown' and val:
                    research.notable_facts = [f.strip() for f in val.split(',') if f.strip()]

    research.verified = True
    research.last_updated = datetime.now().isoformat()

    logger.info(f"Research complete for {school_name}: {len(research.notable_facts)} facts found")

    return research


# ============================================================================
# EMAIL GENERATION
# ============================================================================

@dataclass
class PregeneratedEmail:
    """A pre-generated email ready to send."""
    school: str
    coach_name: str
    coach_email: str
    email_type: str  # intro, followup_1, followup_2
    personalized_content: str  # The AI-generated personalized part
    research_used: Dict  # Research data that was used
    generated_at: str
    used: bool = False
    research_failed: bool = False  # True if research had no real data - Railway should use templates

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'PregeneratedEmail':
        # Handle older emails without research_failed field
        if 'research_failed' not in data:
            data['research_failed'] = False
        return cls(**data)


class EmailGenerator:
    """Generates personalized, human-sounding emails."""

    def __init__(self):
        self.pregenerated: Dict[str, List[PregeneratedEmail]] = {}  # school -> list
        self.research_cache: Dict[str, SchoolResearch] = {}
        self._load()

    def _load(self):
        """Load pregenerated emails and research cache."""
        if PREGENERATED_EMAILS_FILE.exists():
            try:
                with open(PREGENERATED_EMAILS_FILE, 'r') as f:
                    data = json.load(f)
                    for school, emails in data.items():
                        self.pregenerated[school] = [PregeneratedEmail.from_dict(e) for e in emails]
            except Exception as e:
                logger.warning(f"Error loading pregenerated emails: {e}")

        if SCHOOL_RESEARCH_FILE.exists():
            try:
                with open(SCHOOL_RESEARCH_FILE, 'r') as f:
                    data = json.load(f)
                    for school, research in data.items():
                        self.research_cache[school] = SchoolResearch.from_dict(research)
            except Exception as e:
                logger.warning(f"Error loading research cache: {e}")

    def _save(self):
        """Save pregenerated emails and research cache."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        try:
            data = {
                school: [e.to_dict() for e in emails]
                for school, emails in self.pregenerated.items()
            }
            with open(PREGENERATED_EMAILS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving pregenerated emails: {e}")

        try:
            data = {
                school: r.to_dict()
                for school, r in self.research_cache.items()
            }
            with open(SCHOOL_RESEARCH_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving research cache: {e}")

    def get_research(self, school: str, coach_name: str = "", force_refresh: bool = False) -> SchoolResearch:
        """Get research for a school, using cache if available."""
        cache_key = school.lower().strip()

        if not force_refresh and cache_key in self.research_cache:
            cached = self.research_cache[cache_key]
            # Check if cache is fresh (within 7 days)
            try:
                cached_date = datetime.fromisoformat(cached.last_updated)
                if (datetime.now() - cached_date).days < 7:
                    return cached
            except:
                pass

        # Do fresh research
        research = research_school_deep(school, coach_name)
        self.research_cache[cache_key] = research
        self._save()

        return research

    def generate_personalized_content(
        self,
        school: str,
        coach_name: str,
        coach_email: str,
        email_type: str = 'intro',
        research: SchoolResearch = None
    ) -> str:
        """
        Generate personalized email content using AI.
        Returns just the personalized paragraph(s), not the full email.
        """
        # Validate and extract last name from coach name
        if not validate_coach_name(coach_name):
            logger.warning(f"Invalid coach name '{coach_name}' for {school} - using fallback")
            return self._get_fallback_content(school, email_type, research or SchoolResearch(school_name=school)) + EMAIL_SIGNATURE

        # Extract just the last name for email greeting
        last_name = extract_last_name(coach_name)
        if not last_name:
            logger.warning(f"Could not extract last name from '{coach_name}' for {school} - using fallback")
            return self._get_fallback_content(school, email_type, research or SchoolResearch(school_name=school)) + EMAIL_SIGNATURE

        memory = get_email_memory()
        previous_context = memory.get_context_summary(coach_email)

        if research is None:
            research = self.get_research(school, coach_name)

        # Build context about the school
        school_context_parts = []
        if research.division:
            school_context_parts.append(f"Division: {research.division}")
        if research.conference:
            school_context_parts.append(f"Conference: {research.conference}")
        if research.head_coach:
            school_context_parts.append(f"Head Coach: {research.head_coach}")
        if research.ol_coach:
            school_context_parts.append(f"OL Coach: {research.ol_coach}")
        if research.recent_record:
            school_context_parts.append(f"Recent Record: {research.recent_record}")
        if research.conference_standing:
            school_context_parts.append(f"Conference Standing: {research.conference_standing}")
        if research.ol_depth_need:
            school_context_parts.append(f"OL Roster Need: {research.ol_depth_need}")
        if research.notable_academics:
            school_context_parts.append(f"School Known For: {research.notable_academics}")
        if research.notable_facts:
            school_context_parts.append(f"Notable Facts: {'; '.join(research.notable_facts[:3])}")

        school_context = "\n".join(school_context_parts) if school_context_parts else "Limited info available"

        # Get successful email examples for AI to learn from
        successful_examples = self._get_successful_examples(email_type)

        # Different prompts for intro vs followup
        if email_type == 'intro':
            user_prompt = f"""Write a SHORT email to Coach {last_name} at {school}.
{successful_examples}
SCHOOL INFO:
{school_context}

FORMAT - Follow this EXACT structure:
"Good Morning Coach {last_name}, [OPENING - see options below], I'm Keelan Underwood a class of 2026 offensive lineman from Florida, if you're looking to add offensive linemen for next season please check out my film, I think I'd be a great fit for {school}!"

OPENING OPTIONS (pick the BEST one based on what info you have - be SPECIFIC):
- If they WON their conference or made playoffs: "congrats on [winning the conference/making the playoffs]"
- If they had a GOOD season (7+ wins): "congrats on the [their record] season"
- If they NEED OL help (losing seniors, small roster): "I saw you might be looking to add depth on the offensive line"
- If you know their ACADEMICS: "I'm interested in {school}'s [specific program - engineering, business, etc.] and the football program"
- If they have a NEW coach: "excited to see what you're building in your first year"
- If they had a rough season: "excited to see what you're building at {school}" (don't mention bad record)
- LAST RESORT only: "I've been looking at {school}'s program and I'm really interested"

CRITICAL RULES:
- Use SPECIFIC info from above. Do NOT use generic phrases if you have real info.
- Use "offensive linemen" (plural) when talking about the position group
- Use ONLY the last name "{last_name}" - never the full name

Keep it to ONE short paragraph. Be genuine, not fake."""

        elif email_type == 'followup_1':
            user_prompt = f"""Write a SHORT follow-up email to Coach {last_name} at {school}.
{successful_examples}
PREVIOUS EMAIL:
{previous_context}

SCHOOL INFO:
{school_context}

FORMAT - Follow this EXACT structure:
"Good Morning Coach {last_name}, just wanted to follow up on my last email, I saw [SPECIFIC THING - pick ONE: recent game result, player award, recruiting news, or coach quote] and I'm still very interested in {school}, please let me know if you've had a chance to check out my film!"

CRITICAL RULES:
1. Use ONLY the last name "{last_name}" - never the full name
2. You MUST mention something SPECIFIC about {school} - a game, a player, a record, SOMETHING
3. DO NOT just say "I wanted to follow up" with nothing specific
4. Use the SCHOOL INFO above to find something specific to mention
5. If you can't find anything specific, mention their conference or division
6. Keep it to ONE paragraph

Example good followup: "Good Morning Coach Smith, just wanted to follow up on my last email, I saw Ohio State beat Penn State 28-17 last week and the offensive line looked dominant, I'm still very interested in Ohio State, please let me know if you've had a chance to check out my film!"

Example BAD followup (DO NOT DO THIS): "I wanted to follow up on my previous email. I'm still very interested and would appreciate any feedback."

Keep it SHORT but SPECIFIC."""

        else:  # followup_2
            user_prompt = f"""Write a SHORT final follow-up email to Coach {last_name} at {school}.
{successful_examples}
PREVIOUS EMAILS:
{previous_context}

SCHOOL INFO:
{school_context}

FORMAT - Follow this structure:
"Good Morning Coach {last_name}, I know you're busy but I wanted to check in one more time about {school}, I've been working hard this offseason and would love the opportunity to show you what I can do, let me know if there's anything else you need from me!"

CRITICAL: Use ONLY the last name "{last_name}" - never the full name.

Keep it SHORT - one paragraph max."""

        # Generate with AI - try up to 2 times if content is too short
        for attempt in range(2):
            response = query_ollama(user_prompt, system_prompt=HUMANIZE_SYSTEM_PROMPT, temperature=0.8)

            if response:
                content = self._cleanup_ai_content(response, coach_name)

                # Check for minimum viable content (not just a greeting)
                # Must be >100 chars and contain more than just "Good Morning Coach X"
                # A proper intro should include: greeting + specific hook + self intro + ask
                if content and len(content) > 100:
                    # Check it's not JUST a greeting with no substance
                    greeting_only = re.match(
                        r'^Good\s+(Morning|Afternoon|Evening)\s+Coach\s+[\w\s\.]+[,!]?\s*$',
                        content,
                        re.IGNORECASE
                    )
                    if not greeting_only:
                        # Also check for generic followups that lack specifics
                        generic_followup = re.search(
                            r"I wanted to follow up.*I'm still.*interested.*would appreciate",
                            content,
                            re.IGNORECASE | re.DOTALL
                        )
                        if not generic_followup:
                            # For intros, make sure it contains the self-introduction
                            if email_type == 'intro':
                                has_intro = re.search(r"I'm Keelan|I am Keelan|Keelan Underwood", content, re.IGNORECASE)
                                if not has_intro:
                                    logger.warning(f"Attempt {attempt+1}: Intro missing self-introduction, retrying...")
                                    continue
                            return content + EMAIL_SIGNATURE

                logger.warning(f"Attempt {attempt+1}: Content too short or generic, retrying...")

        # Fallback if AI unavailable or produced bad output after retries
        return self._get_fallback_content(school, email_type, research) + EMAIL_SIGNATURE

    def _cleanup_ai_content(self, content: str, coach_name: str) -> str:
        """Clean up AI-generated content to remove giveaways."""
        content = content.strip()

        # Remove AI preambles
        bad_starts = [
            "Sure!", "Here's", "Here is", "Of course", "Certainly",
            "Here are", "I'd be happy", "Absolutely", "Great question",
        ]
        for bad_start in bad_starts:
            if content.lower().startswith(bad_start.lower()):
                parts = content.split('\n', 1)
                content = parts[1].strip() if len(parts) > 1 else content

        # Remove quotes wrapping the whole thing
        if content.startswith('"') and content.endswith('"'):
            content = content[1:-1]
        if content.startswith("'") and content.endswith("'"):
            content = content[1:-1]

        # Remove casual greetings (should be in template, not AI content)
        casual_greetings = [
            f"Hey Coach {coach_name}", f"Hey Coach", "Hey,", "Hi Coach",
            f"Coach {coach_name},", "Dear Coach",
        ]
        for greeting in casual_greetings:
            if content.lower().startswith(greeting.lower()):
                content = content[len(greeting):].strip()
                if content.startswith(','):
                    content = content[1:].strip()

        # Remove bad endings
        bad_endings = [
            "Keep crushing it!", "Go Knights!", "Go Noles!", "Go Gators!",
            "Roll Tide!", "Hook 'em!", "Geaux Tigers!", "Go Dawgs!", "Go Bucks!",
            "Go Hurricanes!", "Go Longhorns!", "Go Seminoles!", "Go Bulldogs!",
            "Look forward to", "Looking forward to",
            "Let me know", "Hope to hear from you",
            "Can't wait to", "Thanks for your time",
            "joining the", "family and contributing",
            "Keep up the great work", "hope this email finds you well",
            "hope things are going well", "Hey there!",
        ]
        for ending in bad_endings:
            if ending.lower() in content.lower()[-100:]:
                # Find and remove the sentence containing this
                sentences = content.split('.')
                content = '. '.join([
                    s for s in sentences
                    if ending.lower() not in s.lower()
                ]).strip()
                if content and not content.endswith('.'):
                    content += '.'

        # Remove parenthetical asides like "(Just a side note:...)"
        content = re.sub(r'\([^)]*side note[^)]*\)', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\(P\.?S\.?[^)]*\)', '', content, flags=re.IGNORECASE)

        # Remove P.S. sections
        if 'P.S.' in content or 'P.S' in content:
            content = re.split(r'P\.?S\.?', content)[0].strip()

        # Remove multiple exclamation points, keep max 1
        while '!!' in content:
            content = content.replace('!!', '!')

        # Remove em dashes
        content = content.replace('—', '-').replace('–', '-')

        # Remove slang/casual language
        slang_replacements = [
            ("y'all", "your"),
            ("you's", "your"),
            ("ya ", "you "),
            ("diggin'", "researching"),
            (" dig ", " appreciate "),
            ("packin'", "at"),
            ("packing in", "standing"),
            ("gonna", "going to"),
            ("wanna", "want to"),
            ("gotta", "have to"),
            ("kinda", "kind of"),
            ("ain't", "am not"),
            ("'bout", "about"),
            ("crushing on", "impressed by"),
            ("blown away", "impressed"),
            ("that family", "the program"),
            ("State family", "State program"),
            # Common -in' endings
            ("smashin'", "dominating"),
            ("watchin'", "watching"),
            ("standin'", "standing"),
            ("weighin'", "weighing"),
            ("grindin'", "grinding"),
            ("workin'", "working"),
            ("gettin'", "getting"),
            ("blockin'", "blocking"),
            ("playin'", "playing"),
            ("knowin'", "knowing"),
            ("doin'", "doing"),
            ("comin'", "coming"),
            ("runnin'", "running"),
            ("hittin'", "hitting"),
            # Other casual phrases
            ("got goin'", "have going"),
            ("Keep up the great work!", ""),
            ("Best regards,.", ""),
            ("Best regards,", ""),
        ]
        for slang, replacement in slang_replacements:
            content = re.sub(re.escape(slang), replacement, content, flags=re.IGNORECASE)

        # Catch any remaining -in' patterns (e.g., "lookin'" -> "looking")
        content = re.sub(r"(\w+)in'(\s|$|[.,!?])", r"\1ing\2", content)

        # Remove casual greetings at the start
        casual_starts = ["hey,", "hey ", "hey!", "there!", "hi,", "hi ", "hello,", "hello "]
        for start in casual_starts:
            if content.lower().startswith(start):
                content = content[len(start):].strip()
                # Capitalize first letter after removal
                if content:
                    content = content[0].upper() + content[1:]

        # Remove "hope this email finds" variations
        content = re.sub(r"I hope this email finds[^.]*\.", "", content, flags=re.IGNORECASE)
        content = re.sub(r"Hope this email finds[^.]*\.", "", content, flags=re.IGNORECASE)

        # Remove AI instruction leakage (text in parentheses with instructions)
        content = re.sub(r'\([^)]*signature[^)]*\)', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\([^)]*name at the end[^)]*\)', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\([^)]*if you prefer[^)]*\)', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\([^)]*no additional[^)]*\)', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\([^)]*available[^)]*\)', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\([^)]*just a[^)]*\)', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\([^)]*note:[^)]*\)', '', content, flags=re.IGNORECASE)

        # Remove bracket placeholders that AI left unfilled like [new coach/transfer]
        content = re.sub(r'\[[^\]]*coach[^\]]*\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\[[^\]]*transfer[^\]]*\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\[[^\]]*player[^\]]*\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\[[^\]]*name[^\]]*\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\[[^\]]*specific[^\]]*\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\[[^\]]*insert[^\]]*\]', '', content, flags=re.IGNORECASE)
        # Clean up any double spaces left behind
        content = re.sub(r'\s+', ' ', content)

        # Remove any remaining quotes wrapping content
        content = content.strip()
        if content.startswith('"') and (content.endswith('"') or content.count('"') == 1):
            content = content.strip('"')
        if content.startswith("'") and (content.endswith("'") or content.count("'") == 1):
            content = content.strip("'")

        # Fix common grammar issues
        content = re.sub(r'\byour are\b', 'you are', content, flags=re.IGNORECASE)
        content = re.sub(r'\bit am\b', 'it is', content, flags=re.IGNORECASE)
        content = re.sub(r'\bif it is not too much trouble\b', '', content, flags=re.IGNORECASE)

        # Fix "offensive lineman" to "offensive linemen" when referring to the position group
        # But keep "lineman" when referring to self ("I'm a... lineman")
        # Pattern: "more offensive lineman" or "add offensive lineman" should be "linemen"
        content = re.sub(r'\bmore offensive lineman\b', 'more offensive linemen', content, flags=re.IGNORECASE)
        content = re.sub(r'\badd offensive lineman\b', 'add offensive linemen', content, flags=re.IGNORECASE)
        content = re.sub(r'\bneed offensive lineman\b', 'need offensive linemen', content, flags=re.IGNORECASE)
        content = re.sub(r'\bneed of offensive lineman\b', 'looking for offensive linemen', content, flags=re.IGNORECASE)
        content = re.sub(r'\bin need of more offensive lineman\b', 'looking to add offensive linemen', content, flags=re.IGNORECASE)
        content = re.sub(r'\bstill in need of more offensive lineman\b', 'looking to add offensive linemen', content, flags=re.IGNORECASE)
        content = re.sub(r"if you're still in need of more offensive lineman", "if you're looking to add offensive linemen", content, flags=re.IGNORECASE)

        # Remove sign-offs that shouldn't be in the personalized content
        signoff_patterns = [
            r'Best,?\s*\[?Your Name\]?\.?$',
            r'Best,?\s*Keelan\.?$',
            r'Best,?\s*Keelan Underwood\.?$',
            r'Best,?\s*$',
            r'Sincerely,?\s*\[?Your Name\]?\.?$',
            r'Sincerely,?\s*Keelan\.?$',
            r'Sincerely,?\s*Keelan Underwood\.?$',
            r'Respectfully,?\s*\[?Your Name\]?\.?$',
            r'Respectfully,?\s*Keelan\.?$',
            r'\[Your Name\]\.?$',
            r'Thanks,?\s*Keelan\.?$',
            r'Thanks,?\s*$',
            r'Keelan Underwood\.?$',
            r'Keelan\.?$',
            r'-\s*Keelan\.?$',
            r'—\s*Keelan\.?$',
        ]
        for pattern in signoff_patterns:
            content = re.sub(pattern, '', content, flags=re.IGNORECASE).strip()

        # FIX BAD SEASON CONGRATULATIONS - never congratulate losing or .500 records
        # Pattern matches: "congrats on finishing/the/a X-Y" where X <= Y (losing or .500 record)
        def fix_bad_season_congrats(match):
            full_match = match.group(0)
            wins = int(match.group(1))
            losses = int(match.group(2))
            if wins <= losses:  # Include .500 records - don't congratulate those either
                return "excited to see what you're building"
            return full_match  # Keep original if winning record

        # Catch multiple patterns: "congrats on the 4-6", "congrats on finishing 1-10", etc.
        content = re.sub(
            r'congrats on (?:the |a |finishing (?:the )?(?:season )?)?(\d+)-(\d+)(?:\s+(?:season|record|year|despite[^.]*)?)?',
            fix_bad_season_congrats,
            content,
            flags=re.IGNORECASE
        )

        # Also catch "congrats on the tough X-Y" and "congrats on a tough X-Y" patterns
        content = re.sub(
            r'congrats on (?:the |a )?tough \d+-\d+[^.!,]*(?:but[^.!]*)?',
            "excited to see what you're building",
            content,
            flags=re.IGNORECASE
        )

        # Catch awkward phrasing like "congrats on ... despite a tough year"
        content = re.sub(
            r'congrats on [^.]*despite[^.]*tough[^.]*',
            "excited to see what you're building",
            content,
            flags=re.IGNORECASE
        )

        # Remove any remaining "tough X-Y season" phrasing that sounds negative
        content = re.sub(
            r'tough \d+-\d+\s*(?:season|record|year)',
            'this season',
            content,
            flags=re.IGNORECASE
        )

        # Catch ANY congratulations combined with a losing record in the same sentence
        # e.g., "Congrats on going 1-10" or "Congrats on the 2-9 season"
        def fix_congrats_losing_record(match):
            full_sentence = match.group(0)
            # Find all records in the sentence
            records = re.findall(r'(\d+)-(\d+)', full_sentence)
            for wins_str, losses_str in records:
                wins, losses = int(wins_str), int(losses_str)
                if wins <= losses:  # Losing or .500 record
                    return "excited to see what you're building this season"
            return full_sentence  # Keep if winning record

        # Match sentences starting with Congrats that contain a record
        content = re.sub(
            r'[Cc]ongrats[^.!]*\d+-\d+[^.!]*',
            fix_congrats_losing_record,
            content
        )

        # Remove "Let's talk" type endings
        lets_talk_patterns = [
            r"Let's talk football\.?$",
            r"Let's talk shop\.?$",
            r"Let's connect\.?$",
            r"Let's chat\.?$",
        ]
        for pattern in lets_talk_patterns:
            content = re.sub(pattern, '', content, flags=re.IGNORECASE).strip()

        # Remove "And hey" casual interjections
        content = re.sub(r'\bAnd hey,?\s*', '', content, flags=re.IGNORECASE)

        # Clean up extra whitespace
        content = re.sub(r'\s+', ' ', content).strip()
        content = re.sub(r'\s+\.', '.', content)

        # Limit to ~3 sentences max (find 3rd period and cut there)
        # But make sure we don't cut off mid-sentence
        sentences = [s.strip() for s in content.split('.') if s.strip()]
        if len(sentences) > 3:
            content = '. '.join(sentences[:3]).strip() + '.'

        # Fix content that got cut off mid-sentence (ends with incomplete thought)
        # Check if content ends abruptly (common patterns)
        incomplete_endings = [
            r'\d+$',  # Ends with just a number like "averaging 306"
            r'\bthe$', r'\ba$', r'\ban$', r'\band$', r'\bor$', r'\bto$', r'\bfor$',
            r'\bwith$', r'\bin$', r'\bon$', r'\bat$', r'\bof$', r'\bby$',
            r'\bthat$', r'\bwhich$', r'\bwho$', r'\bwhere$', r'\bwhen$',
            r"'s$", r"'s\.$",  # Ends with possessive like "UNCG's."
            r'\bif$', r'\bI$', r'\byou$', r'\btheir$', r'\byour$',
        ]
        for pattern in incomplete_endings:
            if re.search(pattern, content, re.IGNORECASE):
                # Remove the incomplete last sentence
                sentences = [s.strip() for s in content.split('.') if s.strip()]
                if len(sentences) > 1:
                    content = '. '.join(sentences[:-1]).strip() + '.'
                elif len(sentences) == 1:
                    # If only one sentence and it's incomplete, try to find last complete thought
                    # Look for comma and cut there
                    comma_pos = content.rfind(',')
                    if comma_pos > 50:  # Only if we have enough content before the comma
                        content = content[:comma_pos] + '!'
                break

        # Also detect and fix sentences ending with "if you've had a chance to check out my film!"
        # that got cut off - these should have a closing
        if content.endswith('my film!') or content.endswith('my film'):
            if 'let me know' not in content.lower() and 'please' not in content.lower():
                content = content.rstrip('!') + ', I think I\'d be a great fit!'

        # Ensure proper ending
        if content and content[-1] not in '.!?':
            content += '!'

        # Final cleanup - remove any trailing incomplete phrases after punctuation
        content = re.sub(r'([.!?])\s+\w{1,3}$', r'\1', content)

        return content

    def _get_successful_examples(self, email_type: str, max_examples: int = 2) -> str:
        """
        Get examples of emails that received positive responses.
        Used to help AI generate better emails by learning from what works.
        """
        try:
            from sheets.cloud_emails import get_cloud_storage
            storage = get_cloud_storage()
            successful = storage.get_successful_emails()

            # Filter by email type and get unique examples
            relevant = [e for e in successful if e.get('email_type') == email_type]

            if not relevant:
                return ""

            # Get up to max_examples unique ones
            examples = []
            seen_bodies = set()
            for email in relevant[:max_examples * 2]:  # Check more to find unique ones
                body = email.get('body', '')[:200]  # First 200 chars for dedup
                if body not in seen_bodies and len(body) > 50:
                    seen_bodies.add(body)
                    # Truncate for prompt
                    truncated = email.get('body', '')[:500]
                    if len(email.get('body', '')) > 500:
                        truncated += '...'
                    examples.append(truncated)
                    if len(examples) >= max_examples:
                        break

            if not examples:
                return ""

            examples_text = "\n---\n".join(examples)
            return f"""
EXAMPLES OF EMAILS THAT GOT RESPONSES (learn from these patterns):
{examples_text}
---
"""
        except Exception as e:
            logger.warning(f"Could not load successful examples: {e}")
            return ""

    def _get_fallback_content(self, school: str, email_type: str, research: SchoolResearch) -> str:
        """Get fallback content when AI is unavailable."""
        if email_type == 'intro':
            if research.recent_record:
                # Check if it's a winning record before congratulating
                record_match = re.match(r'(\d+)-(\d+)', research.recent_record)
                if record_match:
                    wins, losses = int(record_match.group(1)), int(record_match.group(2))
                    if wins > losses:
                        return f"Good Morning Coach, congrats on {school}'s {research.recent_record} season, I'm Keelan Underwood a class of 2026 offensive lineman from Florida, if you're looking to add offensive linemen for next season please check out my film, I think I'd be a great fit for {school}!"
                return f"Good Morning Coach, excited to see what you're building at {school}, I'm Keelan Underwood a class of 2026 offensive lineman from Florida, if you're looking to add offensive linemen for next season please check out my film, I think I'd be a great fit for {school}!"
            elif research.conference:
                return f"Good Morning Coach, I've been looking at {school}'s program in the {research.conference}, I'm Keelan Underwood a class of 2026 offensive lineman from Florida, if you're looking to add offensive linemen for next season please check out my film, I think I'd be a great fit for {school}!"
            else:
                return f"Good Morning Coach, I've been researching {school}'s program and I'm really interested, I'm Keelan Underwood a class of 2026 offensive lineman from Florida, if you're looking to add offensive linemen for next season please check out my film, I think I'd be a great fit for {school}!"

        elif email_type == 'followup_1':
            # Make followup specific with available info
            if research.recent_record:
                return f"Good Morning Coach, just wanted to follow up on my last email, I saw {school} finished the season {research.recent_record} and I'm still very interested in being part of the program, please let me know if you've had a chance to check out my film!"
            elif research.conference:
                return f"Good Morning Coach, just wanted to follow up on my last email, I've been following {school}'s performance in the {research.conference} and I'm still very interested in the program, please let me know if you've had a chance to check out my film!"
            else:
                return f"Good Morning Coach, just wanted to follow up on my last email about {school}, I've been working hard this offseason and I'm still very interested in the program, please let me know if you've had a chance to check out my film!"

        else:
            return f"Good Morning Coach, I know you're busy but I wanted to check in one more time about {school}, I've been working hard this offseason and would love the opportunity to show you what I can do, let me know if there's anything else you need from me!"

    def pregenerate_for_school(
        self,
        school: str,
        coach_name: str,
        coach_email: str,
        num_emails: int = 2
    ) -> List[PregeneratedEmail]:
        """
        Pre-generate multiple emails for a school.
        Typically: 1 intro + 1 followup, or 2 intros for variety.
        """
        logger.info(f"Pre-generating {num_emails} emails for {school}...")

        # Get research first (reused for all emails)
        research = self.get_research(school, coach_name)

        # Check if research has real data
        has_real_research = bool(
            research.recent_record or
            research.notable_facts or
            research.conference or
            research.head_coach or
            research.coach_quotes
        )
        research_failed = not has_real_research

        if research_failed:
            logger.warning(f"Research failed for {school} - marking for Railway template fallback")

        generated = []
        email_types = ['intro', 'followup_1'] if num_emails >= 2 else ['intro']

        for email_type in email_types[:num_emails]:
            content = self.generate_personalized_content(
                school=school,
                coach_name=coach_name,
                coach_email=coach_email,
                email_type=email_type,
                research=research
            )

            email = PregeneratedEmail(
                school=school,
                coach_name=coach_name,
                coach_email=coach_email,
                email_type=email_type,
                personalized_content=content,
                research_used=research.to_dict(),
                generated_at=datetime.now().isoformat(),
                used=False,
                research_failed=research_failed
            )
            generated.append(email)

            # Small delay between generations
            time.sleep(1)

        # Store in cache
        cache_key = school.lower().strip()
        if cache_key not in self.pregenerated:
            self.pregenerated[cache_key] = []
        self.pregenerated[cache_key].extend(generated)
        self._save()

        logger.info(f"Generated {len(generated)} emails for {school}")
        return generated

    def get_pregenerated(self, school: str, email_type: str = 'intro') -> Optional[PregeneratedEmail]:
        """Get an unused pregenerated email for a school."""
        cache_key = school.lower().strip()

        if cache_key not in self.pregenerated:
            return None

        for email in self.pregenerated[cache_key]:
            if email.email_type == email_type and not email.used:
                return email

        return None

    def mark_used(self, school: str, email_type: str):
        """Mark a pregenerated email as used."""
        cache_key = school.lower().strip()

        if cache_key in self.pregenerated:
            for email in self.pregenerated[cache_key]:
                if email.email_type == email_type and not email.used:
                    email.used = True
                    self._save()
                    return

    def get_stats(self) -> Dict:
        """Get generator statistics."""
        total_pregenerated = sum(len(emails) for emails in self.pregenerated.values())
        unused = sum(
            1 for emails in self.pregenerated.values()
            for e in emails if not e.used
        )
        return {
            'schools_with_emails': len(self.pregenerated),
            'total_pregenerated': total_pregenerated,
            'unused_emails': unused,
            'research_cached': len(self.research_cache),
        }


# Singleton
_generator = None

def get_email_generator() -> EmailGenerator:
    global _generator
    if _generator is None:
        _generator = EmailGenerator()
    return _generator


# ============================================================================
# BATCH GENERATION CLI
# ============================================================================

def batch_generate_from_sheet(sheet_data: List[List[str]], headers: List[str], limit: int = 50):
    """
    Batch generate emails from a Google Sheet.
    Run this locally to pre-generate emails for Railway.
    """
    generator = get_email_generator()

    # Find columns
    def find_col(keywords):
        for i, h in enumerate(headers):
            h_lower = h.lower().strip()
            for kw in keywords:
                if kw in h_lower:
                    return i
        return -1

    school_col = find_col(['school'])
    rc_name_col = find_col(['recruiting coordinator', 'rc name'])
    rc_email_col = find_col(['rc email'])
    ol_name_col = find_col(['oline coach', 'ol coach', 'position coach'])
    ol_email_col = find_col(['oc email', 'ol email'])

    generated_count = 0

    for row in sheet_data[:limit]:
        try:
            school = row[school_col].strip() if school_col >= 0 and school_col < len(row) else ''
            if not school:
                continue

            # Generate for RC
            if rc_email_col >= 0 and rc_email_col < len(row):
                rc_email = row[rc_email_col].strip()
                rc_name = row[rc_name_col].strip() if rc_name_col >= 0 and rc_name_col < len(row) else 'Coach'

                if rc_email and '@' in rc_email:
                    # Check if we already have emails for this school
                    existing = generator.get_pregenerated(school, 'intro')
                    if not existing:
                        generator.pregenerate_for_school(school, rc_name, rc_email, num_emails=2)
                        generated_count += 1
                        print(f"Generated for {school} (RC: {rc_name})")
                        time.sleep(2)  # Rate limit

            # Generate for OL coach if different
            if ol_email_col >= 0 and ol_email_col < len(row):
                ol_email = row[ol_email_col].strip()
                ol_name = row[ol_name_col].strip() if ol_name_col >= 0 and ol_name_col < len(row) else 'Coach'

                if ol_email and '@' in ol_email and ol_email != row[rc_email_col].strip() if rc_email_col >= 0 else True:
                    existing = generator.get_pregenerated(school, 'intro')
                    if not existing:
                        generator.pregenerate_for_school(school, ol_name, ol_email, num_emails=2)
                        generated_count += 1
                        print(f"Generated for {school} (OL: {ol_name})")
                        time.sleep(2)

        except Exception as e:
            logger.error(f"Error processing row: {e}")
            continue

    print(f"\nBatch generation complete: {generated_count} schools processed")
    return generator.get_stats()


# ============================================================================
# CLI TESTING
# ============================================================================

if __name__ == '__main__':
    import sys

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    if len(sys.argv) > 1:
        school = ' '.join(sys.argv[1:])
    else:
        school = "Florida State"

    print(f"\n{'='*60}")
    print(f"Generating emails for: {school}")
    print('='*60)

    generator = get_email_generator()

    # Generate 2 emails
    emails = generator.pregenerate_for_school(
        school=school,
        coach_name="Smith",
        coach_email="coach@example.edu",
        num_emails=2
    )

    for email in emails:
        print(f"\n--- {email.email_type.upper()} ---")
        print(email.personalized_content)

    print(f"\n{'='*60}")
    print(f"Stats: {generator.get_stats()}")
