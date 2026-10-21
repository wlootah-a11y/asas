# asas-uaepass

UAE PASS, the UAE's national digital identity, as a sign-in for a service
provider: the authorization-code exchange exactly as UAE PASS documents it,
the user information parsed and normalised into identity facts, and UAE
PASS's own nationality list. Nothing about any one product: what a host does
with the person (link an account, create one, ask) is the host's.

Table-less and router-less variant of the Asas host contract: no session, no
`migrate`, no `seed`, no `build_router`, no `configure`. Everything is an
object the host constructs and owns (the `asas-graph` shape). httpx is the
only dependency.

## Quickstart

```
asas-uaepass @ git+https://github.com/wlootah-a11y/asas.git@asas-uaepass/v0.1.0#subdirectory=packages/asas-uaepass
```

```python
import asas_uaepass

# boot: build settings from *your* configuration; the library reads none.
# Built even when unconfigured, so a deployment with UAE PASS off still boots.
uaepass = asas_uaepass.UaePassClient(asas_uaepass.UaePassSettings(
    base_url=settings.uaepass_base_url,          # https://stg-id.uaepass.ae or https://id.uaepass.ae
    client_id=settings.uaepass_client_id,
    client_secret=settings.uaepass_client_secret,
    callback_url=settings.uaepass_callback_url,  # exactly as registered with UAE PASS
))

# 1. send the browser to UAE PASS (state is your own anti-forgery value)
url = uaepass.authorization_url(state=state, locale="ar")

# 2. on the callback, swap the code for the person
profile = await uaepass.sign_in(code=code)
profile.uuid        # the ONE stable key for a returning person
profile.user_type   # SOP1 | SOP2 | SOP3
profile.facts       # {"email": ..., "mobile": "+971...", "given_name_en": ..., "nationality": "ARE", ...}
profile.unparsed    # fact fields UAE PASS sent a value for that could not be used

# 3. when they sign out, end UAE PASS's session too
uaepass.logout_url(redirect_uri="https://portal.example/login?signed_out=1")
```

## What it gets right, so you do not have to

- **The token request is UAE PASS's, not OAuth's textbook one.** The code,
  grant type and redirect go in the QUERY STRING with HTTP Basic client
  credentials and an EMPTY body, as UAE PASS's own Postman collection sends
  it; a JSON body gets a 500 from the live service.
- **The code is swapped once and never retried**: it is single use and UAE
  PASS wants it used within ten seconds. The user-information read is
  retried exactly once, and only when the connection itself failed, because
  nothing was read and the token is still good for an hour.
- **A returning person is `uuid`, never `sub`.** `sub` varies between
  `UAEPASS/<uuid>` and a bare hex string across UAE PASS's own samples.
- **The samples disagree with each other, and the parser is written for
  that:** keys are read without regard to case (`unifiedID` against
  `unifiedId`), mobiles arrive without a `+` and are kept only as E.164,
  full names are positional comma parts (`Ram,,,,ABC,,`) and an Arabic full
  name keeps the father's and grandfather's names the first and last name
  fields drop, nationality is an alpha-3 code from UAE PASS's own list and
  `XXX` means nobody knows.
- **Only two things fail a sign-in:** no `uuid`, or a `userType` outside
  SOP1/SOP2/SOP3 (the Web Authentication Implementation Assessment Checklist
  asks for exactly that). A bad optional value stores nothing for that field
  and is reported by name in `unparsed`.
- **The Emirates ID number, gender, titles, `idType` and `spuuid` are never
  read**, and `kept_attributes` names, in UAE PASS's own spelling, which of
  the nine attributes the parser knows a deployment keeps; a typo keeps
  less, never more.
- **Nothing secret reaches a log.** Not the code, the token, the client
  secret, UAE PASS's error descriptions or any response body. httpx logs
  every request line at INFO and UAE PASS puts the code in the query string,
  so the client installs a filter that redacts the query of any `/idshub/`
  URL in httpx's own log.
- **Refusals are data.** `UaePassUnconfiguredError` (503, nothing was
  called), `UaePassRejectedError` (401: an unknown code, our own credentials
  refused, a token no longer accepted, an unusable profile) and
  `UaePassUnavailableError` (502: a timeout, no route, a 5xx), each with a
  stable `code`, an `http_status` and a log-only `reason` such as
  `token_401`, which is what an alert on "our credentials were refused"
  keys on.
- **UAE PASS publishes no discovery document or signing keys** (the standard
  addresses answer 403) and returns an ID token only when `openid` is asked
  for, so none is: the details are trusted because they are read from UAE
  PASS itself, over HTTPS, with a token the client obtained itself.

## Surface

- `UaePassSettings(base_url, client_id, client_secret, callback_url,
  timeout_seconds=10, kept_attributes=..., user_agent=...)`: `configured`,
  `missing`.
- `UaePassClient(settings, *, http=None, transport=None)`:
  `authorization_url(state=, locale=)`, `sign_in(code=)`,
  `logout_url(redirect_uri=)`, `configured()`, `aclose()`, async context
  manager.
- `UaePassProfile`: `uuid`, `user_type`, `facts`, `unparsed`, `acr`,
  `email`, `mobile`.
- `parse_userinfo(body, kept=)`, `kept_attributes(setting)`,
  `normalize_mobile`, `normalize_email`, `normalize_name`, `join_name_parts`,
  `FACT_FOR_ATTRIBUTE`, `USER_TYPES`.
- `nationalities`: `NATIONALITIES` (248 alpha-3 codes with English and
  Arabic names, from UAE PASS's attributes list), `normalize_code`, `name`,
  `UAE`.
- `SCOPE`, `ACR_VALUES`, the four paths, `STAGING_BASE_URL`,
  `PRODUCTION_BASE_URL`.

## Not in scope

The account linking rules (a returning person is known by `uuid`; a first
sign-in may link on one matching email or ask), the session the host issues
afterwards, the audit event, and UAE PASS's mobile-app flow
(`mobileondevice`) and visitor scopes.

Extracted from a production platform's candidate sign-in, where each rule
above was checked against UAE PASS's staging service and its own published
samples.
