-- ============================================
-- RecruitSignal Database Schema
-- Run this in Supabase Dashboard → SQL Editor
-- ============================================

-- SCHOOLS
CREATE TABLE IF NOT EXISTS schools (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name TEXT NOT NULL,
    division TEXT,
    conference TEXT,
    state TEXT,
    staff_url TEXT,
    enrollment TEXT,
    academic_tier TEXT,
    is_public BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(name)
);

-- COACHES
CREATE TABLE IF NOT EXISTS coaches (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    school_id UUID REFERENCES schools(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    title TEXT,
    role TEXT CHECK (role IN ('rc', 'ol', 'hc', 'oc', 'dc', 'other')),
    email TEXT,
    twitter TEXT,
    phone TEXT,
    verified BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coaches_school ON coaches(school_id);
CREATE INDEX IF NOT EXISTS idx_coaches_email ON coaches(email);
CREATE INDEX IF NOT EXISTS idx_coaches_role ON coaches(role);

-- ATHLETES (multi-user ready)
CREATE TABLE IF NOT EXISTS athletes (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    auth_user_id UUID UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT,
    grad_year TEXT,
    height TEXT,
    weight TEXT,
    positions TEXT,
    gpa TEXT,
    school TEXT,
    state TEXT,
    highlight_link TEXT,
    profile_image TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- OUTREACH (every email sent/tracked)
CREATE TABLE IF NOT EXISTS outreach (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    athlete_id UUID REFERENCES athletes(id) ON DELETE CASCADE,
    school_id UUID REFERENCES schools(id) ON DELETE SET NULL,
    coach_id UUID REFERENCES coaches(id) ON DELETE SET NULL,
    coach_name TEXT,
    coach_email TEXT,
    coach_role TEXT,
    school_name TEXT,
    email_type TEXT DEFAULT 'intro' CHECK (email_type IN ('intro', 'followup_1', 'followup_2', 'followup_3', 'custom')),
    subject TEXT,
    body TEXT,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'queued', 'sent', 'failed', 'bounced')),
    tracking_id UUID DEFAULT gen_random_uuid(),
    is_ai_generated BOOLEAN DEFAULT false,
    sent_at TIMESTAMPTZ,
    opened BOOLEAN DEFAULT false,
    opened_at TIMESTAMPTZ,
    open_count INT DEFAULT 0,
    replied BOOLEAN DEFAULT false,
    replied_at TIMESTAMPTZ,
    reply_sentiment TEXT CHECK (reply_sentiment IN ('positive', 'neutral', 'negative')),
    reply_snippet TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_outreach_athlete ON outreach(athlete_id);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach(status);
CREATE INDEX IF NOT EXISTS idx_outreach_tracking ON outreach(tracking_id);
CREATE INDEX IF NOT EXISTS idx_outreach_school ON outreach(school_name);
CREATE INDEX IF NOT EXISTS idx_outreach_coach_email ON outreach(coach_email);

-- DM QUEUE
CREATE TABLE IF NOT EXISTS dm_queue (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    athlete_id UUID REFERENCES athletes(id) ON DELETE CASCADE,
    coach_id UUID REFERENCES coaches(id) ON DELETE SET NULL,
    coach_name TEXT,
    coach_twitter TEXT,
    school_name TEXT,
    message TEXT,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'skipped', 'followed')),
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- TEMPLATES
CREATE TABLE IF NOT EXISTS templates (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    athlete_id UUID REFERENCES athletes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    template_type TEXT DEFAULT 'email' CHECK (template_type IN ('email', 'dm', 'followup')),
    coach_type TEXT CHECK (coach_type IN ('rc', 'ol', 'any')),
    is_active BOOLEAN DEFAULT true,
    usage_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- SETTINGS (per athlete)
CREATE TABLE IF NOT EXISTS settings (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    athlete_id UUID REFERENCES athletes(id) ON DELETE CASCADE UNIQUE,
    auto_send_enabled BOOLEAN DEFAULT false,
    auto_send_count INT DEFAULT 25,
    paused_until TIMESTAMPTZ,
    delay_between_emails INT DEFAULT 3,
    days_between_followups INT DEFAULT 7,
    max_followups INT DEFAULT 3,
    send_hour INT DEFAULT 9,
    timezone_offset INT DEFAULT -5,
    notifications_enabled BOOLEAN DEFAULT false,
    ntfy_channel TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- AI EMAILS (pre-generated, waiting to send)
CREATE TABLE IF NOT EXISTS ai_emails (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    athlete_id UUID REFERENCES athletes(id) ON DELETE CASCADE,
    school_name TEXT NOT NULL,
    coach_name TEXT,
    coach_role TEXT,
    subject TEXT,
    body TEXT NOT NULL,
    research_data JSONB,
    hook_used TEXT,
    hook_category TEXT,
    status TEXT DEFAULT 'ready' CHECK (status IN ('ready', 'sent', 'expired', 'failed')),
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_emails_school ON ai_emails(school_name);
CREATE INDEX IF NOT EXISTS idx_ai_emails_status ON ai_emails(status);

-- SCHOOL RESEARCH CACHE
CREATE TABLE IF NOT EXISTS school_research (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    school_name TEXT NOT NULL UNIQUE,
    division TEXT,
    conference TEXT,
    head_coach TEXT,
    ol_coach TEXT,
    recent_record TEXT,
    conference_standing TEXT,
    ol_depth_need TEXT,
    notable_academics TEXT,
    notable_facts TEXT[],
    raw_data JSONB,
    researched_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ DEFAULT (now() + interval '7 days')
);

-- AUTO-UPDATE updated_at TRIGGER
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOR t IN SELECT unnest(ARRAY['schools','coaches','athletes','outreach','templates','settings','ai_emails'])
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS set_updated_at ON %I', t);
        EXECUTE format('CREATE TRIGGER set_updated_at BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION update_updated_at()', t);
    END LOOP;
END $$;
