"""Access checks exercise the real ASGI boundary, including cookie writes and media."""
import hashlib
import re
import time

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import auth, config, storage
from backend.db import ApiToken, SessionLocal
from backend.main import app
from test_api import client, finish_job, jpeg_bytes, mov, s3, started, upload_api, violation_event  # noqa: F401

ORIGIN = {"Origin": "http://testserver"}


def login(client):
    client.headers.clear()
    response = client.post('/api/v1/auth/session', headers=ORIGIN, json={"token": "test-admin"})
    assert response.status_code == 204
    return response


def test_every_private_operation_denies_anonymous_before_validation(client):
    client.headers.clear()
    for path, operations in app.openapi()['paths'].items():
        if path in {'/api/v1/health', '/api/v1/auth/session'}:
            continue
        concrete = re.sub(r'\{[^}]+\}', 'missing', path)
        for method in operations:
            response = client.request(method, concrete)
            assert response.status_code == 401, (method, path, response.text)
            assert response.headers['cache-control'] == 'no-store'
    for path in ['/docs', '/redoc', '/openapi.json', '/docs/oauth2-redirect', '/api/v1/videos/']:
        assert client.get(path).status_code == 401
    assert client.get('/healthz').status_code == 200
    assert client.get('/api/v1/health').status_code == 200
    assert client.get('/api/v1/auth/session').json() == {"authenticated": False, "expires_at": None}


def test_session_cookie_reload_logout_and_no_key_in_response(client):
    response = login(client)
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Max-Age=43200' in cookie
    assert 'test-admin' not in cookie
    state = client.get('/api/v1/auth/session').json()
    assert state['authenticated'] and 43190 < state['expires_at'] - time.time() <= 43200
    assert client.get('/api/v1/videos').status_code == 200
    assert client.get('/docs').status_code == 200
    assert client.get('/api/v1/videos', headers={'X-API-Key': 'wrong'}).status_code == 401
    assert client.delete('/api/v1/auth/session', headers=ORIGIN).status_code == 204
    assert client.get('/api/v1/videos').status_code == 401


@pytest.mark.parametrize('token', ['', 'wrong', 'неверный-токен'])
def test_bad_login(client, token):
    client.headers.clear()
    response = client.post('/api/v1/auth/session', headers=ORIGIN, json={'token': token})
    assert response.status_code == 401
    assert not response.cookies


@pytest.mark.parametrize('origin', [None, 'https://evil.example', 'http://testserver.evil', 'null'])
def test_origin_required_for_login_and_cookie_mutations(client, origin):
    headers = {} if origin is None else {'Origin': origin}
    client.headers.clear()
    assert client.post('/api/v1/auth/session', headers=headers, json={'token': 'test-admin'}).status_code == 403
    login(client)
    assert client.post('/api/v1/tokens', headers=headers).status_code == 403
    assert client.delete('/api/v1/auth/session', headers=headers).status_code == 403
    assert client.post('/api/v1/tokens', headers=ORIGIN).status_code == 501
    assert client.post('/api/v1/tokens', headers={'X-API-Key': 'test-admin'}).status_code == 501


def test_expired_tampered_and_rotated_sessions(client, monkeypatch):
    client.headers.clear()
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, 'get_timestamp', lambda self: int(time.time()) - auth.SESSION_SECONDS - 10)
        expired = auth.new_session()
    client.cookies.set(auth.COOKIE_NAME, expired)
    assert client.get('/api/v1/videos').status_code == 401
    client.cookies.clear()
    login(client)
    cookie = client.cookies.get(auth.COOKIE_NAME)
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, cookie + 'bad')
    assert client.get('/api/v1/videos').status_code == 401
    client.cookies.clear()
    login(client)
    monkeypatch.setattr(config, 'ADMIN_KEY', 'rotated-key')
    assert client.get('/api/v1/videos').status_code == 401
    assert client.get('/api/v1/videos', headers={'X-API-Key': 'test-admin'}).status_code == 401
    assert client.get('/api/v1/videos', headers={'Authorization': 'Bearer rotated-key'}).status_code == 200


def test_old_bot_tokens_never_authenticate(client):
    secret = 'fva_previous-bot-token'
    with SessionLocal() as db:
        db.add(ApiToken(name='old bot', prefix=secret[:12], token_hash=hashlib.sha256(secret.encode()).hexdigest()))
        db.commit()
    client.headers.clear()
    for headers in [{'X-API-Key': secret}, {'Authorization': f'Bearer {secret}'}]:
        assert client.get('/api/v1/videos', headers=headers).status_code == 401


def test_https_cookie_and_empty_key_fail_closed(client, monkeypatch):
    monkeypatch.setattr(config, 'SESSION_COOKIE_SECURE', True)
    response = client.post('/api/v1/auth/session', headers={'Origin': 'https://testserver'}, json={'token': 'test-admin'})
    assert 'Secure' in response.headers['set-cookie']
    monkeypatch.setattr(config, 'ADMIN_KEY', '')
    assert not auth.valid_key('') and not auth.valid_session(auth.new_session())
    with pytest.raises(RuntimeError, match='ADMIN_KEY is required'):
        with TestClient(app):
            pass


def test_cookie_upload_evidence_range_and_edit(client, mov, s3, monkeypatch):
    login(client)
    with mov.open('rb') as file:
        result = client.post('/api/v1/videos', headers=ORIGIN, files={'file': ('sample.mov', file)})
    assert result.status_code == 200
    video_id, job_id = result.json()['video']['id'], result.json()['job']['id']
    finish_job(s3, job_id, [violation_event(1, job_id=job_id)])
    event = client.get(f'/api/v1/videos/{video_id}/events').json()[0]
    assert client.get(event['evidence'][0]['url']).content == jpeg_bytes()
    seen = []
    def stream(key, value):
        seen.append(value)
        return iter([b'1234']), {'Content-Range': 'bytes 2-5/10', 'Accept-Ranges': 'bytes', 'Content-Length': '4'}, 206
    monkeypatch.setattr(storage, 'stream', stream)
    response = client.get(f'/api/v1/videos/{video_id}/stream', headers={'Range': 'bytes=2-5'})
    assert response.status_code == 206 and response.content == b'1234' and seen == ['bytes=2-5']
    assert response.headers['cache-control'] == 'no-store'
    assert client.put(f'/api/v1/videos/{video_id}/boxes?t=1', headers=ORIGIN,
                      json=[{'cls': 'person', 'bbox': [1, 1, 20, 20]}]).status_code == 200
    assert client.post(f"/api/v1/events/{event['id']}/review", headers=ORIGIN,
                       json={'decision': 'approved', 'comment': 'Проверено'}).status_code == 200
