-- Migration v5: Schema fixes for multi-tenant and template system
-- Run this on Supabase SQL Editor

-- 1. Add last_login column to athletes
ALTER TABLE athletes ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ;

-- 2. Broaden coaches.role to support all position types
ALTER TABLE coaches DROP CONSTRAINT IF EXISTS coaches_role_check;
ALTER TABLE coaches ADD CONSTRAINT coaches_role_check 
    CHECK (role IN ('rc', 'ol', 'hc', 'oc', 'dc', 'wr', 'qb', 'rb', 'te', 'dl', 'lb', 'db', 'st', 'ath', 'other'));

-- 3. Broaden templates.template_type to support more types
ALTER TABLE templates DROP CONSTRAINT IF EXISTS templates_template_type_check;
ALTER TABLE templates ADD CONSTRAINT templates_template_type_check 
    CHECK (template_type IN ('email', 'dm', 'followup', 'intro', 'followup_1', 'followup_2', 'followup_3'));

-- 4. Broaden templates.coach_type to support position types
ALTER TABLE templates DROP CONSTRAINT IF EXISTS templates_coach_type_check;
ALTER TABLE templates ADD CONSTRAINT templates_coach_type_check 
    CHECK (coach_type IN ('rc', 'ol', 'any', 'wr', 'qb', 'rb', 'te', 'dl', 'lb', 'db', 'st', 'ath', 'hc'));

-- 5. Add tone and mode columns for easy-mode template builder
ALTER TABLE templates ADD COLUMN IF NOT EXISTS tone TEXT DEFAULT 'professional';
ALTER TABLE templates ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT 'advanced';
ALTER TABLE templates ADD COLUMN IF NOT EXISTS description TEXT;

-- 6. Index for faster template lookups per athlete
CREATE INDEX IF NOT EXISTS idx_templates_athlete_id ON templates(athlete_id);
CREATE INDEX IF NOT EXISTS idx_templates_type ON templates(template_type, coach_type);
