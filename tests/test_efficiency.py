"""
EFFICIENCY REGRESSIONS.

These pin properties that are invisible in normal use and would come
back silently. Each one was a measured problem before it was a test:

  board render      308 ms / 6 queries  ->  3 ms / 4 queries
  distribution      131 ms              ->  2 ms
  list payload      96% descriptions    ->  none
  ATS fan-out       ~960 ms per board, serial -> pooled

They assert SHAPE (query counts, columns fetched, concurrency), not wall
clock, so they stay honest on a slow or busy machine.
"""

import time

import pytest

from core import board, db, discover, prefilter, session


@pytest.fixture
def loaded(tmp_path):
    """A corpus big enough that O(n) mistakes show up."""
    conn = db.connect(str(tmp_path / "eff.db"))
    uid, _ = session.create_account(conn, "alice", "pw")
    db.save_profile(conn, uid, {
        "target": {"role_family": "Manager"}, "hard_filters": {},
        "weights": {"trajectory_fit": 60, "company_quality": 32,
                    "experience_fit": 8},
        "dimension_definitions": {}, "search_terms": ["Manager"]})

    description = "Responsibilities. " * 250        # ~4KB, the real cap
    for i in range(300):
        job_id = db.upsert_job(conn, {
            "url": f"https://b.test/{i}", "title": f"Manager {i}",
            "company": f"Company {i % 20}", "location": "Mumbai",
            "description": description, "source": "greenhouse",
            "found_at": db.now()})
        db.upsert_user_job(conn, uid, job_id, {
            "score": 4.0 + (i % 60) / 10,
            "score_breakdown": {"trajectory_fit": 7}})
    conn.commit()
    yield conn, uid
    conn.close()


class Queries:
    """Counts the SELECTs a block of code actually issues."""

    def __init__(self, conn):
        self.conn, self.seen = conn, []

    def __enter__(self):
        self.conn.set_trace_callback(self.seen.append)
        return self

    def __exit__(self, *a):
        self.conn.set_trace_callback(None)

    @property
    def selects(self):
        return [q for q in self.seen if q.strip().upper().startswith("SELECT")]


# ============================================================
# Query counts — the board used to issue one per column
# ============================================================
def test_the_board_does_not_query_once_per_column(loaded):
    conn, uid = loaded
    with Queries(conn) as q:
        board.build_board(conn, uid)

    assert len(q.selects) <= 4, (
        f"board issued {len(q.selects)} queries; it used to run one per "
        f"column plus one for the distribution, and that is the regression")


def test_the_board_query_count_does_not_grow_with_the_corpus(loaded):
    """The count must depend on the number of columns, not of jobs."""
    conn, uid = loaded
    with Queries(conn) as small:
        board.build_board(conn, uid)

    for i in range(300, 600):
        job_id = db.upsert_job(conn, {"url": f"https://b.test/{i}",
                                      "title": f"Manager {i}",
                                      "found_at": db.now()})
        db.upsert_user_job(conn, uid, job_id, {"score": 7.0})
    conn.commit()

    with Queries(conn) as big:
        board.build_board(conn, uid)

    assert len(big.selects) == len(small.selects)


def test_the_distribution_reads_one_column_not_whole_rows(loaded):
    conn, uid = loaded
    with Queries(conn) as q:
        board.distribution(conn, uid)

    assert len(q.selects) == 1
    sql = q.selects[0].upper()
    assert "JOIN" not in sql, "the distribution joined tables to average numbers"
    assert "DESCRIPTION" not in sql


def test_counting_does_not_fetch_the_rows(loaded):
    conn, uid = loaded
    with Queries(conn) as q:
        db.count_user_jobs(conn, uid, statuses=["new"], min_score=4.0)

    assert "COUNT(" in q.selects[0].upper()
    assert "J.TITLE" not in q.selects[0].upper()


# ============================================================
# Payload — descriptions are 4KB each and the board shows none
# ============================================================
def test_list_views_do_not_carry_descriptions(loaded):
    conn, uid = loaded
    rows = db.list_user_jobs(conn, uid)

    assert rows, "fixture produced nothing"
    assert all(not r.get("description") for r in rows), (
        "list_user_jobs is carrying 4KB descriptions again; on 800 jobs "
        "that was 96% of the payload for a view that renders none of it")


def test_the_detail_view_still_gets_the_description(loaded):
    """The saving must not cost the one page that needs the text."""
    conn, uid = loaded
    job_id = db.list_user_jobs(conn, uid, limit=1)[0]["id"]

    assert len(db.get_user_job(conn, uid, job_id)["description"]) > 1000


def test_the_board_carries_no_descriptions_either(loaded):
    conn, uid = loaded
    data = board.build_board(conn, uid)

    for column in data["columns"]:
        for job in column["jobs"]:
            assert not job.get("description")


# ============================================================
# Board semantics survived the rewrite
# ============================================================
def test_new_is_capped_but_reports_the_true_total(loaded):
    conn, uid = loaded
    data = board.build_board(conn, uid, limit=15)
    new = data["columns"][0]

    assert new["shown"] == 15
    assert new["total"] > 15, "the '15 of N' count lost its N"


def test_curated_columns_are_not_capped(loaded):
    conn, uid = loaded
    for row in db.list_user_jobs(conn, uid, limit=25):
        db.set_status(conn, uid, row["id"], "interested")

    interested = board.build_board(conn, uid)["columns"][1]
    assert interested["shown"] == 25, "hid work the user had already curated"


def test_curated_columns_stay_sorted_by_score(loaded):
    conn, uid = loaded
    for row in db.list_user_jobs(conn, uid, limit=10):
        db.set_status(conn, uid, row["id"], "applied")

    scores = [j["score"] for j in board.build_board(conn, uid)["columns"][3]["jobs"]]
    assert scores == sorted(scores, reverse=True)


def test_the_outcome_column_merges_all_its_states(loaded):
    conn, uid = loaded
    rows = db.list_user_jobs(conn, uid, limit=3)
    for row, status in zip(rows, ["rejected", "interview", "offer"]):
        db.set_status(conn, uid, row["id"], status)

    outcome = board.build_board(conn, uid)["columns"][4]
    assert outcome["shown"] == 3


def test_ignored_jobs_stay_off_the_board_and_out_of_the_spread(loaded):
    conn, uid = loaded
    before = board.distribution(conn, uid)["count"]
    for row in db.list_user_jobs(conn, uid, limit=5):
        db.ignore_job(conn, uid, row["id"], "no")

    data = board.build_board(conn, uid)
    assert data["distribution"]["count"] == before - 5
    assert all(j["status"] != "ignored"
               for c in data["columns"] for j in c["jobs"])


# ============================================================
# Network fan-out
# ============================================================
def test_every_configured_ats_board_type_is_actually_fetched():
    """`ashby:` sat in sources.yml for months with no fetcher behind it —
    adding slugs produced silence, not listings."""
    import yaml

    configured = set(yaml.safe_load(
        open("config/sources.yml", encoding="utf-8"))["ats_targets"])

    for board in configured:
        assert hasattr(discover, f"from_{board}"), (
            f"sources.yml offers an '{board}:' list but there is no "
            f"discover.from_{board} — slugs added there do nothing")


def test_all_three_board_types_reach_the_fan_out(monkeypatch):
    seen = []
    for board in ("greenhouse", "lever", "ashby"):
        monkeypatch.setattr(discover, f"from_{board}",
                            lambda slug, b=board: seen.append(b) or [])

    discover._fetch_ats({"greenhouse": ["a"], "lever": ["b"], "ashby": ["c"]})

    assert sorted(seen) == ["ashby", "greenhouse", "lever"]


def test_ats_boards_are_fetched_concurrently(monkeypatch):
    """Serial fetching cost ~960ms per board and landed on the boot scan."""
    live = {"now": 0, "peak": 0}

    def slow(slug):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.15)
        live["now"] -= 1
        return [{"url": f"https://x/{slug}", "title": slug}]

    monkeypatch.setattr(discover, "from_greenhouse", slow)
    monkeypatch.setattr(discover, "from_lever", slow)

    started = time.perf_counter()
    rows = discover._fetch_ats({"greenhouse": [f"c{i}" for i in range(6)],
                                "lever": ["d1", "d2"]})
    elapsed = time.perf_counter() - started

    assert len(rows) == 8
    assert live["peak"] > 1, "boards were still fetched one at a time"
    assert elapsed < 8 * 0.15, "no wall-clock benefit from the pool"


def test_one_dead_slug_does_not_take_down_the_tier(monkeypatch):
    def flaky(slug):
        if slug == "gone":
            raise RuntimeError("404")
        return [{"url": f"https://x/{slug}", "title": slug}]

    monkeypatch.setattr(discover, "from_greenhouse", flaky)
    monkeypatch.setattr(discover, "from_lever", flaky)

    rows = discover._fetch_ats({"greenhouse": ["ok1", "gone", "ok2"]})
    assert len(rows) == 2


def test_no_ats_targets_makes_no_threads(monkeypatch):
    assert discover._fetch_ats({}) == []
    assert discover._fetch_ats({"greenhouse": [], "lever": None}) == []


# ============================================================
# Prefilter
# ============================================================
def test_prefilter_builds_the_token_set_once_not_per_listing(monkeypatch):
    profile = {"target": {"role_family": "Brand Manager",
                          "alt_titles": ["Brand Lead"]},
               "hard_filters": {}, "search_terms": ["Brand Manager"]}
    calls = {"n": 0}
    real = prefilter._tokens

    def counted(phrases):
        calls["n"] += 1
        return real(phrases)

    monkeypatch.setattr(prefilter, "_tokens", counted)
    jobs = [{"url": f"u{i}", "title": "Brand Manager", "description": "d"}
            for i in range(200)]

    prefilter.prefilter(jobs, profile)

    assert calls["n"] == 1, (
        f"rebuilt the target token set {calls['n']} times for 200 listings")
