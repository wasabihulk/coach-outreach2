#!/usr/bin/env python3
"""
Batch Email Generator
=====================
Run this script locally (on your Mac) to pre-generate personalized emails
for all coaches in your spreadsheet. The generated emails are cached and
will be used when sending from Railway.

Usage:
    python3 generate_emails.py              # Generate for all coaches
    python3 generate_emails.py --limit 10   # Generate for first 10 schools
    python3 generate_emails.py --school "Florida State"  # Generate for one school

Requirements:
    - Ollama running locally (ollama serve)
    - Google Sheets credentials configured
"""

import argparse
import sys
import os
import time
import logging

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from enterprise.email_generator import (
    get_email_generator, get_email_memory,
    CONFIG
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
            print(f"Ollama is running with models: {', '.join(model_names)}")
            return True
    except:
        pass
    print("ERROR: Ollama is not running!")
    print("Start it with: ollama serve")
    return False


def generate_for_single_school(school: str, coach_name: str = "Coach"):
    """Generate emails for a single school."""
    generator = get_email_generator()

    print(f"\nGenerating emails for {school}...")
    print("-" * 50)

    emails = generator.pregenerate_for_school(
        school=school,
        coach_name=coach_name,
        coach_email=f"coach@{school.lower().replace(' ', '')}.edu",
        num_emails=2
    )

    for email in emails:
        print(f"\n[{email.email_type.upper()}]")
        print(email.personalized_content)
        print()

    return len(emails)


def generate_from_sheet(limit: int = 50):
    """Generate emails for coaches from Google Sheet."""
    try:
        # Import sheet functions
        from app import get_sheet
    except ImportError:
        print("ERROR: Could not import app module")
        return

    sheet = get_sheet()
    if not sheet:
        print("ERROR: Could not connect to Google Sheet")
        print("Make sure your credentials are configured.")
        return

    print("Connected to Google Sheet")

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
    generated_count = 0
    skipped_count = 0

    print(f"\nProcessing up to {limit} schools...")
    print("=" * 60)

    for i, row in enumerate(rows[:limit]):
        try:
            school = row[school_col].strip() if school_col < len(row) else ''
            if not school:
                continue

            # Check if we already have emails for this school
            existing = generator.get_pregenerated(school, 'intro')
            if existing and not existing.used:
                skipped_count += 1
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

            print(f"\n[{i+1}/{limit}] {school} - {coach_name}")

            emails = generator.pregenerate_for_school(
                school=school,
                coach_name=coach_name,
                coach_email=coach_email,
                num_emails=2
            )

            generated_count += 1

            # Show preview of intro
            intro = next((e for e in emails if e.email_type == 'intro'), None)
            if intro:
                preview = intro.personalized_content[:80] + "..." if len(intro.personalized_content) > 80 else intro.personalized_content
                print(f"   Intro: {preview}")

            # Rate limit
            time.sleep(3)

        except Exception as e:
            print(f"   ERROR: {e}")
            continue

    print("\n" + "=" * 60)
    print(f"DONE!")
    print(f"  Generated: {generated_count} schools")
    print(f"  Skipped (already had): {skipped_count} schools")

    stats = generator.get_stats()
    print(f"\nTotal in cache:")
    print(f"  Schools with emails: {stats['schools_with_emails']}")
    print(f"  Total pre-generated: {stats['total_pregenerated']}")
    print(f"  Unused (ready to send): {stats['unused_emails']}")


def main():
    parser = argparse.ArgumentParser(
        description='Pre-generate personalized emails for coaches'
    )
    parser.add_argument(
        '--limit', type=int, default=50,
        help='Maximum number of schools to process (default: 50)'
    )
    parser.add_argument(
        '--school', type=str,
        help='Generate for a specific school only'
    )
    parser.add_argument(
        '--coach', type=str, default='Coach',
        help='Coach name (used with --school)'
    )
    parser.add_argument(
        '--stats', action='store_true',
        help='Show statistics only, do not generate'
    )

    args = parser.parse_args()

    print("=" * 60)
    print("   BATCH EMAIL GENERATOR")
    print("=" * 60)

    # Check Ollama first
    if not args.stats and not check_ollama():
        sys.exit(1)

    if args.stats:
        generator = get_email_generator()
        memory = get_email_memory()

        print("\nEmail Generator Stats:")
        print("-" * 30)
        stats = generator.get_stats()
        for key, value in stats.items():
            print(f"  {key}: {value}")

        print("\nEmail Memory Stats:")
        print("-" * 30)
        mem_stats = memory.get_stats()
        for key, value in mem_stats.items():
            print(f"  {key}: {value}")

    elif args.school:
        generate_for_single_school(args.school, args.coach)
    else:
        generate_from_sheet(args.limit)


if __name__ == '__main__':
    main()
