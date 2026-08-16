"""
COMPANY DISCOVERY — who to watch, worked out rather than typed in.

The thing being replaced: seventeen company slugs someone typed into
sources.yml once. Every user watched the same seventeen, which suited
none of them in particular, and adding one meant editing YAML, so nobody
ever did.

The probe half is the valuable half and it is free — a company either
answers one of the five public ATS APIs with live roles or it does not.
Run live while writing this, twelve of fifteen real Indian and global
employers resolved with no key and no cost.
"""

import pytest

from core import companies, db, scheduler, session


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "co.db"))
    yield c
    c.close()


@pytest.fixture
def user(conn):
    uid, _ = session.create_account(conn, "alice", "pw")
    return uid


# ============================================================
# Turning a company name into something probeable
# ============================================================
@pytest.mark.parametrize("name,expected", [
    ("Swiggy Technologies Private Limited", "swiggy"),
    ("PhonePe", "phonepe"),
    ("Freshworks Inc.", "freshworks"),
    ("Zeta Suite", "zeta"),
])
def test_legal_padding_is_stripped_before_probing(name, expected):
    """
    Nobody's job board is called `bundltechnologiesprivatelimited`. The
    LLM is told to return common names for this reason, but it will not
    always comply, so the noise is stripped here too.
    """
    assert expected in companies.slug_variants(name)


def test_only_a_few_spellings_are_tried():
    """
    Each variant multiplies probes by the number of platforms. Three
    plausible guesses beat ten implausible ones.
    """
    assert len(companies.slug_variants("Tata Consumer Products Limited")) <= 3


def test_a_nonsense_name_produces_nothing_to_probe():
    assert companies.slug_variants("   ") == []
    assert companies.slug_variants(None) == []


# ============================================================
# Reading each platform's answer
# ============================================================
@pytest.mark.parametrize("platform,payload,expected", [
    ("greenhouse", {"jobs": [1, 2, 3]}, 3),
    ("lever", [1, 2], 2),
    ("ashby", {"jobs": [1]}, 1),
    ("smartrecruiters", {"totalFound": 46, "content": [1]}, 46),
    ("workable", {"jobs": []}, 0),
    ("greenhouse", "not json at all", 0),
])
def test_each_platform_reports_its_job_count_differently(platform, payload,
                                                         expected):
    """Five APIs, five shapes. A miscount here silently drops a board."""
    assert companies._count(platform, payload) == expected


def test_a_board_with_no_live_roles_is_not_recorded(monkeypatch):
    """An empty board is indistinguishable from a wrong guess, and
    recording it would put a dead slug in every future scan."""
    monkeypatch.setattr(companies, "probe_slug", lambda slug, timeout=12: [])
    assert companies.resolve(["Nobody Ltd"]) == []


def test_the_same_board_is_not_recorded_twice(monkeypatch):
    """Variants of one name can resolve to the same slug."""
    monkeypatch.setattr(companies, "probe_slug",
                        lambda slug, timeout=12: [("lever", 5)]
                        if slug == "cred" else [])

    found = companies.resolve(["Cred", "CRED"])

    assert len(found) == 1


# ============================================================
# Storage — shared, like the scrape itself
# ============================================================
def test_a_discovered_board_survives_and_updates(conn, user):
    db.save_job_board(conn, "lever", "meesho", "Meesho", 54, user)
    first = db.list_job_boards(conn)[0]

    db.save_job_board(conn, "lever", "meesho", "Meesho", 61, user)
    second = db.list_job_boards(conn)[0]

    assert len(db.list_job_boards(conn)) == 1
    assert second["jobs_seen"] == 61
    assert second["discovered_at"] == first["discovered_at"], \
        "when it was first found should not move"


def test_a_board_can_be_switched_off_without_being_forgotten(conn, user):
    db.save_job_board(conn, "lever", "meesho", "Meesho", 54, user)
    db.set_job_board_enabled(conn, "lever", "meesho", False)

    assert db.list_job_boards(conn) == []
    assert len(db.list_job_boards(conn, enabled_only=False)) == 1


# ============================================================
# How discovery reaches a scan
# ============================================================
def test_discovered_boards_join_the_hand_written_ones(conn, user):
    """
    A union, never a replacement. Someone who typed a company into
    sources.yml keeps it even if probing never surfaced it — discovery
    can only add coverage, never quietly remove it.
    """
    db.save_job_board(conn, "greenhouse", "postman", "Postman", 109, user)
    db.save_job_board(conn, "smartrecruiters", "swiggy", "Swiggy", 46, user)
    sources = {"ats_targets": {"greenhouse": ["phonepe"], "lever": ["cred"]}}

    scheduler._merge_discovered_boards(conn, sources)
    ats = sources["ats_targets"]

    assert set(ats["greenhouse"]) == {"phonepe", "postman"}
    assert ats["lever"] == ["cred"]                 # untouched
    assert ats["smartrecruiters"] == ["swiggy"]     # a platform section added


def test_merging_twice_does_not_duplicate_a_slug(conn, user):
    db.save_job_board(conn, "greenhouse", "postman", "Postman", 109, user)
    sources = {"ats_targets": {"greenhouse": ["postman"]}}

    scheduler._merge_discovered_boards(conn, sources)

    assert sources["ats_targets"]["greenhouse"] == ["postman"]


def test_a_disabled_board_stays_out_of_scans(conn, user):
    db.save_job_board(conn, "lever", "meesho", "Meesho", 54, user)
    db.set_job_board_enabled(conn, "lever", "meesho", False)
    sources = {"ats_targets": {}}

    scheduler._merge_discovered_boards(conn, sources)

    assert sources["ats_targets"].get("lever", []) == []


# ============================================================
# It must not spend money it was not asked to
# ============================================================
def test_discovery_never_runs_during_a_scan():
    """
    Probing is free but the candidate-generation call is not, and a scan
    should never surprise anyone with an LLM call they did not ask for.
    It is a command, deliberately.
    """
    import inspect
    source = inspect.getsource(scheduler)
    assert "discover_for_user" not in source
    assert "candidates_from_profile" not in source


def test_a_profile_with_no_target_asks_for_nothing(monkeypatch):
    """No role, no alt titles: there is nothing to ask about, and asking
    anyway spends a call to be told so."""
    called = []
    monkeypatch.setattr(companies, "call_llm",
                        lambda *a, **k: called.append(1) or [])

    assert companies.candidates_from_profile({"target": {}}) == []
    assert called == []


def test_junk_from_the_model_does_not_become_a_probe_list(monkeypatch):
    monkeypatch.setattr(companies, "call_llm",
                        lambda *a, **k: {"not": "a list"})

    result = companies.candidates_from_profile(
        {"target": {"role_family": "Product Manager"}})

    assert result == []


def test_supplied_names_skip_the_model_entirely(conn, user, monkeypatch):
    """`--names` exists so the whole feature works with no API key."""
    monkeypatch.setattr(companies, "candidates_from_profile",
                        lambda *a, **k: pytest.fail("should not be called"))
    monkeypatch.setattr(companies, "probe_slug",
                        lambda slug, timeout=12: [("lever", 7)])

    found = companies.discover_for_user(conn, user, {},
                                        extra_names=["Meesho"])

    assert found[0]["slug"] == "meesho"
    assert db.list_job_boards(conn)[0]["jobs_seen"] == 7
