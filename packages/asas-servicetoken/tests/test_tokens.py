"""The token itself: what an issuer stamps and what a verifier accepts.

Two services on two hosts never share a clock. The tests here move one side's
clock through its injected ``clock`` and leave the other on real time, which
is how a verifier that sits behind (or ahead of) the issuer is reproduced
without touching PyJWT's own reading of the time.
"""

from __future__ import annotations

import hashlib
import logging
import time

import jwt
import pytest

import asas_servicetoken
from asas_servicetoken import (
    REFUSAL_REASONS,
    Issuer,
    SecretUnset,
    TokenRefused,
    Verifier,
    bearer_token,
    fingerprint,
)

SECRET = "a-shared-secret-of-at-least-thirty-two-bytes"


def issuer(**kw) -> Issuer:
    kw.setdefault("issuer", "core")
    kw.setdefault("audience", "engine")
    return Issuer(SECRET, **kw)


def verifier(**kw) -> Verifier:
    kw.setdefault("issuer", "core")
    kw.setdefault("audience", "engine")
    return Verifier(SECRET, **kw)


def claims_of(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


# ── round trip ───────────────────────────────────────────────────────────────


def test_a_minted_token_verifies_and_carries_its_claims() -> None:
    claims = verifier().verify(issuer().mint(claims={"tenant": "t1"}))

    assert claims.subject == "core"
    assert claims.issuer == "core"
    assert claims.audience == "engine"
    assert claims.get("tenant") == "t1"
    assert claims.issued_at is not None and claims.expires_at is not None


def test_the_subject_may_differ_from_the_issuer() -> None:
    claims = verifier().verify(issuer(subject="core-worker-3").mint())

    assert claims.issuer == "core"
    assert claims.subject == "core-worker-3"


def test_extra_claims_cannot_override_the_registered_ones() -> None:
    token = issuer().mint(claims={"iss": "impostor", "sub": "impostor", "exp": 1})

    assert claims_of(token)["iss"] == "core"
    assert claims_of(token)["sub"] == "core"
    assert claims_of(token)["exp"] > 1


# ── the clock rules ──────────────────────────────────────────────────────────


def test_iat_is_backdated_so_a_peer_clock_behind_ours_still_accepts_it() -> None:
    before = int(time.time())

    claims = claims_of(issuer().mint())

    assert claims["iat"] <= before - asas_servicetoken.DEFAULT_BACKDATE_S
    assert asas_servicetoken.DEFAULT_BACKDATE_S > 0


def test_the_token_still_lives_its_full_ttl() -> None:
    """exp is measured from now, never from the backdated iat."""
    before = int(time.time())

    claims = claims_of(issuer().mint())

    assert claims["exp"] >= before + asas_servicetoken.DEFAULT_TTL_S
    assert claims["exp"] - claims["iat"] == (
        asas_servicetoken.DEFAULT_TTL_S + asas_servicetoken.DEFAULT_BACKDATE_S
    )


def test_an_extra_backdate_moves_iat_only() -> None:
    now = 1_800_000_000
    plain = claims_of(issuer(clock=lambda: now).mint())
    deeper = claims_of(issuer(clock=lambda: now).mint(extra_backdate_s=300))

    assert deeper["iat"] == plain["iat"] - 300
    assert deeper["exp"] == plain["exp"], "a deeper backdate buys no extra life"


def test_the_backdate_is_configurable() -> None:
    before = int(time.time())

    claims = claims_of(issuer(backdate_s=600).mint())

    assert claims["iat"] <= before - 600


def test_an_issuer_clock_ahead_of_the_verifier_is_immature_past_the_leeway() -> None:
    ahead = issuer(clock=lambda: time.time() + 200)  # 200 s ahead, minus the 30 s backdate

    with pytest.raises(TokenRefused) as refused:
        verifier(leeway_s=60).verify(ahead.mint())
    assert refused.value.reason == "immature"

    assert verifier(leeway_s=300).verify(ahead.mint()).subject == "core"


def test_a_verifier_well_behind_the_issuer_still_accepts_within_the_backdate() -> None:
    """The backdate alone, with NO leeway on the verifier: the belt for a peer
    that has not picked the leeway up yet."""
    slightly_ahead = issuer(clock=lambda: time.time() + 20)

    assert verifier(leeway_s=0).verify(slightly_ahead.mint()).subject == "core"


def test_an_expired_token_is_expired() -> None:
    old = issuer(clock=lambda: time.time() - 1000)

    with pytest.raises(TokenRefused) as refused:
        verifier().verify(old.mint())
    assert refused.value.reason == "expired"


# ── the other refusals ───────────────────────────────────────────────────────


def test_every_refusal_names_its_reason() -> None:
    cases = {
        "missing": ("", verifier()),
        "signature": (Issuer("another-secret-of-at-least-thirty-two-bytes", issuer="core", audience="engine").mint(), verifier()),
        "issuer": (issuer(issuer="someone-else").mint(), verifier()),
        "audience": (issuer(audience="someone-else").mint(), verifier()),
        "claims": (jwt.encode({"iss": "core", "aud": "engine"}, SECRET, algorithm="HS256"), verifier()),
        "malformed": ("not.a.jwt", verifier()),
        "secret_unset": (issuer().mint(), Verifier("", issuer="core", audience="engine")),
    }
    for reason, (token, v) in cases.items():
        with pytest.raises(TokenRefused) as refused:
            v.verify(token)
        assert refused.value.reason == reason, reason
        assert reason in REFUSAL_REASONS


def test_a_refusal_never_carries_the_token() -> None:
    token = issuer(audience="someone-else").mint()

    with pytest.raises(TokenRefused) as refused:
        verifier().verify(token)

    assert token not in str(refused.value)
    assert token not in refused.value.detail


def test_an_unknown_reason_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        TokenRefused("tuesday")


def test_a_verifier_that_names_no_audience_ignores_the_claim() -> None:
    assert verifier(audience=None).verify(issuer().mint()).audience == "engine"
    assert verifier(audience=None).verify(issuer(audience=None).mint()).audience is None


def test_a_verifier_that_names_no_issuer_ignores_the_claim() -> None:
    assert verifier(issuer=None).verify(issuer(issuer="whoever").mint()).issuer == "whoever"


def test_required_claims_are_the_hosts_choice() -> None:
    bare = jwt.encode({"sub": "x", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")

    with pytest.raises(TokenRefused) as refused:
        Verifier(SECRET).verify(bare)  # iat required by default
    assert refused.value.reason == "claims"

    assert Verifier(SECRET, require=("exp", "sub")).verify(bare).subject == "x"


def test_a_token_signed_with_another_algorithm_is_refused() -> None:
    token = jwt.encode(
        {"iss": "core", "aud": "engine", "sub": "core", "iat": int(time.time()), "exp": int(time.time()) + 60},
        SECRET * 2,  # HS512 wants 64 bytes; the verifier is given the same bytes below
        algorithm="HS512",
    )

    with pytest.raises(TokenRefused) as refused:
        Verifier(SECRET * 2, issuer="core", audience="engine").verify(token)
    assert refused.value.reason == "malformed"


# ── the secret ───────────────────────────────────────────────────────────────


def test_a_callable_secret_is_read_on_every_call() -> None:
    """A rotated secret needs no restart on either side."""
    live = {"value": "first-secret-of-at-least-thirty-two-bytes"}
    i = Issuer(lambda: live["value"], issuer="core", audience="engine")
    v = Verifier(lambda: live["value"], issuer="core", audience="engine")

    before = i.mint()
    live["value"] = "second-secret-of-at-least-thirty-two-bytes"
    after = i.mint()

    assert v.verify(after).subject == "core"
    with pytest.raises(TokenRefused) as refused:
        v.verify(before)
    assert refused.value.reason == "signature"


def test_an_unset_secret_cannot_mint() -> None:
    with pytest.raises(SecretUnset) as unset:
        Issuer("", issuer="core", unset_hint="CORE_ENGINE_SHARED_SECRET").mint()

    assert "CORE_ENGINE_SHARED_SECRET" in str(unset.value)
    assert unset.value.hint == "CORE_ENGINE_SHARED_SECRET"


def test_an_unset_secret_sends_no_header_and_says_so_once(caplog) -> None:
    """The shape an unresolved vault reference produces: no header, every
    call refused, and (now) one log line naming the setting."""
    i = Issuer(lambda: "", issuer="core", audience="engine", unset_hint="CORE_ENGINE_SHARED_SECRET")

    with caplog.at_level(logging.ERROR, logger="asas_servicetoken"):
        first = i.headers()
        second = i.headers()

    assert first == {} and second == {}
    assert caplog.text.count("CORE_ENGINE_SHARED_SECRET") == 1
    assert "engine" in caplog.text


def test_headers_carry_a_bearer_token_that_verifies() -> None:
    headers = issuer().headers()

    assert set(headers) == {"Authorization"}
    assert verifier().verify(bearer_token(headers["Authorization"])).subject == "core"


def test_the_fingerprint_is_short_stable_and_not_the_secret() -> None:
    assert fingerprint(SECRET) == "sha256:" + hashlib.sha256(SECRET.encode()).hexdigest()[:12]
    assert fingerprint(SECRET) == fingerprint(SECRET)
    assert fingerprint(SECRET) != fingerprint(SECRET + "x")
    assert SECRET not in fingerprint(SECRET)
    assert fingerprint("") == "<unset>"


# ── the diagnostics ──────────────────────────────────────────────────────────


def test_describe_states_the_facts_and_never_the_secret() -> None:
    line = issuer(clock=lambda: 1_800_000_000).describe()

    assert "clock reads 1800000000" in line
    assert "iat=1799999970 (backdated 30s)" in line
    assert "exp=1800000300 (ttl 300s)" in line
    assert fingerprint(SECRET) in line
    assert SECRET not in line


def test_report_states_the_issuers_iat_as_an_offset_from_this_clock() -> None:
    now = 1_800_000_000
    token = issuer(clock=lambda: now + 600).mint()  # the issuer's clock leads by ten minutes

    line = verifier(clock=lambda: now).report(token)

    assert "+570s from this clock" in line  # 600 ahead, 30 backdated
    assert "iss='core'" in line and "aud='engine'" in line
    assert "leeway 60s" in line
    assert fingerprint(SECRET) in line
    assert token not in line and SECRET not in line


def test_report_copes_with_no_token_and_with_garbage() -> None:
    assert "no token was presented" in verifier().report("")
    assert "could not be read" in verifier().report("garbage")


# ── the header ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "token"),
    [
        ("Bearer abc.def.ghi", "abc.def.ghi"),
        ("bearer abc.def.ghi", "abc.def.ghi"),
        ("  Bearer   abc.def.ghi  ", "abc.def.ghi"),
        ("Basic abc", ""),
        ("Bearer", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_bearer_token_parsing(header, token) -> None:
    assert bearer_token(header) == token


def test_definitions_fail_loud() -> None:
    with pytest.raises(ValueError):
        Issuer(SECRET, issuer="core", ttl_s=0)
    with pytest.raises(ValueError):
        Issuer(SECRET, issuer="core", backdate_s=-1)
    with pytest.raises(ValueError):
        Verifier(SECRET, leeway_s=-1)
