# asas-servicetoken

Signed tokens between two services that share a secret. One service signs
each request it sends to a peer, the peer checks the signature, and the two
run on different hosts whose clocks never agree. This package is the rules
that make that work written down once, with tests, for both sides.

Table-less **and** router-less variant of the Asas host contract: no session,
no `migrate`, no `seed`, no `configure`. The only dependency is PyJWT; httpx
and FastAPI are extras the package imports on first use.

## Quickstart

```
asas-servicetoken[httpx,fastapi] @ git+https://github.com/wlootah-a11y/asas.git@asas-servicetoken/v0.1.0#subdirectory=packages/asas-servicetoken
```

The side that calls:

```python
import asas_servicetoken

engine = asas_servicetoken.Issuer(
    lambda: settings.engine_shared_secret,   # read on every call: a rotated secret needs no restart
    issuer="core",                           # iss and sub
    audience="engine",                       # aud, which the peer checks
    unset_hint="CORE_ENGINE_SHARED_SECRET",  # named in the one log line an unset secret gets
)

response = await asas_servicetoken.send_with_service_token(
    engine, lambda headers: client.post(url, json=body, headers=headers)
)
```

The side that is called:

```python
from fastapi import APIRouter, Depends
import asas_servicetoken

core = asas_servicetoken.Verifier(
    lambda: settings.engine_shared_secret, issuer="core", audience="engine"
)
router = APIRouter(dependencies=[Depends(asas_servicetoken.require_service_token(core))])
```

That is the whole adoption: an `Issuer` per peer you call, a `Verifier` per
peer that calls you.

## What it gets right, so you do not have to

- **A verifier whose clock runs behind sees a token "issued in the future".**
  PyJWT refuses an `iat` even one second in the verifier's future, so ordinary
  NTP drift reads as a forged token. The issuer stamps `iat` behind its own
  clock (`backdate_s`, 30 s) and the verifier allows `leeway_s` (60 s) of
  drift. `exp` stays measured from now, so neither costs the token any life,
  and the leeway is seconds against a lifetime of minutes, so `exp` still
  means something.
- **A refusal has three causes that look identical from the caller:** the
  clocks are too far apart, the secrets differ, or no header was sent. Each
  side logs the facts that tell them apart and never the secret itself:
  `Issuer.describe()` states its clock, the `iat` and `exp` it stamps and a
  fingerprint of its secret; `Verifier.report(token)` states the issuer's
  `iat` as a signed offset from the verifier's clock (`+600s` is a clock to
  fix, `+0s` beside a signature refusal is a secret to compare), what the
  token claims as its issuer and audience, and a fingerprint of the secret
  this side holds.
- **A refused call is sent once more, with a token stamped further back.**
  Re-signing alone would not help: inside one second the claims are identical
  and the "fresh" token is the refused one byte for byte. The retry's `iat` is
  backdated a whole token lifetime further, which clears a drift of that plus
  the peer's leeway at no cost in validity. One retry only, and only on 401
  or 403, so a wrong secret still surfaces as a failure rather than as a
  service that is merely slower. It is safe because a peer refuses in its
  auth dependency, before any route body runs.
- **A streamed response gets the same treatment** without buffering the body,
  and the first response is closed before the retry goes out, so a refusing
  peer cannot exhaust the connection pool.
- **An unset secret sends no header at all and says so once**, naming the
  setting it should have come from. That is the shape an unresolved vault
  reference produces in a deployment, and it must read as a configuration
  fault in the log rather than as a peer that is down. The verifier fails
  closed: with no secret it refuses everything.
- **The caller is told nothing it can use.** The FastAPI dependency answers
  one generic 401 whatever the cause; the reason goes to this side's log.
- **Refusals are data.** `TokenRefused.reason` is one of `missing`,
  `secret_unset`, `immature`, `expired`, `signature`, `issuer`, `audience`,
  `claims`, `malformed`; `detail` is the library's own message. Nothing has
  to match an English sentence.

## Surface

- `Issuer(secret, *, issuer, audience=None, subject=None, ttl_s=300,
  backdate_s=30, algorithm="HS256", clock=time.time, unset_hint="")`:
  `mint(extra_backdate_s=0, claims=None)` (raises `SecretUnset`),
  `headers(...)` (`{}` when unset, logged once), `describe()`.
- `Verifier(secret, *, issuer=None, audience=None, leeway_s=60,
  algorithm="HS256", require=("exp", "iat", "sub"), clock=time.time,
  unset_hint="")`: `verify(token) -> Claims` (raises `TokenRefused`),
  `report(token)`.
- `Claims`: `subject`, `issuer`, `audience`, `issued_at`, `expires_at`,
  `raw`, `get(name)`.
- `bearer_token(header)`, `fingerprint(secret)`, `resolve_secret(secret)`.
- `send_with_service_token(issuer, send)`,
  `stream_with_service_token(issuer, client, build)`,
  `log_refusal(issuer, response)`, `UNAUTHORIZED` (httpx extra).
- `require_service_token(verifier, *, detail=...)` (fastapi extra).

A secret is a string or a zero-argument callable read on every call. A
`clock` is injected so a test can move one side without touching the other.

## Not in scope

No asymmetric keys or key sets (an OIDC provider's JWKS is a different
problem, with a cache and a rollover), no user sessions, no scopes or roles
(`asas-access`), no token store. Two services, one secret, two clocks.

Extracted from a production platform whose two tiers minted and verified
this token in four separate places, two per side, and learned each rule
above from an outage: the first drift of one second took a whole board read
to a 502 with nothing misconfigured.
