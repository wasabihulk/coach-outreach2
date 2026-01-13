#!/usr/bin/env python3
"""
Batch Email Generator with Daily API Limits
============================================
Generates personalized emails for coaches with school-specific research.
Automatically stops at daily API limit (100 queries = ~20 schools/day).

Usage:
    python3 generate_emails.py              # Generate for schools (respects daily limit)
    python3 generate_emails.py --stats      # Show current stats
    python3 generate_emails.py --clear-bad  # Clear schools without research data
    python3 generate_emails.py --force      # Regenerate even if cached (for schools without research)

The script can be run daily via cron - it will automatically:
- Skip schools that already have good emails with research
- Stop when daily API limit is reached
- Resume the next day where it left off

Requirements:
    - Ollama running locally (ollama serve)
    - Google Sheets credentials configured
"""

import argparse
import sys
import os
import time
import json
import logging
from pathlib import Path
from datetime import date

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from enterprise.email_generator import (
    get_email_generator, get_email_memory,
    CONFIG, APILimitReached,
    get_api_usage_today, get_remaining_api_calls, get_remaining_schools_today,
    DAILY_API_LIMIT, SEARCHES_PER_SCHOOL,
    PREGENERATED_EMAILS_FILE, SCHOOL_RESEARCH_FILE, DATA_DIR
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def check_ollama():
    """Check if Ollama is running."""
    import requests
    try:
        response = requests.get(
            CONFIG['OLLAMA_URL'].replace('/api/generate', '/api/tags'),
            timeout=5
        )
        if response.status_code == 200:
            models = response.json().get('models', [])
            model_names = [m['name'] for m in models]
            print(f"Ollama running: {', '.join(model_names)}")
            return True
    except:
        pass
    print("ERROR: Ollama is not running! Start with: ollama serve")
    return False


def show_api_status():
    """Show current API usage status."""
    used = get_api_usage_today()
    remaining = get_remaining_api_calls()
    schools_possible = get_remaining_schools_today()

    print(f"\n📊 API Status (resets midnight Pacific):")
    print(f"   Used today: {used}/{DAILY_API_LIMIT}")
    print(f"   Remaining: {remaining} queries")
    print(f"   Schools possible: ~{schools_possible} (using {SEARCHES_PER_SCHOOL} queries each)")


def get_schools_needing_research():
    """Get list of schools that need research data."""
    # Load research cache
    schools_with_research = set()
    if SCHOOL_RESEARCH_FILE.exists():
        try:
            with open(SCHOOL_RESEARCH_FILE, 'r') as f:
                research_data = json.load(f)
                for school, data in research_data.items():
                    # Check if has meaningful research
                    has_research = any([
                        data.get('division'),
                        data.get('conference'),
                        data.get('recent_record'),
                        data.get('notable_facts'),
                        data.get('head_coach')
                    ])
                    if has_research:
                        schools_with_research.add(school.lower().strip())
        except Exception as e:
            logger.warning(f"Error loading research cache: {e}")

    # Load pregenerated emails
    schools_with_emails = set()
    if PREGENERATED_EMAILS_FILE.exists():
        try:
            with open(PREGENERATED_EMAILS_FILE, 'r') as f:
                email_data = json.load(f)
                for school in email_data.keys():
                    schools_with_emails.add(school.lower().strip())
        except Exception as e:
            logger.warning(f"Error loading email cache: {e}")

    # Schools that have emails but no research = need to regenerate
    need_research = schools_with_emails - schools_with_research
    return need_research, schools_with_research, schools_with_emails


def clear_schools_without_research():
    """Remove cached emails for schools that don't have research data."""
    need_research, _, _ = get_schools_needing_research()

    if not need_research:
        print("All cached schools have research data!")
        return 0

    print(f"\nClearing {len(need_research)} schools without research data:")

    # Load and modify pregenerated emails
    if PREGENERATED_EMAILS_FILE.exists():
        with open(PREGENERATED_EMAILS_FILE, 'r') as f:
            email_data = json.load(f)

        original_count = len(email_data)
        for school in list(email_data.keys()):
            if school.lower().strip() in need_research:
                del email_data[school]
                print(f"   Cleared: {school}")

        with open(PREGENERATED_EMAILS_FILE, 'w') as f:
            json.dump(email_data, f, indent=2)

        cleared = original_count - len(email_data)
        print(f"\nCleared {cleared} schools. They will be regenerated with research.")
        return cleared

    return 0


def generate_from_sheet(force_refresh: bool = False, limit: int = None):
    """Generate emails for coaches from Google Sheet."""
    try:
        from app import get_sheet
    except ImportError:
        print("ERROR: Could not import app module")
        return

    # Check API status first
    show_api_status()
    remaining_schools = get_remaining_schools_today()

    if remaining_schools == 0:
        print("\n⛔ Daily API limit reached. Run again tomorrow!")
        return

    sheet = get_sheet()
    if not sheet:
        print("ERROR: Could not connect to Google Sheet")
        return

    print("\n✓ Connected to Google Sheet")

    data = sheet.get_all_values()
    if len(data) < 2:
        print("ERROR: Sheet has no data")
        return

    headers = [h.lower() for h in data[0]]
    rows = data[1:]

    # Find columns
    def find_col(keywords):
        for i, h in enumerate(headers):
            for kw in keywords:
                if kw in h:
                    return i
        return -1

    school_col = find_col(['school'])
    rc_name_col = find_col(['recruiting coordinator', 'rc name'])
    rc_email_col = find_col(['rc email'])
    ol_name_col = find_col(['oline coach', 'ol coach', 'position coach'])
    ol_email_col = find_col(['oc email', 'ol email'])

    if school_col < 0:
        print("ERROR: Could not find 'school' column")
        return

    generator = get_email_generator()

    # Get schools needing research
    need_research, has_research, has_emails = get_schools_needing_research()

    # Collect schools to process
    schools_to_process = []
    for row in rows:
        try:
            school = row[school_col].strip() if school_col < len(row) else ''
            if not school:
                continue

            school_key = school.lower().strip()

            # Skip if already has emails (unless force refresh)
            if not force_refresh and school_key in has_emails:
                continue

            # Get coach info
            rc_email = row[rc_email_col].strip() if rc_email_col >= 0 and rc_email_col < len(row) else ''
            rc_name = row[rc_name_col].strip() if rc_name_col >= 0 and rc_name_col < len(row) else 'Coach'
            ol_email = row[ol_email_col].strip() if ol_email_col >= 0 and ol_email_col < len(row) else ''
            ol_name = row[ol_name_col].strip() if ol_name_col >= 0 and ol_name_col < len(row) else 'Coach'

            # Use whichever coach has an email
            if rc_email and '@' in rc_email:
                coach_email = rc_email
                coach_name = rc_name
            elif ol_email and '@' in ol_email:
                coach_email = ol_email
                coach_name = ol_name
            else:
                continue  # No valid email

            schools_to_process.append({
                'school': school,
                'coach_name': coach_name,
                'coach_email': coach_email,
                'needs_research': school_key in need_research or school_key not in has_research
            })

        except Exception as e:
            continue

    # Apply limit
    if limit:
        remaining_schools = min(remaining_schools, limit)

    total_to_process = len(schools_to_process)
    will_process = min(total_to_process, remaining_schools)

    print(f"\n📋 Schools Status:")
    print(f"   Total needing emails: {total_to_process}")
    print(f"   Can process today: {will_process}")
    if total_to_process > will_process:
        print(f"   Remaining for later: {total_to_process - will_process}")

    if will_process == 0:
        print("\n✓ Nothing to process!")
        return

    print(f"\n{'='*60}")
    print(f"   GENERATING EMAILS ({will_process} schools)")
    print(f"{'='*60}")

    generated_count = 0
    api_limit_hit = False

    for i, school_info in enumerate(schools_to_process[:will_process]):
        try:
            school = school_info['school']
            coach_name = school_info['coach_name']
            coach_email = school_info['coach_email']

            print(f"\n[{i+1}/{will_process}] {school}")
            print(f"   Coach: {coach_name}")

            emails = generator.pregenerate_for_school(
                school=school,
                coach_name=coach_name,
                coach_email=coach_email,
                num_emails=2
            )

            generated_count += 1

            # Show preview
            intro = next((e for e in emails if e.email_type == 'intro'), None)
            if intro:
                preview = intro.personalized_content[:80] + "..."
                print(f"   ✓ Generated: {preview}")

            # Show remaining API calls
            remaining = get_remaining_api_calls()
            print(f"   API: {remaining} calls remaining")

            time.sleep(2)

        except APILimitReached as e:
            print(f"\n⛔ API LIMIT REACHED: {e}")
            api_limit_hit = True
            break
        except Exception as e:
            print(f"   ERROR: {e}")
            continue

    print(f"\n{'='*60}")
    print(f"   DONE!")
    print(f"{'='*60}")
    print(f"   Generated: {generated_count} schools")

    if api_limit_hit:
        print(f"\n⚠️  API limit hit. Run again tomorrow to continue!")
        print(f"   Remaining schools: {total_to_process - generated_count}")

    # Show final stats
    stats = generator.get_stats()
    print(f"\n📊 Total Cache:")
    print(f"   Schools with emails: {stats['schools_with_emails']}")
    print(f"   Total pre-generated: {stats['total_pregenerated']}")
    print(f"   Research cached: {stats['research_cached']}")


def main():
    parser = argparse.ArgumentParser(
        description='Generate personalized emails with school research (respects daily API limits)'
    )
    parser.add_argument(
        '--limit', type=int,
        help='Maximum schools to process this run'
    )
    parser.add_argument(
        '--stats', action='store_true',
        help='Show statistics only'
    )
    parser.add_argument(
        '--clear-bad', action='store_true',
        help='Clear schools without research data so they get regenerated'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Force regenerate even for schools with existing emails'
    )
    parser.add_argument(
        '--api-status', action='store_true',
        help='Show API usage status only'
    )

    args = parser.parse_args()

    print("="*60)
    print("   BATCH EMAIL GENERATOR")
    print("="*60)

    if args.api_status:
        show_api_status()
        return

    if args.stats:
        show_api_status()

        generator = get_email_generator()
        memory = get_email_memory()

        print("\n📧 Email Generator Stats:")
        stats = generator.get_stats()
        for key, value in stats.items():
            print(f"   {key}: {value}")

        # Show schools needing research
        need_research, has_research, has_emails = get_schools_needing_research()
        print(f"\n📊 Research Status:")
        print(f"   Schools with research: {len(has_research)}")
        print(f"   Schools needing research: {len(need_research)}")

        if need_research:
            print(f"\n   Schools missing research:")
            for s in list(need_research)[:5]:
                print(f"      - {s}")
            if len(need_research) > 5:
                print(f"      ... and {len(need_research) - 5} more")
        return

    if args.clear_bad:
        clear_schools_without_research()
        return

    # Check Ollama before generating
    if not check_ollama():
        sys.exit(1)

    generate_from_sheet(force_refresh=args.force, limit=args.limit)


if __name__ == '__main__':
    main()
