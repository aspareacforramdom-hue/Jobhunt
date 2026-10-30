"""Single-user handover regressions; all services stay mocked."""
import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from core import db, personal, tailor
from web.app import create_app


@pytest.fixture
def private_app(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(personal, 'PROFILE_PATH', tmp_path / 'missing-profile.json')
    path = tmp_path / 'private.db'
    conn = db.connect(path)
    owner = personal.ensure_owner(conn)
    profile = {'hard_filters': {'locations': ['Bengaluru'], 'min_ctc_lpa': 25},
               'weights': {'trajectory_fit': 80, 'experience_fit': 20},
               'resume_text': 'Led client service operations.'}
    db.save_profile(conn, owner['id'], profile)
    jid = db.upsert_job(conn, {'url': 'https://example.com/operations',
                              'title': 'Service Operations Manager', 'company': 'Example Bank',
                              'location': 'Bengaluru', 'description': 'Customer service operations. ' * 30})
    db.upsert_user_job(conn, owner['id'], jid,
                       {'score': 7, 'profile_revision': personal.revision(profile),
                        'score_breakdown': {'trajectory_fit': 7, 'experience_fit': 7},
                        'evaluation': {'evidence_coverage': 60}})
    conn.close()
    app = create_app(db_path=path, single_user=True)
    status = {'running': False, 'phase': '', 'found': 0, 'last_found': 0,
              'last_finished': None, 'cooldown': 0, 'error': None, 'notes': []}
    monkeypatch.setattr(app.state.scanner, 'status', lambda: dict(status))
    monkeypatch.setattr(app.state.scanner, 'start', lambda *a, **k: pytest.fail('Unexpected scan'))
    monkeypatch.setattr(tailor, 'prepare_application', lambda *a: pytest.fail('Unexpected AI call'))
    return app, path, owner['id'], jid, status


def test_missing_profile_renders_recovery_message(tmp_path, monkeypatch):
    monkeypatch.setattr(personal, 'PROFILE_PATH', tmp_path / 'absent.json')
    with TestClient(create_app(db_path=tmp_path / 'empty.db', single_user=True)) as client:
        response = client.get('/')
        assert response.status_code == 200
        assert 'Load the private candidate profile' in response.text


def test_draft_generation_without_api_is_clear_and_non_spending(private_app):
    app, path, uid, jid, _ = private_app
    with TestClient(app) as client:
        detail = client.get(f'/jobs/{jid}')
        assert 'Draft generation is unavailable' in detail.text
        assert 'Prepare application</button>' not in detail.text
        response = client.post(f'/jobs/{jid}/prepare')
        assert response.status_code == 200
        assert 'Draft generation is unavailable' in response.text
        # Manual application tracking still works without a generated draft.
        client.post(f'/jobs/{jid}/status', data={'status': 'applied', 'back': '/applications'})
        tracked = client.get('/applications').text
        assert 'Application tracked' in tracked
        assert f'href="/jobs/{jid}"' in tracked


def test_saved_search_filters_before_display_limit(private_app):
    app, path, uid, jid, _ = private_app
    conn = db.connect(path)
    revision = personal.revision(db.load_profile(conn, uid))
    for i in range(201):
        added = db.upsert_job(conn, {'url': f'https://example.com/{i}', 'title': f'Role {i}', 'company': 'Bank'})
        db.upsert_user_job(conn, uid, added, {'score': 9, 'profile_revision': revision})
    db.set_status(conn, uid, jid, 'interested')
    conn.close()
    with TestClient(app) as client:
        response = client.get('/?status=interested')
        assert 'Service Operations Manager' in response.text
        assert '1 of 1 matching leads shown' in response.text


def test_invalid_constructor_never_touches_database(monkeypatch):
    monkeypatch.setattr(db, 'connect', lambda *a: pytest.fail('Database touched before validation'))
    with pytest.raises(TypeError):
        create_app('wrong-positional-path')


def test_single_user_browser_routes_and_completed_scan(private_app, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    import uvicorn
    app, _, _, jid, status = private_app
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)
    assert server.started
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            base = f'http://127.0.0.1:{port}'
            for width in (390, 1400):
                page.set_viewport_size({'width': width, 'height': 900})
                for route in ('/', '/sources', '/applications', '/ignored', '/jobs', f'/jobs/{jid}'):
                    assert page.goto(base + route).status == 200
                    assert not page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth'), (width, route)
                page.goto(base)
                page.screenshot(path=str(tmp_path / f'dashboard-{width}.png'), full_page=True)
            status.update(running=True, phase='Scoring', found=3)
            page.goto(base)
            assert page.locator('#scan-btn').is_disabled()
            assert page.get_by_role('button', name='Free discovery').is_disabled()
            status.update(running=False, last_finished='2026-09-28T12:00:00+00:00', last_found=3)
            page.wait_for_function("!document.querySelector('#scan-btn').disabled", timeout=10000)
            page.wait_for_load_state('networkidle')
            assert page.get_by_role('button', name='Free discovery').is_enabled()
            assert 'Scanning' not in page.locator('#scan-state').inner_text()
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        worker.join(timeout=10)
