"""
THE SHORTLISTER — the stage that decides what the scorer is allowed to read.

Everything asserted here was measured, not assumed. My first version of
this module got two things wrong and the tests are the record of it:

  1. It averaged all of a person's target titles into one vector. Six
     phrases ending in "Manager" average to something that mostly means
     "manager", and it duly returned "Customer Loyality Manager" and
     "Enterprise Account Manager" for a brand marketing search.

  2. It fed the job description in alongside the title. Descriptions all
     open with the same corporate throat-clearing, so every listing
     drifted toward one bland centre and the title's signal was diluted.

Both are cheap to reintroduce and invisible when you do — the scan still
runs, still prints a number, and just quietly shortlists worse. Hence
these.
"""

import pytest

from core import prefilter, semantic

needs_model = pytest.mark.skipif(
    not semantic.available(),
    reason="embedding model not downloaded; run `run.py warm-up`")

BRAND = {
    "target": {"role_family": "Brand & Product Marketing",
               "alt_titles": ["Brand Manager", "Product Marketing Manager",
                              "Consumer Marketing Manager"]},
    "hard_filters": {},
    "search_terms": ["Brand Manager", "Marketing Manager"],
}


def _job(title, company="Acme"):
    return {"url": f"https://x/{title}", "title": title, "company": company,
            "description": "We are a fast growing team. Great benefits. "
                           "Equal opportunity employer. Apply now."}


# ============================================================
# The lesson that cost the most to learn
# ============================================================
@needs_model
def test_a_role_that_names_the_job_beats_one_that_merely_ends_in_manager():
    """
    The exact failure the old word-counting filter had, and the exact
    failure a blended anchor vector reintroduces.
    """
    jobs = [_job("Customer Loyality Manager"),
            _job("Enterprise Account Manager - D2C Brands"),
            _job("Senior Product Marketing Manager"),
            _job("Associate Manager - Risk & Compliance")]

    ranked = semantic.rank(jobs, BRAND)

    assert ranked[0]["title"] == "Senior Product Marketing Manager"
    top = ranked[0]["_semantic_score"]
    for other in ranked[1:]:
        assert other["_semantic_score"] < top
    risk = next(j for j in ranked if "Risk" in j["title"])
    assert risk["_semantic_score"] < 0.5, \
        "a compliance role must not look like a marketing role"


@needs_model
def test_each_target_title_is_matched_separately_not_averaged():
    """
    A person wants anything close to ANY of their target titles, not
    something close to the average of all of them. With separate
    anchors, a listing that nails one alt title scores high even when it
    is unlike the others.
    """
    profile = {"target": {"role_family": "Data Engineering",
                          "alt_titles": ["Analytics Engineer",
                                         "Pipeline Engineer"]},
               "hard_filters": {}, "search_terms": []}

    ranked = semantic.rank([_job("Analytics Engineer"),
                            _job("Office Administrator")], profile)

    assert ranked[0]["title"] == "Analytics Engineer"
    assert ranked[0]["_semantic_score"] > 0.7, \
        "an exact alt-title match should score near the top of the range"


@needs_model
def test_the_description_is_not_used_for_shortlisting():
    """
    Boilerplate is identical across listings, so including it pulls
    everything toward the same centre. Two very different roles carrying
    the SAME description must still be told apart.
    """
    same_boilerplate = "We are a fast growing team. Great benefits."
    a = {"url": "a", "title": "Brand Manager", "company": "X",
         "description": same_boilerplate}
    b = {"url": "b", "title": "Warehouse Loading Supervisor", "company": "X",
         "description": same_boilerplate}

    ranked = semantic.rank([b, a], BRAND)

    assert ranked[0]["title"] == "Brand Manager"
    assert ranked[0]["_semantic_score"] - ranked[1]["_semantic_score"] > 0.3
    assert "description" not in semantic.job_text(a)


# ============================================================
# The floor and the margin do different jobs
# ============================================================
@needs_model
def test_a_thin_day_still_surfaces_the_stretch_role():
    """
    The relative margin is a spend control. On a two-listing day there
    is no spend to control, and applying it anyway dropped
    "Director - Brand" from a brand search — the exact kind of title
    this tool exists to surface.
    """
    kept = prefilter.prefilter([_job("Head of Brand Marketing"),
                                _job("Director - Brand")], BRAND)

    assert len(kept) == 2


@needs_model
def test_the_floor_holds_even_when_almost_nothing_matched():
    """
    ...but relaxing the margin must never mean keeping junk. A listing
    unrelated to the search is not a candidate on any day, however
    quiet.
    """
    kept = prefilter.prefilter([_job("Warehouse Loading Supervisor"),
                                _job("Telecalling Executive")], BRAND)

    assert kept == []


@needs_model
def test_a_busy_day_is_capped_so_the_scorer_is_not_flooded():
    """
    The old cap was a share of the RAW scrape, so it grew with the noise
    — one real profile got 179 candidates out of 717 because the day was
    large, not because 179 were any good.
    """
    jobs = [_job(f"Brand Manager {i}") for i in range(200)]

    kept = prefilter.prefilter(jobs, BRAND, top_n=40)

    assert len(kept) == 40


# ============================================================
# It has to work on a laptop with no model and no internet
# ============================================================
def test_shortlisting_falls_back_to_keywords_when_embedding_is_absent(
        monkeypatch):
    monkeypatch.setattr(semantic, "rank", lambda jobs, profile: None)

    kept = prefilter.prefilter([_job("Brand Manager"),
                                _job("Telecalling Executive")], BRAND)

    assert [j["title"] for j in kept] == ["Brand Manager"]
    assert kept[0]["_prefilter_signal"] > 0


def test_every_kept_listing_carries_a_ranking_score_whichever_path_ran():
    """Downstream code should not have to know which shortlister ran."""
    kept = prefilter.prefilter([_job("Brand Manager")], BRAND)
    assert kept and "_prefilter_signal" in kept[0]


def test_ranking_an_empty_corpus_is_not_an_error():
    assert semantic.rank([], BRAND) == []


def test_a_profile_with_no_target_cannot_be_ranked():
    assert semantic.rank([_job("Brand Manager")], {"target": {}}) is None
