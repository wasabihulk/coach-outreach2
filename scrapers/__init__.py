"""
scrapers/__init__.py - Scraper Modules
============================================================================
Enterprise-grade scrapers for extracting coach information.

Modules:
- unified_scraper: Combined name + email extraction from staff pages

Author: Coach Outreach System
Version: 4.0.0
============================================================================
"""

try:
    from scrapers.unified_scraper import UnifiedCoachExtractor, CoachRecord, extract_coaches
except ImportError:
    UnifiedCoachExtractor = None
    CoachRecord = None
    extract_coaches = None

__all__ = [
    'UnifiedCoachExtractor', 'CoachRecord', 'extract_coaches',
]
