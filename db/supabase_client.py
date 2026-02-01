"""
RecruitSignal — Supabase Database Integration
Replaces Google Sheets as primary data store.
"""

import os
import logging
from datetime import datetime, timezone
from supabase import create_client, Client

logger = logging.getLogger(__name__)

# Singleton
_db_instance = None


def get_db():
    """Get or create the Supabase DB singleton."""
    global _db_instance
    if _db_instance is None:
        _db_instance = SupabaseDB()
    return _db_instance


class SupabaseDB:
    def __init__(self):
        self.url = os.environ.get('SUPABASE_URL', 'https://sdugzlvnlfejiwmrrysf.supabase.co')
        self.key = os.environ.get('SUPABASE_SERVICE_KEY', '')
        if not self.key:
            self.key = os.environ.get('SUPABASE_KEY', '')
        if not self.key:
            raise ValueError("SUPABASE_SERVICE_KEY environment variable required")
        self.client: Client = create_client(self.url, self.key)
        self._athlete_id = None
        logger.info("Supabase connected: %s", self.url)

    # ==========================================
    # ATHLETE (current user)
    # ==========================================

    def get_or_create_athlete(self, name, email, **profile):
        """Get existing athlete by email or create new one. Returns athlete row."""
        result = self.client.table('athletes').select('*').eq('email', email).limit(1).execute()
        if result.data:
            self._athlete_id = result.data[0]['id']
            return result.data[0]

        data = {'name': name, 'email': email, **profile}
        result = self.client.table('athletes').insert(data).execute()
        self._athlete_id = result.data[0]['id']
        return result.data[0]

    @property
    def athlete_id(self):
        return self._athlete_id

    @athlete_id.setter
    def athlete_id(self, value):
        self._athlete_id = value

    def update_athlete(self, **fields):
        if not self._athlete_id:
            return None
        return self.client.table('athletes').update(fields).eq('id', self._athlete_id).execute()

    # ==========================================
    # SCHOOLS
    # ==========================================

    def add_school(self, name, division=None, conference=None, state=None, staff_url=None, **extra):
        """Add school, skip if already exists."""
        data = {'name': name, 'division': division, 'conference': conference, 'state': state, 'staff_url': staff_url, **extra}
        data = {k: v for k, v in data.items() if v is not None}
        try:
            return self.client.table('schools').upsert(data, on_conflict='name').execute()
        except Exception as e:
            logger.error("Failed to add school %s: %s", name, e)
            return None

    def get_school(self, name):
        result = self.client.table('schools').select('*').eq('name', name).limit(1).execute()
        return result.data[0] if result.data else None

    def search_schools(self, query=None, division=None, state=None, conference=None, limit=50):
        q = self.client.table('schools').select('*')
        if query:
            q = q.ilike('name', f'%{query}%')
        if division:
            q = q.eq('division', division)
        if state:
            q = q.eq('state', state)
        if conference:
            q = q.ilike('conference', f'%{conference}%')
        return q.limit(limit).execute().data

    def get_all_schools(self):
        return self.client.table('schools').select('*').order('name').execute().data

    # ==========================================
    # COACHES
    # ==========================================

    def add_coach(self, school_name, name, role, email=None, twitter=None, title=None):
        school = self.get_school(school_name)
        school_id = school['id'] if school else None
        data = {
            'school_id': school_id,
            'name': name,
            'role': role,
            'email': email,
            'twitter': twitter,
            'title': title,
        }
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.table('coaches').insert(data).execute()

    def get_coaches_for_school(self, school_name):
        school = self.get_school(school_name)
        if not school:
            return []
        return self.client.table('coaches').select('*').eq('school_id', school['id']).execute().data

    def update_coach(self, coach_id, **fields):
        return self.client.table('coaches').update(fields).eq('id', coach_id).execute()

    def find_coach_by_email(self, email):
        result = self.client.table('coaches').select('*, schools(name, division, conference)').eq('email', email).limit(1).execute()
        return result.data[0] if result.data else None

    def add_school_with_coaches(self, school_name, staff_url=None,
                                 rc_name=None, rc_email=None, rc_twitter=None,
                                 ol_name=None, ol_email=None, ol_twitter=None,
                                 division=None, conference=None, state=None):
        """Add school + RC + OL coaches in one call (replaces sheet append_row)."""
        self.add_school(name=school_name, staff_url=staff_url,
                        division=division, conference=conference, state=state)
        coaches_added = []
        if rc_name:
            self.add_coach(school_name=school_name, name=rc_name, role='rc',
                           email=rc_email, twitter=rc_twitter)
            coaches_added.append(rc_name)
        if ol_name:
            self.add_coach(school_name=school_name, name=ol_name, role='ol',
                           email=ol_email, twitter=ol_twitter)
            coaches_added.append(ol_name)
        return coaches_added

    def get_coaches_to_email(self, limit=25, days_between=7):
        """Get coaches with emails who are due for outreach.
        Returns list of dicts with coach info + email_stage."""
        # Get all coaches with emails
        q = self.client.table('coaches').select('*, schools(name, division, conference)')
        coaches = q.not_.is_('email', 'null').execute().data
        if not coaches:
            return []

        results = []
        for coach in coaches:
            email = coach.get('email', '').strip()
            if not email:
                continue
            school_info = coach.get('schools') or {}
            school_name = school_info.get('name', '') if isinstance(school_info, dict) else ''

            # Get latest outreach for this coach
            outreach = (self.client.table('outreach')
                        .select('email_type, sent_at, replied, status')
                        .eq('coach_email', email)
                        .order('sent_at', desc=True)
                        .limit(5)
                        .execute().data)

            # Determine stage
            stage = self._compute_email_stage(outreach)

            # Check if due (enough days since last email)
            if outreach and stage != 'new':
                latest = outreach[0]
                if latest.get('replied'):
                    continue  # Already replied, skip
                sent_at = latest.get('sent_at')
                if sent_at:
                    from datetime import timedelta
                    try:
                        sent_dt = datetime.fromisoformat(sent_at.replace('Z', '+00:00'))
                        if (datetime.now(timezone.utc) - sent_dt).days < days_between:
                            continue  # Too soon
                    except (ValueError, TypeError):
                        pass

            if stage == 'done':
                continue  # All followups sent

            results.append({
                'coach_id': coach['id'],
                'coach_name': coach.get('name', ''),
                'coach_email': email,
                'coach_role': coach.get('role', ''),
                'school_name': school_name,
                'school_id': coach.get('school_id'),
                'twitter': coach.get('twitter', ''),
                'email_stage': stage,
                'division': school_info.get('division') if isinstance(school_info, dict) else None,
                'conference': school_info.get('conference') if isinstance(school_info, dict) else None,
            })
            if len(results) >= limit:
                break

        return results

    def _compute_email_stage(self, outreach_records):
        """Given a coach's outreach history, return their next email stage."""
        if not outreach_records:
            return 'new'
        sent = [r for r in outreach_records if r.get('status') == 'sent']
        if not sent:
            return 'new'
        types_sent = [r.get('email_type', '') for r in sent]
        if 'followup_2' in types_sent:
            return 'done'
        if 'followup_1' in types_sent:
            return 'followup_2'
        if 'intro' in types_sent:
            return 'followup_1'
        return 'followup_1'  # Default: at least one sent

    def get_coach_email_stage(self, coach_id):
        """Get the email stage for a specific coach."""
        coach = self.client.table('coaches').select('email').eq('id', coach_id).limit(1).execute()
        if not coach.data or not coach.data[0].get('email'):
            return 'no_email'
        email = coach.data[0]['email']
        outreach = (self.client.table('outreach')
                    .select('email_type, sent_at, replied, status')
                    .eq('coach_email', email)
                    .order('sent_at', desc=True)
                    .limit(5)
                    .execute().data)
        return self._compute_email_stage(outreach)

    def mark_coach_contacted(self, coach_id, date=None, notes=None):
        """Update coach contacted_date and notes."""
        update = {}
        update['contacted_date'] = date or datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if notes:
            # Append to existing notes
            existing = self.client.table('coaches').select('notes').eq('id', coach_id).limit(1).execute()
            old = ''
            if existing.data and existing.data[0].get('notes'):
                old = existing.data[0]['notes'] + '; '
            update['notes'] = old + notes
        return self.client.table('coaches').update(update).eq('id', coach_id).execute()

    def mark_coach_responded(self, coach_id, sentiment=None):
        """Mark coach as responded."""
        update = {
            'responded': True,
            'responded_at': datetime.now(timezone.utc).isoformat(),
        }
        if sentiment:
            update['response_sentiment'] = sentiment
        return self.client.table('coaches').update(update).eq('id', coach_id).execute()

    def mark_coach_bounced(self, coach_id):
        """Clear email on bounce, add note."""
        existing = self.client.table('coaches').select('notes, email').eq('id', coach_id).limit(1).execute()
        old_notes = ''
        old_email = ''
        if existing.data:
            old_notes = (existing.data[0].get('notes') or '')
            old_email = existing.data[0].get('email', '')
            if old_notes:
                old_notes += '; '
        return self.client.table('coaches').update({
            'email': None,
            'notes': f"{old_notes}BOUNCED ({old_email})",
        }).eq('id', coach_id).execute()

    def get_email_queue_status(self):
        """Get counts of coaches by email stage."""
        coaches = self.client.table('coaches').select('email').not_.is_('email', 'null').execute().data
        counts = {'new': 0, 'followup_1': 0, 'followup_2': 0, 'done': 0, 'replied': 0, 'total_with_email': 0}
        for coach in coaches:
            email = (coach.get('email') or '').strip()
            if not email:
                continue
            counts['total_with_email'] += 1
            outreach = (self.client.table('outreach')
                        .select('email_type, status, replied')
                        .eq('coach_email', email)
                        .order('sent_at', desc=True)
                        .limit(5)
                        .execute().data)
            if outreach and any(r.get('replied') for r in outreach):
                counts['replied'] += 1
                continue
            stage = self._compute_email_stage(outreach)
            counts[stage] = counts.get(stage, 0) + 1
        return counts

    def get_all_coaches_with_schools(self, limit=1000):
        """Get all coaches joined with school info."""
        return (self.client.table('coaches')
                .select('*, schools(name, division, conference, state, staff_url)')
                .order('name')
                .limit(limit)
                .execute().data)

    # ==========================================
    # OUTREACH (email tracking — the big one)
    # ==========================================

    def create_outreach(self, coach_email, coach_name, school_name, coach_role='ol',
                        subject='', body='', email_type='intro', is_ai=False):
        """Create an outreach record. Returns the row with tracking_id."""
        school = self.get_school(school_name)
        data = {
            'athlete_id': self._athlete_id,
            'school_id': school['id'] if school else None,
            'coach_email': coach_email,
            'coach_name': coach_name,
            'school_name': school_name,
            'coach_role': coach_role,
            'subject': subject,
            'body': body,
            'email_type': email_type,
            'is_ai_generated': is_ai,
            'status': 'pending',
        }
        result = self.client.table('outreach').insert(data).execute()
        return result.data[0] if result.data else None

    def mark_sent(self, outreach_id):
        return self.client.table('outreach').update({
            'status': 'sent',
            'sent_at': datetime.now(timezone.utc).isoformat(),
        }).eq('id', outreach_id).execute()

    def mark_failed(self, outreach_id, reason=''):
        return self.client.table('outreach').update({
            'status': 'failed',
        }).eq('id', outreach_id).execute()

    def track_open(self, tracking_id):
        """Called when tracking pixel is hit. Increments open count."""
        result = self.client.table('outreach').select('id, open_count').eq('tracking_id', tracking_id).limit(1).execute()
        if not result.data:
            return None
        row = result.data[0]
        update = {
            'opened': True,
            'open_count': (row.get('open_count') or 0) + 1,
        }
        if not row.get('opened_at'):
            update['opened_at'] = datetime.now(timezone.utc).isoformat()
        return self.client.table('outreach').update(update).eq('id', row['id']).execute()

    def track_reply(self, coach_email, sentiment=None, snippet=None):
        """Mark most recent outreach to this coach as replied."""
        result = (self.client.table('outreach')
                  .select('id')
                  .eq('coach_email', coach_email)
                  .eq('status', 'sent')
                  .order('sent_at', desc=True)
                  .limit(1)
                  .execute())
        if not result.data:
            return None
        update = {
            'replied': True,
            'replied_at': datetime.now(timezone.utc).isoformat(),
        }
        if sentiment:
            update['reply_sentiment'] = sentiment
        if snippet:
            update['reply_snippet'] = snippet[:500]
        return self.client.table('outreach').update(update).eq('id', result.data[0]['id']).execute()

    def get_pending_outreach(self, limit=25):
        q = self.client.table('outreach').select('*').eq('status', 'pending')
        if self._athlete_id:
            q = q.eq('athlete_id', self._athlete_id)
        return q.order('created_at').limit(limit).execute().data

    def get_sent_outreach(self, limit=100):
        q = self.client.table('outreach').select('*').eq('status', 'sent')
        if self._athlete_id:
            q = q.eq('athlete_id', self._athlete_id)
        return q.order('sent_at', desc=True).limit(limit).execute().data

    def get_outreach_stats(self):
        """Dashboard stats."""
        if not self._athlete_id:
            return {}
        base = self.client.table('outreach').select('id', count='exact')
        total = base.eq('athlete_id', self._athlete_id).execute().count or 0
        sent = base.eq('athlete_id', self._athlete_id).eq('status', 'sent').execute().count or 0
        opened = base.eq('athlete_id', self._athlete_id).eq('opened', True).execute().count or 0
        replied = base.eq('athlete_id', self._athlete_id).eq('replied', True).execute().count or 0

        return {
            'total': total,
            'sent': sent,
            'pending': total - sent,
            'opened': opened,
            'replied': replied,
            'open_rate': round((opened / sent * 100), 1) if sent > 0 else 0,
            'response_rate': round((replied / sent * 100), 1) if sent > 0 else 0,
        }

    def get_hot_leads(self, limit=20):
        """Coaches who opened or replied, sorted by engagement."""
        q = (self.client.table('outreach')
             .select('coach_name, coach_email, school_name, open_count, replied, reply_sentiment, opened_at, replied_at')
             .eq('status', 'sent')
             .eq('opened', True))
        if self._athlete_id:
            q = q.eq('athlete_id', self._athlete_id)
        return q.order('open_count', desc=True).limit(limit).execute().data

    def get_recent_responses(self, limit=20):
        q = (self.client.table('outreach')
             .select('*')
             .eq('replied', True))
        if self._athlete_id:
            q = q.eq('athlete_id', self._athlete_id)
        return q.order('replied_at', desc=True).limit(limit).execute().data

    def was_coach_contacted(self, coach_email):
        """Check if we already emailed this coach."""
        result = (self.client.table('outreach')
                  .select('id', count='exact')
                  .eq('coach_email', coach_email)
                  .in_('status', ['sent', 'pending']))
        if self._athlete_id:
            result = result.eq('athlete_id', self._athlete_id)
        return (result.execute().count or 0) > 0

    # ==========================================
    # DM QUEUE (full tracking)
    # ==========================================

    def add_to_dm_queue(self, coach_name, coach_twitter, school_name, message='', coach_id=None, notes=None):
        """Add a coach to the DM queue."""
        data = {
            'athlete_id': self._athlete_id,
            'coach_name': coach_name,
            'coach_twitter': coach_twitter,
            'school_name': school_name,
            'message': message,
            'status': 'pending',
        }
        if coach_id:
            data['coach_id'] = coach_id
        if notes:
            data['notes'] = notes
        return self.client.table('dm_queue').insert(data).execute()

    def get_dm_queue(self, status='pending', limit=50):
        """Get DM queue filtered by status."""
        q = self.client.table('dm_queue').select('*').eq('status', status)
        if self._athlete_id:
            q = q.eq('athlete_id', self._athlete_id)
        return q.order('created_at').limit(limit).execute().data

    def get_all_dm_history(self, limit=200):
        """Get all DM records regardless of status."""
        q = self.client.table('dm_queue').select('*')
        if self._athlete_id:
            q = q.eq('athlete_id', self._athlete_id)
        return q.order('created_at', desc=True).limit(limit).execute().data

    def mark_dm_status(self, dm_id, status, notes=None):
        """Mark a DM with status: messaged, followed, skipped, wrong_handle, no_handle."""
        now = datetime.now(timezone.utc).isoformat()
        update = {'status': status}

        if status == 'messaged':
            update['sent_at'] = now
        elif status == 'followed':
            update['followed_at'] = now
        elif status == 'wrong_handle':
            update['marked_wrong_at'] = now
            update['needs_rescrape'] = True

        if notes:
            # Append to existing notes
            existing = self.client.table('dm_queue').select('notes').eq('id', dm_id).limit(1).execute()
            old_notes = ''
            if existing.data and existing.data[0].get('notes'):
                old_notes = existing.data[0]['notes'] + '; '
            update['notes'] = old_notes + notes

        return self.client.table('dm_queue').update(update).eq('id', dm_id).execute()

    def was_coach_dmed(self, coach_twitter):
        """Check if we already DMed or interacted with this coach."""
        result = (self.client.table('dm_queue')
                  .select('id, status', count='exact')
                  .eq('coach_twitter', coach_twitter)
                  .in_('status', ['messaged', 'followed', 'pending']))
        if self._athlete_id:
            result = result.eq('athlete_id', self._athlete_id)
        return (result.execute().count or 0) > 0

    def get_dm_stats(self):
        """Get DM stats by status."""
        if not self._athlete_id:
            return {}
        base = self.client.table('dm_queue').select('id', count='exact').eq('athlete_id', self._athlete_id)
        pending = base.eq('status', 'pending').execute().count or 0
        messaged = base.eq('status', 'messaged').execute().count or 0
        followed = base.eq('status', 'followed').execute().count or 0
        skipped = base.eq('status', 'skipped').execute().count or 0
        wrong = base.eq('status', 'wrong_handle').execute().count or 0
        return {
            'pending': pending,
            'messaged': messaged,
            'followed': followed,
            'skipped': skipped,
            'wrong_handle': wrong,
            'total': pending + messaged + followed + skipped + wrong,
        }

    def get_wrong_handles(self, limit=50):
        """Get coaches with wrong Twitter handles that need re-scraping."""
        q = (self.client.table('dm_queue')
             .select('*')
             .eq('needs_rescrape', True))
        if self._athlete_id:
            q = q.eq('athlete_id', self._athlete_id)
        return q.order('marked_wrong_at', desc=True).limit(limit).execute().data

    def find_dm_by_twitter(self, coach_twitter):
        """Find a DM record by Twitter handle."""
        result = (self.client.table('dm_queue')
                  .select('*')
                  .eq('coach_twitter', coach_twitter)
                  .order('created_at', desc=True)
                  .limit(1)
                  .execute())
        return result.data[0] if result.data else None

    def find_dm_by_coach_school(self, coach_name, school_name):
        """Find a DM record by coach name + school."""
        result = (self.client.table('dm_queue')
                  .select('*')
                  .eq('coach_name', coach_name)
                  .eq('school_name', school_name)
                  .order('created_at', desc=True)
                  .limit(1)
                  .execute())
        return result.data[0] if result.data else None

    # ==========================================
    # TEMPLATES
    # ==========================================

    def get_templates(self, template_type=None):
        q = self.client.table('templates').select('*')
        if self._athlete_id:
            q = q.eq('athlete_id', self._athlete_id)
        if template_type:
            q = q.eq('template_type', template_type)
        return q.order('created_at').execute().data

    def create_template(self, name, body, subject=None, template_type='email', coach_type='any'):
        data = {
            'athlete_id': self._athlete_id,
            'name': name,
            'body': body,
            'subject': subject,
            'template_type': template_type,
            'coach_type': coach_type,
        }
        return self.client.table('templates').insert(data).execute()

    def update_template(self, template_id, **fields):
        return self.client.table('templates').update(fields).eq('id', template_id).execute()

    def delete_template(self, template_id):
        return self.client.table('templates').delete().eq('id', template_id).execute()

    def toggle_template(self, template_id, active):
        return self.client.table('templates').update({'is_active': active}).eq('id', template_id).execute()

    # ==========================================
    # SETTINGS
    # ==========================================

    def get_settings(self):
        if not self._athlete_id:
            return {}
        result = self.client.table('settings').select('*').eq('athlete_id', self._athlete_id).limit(1).execute()
        return result.data[0] if result.data else {}

    def save_settings(self, **fields):
        if not self._athlete_id:
            return None
        existing = self.get_settings()
        if existing:
            return self.client.table('settings').update(fields).eq('athlete_id', self._athlete_id).execute()
        else:
            fields['athlete_id'] = self._athlete_id
            return self.client.table('settings').insert(fields).execute()

