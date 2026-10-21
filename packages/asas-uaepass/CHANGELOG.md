# Changelog: `asas-uaepass`

Versions follow semver, and the git tag matches this file: `asas-uaepass/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release. Extracted from a production platform's candidate sign-in and
made independent of it: the client, the parser and the nationality list;
the account linking rules stay with the host.

- **`UaePassClient`**: `authorization_url`, `sign_in` (the code swapped once
  with Basic credentials and an empty body, the user information read with
  one connection retry, the token dropped), `logout_url`; built even when
  unconfigured, with every call then `UaePassUnconfiguredError`.
- **`UaePassSettings`**: the four values a sign-in needs, `configured` and
  `missing`, the kept-attributes list in UAE PASS's own spelling.
- **`parse_userinfo` and `UaePassProfile`**: case-insensitive keys, `uuid`
  as the key and never `sub`, E.164 mobiles, positional full names, alpha-3
  nationalities with `XXX` as "unknown", a first or last name taken from the
  full name when UAE PASS left it empty, `unparsed` for what could not be
  used; only a missing `uuid` or an unknown `userType` fails the sign-in.
- **Three errors** with a stable `code`, an `http_status` and a log-only
  `reason`: unconfigured (503), rejected (401), unavailable (502).
- **`nationalities`**: UAE PASS's own 248-code list with English and Arabic
  names, `normalize_code`, `name`.
- **Nothing secret in any log**, including httpx's own request line, whose
  `/idshub/` query strings are redacted by an installed filter.
