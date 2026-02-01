-- ============================================
-- RecruitSignal Migration v2
-- Run in Supabase Dashboard → SQL Editor
-- ============================================

-- 1. Clean schools table (remove unused columns)
ALTER TABLE schools DROP COLUMN IF EXISTS enrollment;
ALTER TABLE schools DROP COLUMN IF EXISTS academic_tier;
ALTER TABLE schools DROP COLUMN IF EXISTS is_public;

-- 2. Drop unused tables
DROP TABLE IF EXISTS school_research;
DROP TABLE IF EXISTS ai_emails;

-- 3. Upgrade dm_queue with full tracking
ALTER TABLE dm_queue ADD COLUMN IF NOT EXISTS followed_at TIMESTAMPTZ;
ALTER TABLE dm_queue ADD COLUMN IF NOT EXISTS marked_wrong_at TIMESTAMPTZ;
ALTER TABLE dm_queue ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE dm_queue ADD COLUMN IF NOT EXISTS needs_rescrape BOOLEAN DEFAULT false;

-- Update status constraint to include all DM statuses
ALTER TABLE dm_queue DROP CONSTRAINT IF EXISTS dm_queue_status_check;
ALTER TABLE dm_queue ADD CONSTRAINT dm_queue_status_check
  CHECK (status IN ('pending', 'messaged', 'followed', 'skipped', 'wrong_handle', 'no_handle', 'sent'));

-- Add indexes for DM queue
CREATE INDEX IF NOT EXISTS idx_dm_queue_athlete ON dm_queue(athlete_id);
CREATE INDEX IF NOT EXISTS idx_dm_queue_status ON dm_queue(status);
CREATE INDEX IF NOT EXISTS idx_dm_queue_twitter ON dm_queue(coach_twitter);

-- Add updated_at trigger for dm_queue
DROP TRIGGER IF EXISTS set_updated_at ON dm_queue;
ALTER TABLE dm_queue ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
CREATE TRIGGER set_updated_at BEFORE UPDATE ON dm_queue
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
