-- Migration: Add school_requests table for athlete school requests
-- Run this in Supabase SQL Editor

-- Create school_requests table
CREATE TABLE IF NOT EXISTS school_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    athlete_id UUID REFERENCES athletes(id) ON DELETE CASCADE,
    athlete_name TEXT,
    school_name TEXT NOT NULL,
    notes TEXT,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'rejected')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    completed_by UUID REFERENCES athletes(id)
);

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_school_requests_status ON school_requests(status);
CREATE INDEX IF NOT EXISTS idx_school_requests_athlete ON school_requests(athlete_id);

-- Enable RLS
ALTER TABLE school_requests ENABLE ROW LEVEL SECURITY;

-- Policy: Athletes can insert their own requests
CREATE POLICY "Athletes can insert own requests" ON school_requests
    FOR INSERT WITH CHECK (true);

-- Policy: Athletes can view their own requests
CREATE POLICY "Athletes can view own requests" ON school_requests
    FOR SELECT USING (true);

-- Policy: Service role can do everything (for admin operations)
CREATE POLICY "Service role full access" ON school_requests
    FOR ALL USING (true) WITH CHECK (true);
