"""§15 security addendum — server administration authentication.

Mutating routes (POST/PUT/DELETE) must require an admin credential when the
server is configured with one; read-only GETs stay public so the game client
can poll world state. Credential comparison is constant time, and GPU render
workers get their own weaker token scoped to image-task endpoints only.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poem.api import (
    ADMIN_TOKEN_ENV,
    AUTH_HEADER,
    WORKER_TOKEN_ENV,
    WorldServer,
    generate_admin_token,
)
from poem.chronology import Civilization

DAY = datetime(2026, 10, 10, tzinfo=timezone.utc)
ADMIN = "s3cr3t-admin-token"


def make_server(**kw):
    kw.setdefault("admin_token", ADMIN)
    srv = WorldServer("w1", **kw)
    srv.add_civilization(Civilization(id="c1", name="Rivers", progress=0.25))
    return srv


LAW_BODY = {
    "id": "law-tax", "civilization_id": "c1", "title": "Head tax",
    "raw_text": "Every settlement pays 1 unit per hearth.",
    "canonical": {"kind": "tax"}, "complexity": 1.5,
}


# ---------------------------------------------------------------------------
# Unauthenticated / wrongly authenticated mutating requests are rejected
# ---------------------------------------------------------------------------

def test_post_without_token_is_401():
    srv = make_server()
    r = srv.handle("POST", "/api/v1/laws", LAW_BODY)
    assert r["status"] == 401
    # nothing was created
    assert srv.handle("GET", "/api/v1/laws")["laws"] == []


def test_put_delete_also_guarded():
    srv = make_server()
    assert srv.handle("PUT", "/api/v1/laws/drafts/x", {})["status"] == 401
    assert srv.handle("DELETE", "/api/v1/laws/drafts/x", {})["status"] == 401


def test_wrong_token_is_403():
    srv = make_server()
    r = srv.handle("POST", "/api/v1/laws", LAW_BODY,
                   headers={AUTH_HEADER: "wrong-token"})
    assert r["status"] == 403


def test_get_routes_stay_public():
    srv = make_server()
    assert srv.handle("POST", "/api/v1/laws", LAW_BODY,
                      headers={AUTH_HEADER: ADMIN})["status"] == 200
    # reads need no credential at all
    assert srv.handle("GET", "/api/v1/laws")["status"] == 200
    assert srv.handle("GET", "/api/v1/worlds/w1/chronology")["status"] == 200
    assert srv.handle("GET", "/api/v1/laws/law-tax")["status"] == 200


# ---------------------------------------------------------------------------
# Accepted credential spellings
# ---------------------------------------------------------------------------

def test_bearer_header_accepted():
    srv = make_server()
    r = srv.handle("POST", "/api/v1/laws", LAW_BODY,
                   headers={"Authorization": f"Bearer {ADMIN}"})
    assert r["status"] == 200


def test_x_auth_header_accepted():
    srv = make_server()
    r = srv.handle("POST", "/api/v1/laws", LAW_BODY,
                   headers={AUTH_HEADER: ADMIN})
    assert r["status"] == 200


def test_body_token_accepted_for_frameworks_without_headers():
    srv = make_server()
    body = dict(LAW_BODY, auth_token=ADMIN)
    r = srv.handle("POST", "/api/v1/laws", body)
    assert r["status"] == 200


def test_case_insensitive_headers():
    srv = make_server()
    r = srv.handle("POST", "/api/v1/laws", LAW_BODY,
                   headers={"x-auth-token": ADMIN})
    assert r["status"] == 200


# ---------------------------------------------------------------------------
# Turn engine & diplomacy routes are admin-only too
# ---------------------------------------------------------------------------

def test_turn_run_requires_admin():
    srv = make_server()
    assert srv.handle("POST", "/api/v1/worlds/w1/turns/open",
                      {"started_at": DAY.isoformat(),
                       "simulation_cutoff_at": DAY.isoformat()}
                      )["status"] == 401
    r = srv.handle("POST", "/api/v1/worlds/w1/turns/open",
                   {"started_at": DAY.isoformat(),
                    "simulation_cutoff_at": DAY.isoformat()},
                   headers={AUTH_HEADER: ADMIN})
    assert r["status"] == 200
    assert srv.handle("POST", "/api/v1/worlds/w1/turns/run", {})[
        "status"] == 401
    assert srv.handle("POST", "/api/v1/worlds/w1/turns/run", {},
                      headers={AUTH_HEADER: ADMIN})["status"] == 200


def test_war_declaration_requires_admin():
    srv = make_server()
    body = {"world_id": "w1", "attacker_id": "c1", "defender_id": "c9"}
    assert srv.handle("POST", "/api/v1/wars", body)["status"] == 401
    assert srv.handle("POST", "/api/v1/wars", body,
                      headers={AUTH_HEADER: ADMIN})["status"] == 200


# ---------------------------------------------------------------------------
# Open server (no token configured) keeps working — tests & local play
# ---------------------------------------------------------------------------

def test_open_server_when_no_token(monkeypatch):
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    srv = WorldServer("w-open")            # admin_token defaults to ""
    srv.add_civilization(Civilization(id="c1", name="R", progress=0.25))
    assert srv.handle("POST", "/api/v1/laws", LAW_BODY)["status"] == 200


def test_env_var_configuration(monkeypatch):
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "from-env")
    monkeypatch.delenv(WORKER_TOKEN_ENV, raising=False)
    srv = WorldServer("w-env")
    assert srv.handle("POST", "/api/v1/laws", LAW_BODY)["status"] == 401
    assert srv.handle("POST", "/api/v1/laws", LAW_BODY,
                      headers={AUTH_HEADER: "from-env"})["status"] == 200


# ---------------------------------------------------------------------------
# GPU worker token: scoped rights, admin still works everywhere
# ---------------------------------------------------------------------------

def _queue_task(srv, task_id="task-1"):
    t = _Task()
    t.id = task_id
    srv.engine.image_queue.append(t)


class _Task:
    def __init__(self):
        self.id = "task-1"
        self.world_id = "w1"
        self.turn_id = "t1"
        self.status = "queued"
        self.prompt = "a ford at dawn"
        self.asset_ref = None
        self.payload = {}
        self.claimed_by = None
        self.lease_expires_at = None
        self.created_at = None
        self.started_at = None
        self.attempts = 0

    def to_json(self):
        return {"id": self.id, "status": self.status}


def test_worker_token_can_claim_but_not_rewrite_laws():
    monkey_srv = make_server(worker_token="worker-key")
    _queue_task(monkey_srv)
    r = monkey_srv.handle("POST", "/api/v1/images/task-1/claim",
                          {"worker_id": "gpu-7"},
                          headers={"X-Worker-Token": "worker-key"})
    assert r["status"] == 200
    # worker key must NOT unlock law editing
    r2 = monkey_srv.handle("POST", "/api/v1/laws", LAW_BODY,
                           headers={"X-Worker-Token": "worker-key"})
    assert r2["status"] == 401 or r2["status"] == 403
    # full admin token still works on worker endpoints
    _queue_task(monkey_srv, task_id="task-2")
    r3 = monkey_srv.handle("POST", "/api/v1/images/task-2/claim",
                           {"worker_id": "gpu-8"},
                           headers={AUTH_HEADER: ADMIN})
    assert r3["status"] == 200


def test_generate_admin_token_is_strong_and_unique():
    t1, t2 = generate_admin_token(), generate_admin_token()
    assert t1 != t2
    assert len(t1) >= 32


def test_empty_token_never_matches_configured_admin():
    srv = make_server()
    # empty-string credentials must not pass a configured check
    assert srv.handle("POST", "/api/v1/laws", LAW_BODY,
                      headers={AUTH_HEADER: ""})["status"] == 401
