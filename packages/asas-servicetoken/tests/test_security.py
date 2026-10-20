"""The FastAPI dependency: one generic 401 to the caller, the reason in the log."""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from asas_servicetoken import Claims, Issuer, Verifier, require_service_token

SECRET = "a-shared-secret-of-at-least-thirty-two-bytes"


def build_app(verifier=None) -> FastAPI:
    app = FastAPI()
    v = verifier or Verifier(SECRET, issuer="engine", audience="core")

    @app.get("/internal")
    def internal(claims: Claims = Depends(require_service_token(v))) -> dict:
        return {"caller": claims.subject}

    return app


def test_a_peer_holding_the_secret_is_admitted_and_named() -> None:
    engine = Issuer(SECRET, issuer="engine", audience="core")
    client = TestClient(build_app())

    response = client.get("/internal", headers=engine.headers())

    assert response.status_code == 200
    assert response.json() == {"caller": "engine"}


def test_no_header_is_a_401_with_a_challenge() -> None:
    response = TestClient(build_app()).get("/internal")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"detail": "A service token is required."}


def test_the_caller_is_told_nothing_it_can_use_but_the_log_is(caplog) -> None:
    stranger = Issuer("someone-elses-secret-of-thirty-two-bytes", issuer="engine", audience="core")
    token = stranger.mint()

    with caplog.at_level(logging.WARNING, logger="asas_servicetoken"):
        response = TestClient(build_app()).get("/internal", headers=stranger.headers())

    assert response.status_code == 401
    assert response.json() == {"detail": "A service token is required."}
    assert "signature" in caplog.text
    assert "GET /internal" in caplog.text
    assert token not in caplog.text


def test_the_wrong_audience_is_refused_the_same_way() -> None:
    other = Issuer(SECRET, issuer="engine", audience="someone-else")

    response = TestClient(build_app()).get("/internal", headers=other.headers())

    assert response.status_code == 401


def test_an_unset_secret_fails_closed() -> None:
    engine = Issuer(SECRET, issuer="engine", audience="core")
    app = build_app(Verifier("", issuer="engine", audience="core"))

    response = TestClient(app).get("/internal", headers=engine.headers())

    assert response.status_code == 401


def test_a_callable_verifier_is_read_per_request() -> None:
    """A host whose settings are live hands a factory, and a secret rotated
    between two requests is honoured on the second."""
    live = {"secret": "first-secret-of-at-least-thirty-two-bytes"}
    app = build_app(lambda: Verifier(live["secret"], issuer="engine", audience="core"))
    client = TestClient(app)
    first = Issuer(live["secret"], issuer="engine", audience="core").headers()

    assert client.get("/internal", headers=first).status_code == 200
    live["secret"] = "second-secret-of-at-least-thirty-two-bytes"
    assert client.get("/internal", headers=first).status_code == 401
    second = Issuer(live["secret"], issuer="engine", audience="core").headers()
    assert client.get("/internal", headers=second).status_code == 200


def test_the_detail_is_the_hosts_wording() -> None:
    app = FastAPI()
    v = Verifier(SECRET)

    @app.get("/x", dependencies=[Depends(require_service_token(v, detail="An engine token is required."))])
    def x() -> dict:
        return {}

    assert TestClient(app).get("/x").json() == {"detail": "An engine token is required."}
