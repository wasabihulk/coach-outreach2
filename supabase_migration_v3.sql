-- ============================================
-- RecruitSignal Migration v3 — Coach Tracking
-- Run in Supabase Dashboard → SQL Editor
-- ============================================

ALTER TABLE coaches ADD COLUMN IF NOT EXISTS contacted_date TEXT;
ALTER TABLE coaches ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE coaches ADD COLUMN IF NOT EXISTS responded BOOLEAN DEFAULT false;
ALTER TABLE coaches ADD COLUMN IF NOT EXISTS responded_at TIMESTAMPTZ;
ALTER TABLE coaches ADD COLUMN IF NOT EXISTS response_sentiment TEXT;

CREATE INDEX IF NOT EXISTS idx_coaches_responded ON coaches(responded);
