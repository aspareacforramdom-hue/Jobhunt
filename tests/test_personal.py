import json

import pytest
from fastapi.testclient import TestClient

from core import constraints, db, personal
from core.enrich import description, public_url
from web.app import create_app


def test_local_dashboard_without_login(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"target":{"role":"Operations Manager"},"hard_filters":{"locations":["Bengaluru"],"min_ctc_lpa":25}}))
    monkeypatch.setattr(personal, "PROFILE_PATH", profile_path)
    app = create_app(boot_scan=False, db_path=tmp_path / "local.db", single_user=True)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert "Dashboard" in client.get("/").text
        assert 'href="/logout"' not in client.get("/").text
        assert client.post("/scan", headers={"Origin":"https://evil.example"}).status_code == 403
        assert client.get("/", headers={"Host":"evil.example"}).status_code == 403
        response = client.post("/preferences", data={"location":"remote","salary":"25"})
        assert response.status_code == 200
        assert json.loads(profile_path.read_text())["hard_filters"]["locations"] == ["Remote India"]
        response = client.post("/preferences", data={"location":"both","salary":"25"})
        assert response.status_code == 200
        updated = json.loads(profile_path.read_text())
        assert updated["hard_filters"]["locations"] == [
            "Mumbai, India", "Bengaluru, India", "Remote India"]
        assert any("Confirm Mumbai relocation" in s for s in updated["uncertainties"])
        assert constraints.assess({"title":"Operations Manager", "location":"Mumbai"}, updated)["eligible"]
        assert "Mumbai, India" in client.get("/sources").text
        assert client.post("/preferences",data={"location":"remote","salary":"nan"}).status_code == 400


def test_shared_database_refused(tmp_path):
    conn = db.connect(tmp_path / "shared.db")
    db.create_user(conn,"someone","hash","salt")
    with pytest.raises(RuntimeError): personal.ensure_owner(conn)
    conn.close()


def test_dealbreakers_distinguish_unknowns():
    p={"hard_filters":{"locations":["Bengaluru","Remote India"],"min_ctc_lpa":25,"no_sales":True}}
    j={"title":"Operations Manager","location":"Bangalore","description":"No sales targets. " + "Customer operations. "*30}
    result=constraints.assess(j,p)
    assert result["eligible"]
    assert result["salary"] == "Salary unverified"
    assert not constraints.assess(dict(j,salary_max_lpa=20),p)["eligible"]
    assert not constraints.assess(dict(j,location="Mumbai"),p)["eligible"]
    assert not constraints.assess(dict(j,location="Remote USA"),p)["eligible"]
    assert not constraints.assess(dict(j,description="Own sales targets"),p)["eligible"]


def test_enrichment_extracts_real_description():
    assert description('<script type="application/ld+json">{"@type":"JobPosting","description":"<p>Real duties</p>"}</script>') == "Real duties"
    assert description('<div class="show-more-less-html__markup">Client operations</div>') == "Client operations"
    assert description('<html>Marketing navigation only</html>') == ""
    with pytest.raises(ValueError): public_url("http://127.0.0.1/")


def test_outcome_and_metadata_roundtrip(tmp_path):
    conn=db.connect(tmp_path / "local.db")
    u=personal.ensure_owner(conn)
    jid=db.upsert_job(conn,{"url":"https://example.com/job","title":"Operations","salary_max_lpa":30})
    db.upsert_user_job(conn,u["id"],jid,{"score":6,"evaluation":{"salary":"30 LPA"},"profile_revision":"abc"})
    db.set_status(conn,u["id"],jid,"applied")
    job=db.get_user_job(conn,u["id"],jid)
    assert job["salary_max_lpa"] == 30
    assert job["profile_revision"] == "abc"
    assert db.get_application(conn,u["id"],jid)["outcome"] == "applied"
    assert conn.execute("SELECT count(*) FROM outcome_events").fetchone()[0] == 1
    conn.close()


def test_personal_pages_and_actions_end_to_end(tmp_path, monkeypatch):
    profile_path = tmp_path / 'profile.json'
    profile_path.write_text(json.dumps({'hard_filters': {'locations': ['Bengaluru'], 'min_ctc_lpa':25}}))
    monkeypatch.setattr(personal, 'PROFILE_PATH', profile_path)
    path = tmp_path / 'personal.db'
    conn = db.connect(path)
    owner = personal.bootstrap(conn)
    jid = db.upsert_job(conn, {'url':'https://example.com/job','title':'Operations Manager',
                              'company':None,'location':'Bengaluru','description':'Customer service. '*30})
    db.upsert_user_job(conn,owner['id'],jid,{'score':7,'evaluation':{'evidence_coverage':57}})
    conn.close()
    app = create_app(boot_scan=False,db_path=path,single_user=True)
    started = []
    monkeypatch.setattr(app.state.scanner,'start',lambda kind, **kwargs:(started.append(kind) or True,'Started'))
    with TestClient(app) as client:
        for url in ['/', '/?q=operations','/jobs','/sources','/sources?p=-2','/ignored','/applications',
                    '/export.xlsx', '/scan/status', f'/jobs/{jid}',f'/applications/{jid}','/intake','/login']:
            assert client.get(url).status_code == 200, url
        assert client.post(f'/jobs/{jid}/status', data={'status':'interested'}).status_code == 200
        assert client.post(f'/jobs/{jid}/ignore',data={'reason':'test'}).status_code == 200
        assert client.post(f'/jobs/{jid}/restore').status_code == 200
        assert client.post(f'/jobs/{jid}/reviews').status_code == 200
        assert client.post('/scan',data={'mode':'discover'}).status_code == 200
        assert started == ['discover']


def test_discovery_only_never_scores_or_spends(tmp_path, monkeypatch):
    from core import scheduler
    path = tmp_path / 'db.sqlite'
    conn = db.connect(path)
    owner = personal.ensure_owner(conn)
    db.save_profile(conn, owner['id'], {'search_terms':['operations'], 'hard_filters':{}})
    config = tmp_path / 'sources.yml'
    config.write_text('jobspy: {}\napify: {}\n')
    scanner = scheduler.Scanner(db_path=path,sources_path=str(config))
    def discover(sources, dry=False):
        assert dry is True
        return [{'url':'https://example.com/job','title':'Operations Manager'}], []
    monkeypatch.setattr(scheduler.discover,'discover_all',discover)
    monkeypatch.setattr(scheduler.score, 'score_jobs',
                        lambda *a, **kw: pytest.fail('Discovery cannot spend AI tokens'))
    scanner._scan(conn,'discover')
    assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    conn.close()


def test_dashboard_hides_stale_new_scores_but_keeps_saved_jobs(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"target": {"role": "Operations Manager"},
                                        "hard_filters": {}}))
    monkeypatch.setattr(personal, "PROFILE_PATH", profile_path)
    path = tmp_path / "personal.db"
    conn = db.connect(path)
    owner = personal.bootstrap(conn)
    current = personal.revision(db.load_profile(conn, owner["id"]))
    for suffix, revision, status in (("current", current, "new"),
                                     ("stale", "old", "new"),
                                     ("saved", "old", "interested")):
        jid = db.upsert_job(conn, {"url": f"https://example.com/{suffix}",
                                  "title": suffix.title(), "company": "Bank"})
        db.upsert_user_job(conn, owner["id"], jid,
                           {"score": 6, "profile_revision": revision})
        if status != "new":
            db.set_status(conn, owner["id"], jid, status)
    conn.close()
    with TestClient(create_app(boot_scan=False, db_path=path, single_user=True)) as client:
        page = client.get("/").text
    assert "Current" in page
    assert "Saved" in page
    assert "Stale" not in page


def test_saved_draft_reuses_existing_work(tmp_path, monkeypatch):
    from core import tailor
    path = tmp_path / 'db.sqlite'
    monkeypatch.setattr(personal,'PROFILE_PATH',tmp_path/'absent.json')
    conn=db.connect(path)
    user=personal.ensure_owner(conn)
    jid=db.upsert_job(conn,{'url':'https://example.com/job','title':'Operations','company':'Bank'})
    db.upsert_user_job(conn,user['id'],jid,{'score':7})
    db.save_application(conn,user['id'],jid,{'headline':'Existing draft'}, {})
    conn.close()
    monkeypatch.setattr(tailor,'prepare_application',lambda *a:pytest.fail('Saved draft must avoid AI'))
    with TestClient(create_app(boot_scan=False,db_path=path,single_user=True)) as client:
        result=client.post(f'/jobs/{jid}/prepare')
        assert result.status_code == 200
        assert 'Existing draft' in result.text


def test_unrated_leads_visible_and_paginated(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"hard_filters": {"locations": ["Bengaluru"]}}))
    monkeypatch.setattr(personal, "PROFILE_PATH", profile_path)
    path = tmp_path / "pending.db"
    app = create_app(boot_scan=False, db_path=path, single_user=True)
    conn = db.connect(path)
    uid = db.all_users(conn)[0]["id"]
    for i in range(205):
        jid = db.upsert_job(conn, {"url": f"https://example.com/{i}", "title": f"Operations Manager {i}", "location": "Bengaluru"})
        db.upsert_user_job(conn, uid, jid, {"verdict": "Awaiting evidence rating"}, commit=False)
    conn.commit()
    conn.close()
    with TestClient(app) as client:
        first = client.get("/?status=unrated")
        assert first.status_code == 200
        assert "200 of 205" in first.text
        assert "Unrated</span>" in first.text
        second = client.get("/?status=unrated&p=2")
        assert "5 of 205" in second.text
        assert "Unrated" in client.get(f"/jobs/{jid}").text
        assert "Earlier assessment:" not in client.get(f"/jobs/{jid}").text
