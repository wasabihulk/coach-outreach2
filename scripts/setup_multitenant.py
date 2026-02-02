#!/usr/bin/env python3
"""
Setup script for multi-tenant migration.
- Sets Keelan's password
- Migrates existing Gmail env vars into encrypted DB storage
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.supabase_client import SupabaseDB


def main():
    print("=== Multi-Tenant Setup ===\n")

    db = SupabaseDB()

    # 1. Set Keelan's password
    email = 'underwoodkeelan@gmail.com'
    athlete = db.client.table('athletes').select('id,name,email').eq('email', email).execute().data
    if not athlete:
        print(f"ERROR: No athlete found with email {email}")
        return
    athlete = athlete[0]
    aid = athlete['id']
    print(f"Found athlete: {athlete['name']} ({aid})")

    pw = input("Set password for Keelan (min 8 chars): ").strip()
    if len(pw) < 8:
        print("Password too short!")
        return
    db.set_athlete_password(aid, pw)
    print("Password set.\n")

    # 2. Migrate Gmail credentials from env vars
    enc_key = os.environ.get('CREDENTIALS_ENCRYPTION_KEY', '')
    if not enc_key:
        print("CREDENTIALS_ENCRYPTION_KEY not set in environment. Skipping Gmail migration.")
        print("Set it and re-run, or add credentials via the admin panel.")
        return

    gmail_cid = os.environ.get('GMAIL_CLIENT_ID', '')
    gmail_csec = os.environ.get('GMAIL_CLIENT_SECRET', '')
    gmail_rtok = os.environ.get('GMAIL_REFRESH_TOKEN', '')
    gmail_email = os.environ.get('EMAIL_ADDRESS', '')

    if gmail_cid and gmail_csec and gmail_rtok:
        db.save_athlete_credentials(aid, gmail_cid, gmail_csec, gmail_rtok, gmail_email, enc_key)
        print(f"Gmail credentials migrated for {gmail_email}")
    else:
        print("No Gmail env vars found to migrate.")

    print("\nDone! You can now log in at /login")


if __name__ == '__main__':
    main()
