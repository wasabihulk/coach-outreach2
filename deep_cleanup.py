#!/usr/bin/env python3
"""
Deep cleanup - finds and fixes/deletes ALL quality issues.
"""
import json
import re
from pathlib import Path

DATA_DIR = Path.home() / '.coach_outreach'
PREGENERATED_FILE = DATA_DIR / 'pregenerated_emails.json'

NEW_SIGNATURE = """
Keelan Underwood
2026 OL | The Benjamin School
6'3" 295 lbs | 3.0 GPA
Film: https://www.hudl.com/profile/21702795/Keelan-Underwood
910-747-1140"""

def check_email_quality(content, school, coach_name, email_type):
    """
    Check email for quality issues. Returns (is_ok, reason) tuple.
    """
    if not content or len(content.strip()) < 50:
        return False, "Content too short or empty"
    
    # Check for missing/broken signature
    if "Keelan Underwood" not in content:
        return False, "Missing signature"
    
    # Check for broken signature (spaces in URL)
    if "www. hudl. com" in content or "hudl. com" in content:
        return False, "Broken Hudl URL"
    
    # Check for missing coach name in greeting
    if re.search(r'Good (Morning|Afternoon) Coach\s*,', content):
        return False, "Missing coach name in greeting"
    
    # Check for double commas
    if ",," in content:
        return False, "Double comma"
    
    # Check for unfilled placeholders
    if re.search(r'\[[^\]]+\]', content):
        return False, f"Unfilled placeholder: {re.search(r'\[[^\]]+\]', content).group()}"
    
    # Check for duplicate phrases
    dup_patterns = [
        r"excited to see what you're building.*excited to see what you're building",
        r"I'm still very interested.*I'm still very interested",
    ]
    for pattern in dup_patterns:
        if re.search(pattern, content, re.IGNORECASE):
            return False, "Duplicate phrase"
    
    # Check for weird quote formatting
    if '," "' in content or '" "' in content:
        return False, "Weird quote formatting"
    
    # Check for AI instruction leakage
    ai_leaks = [
        "Note:", "Keep in mind", "Consider", "might not be as effective",
        "reach out at other times", "recruiting events"
    ]
    for leak in ai_leaks:
        if leak in content and "I saw" not in content[:50]:  # Only if it's instruction text
            # Check if it's in a suspicious context
            if "Note:" in content or "Keep in mind" in content:
                return False, f"AI instruction leak: {leak}"
    
    # Check for cut-off sentences (ends abruptly)
    # Look for sentences ending with incomplete thoughts
    incomplete_patterns = [
        r'St\.\s*$',  # Ends with "St."
        r'W\. Va\.\s*$',  # Ends with "W. Va."
        r'especially\s+\w+\s*$',  # Ends with "especially [word]"
        r', I\'m still very interested in \w+\.\s*$',  # Incomplete interest statement
    ]
    for pattern in incomplete_patterns:
        # Check last 100 chars before signature
        pre_sig = content.split("Keelan Underwood")[0] if "Keelan Underwood" in content else content
        if re.search(pattern, pre_sig[-100:] if len(pre_sig) > 100 else pre_sig):
            return False, "Cut-off/incomplete sentence"
    
    # Check for mentioning bad records (losing or .500)
    bad_record_patterns = [
        r'congrats on (?:the |going |a )?(\d+)-(\d+)',
        r'(\d+)-(\d+) (?:season|record|year).*(?:tough|rough|difficult)',
        r'(?:loss|lost|losing) to',
        r'record is.*concerning',
        r'could use some improvement',
        r'tough season',
    ]
    
    for pattern in bad_record_patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            # For record patterns, check if it's a losing record
            if match.lastindex and match.lastindex >= 2:
                try:
                    wins = int(match.group(1))
                    losses = int(match.group(2))
                    if wins <= losses:
                        return False, f"Mentions bad record: {wins}-{losses}"
                except:
                    pass
            elif "loss" in pattern.lower() or "concerning" in pattern.lower() or "improvement" in pattern.lower():
                return False, f"Negative language: {match.group(0)[:50]}"
    
    # Check for mentioning losses specifically
    if re.search(r"(?:lost|loss) to .+ \d+-\d+", content, re.IGNORECASE):
        return False, "Mentions a loss"
    
    # Check for home record that's losing
    home_record = re.search(r'(\d+)-(\d+) home record', content, re.IGNORECASE)
    if home_record:
        wins, losses = int(home_record.group(1)), int(home_record.group(2))
        if wins <= losses:
            return False, f"Mentions bad home record: {wins}-{losses}"
    
    return True, "OK"

def main():
    print("=" * 60)
    print("DEEP EMAIL QUALITY CHECK")
    print("=" * 60)
    
    with open(PREGENERATED_FILE, 'r') as f:
        emails = json.load(f)
    
    issues = []
    to_delete = []
    
    for school, email_list in emails.items():
        if not isinstance(email_list, list):
            continue
        
        for i, email in enumerate(email_list):
            content = email.get('personalized_content', '')
            coach = email.get('coach_name', '')
            etype = email.get('email_type', '')
            
            is_ok, reason = check_email_quality(content, school, coach, etype)
            
            if not is_ok:
                issues.append({
                    'school': school,
                    'coach': coach,
                    'type': etype,
                    'reason': reason,
                    'preview': content[:100] if content else 'EMPTY'
                })
                to_delete.append((school, i))
    
    print(f"\nFound {len(issues)} emails with issues:\n")
    
    for issue in issues:
        print(f"ISSUE: {issue['school']} ({issue['type']})")
        print(f"  Reason: {issue['reason']}")
        print(f"  Preview: {issue['preview'][:80]}...")
        print()
    
    # Delete bad emails
    print("=" * 60)
    print("DELETING BAD EMAILS...")
    print("=" * 60)
    
    # Group deletions by school
    deletions_by_school = {}
    for school, idx in to_delete:
        if school not in deletions_by_school:
            deletions_by_school[school] = []
        deletions_by_school[school].append(idx)
    
    # Delete in reverse order to maintain indices
    for school, indices in deletions_by_school.items():
        for idx in sorted(indices, reverse=True):
            if school in emails and idx < len(emails[school]):
                del emails[school][idx]
        # Remove school entirely if no emails left
        if school in emails and len(emails[school]) == 0:
            del emails[school]
    
    # Save cleaned emails
    with open(PREGENERATED_FILE, 'w') as f:
        json.dump(emails, f, indent=2)
    
    remaining = sum(len(v) for v in emails.values() if isinstance(v, list))
    print(f"\nDeleted {len(issues)} bad emails")
    print(f"Remaining emails: {remaining}")
    print(f"Remaining schools: {len(emails)}")

if __name__ == '__main__':
    main()
