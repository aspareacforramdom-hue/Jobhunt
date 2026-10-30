"""Cross-process scans and persistence regressions for the local handover."""
from core import db, personal, scheduler, constraints
import pytest
from core.scan_lock import ScanLock


def test_two_scanners_cannot_spend_concurrently(tmp_path):
    path = tmp_path / 'private.db'
    lease = ScanLock(path)
    assert lease.acquire()
    try:
        scanner = scheduler.Scanner(db_path=path)
        assert scanner.start(override=True)[0] is False
        assert scanner.status()['running'] is True
    finally:
        lease.release()
    assert not scanner.status()['running']


def test_failed_scan_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / 'private.db'
    scanner = scheduler.Scanner(db_path=path)
    def fail(*args):
        raise RuntimeError('Source failed')
    monkeypatch.setattr(scanner, '_scan', fail)
    scanner._run('manual')
    restarted = scheduler.Scanner(db_path=path)
    assert restarted.status()['error'] == 'Source failed'
    assert restarted.status()['running'] is False
    assert not restarted._process_lock.busy()


def test_rediscovery_preserves_full_description_and_salary(tmp_path):
    c = db.connect(tmp_path / 'db.sqlite')
    original = {"url": "https://bank.example/1", "description": "Full responsibilities. " * 100,
                "salary_text": "25-30 LPA", "verification": "Full description"}
    jid = db.upsert_job(c, original)
    db.upsert_job(c, {"url": original['url'], "description": "short snippet",
                       "work_mode": "Remote", "needs_enrichment": True})
    row = db.get_job(c, jid)
    assert row['description'] == original['description']
    assert '25-30 LPA' in row['metadata_json']
    assert 'Remote' in row['metadata_json']
    assert not row['needs_enrichment']
    c.close()


def test_new_candidate_evidence_invalidates_saved_rating():
    p = {"evidence": ["Led operations"], "candidate": {"years": 5}}
    before = personal.revision(p)
    p['evidence'].append('Managed a team of 10')
    assert personal.revision(p) != before


@pytest.mark.parametrize('low,high', [(30,25), (25,float('inf')), (float('nan'),30), (-5,25)])
def test_malformed_pay_remains_unverified(low, high):
    assert constraints.salary_range({'salary_min_lpa':low,'salary_max_lpa':high}) == (None,None)
