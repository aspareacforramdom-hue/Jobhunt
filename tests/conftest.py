"""Shared test setup. Puts the project root on the path and keeps
every test off the real database and off the network."""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Never inherit live paid-service keys into tests. Individual tests set fakes.
for _name in ("OPENAI_API_KEY", "APIFY_TOKEN", "FIRECRAWL_API_KEY", "TELEGRAM_BOT_TOKEN"):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def isolate_usage_ledger(tmp_path, monkeypatch):
    from core import usage, firecrawl_budget, firecrawl
    monkeypatch.setattr(usage, 'PATH', tmp_path / 'usage.db')
    monkeypatch.setattr(firecrawl_budget, 'LEDGER', tmp_path / 'firecrawl-budget.db')
    firecrawl.take_notes()


@pytest.fixture
def ai_pm_profile():
    """Someone moving into AI product management."""
    return {
        "target": {
            "role_family": "AI Product Manager",
            "alt_titles": ["Technical Product Manager"],
            "seniority": "APM to PM",
        },
        "hard_filters": {"min_ctc_lpa": 18, "locations": ["Mumbai"],
                         "exclude_keywords": ["field officer"],
                         "exclude_titles": [], "max_years_required": 8},
        "weights": {"trajectory_fit": 30, "ai_product_depth": 30,
                    "domain_leverage": 20, "company_quality": 12,
                    "experience_fit": 8},
        "dimension_definitions": {
            "ai_product_depth": "Is this genuine ML/AI product work with "
                                "model decisions, or a dashboard called AI?",
            "domain_leverage": "Does their banking background become an "
                               "advantage in this specific role?",
        },
        "angles": ["Banking insider who ships AI products solo"],
        "search_terms": ["AI Product Manager", "Technical Product Manager"],
    }


@pytest.fixture
def brand_profile():
    """Someone moving from banking sales into brand marketing.

    Deliberately shares no scoring vocabulary with the profile above."""
    return {
        "target": {
            "role_family": "Brand Marketing Manager",
            "alt_titles": ["Brand Manager", "Marketing Manager"],
            "seniority": "Manager",
        },
        "hard_filters": {"min_ctc_lpa": 14, "locations": ["Mumbai"],
                         "exclude_keywords": ["telecalling"],
                         "exclude_titles": [], "max_years_required": None},
        "weights": {"trajectory_fit": 30, "creative_ownership": 30,
                    "brand_scale": 20, "company_quality": 12,
                    "experience_fit": 8},
        "dimension_definitions": {
            "creative_ownership": "Does this role own creative direction, "
                                  "or only execute someone else's brief?",
            "brand_scale": "How large and consumer-visible is the brand "
                           "they would be responsible for?",
        },
        "angles": ["Sold the product face to face for six years"],
        "search_terms": ["Brand Manager", "Brand Marketing Manager"],
    }
