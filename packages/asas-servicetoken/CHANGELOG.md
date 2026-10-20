# Changelog: `asas-servicetoken`

Versions follow semver, and the git tag matches this file: `asas-servicetoken/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release. Extracted from a production platform whose two tiers minted
and verified this token in four separate places, each carrying its own copy
of the clock-skew rules.

- **`Issuer`**: a signed HS256 token per call with `iat` backdated behind the
  issuer's clock and `exp` a full lifetime from now; `headers()` for the
  `Authorization` header, which sends nothing and logs once when the secret
  is unset; `describe()` for a refusal log (clock, `iat`, `exp`, lifetime,
  secret fingerprint, never the secret).
- **`Verifier`**: `verify(token)` with leeway for the peer's clock, a
  required-claims list and optional issuer and audience checks, failing
  closed on an unset secret; `report(token)` states the issuer's `iat` as a
  signed offset from this clock, the claimed issuer and audience, and the
  local secret's fingerprint.
- **Refusals are data:** `TokenRefused.reason` is one of nine stable
  reasons; `SecretUnset` names the setting through `unset_hint`.
- **`send_with_service_token`** and **`stream_with_service_token`** (httpx
  extra): one retry on 401 or 403 with a token stamped a whole lifetime
  further back, the first streamed response closed before the retry, and
  `log_refusal`, which never logs a response body.
- **`require_service_token`** (fastapi extra): a dependency answering one
  generic 401 with `WWW-Authenticate: Bearer`, the reason going to the log.
- A secret is a value or a callable read per call; a `clock` is injectable
  on both sides.
