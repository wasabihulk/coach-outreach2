"""
AI-Powered Email Personalization Hooks
============================================================================
Generates unique, personalized reasons for being interested in each school
by researching the program and creating compelling hooks that make emails
stand out from generic mass outreach.

Features:
- School research via web search and school website scraping
- AI-generated personalized hooks using Ollama
- Hook tracking to avoid repetition
- Multiple hook categories (coaching, facilities, culture, academics, location)
- Fallback hooks when AI unavailable

Author: Coach Outreach System
Version: 1.0.0
============================================================================
"""

import json
import os
import random
import hashlib
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Configuration
CONFIG = {
    'OLLAMA_URL': os.environ.get('OLLAMA_URL', 'http://localhost:11434/api/generate'),
    'OLLAMA_MODEL': os.environ.get('OLLAMA_MODEL', 'llama3.2:3b'),
    'OLLAMA_TIMEOUT': 60,
    'GOOGLE_API_KEY': os.environ.get('GOOGLE_API_KEY', ''),
    'GOOGLE_CSE_ID': os.environ.get('GOOGLE_CSE_ID', ''),
}

# Data directory
DATA_DIR = Path.home() / '.coach_outreach'
HOOKS_DB_FILE = DATA_DIR / 'hooks_database.json'
SCHOOL_RESEARCH_CACHE = DATA_DIR / 'school_research_cache.json'


# ============================================================================
# HOOK CATEGORIES AND FALLBACKS
# ============================================================================

HOOK_CATEGORIES = [
    'coaching_style',      # Coach's reputation, philosophy, player development
    'program_culture',     # Team culture, traditions, work ethic
    'facilities',          # Training facilities, stadium, resources
    'academics',           # Academic programs, graduation rates, support
    'location',            # Geographic appeal, climate, proximity
    'recent_success',      # Recent wins, bowl games, player development to NFL
    'offensive_line',      # OL-specific: blocking schemes, NFL linemen produced
]

# Fallback hooks when AI/research unavailable - categorized by what makes sense for an OL
FALLBACK_HOOKS = {
    'generic': [
        "The culture of hard work and player development at {school} stands out to me",
        "I've been impressed by the offensive line play I've seen from {school}",
        "The coaching staff at {school} has a reputation for developing players",
        "I believe {school}'s program would be a great fit for my work ethic",
        "{school}'s commitment to both athletics and academics aligns with my goals",
        "The tradition and pride at {school} is something I want to be part of",
    ],
    'fbs': [
        "The high level of competition in the {conference} is where I want to prove myself",
        "{school}'s history of sending offensive linemen to the NFL is impressive",
        "Competing at the FBS level at {school} would push me to reach my potential",
    ],
    'fcs': [
        "{school}'s competitive FCS program and strong academics appeal to me",
        "The opportunity to contribute early at {school} while getting a quality education is exciting",
        "I appreciate that {school} competes at a high level while prioritizing player development",
    ],
    'd2': [
        "The balance of competitive football and academics at {school} fits what I'm looking for",
        "{school}'s D2 program offers the competition level where I can make an immediate impact",
        "The tight-knit community feel at {school} appeals to me",
    ],
    'd3': [
        "{school}'s emphasis on the true student-athlete experience resonates with me",
        "The academic opportunities at {school} combined with competitive football is ideal",
        "I value that {school} develops well-rounded players both on and off the field",
    ],
}


# ============================================================================
# SCHOOL RESEARCH
# ============================================================================

@dataclass
class SchoolResearch:
    """Research data about a school's football program."""
    school_name: str
    division: str = ""
    conference: str = ""
    head_coach: str = ""
    ol_coach: str = ""
    recent_record: str = ""
    notable_facts: List[str] = field(default_factory=list)
    ol_facts: List[str] = field(default_factory=list)  # OL-specific facts
    academic_strengths: List[str] = field(default_factory=list)
    location_info: str = ""
    facilities_info: str = ""
    culture_notes: str = ""
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            'school_name': self.school_name,
            'division': self.division,
            'conference': self.conference,
            'head_coach': self.head_coach,
            'ol_coach': self.ol_coach,
            'recent_record': self.recent_record,
            'notable_facts': self.notable_facts,
            'ol_facts': self.ol_facts,
            'academic_strengths': self.academic_strengths,
            'location_info': self.location_info,
            'facilities_info': self.facilities_info,
            'culture_notes': self.culture_notes,
            'last_updated': self.last_updated,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'SchoolResearch':
        return cls(**data)


def load_research_cache() -> Dict[str, SchoolResearch]:
    """Load cached school research."""
    if SCHOOL_RESEARCH_CACHE.exists():
        try:
            with open(SCHOOL_RESEARCH_CACHE, 'r') as f:
                data = json.load(f)
                return {k: SchoolResearch.from_dict(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"Error loading research cache: {e}")
    return {}


def save_research_cache(cache: Dict[str, SchoolResearch]):
    """Save school research cache."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(SCHOOL_RESEARCH_CACHE, 'w') as f:
            json.dump({k: v.to_dict() for k, v in cache.items()}, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving research cache: {e}")


def google_search(query: str, num_results: int = 3) -> List[Dict]:
    """Search Google for information about a school."""
    if not CONFIG['GOOGLE_API_KEY'] or not CONFIG['GOOGLE_CSE_ID']:
        logger.debug("Google API not configured, skipping web search")
        return []

    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            'key': CONFIG['GOOGLE_API_KEY'],
            'cx': CONFIG['GOOGLE_CSE_ID'],
            'q': query,
            'num': num_results,
        }
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get('items', []):
            results.append({
                'title': item.get('title', ''),
                'snippet': item.get('snippet', ''),
                'link': item.get('link', ''),
            })
        return results
    except Exception as e:
        logger.warning(f"Google search error: {e}")
        return []


def query_ollama(prompt: str, timeout: int = None) -> Optional[str]:
    """Query Ollama for text generation."""
    try:
        payload = {
            'model': CONFIG['OLLAMA_MODEL'],
            'prompt': prompt,
            'stream': False,
            'options': {
                'temperature': 0.7,
                'num_predict': 500,
            }
        }

        response = requests.post(
            CONFIG['OLLAMA_URL'],
            json=payload,
            timeout=timeout or CONFIG['OLLAMA_TIMEOUT']
        )
        response.raise_for_status()

        result = response.json()
        return result.get('response', '').strip()
    except requests.exceptions.Timeout:
        logger.warning("Ollama request timed out")
        return None
    except requests.exceptions.ConnectionError:
        logger.debug("Ollama not available")
        return None
    except Exception as e:
        logger.warning(f"Ollama error: {e}")
        return None


def research_school(school_name: str, division: str = "", conference: str = "",
                    force_refresh: bool = False) -> SchoolResearch:
    """
    Research a school's football program.
    Uses cached data if available and not expired.
    """
    cache = load_research_cache()
    cache_key = school_name.lower().strip()

    # Check cache (valid for 30 days)
    if not force_refresh and cache_key in cache:
        cached = cache[cache_key]
        try:
            cached_date = datetime.fromisoformat(cached.last_updated)
            if (datetime.now() - cached_date).days < 30:
                logger.debug(f"Using cached research for {school_name}")
                return cached
        except:
            pass

    research = SchoolResearch(
        school_name=school_name,
        division=division,
        conference=conference,
    )

    # Try Google search for recent info
    search_queries = [
        f"{school_name} football 2024 2025",
        f"{school_name} offensive line football",
        f"{school_name} football program culture",
    ]

    all_snippets = []
    for query in search_queries:
        results = google_search(query, num_results=2)
        for r in results:
            if r.get('snippet'):
                all_snippets.append(r['snippet'])

    # Use Ollama to extract structured info from search results
    if all_snippets:
        combined_text = " ".join(all_snippets)[:3000]

        prompt = f"""Analyze this information about {school_name} football program and extract key facts.

SEARCH RESULTS:
{combined_text}

Extract the following if mentioned (leave empty if not found):
1. Head coach name
2. Recent record or achievements
3. Notable facts about the program
4. Any offensive line specific information
5. Anything about program culture or facilities

Respond in this exact format:
HEAD_COACH: [name or empty]
RECORD: [recent record or empty]
FACTS: [comma-separated notable facts]
OL_INFO: [offensive line specific info or empty]
CULTURE: [culture/facilities notes or empty]
"""

        ai_response = query_ollama(prompt)
        if ai_response:
            # Parse the response
            lines = ai_response.split('\n')
            for line in lines:
                if line.startswith('HEAD_COACH:'):
                    research.head_coach = line.replace('HEAD_COACH:', '').strip()
                elif line.startswith('RECORD:'):
                    research.recent_record = line.replace('RECORD:', '').strip()
                elif line.startswith('FACTS:'):
                    facts = line.replace('FACTS:', '').strip()
                    if facts:
                        research.notable_facts = [f.strip() for f in facts.split(',') if f.strip()]
                elif line.startswith('OL_INFO:'):
                    ol_info = line.replace('OL_INFO:', '').strip()
                    if ol_info:
                        research.ol_facts = [ol_info]
                elif line.startswith('CULTURE:'):
                    research.culture_notes = line.replace('CULTURE:', '').strip()

    # Save to cache
    cache[cache_key] = research
    save_research_cache(cache)

    return research


# ============================================================================
# HOOK GENERATION
# ============================================================================

@dataclass
class HookRecord:
    """Record of a hook used for a school."""
    hook_text: str
    school: str
    category: str
    generated_at: str
    email_type: str  # intro, followup_1, followup_2

    def to_dict(self) -> dict:
        return {
            'hook_text': self.hook_text,
            'school': self.school,
            'category': self.category,
            'generated_at': self.generated_at,
            'email_type': self.email_type,
        }


class HookDatabase:
    """Tracks used hooks to avoid repetition."""

    def __init__(self):
        self.hooks: Dict[str, List[HookRecord]] = {}  # school -> list of hooks used
        self._load()

    def _load(self):
        """Load hooks database from disk."""
        if HOOKS_DB_FILE.exists():
            try:
                with open(HOOKS_DB_FILE, 'r') as f:
                    data = json.load(f)
                    for school, hooks in data.items():
                        self.hooks[school] = [
                            HookRecord(
                                hook_text=h['hook_text'],
                                school=h['school'],
                                category=h['category'],
                                generated_at=h['generated_at'],
                                email_type=h['email_type'],
                            ) for h in hooks
                        ]
            except Exception as e:
                logger.warning(f"Error loading hooks database: {e}")

    def _save(self):
        """Save hooks database to disk."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                school: [h.to_dict() for h in hooks]
                for school, hooks in self.hooks.items()
            }
            with open(HOOKS_DB_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving hooks database: {e}")

    def get_used_hooks(self, school: str) -> List[str]:
        """Get list of hook texts already used for a school."""
        school_key = school.lower().strip()
        return [h.hook_text for h in self.hooks.get(school_key, [])]

    def get_used_categories(self, school: str) -> List[str]:
        """Get categories already used for a school."""
        school_key = school.lower().strip()
        return [h.category for h in self.hooks.get(school_key, [])]

    def record_hook(self, school: str, hook_text: str, category: str, email_type: str):
        """Record a hook as used for a school."""
        school_key = school.lower().strip()
        if school_key not in self.hooks:
            self.hooks[school_key] = []

        self.hooks[school_key].append(HookRecord(
            hook_text=hook_text,
            school=school_key,
            category=category,
            generated_at=datetime.now().isoformat(),
            email_type=email_type,
        ))
        self._save()

    def get_stats(self) -> Dict:
        """Get database statistics."""
        total_hooks = sum(len(hooks) for hooks in self.hooks.values())
        schools_with_hooks = len(self.hooks)
        return {
            'total_hooks_generated': total_hooks,
            'schools_with_hooks': schools_with_hooks,
            'avg_hooks_per_school': round(total_hooks / max(schools_with_hooks, 1), 1),
        }


# Singleton instance
_hook_db = None

def get_hook_database() -> HookDatabase:
    """Get singleton hook database instance."""
    global _hook_db
    if _hook_db is None:
        _hook_db = HookDatabase()
    return _hook_db


def generate_ai_hook(school: str, research: SchoolResearch,
                     used_hooks: List[str], used_categories: List[str],
                     email_type: str = 'intro') -> Tuple[str, str]:
    """
    Generate a personalized hook using AI.
    Returns (hook_text, category).
    """
    # Build context from research
    context_parts = []
    if research.division:
        context_parts.append(f"Division: {research.division}")
    if research.conference:
        context_parts.append(f"Conference: {research.conference}")
    if research.head_coach:
        context_parts.append(f"Head Coach: {research.head_coach}")
    if research.recent_record:
        context_parts.append(f"Recent record: {research.recent_record}")
    if research.notable_facts:
        context_parts.append(f"Notable facts: {', '.join(research.notable_facts[:3])}")
    if research.ol_facts:
        context_parts.append(f"OL info: {', '.join(research.ol_facts)}")
    if research.culture_notes:
        context_parts.append(f"Culture: {research.culture_notes}")

    context = "\n".join(context_parts) if context_parts else "No specific research available"

    # Determine which categories to avoid
    available_categories = [c for c in HOOK_CATEGORIES if c not in used_categories]
    if not available_categories:
        available_categories = HOOK_CATEGORIES  # Reset if all used

    prompt = f"""You are helping a high school offensive lineman write personalized college recruiting emails.

Generate ONE short, genuine sentence expressing specific interest in {school}'s football program.

SCHOOL INFO:
{context}

REQUIREMENTS:
- Write from the perspective of a 2026 OL prospect
- Be specific to {school} - mention something unique about them
- Sound genuine, not generic or salesy
- Keep it to 1-2 sentences max
- Focus on ONE of these aspects: {', '.join(available_categories[:3])}
- Do NOT repeat these previously used hooks: {used_hooks[:2] if used_hooks else 'none'}

Write ONLY the hook sentence, nothing else:"""

    ai_response = query_ollama(prompt, timeout=30)

    if ai_response:
        # Clean up the response
        hook = ai_response.strip().strip('"').strip("'")
        # Remove any AI prefixes/suffixes
        for prefix in ["Here's", "Here is", "Hook:", "Response:", "Sure,", "Certainly,"]:
            if hook.lower().startswith(prefix.lower()):
                hook = hook[len(prefix):].strip()

        # Ensure it ends properly
        if hook and not hook[-1] in '.!':
            hook += '.'

        # Determine category (best guess from content)
        category = 'program_culture'  # default
        hook_lower = hook.lower()
        if any(word in hook_lower for word in ['coach', 'staff', 'develop']):
            category = 'coaching_style'
        elif any(word in hook_lower for word in ['facility', 'stadium', 'training']):
            category = 'facilities'
        elif any(word in hook_lower for word in ['academic', 'education', 'degree', 'graduate']):
            category = 'academics'
        elif any(word in hook_lower for word in ['line', 'blocking', 'nfl', 'lineman', 'linemen']):
            category = 'offensive_line'
        elif any(word in hook_lower for word in ['win', 'championship', 'bowl', 'playoff', 'record']):
            category = 'recent_success'
        elif any(word in hook_lower for word in ['location', 'campus', 'city', 'state', 'weather']):
            category = 'location'

        return hook, category

    return None, None


def get_fallback_hook(school: str, division: str = "",
                      used_hooks: List[str] = None, conference: str = "") -> Tuple[str, str]:
    """Get a fallback hook when AI is unavailable."""
    used_hooks = used_hooks or []

    # Determine division category
    division_lower = division.lower() if division else ""
    if 'fbs' in division_lower or division_lower in ['d1', 'division 1', 'power 5', 'group of 5']:
        division_hooks = FALLBACK_HOOKS['fbs']
    elif 'fcs' in division_lower:
        division_hooks = FALLBACK_HOOKS['fcs']
    elif 'd2' in division_lower or 'division 2' in division_lower:
        division_hooks = FALLBACK_HOOKS['d2']
    elif 'd3' in division_lower or 'division 3' in division_lower:
        division_hooks = FALLBACK_HOOKS['d3']
    else:
        division_hooks = []

    # Combine with generic hooks
    all_hooks = FALLBACK_HOOKS['generic'] + division_hooks

    # Filter out used hooks (by checking similarity)
    available = []
    for hook in all_hooks:
        formatted = hook.format(school=school, conference=conference or "your conference")
        # Check if similar hook was used (simple substring check)
        is_similar = any(
            formatted[:30].lower() in used.lower() or used[:30].lower() in formatted.lower()
            for used in used_hooks
        )
        if not is_similar:
            available.append(formatted)

    if not available:
        available = [hook.format(school=school, conference=conference or "your conference")
                    for hook in FALLBACK_HOOKS['generic']]

    hook = random.choice(available)
    return hook, 'generic'


# ============================================================================
# MAIN API
# ============================================================================

def generate_personalized_hook(school: str, division: str = "", conference: str = "",
                               email_type: str = 'intro', use_ai: bool = True) -> str:
    """
    Generate a personalized hook for a school.

    Args:
        school: School name
        division: Division (FBS, FCS, D2, D3)
        conference: Conference name
        email_type: Type of email (intro, followup_1, followup_2)
        use_ai: Whether to try AI generation (falls back to templates if unavailable)

    Returns:
        Personalized hook text
    """
    db = get_hook_database()
    used_hooks = db.get_used_hooks(school)
    used_categories = db.get_used_categories(school)

    hook = None
    category = 'generic'

    if use_ai:
        # Research the school
        try:
            research = research_school(school, division, conference)

            # Try AI generation
            hook, category = generate_ai_hook(
                school, research, used_hooks, used_categories, email_type
            )
        except Exception as e:
            logger.warning(f"AI hook generation failed for {school}: {e}")

    # Fallback to template hooks
    if not hook:
        hook, category = get_fallback_hook(school, division, used_hooks, conference)

    # Record the hook
    db.record_hook(school, hook, category, email_type)

    return hook


def get_hook_for_template(school: str, division: str = "", conference: str = "",
                          email_type: str = 'intro') -> Dict[str, str]:
    """
    Get hook data for template substitution.

    Returns dict with:
        - personalized_hook: The hook text
        - hook_category: Category of the hook
    """
    hook = generate_personalized_hook(school, division, conference, email_type)

    return {
        'personalized_hook': hook,
        'hook_category': 'personalized',
    }


# ============================================================================
# CLI TESTING
# ============================================================================

if __name__ == '__main__':
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1:
        school = ' '.join(sys.argv[1:])
    else:
        school = "Florida State"

    print(f"\nGenerating personalized hook for: {school}")
    print("-" * 50)

    # Generate a hook
    hook = generate_personalized_hook(school, division="FBS", conference="ACC")
    print(f"Hook: {hook}")

    # Show database stats
    db = get_hook_database()
    stats = db.get_stats()
    print(f"\nDatabase stats: {stats}")
