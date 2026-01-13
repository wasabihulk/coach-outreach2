#!/usr/bin/env python3
"""
Cleanup script for pregenerated emails.
Fixes quality issues and deletes bad emails.
"""
import json
import re
import sys
from pathlib import Path

DATA_DIR = Path.home() / '.coach_outreach'
PREGENERATED_FILE = DATA_DIR / 'pregenerated_emails.json'
BACKUP_FILE = DATA_DIR / 'pregenerated_emails_backup.json'

# New signature with Hudl link
NEW_SIGNATURE = """
Keelan Underwood
2026 OL | The Benjamin School
6'3" 295 lbs | 3.0 GPA
Film: https://www.hudl.com/profile/21702795/Keelan-Underwood
910-747-1140"""

OLD_SIGNATURE = """
Keelan Underwood
2026 OL | The Benjamin School
6'3" 295 lbs | 3.0 GPA
910-747-1140"""

def extract_last_name(full_name):
    """Extract last name from full name."""
    if not full_name or not full_name.strip():
        return ""
    name = full_name.strip()
    prefixes = ['Coach ', 'Dr. ', 'Mr. ', 'Mrs. ', 'Ms. ']
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):]
    suffixes = [' Jr.', ' Jr', ' Sr.', ' Sr', ' III', ' II', ' IV']
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    parts = name.strip().split()
    if len(parts) == 0:
        return ""
    elif len(parts) == 1:
        return parts[0]
    else:
        return parts[-1]

def fix_email_content(content, coach_name, school):
    """Fix quality issues in email content."""
    if not content:
        return None, "Empty content"
    
    # Check for missing coach name in greeting
    if "Good Morning Coach ," in content or "Good Morning Coach," in content.replace("Coach ,", "Coach,"):
        if re.search(r'Good Morning Coach\s*,', content):
            return None, "Missing coach name in greeting"
    
    # Extract last name
    last_name = extract_last_name(coach_name)
    if not last_name:
        return None, f"Invalid coach name: {coach_name}"
    
    # Fix full name to last name only in greetings
    # Pattern: "Good Morning Coach FirstName LastName," -> "Good Morning Coach LastName,"
    full_name_pattern = rf'Good Morning Coach {re.escape(coach_name)},'
    if re.search(full_name_pattern, content, re.IGNORECASE):
        content = re.sub(full_name_pattern, f'Good Morning Coach {last_name},', content, flags=re.IGNORECASE)
    
    # Also fix patterns like "Coach John Smith" -> "Coach Smith"
    parts = coach_name.split()
    if len(parts) >= 2:
        for i in range(len(parts) - 1):
            possible_full = ' '.join(parts[i:])
            if possible_full.lower() != last_name.lower():
                content = re.sub(rf'Coach {re.escape(possible_full)}([,\s])', rf'Coach {last_name}\1', content, flags=re.IGNORECASE)
    
    # Fix grammar: "offensive lineman" -> "offensive linemen" (when plural)
    content = re.sub(r'\bmore offensive lineman\b', 'more offensive linemen', content, flags=re.IGNORECASE)
    content = re.sub(r'\badd offensive lineman\b', 'add offensive linemen', content, flags=re.IGNORECASE)
    content = re.sub(r'\bneed offensive lineman\b', 'need offensive linemen', content, flags=re.IGNORECASE)
    content = re.sub(r'\bneed of offensive lineman\b', 'looking for offensive linemen', content, flags=re.IGNORECASE)
    content = re.sub(r'\bin need of more offensive lineman\b', 'looking to add offensive linemen', content, flags=re.IGNORECASE)
    content = re.sub(r'\bstill in need of more offensive lineman\b', 'looking to add offensive linemen', content, flags=re.IGNORECASE)
    content = re.sub(r"if you're still in need of more offensive lineman", "if you're looking to add offensive linemen", content, flags=re.IGNORECASE)
    
    # Update signature to include Hudl link
    if OLD_SIGNATURE.strip() in content and "hudl.com" not in content.lower():
        content = content.replace(OLD_SIGNATURE.strip(), NEW_SIGNATURE.strip())
    
    # Add signature if missing entirely
    if "Keelan Underwood" not in content:
        content = content.strip() + NEW_SIGNATURE
    elif "hudl.com" not in content.lower():
        # Has name but no Hudl link - add it
        content = re.sub(
            r'(6\'3" 295 lbs \| 3\.0 GPA)\n(910-747-1140)',
            r'\1\nFilm: https://www.hudl.com/profile/21702795/Keelan-Underwood\n\2',
            content
        )
    
    # Check for weird/invalid content
    weird_patterns = [
        r'\d{3,}\s*points',  # "146 points" type nonsense
        r'averaging \d{3,}',  # "averaging 306" nonsense
    ]
    for pattern in weird_patterns:
        if re.search(pattern, content, re.IGNORECASE):
            # Remove the weird sentence
            sentences = content.split('.')
            content = '. '.join([s for s in sentences if not re.search(pattern, s, re.IGNORECASE)])
            if not content.endswith('.') and not content.endswith('!'):
                content += '!'
    
    return content, None

def main():
    print("=" * 60)
    print("EMAIL CLEANUP SCRIPT")
    print("=" * 60)
    
    if not PREGENERATED_FILE.exists():
        print("No pregenerated emails file found!")
        return
    
    # Load emails
    with open(PREGENERATED_FILE, 'r') as f:
        emails = json.load(f)
    
    # Backup first
    with open(BACKUP_FILE, 'w') as f:
        json.dump(emails, f, indent=2)
    print(f"Backed up to {BACKUP_FILE}")
    
    total = 0
    fixed = 0
    deleted = 0
    schools_deleted = []
    
    cleaned_emails = {}
    
    for school, email_list in emails.items():
        if not isinstance(email_list, list):
            continue
            
        cleaned_list = []
        for email in email_list:
            total += 1
            content = email.get('personalized_content', '')
            coach_name = email.get('coach_name', '')
            
            fixed_content, error = fix_email_content(content, coach_name, school)
            
            if error:
                print(f"  DELETED: {school} - {error}")
                deleted += 1
                if school not in schools_deleted:
                    schools_deleted.append(school)
            else:
                if fixed_content != content:
                    fixed += 1
                    print(f"  FIXED: {school}")
                email['personalized_content'] = fixed_content
                cleaned_list.append(email)
        
        if cleaned_list:
            cleaned_emails[school] = cleaned_list
    
    # Save cleaned emails
    with open(PREGENERATED_FILE, 'w') as f:
        json.dump(cleaned_emails, f, indent=2)
    
    print()
    print("=" * 60)
    print("CLEANUP COMPLETE")
    print("=" * 60)
    print(f"Total emails processed: {total}")
    print(f"Emails fixed: {fixed}")
    print(f"Emails deleted: {deleted}")
    print(f"Schools removed entirely: {len([s for s in schools_deleted if s not in cleaned_emails])}")
    print(f"Remaining schools: {len(cleaned_emails)}")
    print()
    print("Schools that had emails deleted:")
    for s in schools_deleted[:20]:
        print(f"  - {s}")
    if len(schools_deleted) > 20:
        print(f"  ... and {len(schools_deleted) - 20} more")

if __name__ == '__main__':
    main()
