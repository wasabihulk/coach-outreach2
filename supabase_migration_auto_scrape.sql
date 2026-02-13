-- Migration: Add auto-scrape fields to school_requests table
-- Run this in Supabase SQL Editor

-- Add columns for auto-scrape results
ALTER TABLE school_requests
ADD COLUMN IF NOT EXISTS staff_url TEXT,
ADD COLUMN IF NOT EXISTS scraped_data JSONB,
ADD COLUMN IF NOT EXISTS scrape_status TEXT DEFAULT 'pending' CHECK (scrape_status IN ('pending', 'scraping', 'success', 'failed', 'manual_needed')),
ADD COLUMN IF NOT EXISTS scrape_error TEXT;

-- Add index for scrape status
CREATE INDEX IF NOT EXISTS idx_school_requests_scrape_status ON school_requests(scrape_status);
