-- ============================================
-- RecruitSignal Migration — Multi-Tenant
-- Run in Supabase Dashboard → SQL Editor
-- ============================================

-- 1. Auth columns on athletes
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT false;
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true;
CREATE UNIQUE INDEX IF NOT EXISTS idx_athletes_email ON athletes(email);

-- 2. Encrypted Gmail credentials per athlete
CREATE TABLE IF NOT EXISTS athlete_credentials (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    athlete_id UUID REFERENCES athletes(id) ON DELETE CASCADE UNIQUE,
    gmail_client_id TEXT,
    gmail_client_secret TEXT,
    gmail_refresh_token TEXT,
    gmail_email TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_athlete_credentials_athlete ON athlete_credentials(athlete_id);

-- 3. School selection per athlete (junction table)
CREATE TABLE IF NOT EXISTS athlete_schools (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    athlete_id UUID REFERENCES athletes(id) ON DELETE CASCADE,
    school_id UUID REFERENCES schools(id) ON DELETE CASCADE,
    coach_preference TEXT CHECK (coach_preference IN ('position_coach', 'rc', 'both')) DEFAULT 'both',
    added_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(athlete_id, school_id)
);

CREATE INDEX IF NOT EXISTS idx_athlete_schools_athlete ON athlete_schools(athlete_id);
CREATE INDEX IF NOT EXISTS idx_athlete_schools_school ON athlete_schools(school_id);

-- 4. Triggers for updated_at
DROP TRIGGER IF EXISTS set_updated_at_athlete_credentials ON athlete_credentials;
CREATE TRIGGER set_updated_at_athlete_credentials
BEFORE UPDATE ON athlete_credentials
FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- 5. Seed Keelan as admin + auto-select all existing schools
UPDATE athletes SET is_admin = true, is_active = true WHERE email = 'underwoodkeelan@gmail.com';

INSERT INTO athlete_schools (athlete_id, school_id, coach_preference)
SELECT a.id, s.id, 'both'
FROM athletes a, schools s
WHERE a.email = 'underwoodkeelan@gmail.com'
ON CONFLICT (athlete_id, school_id) DO NOTHING;
