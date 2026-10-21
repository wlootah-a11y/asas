# asas-linkedin

LinkedIn for a hiring product, the generic half: Sign in with LinkedIn,
Apply with LinkedIn, posting as an organization Page, and the Jobs XML feed
LinkedIn reads for Job Wrapping. Nothing about any one product: which jobs
go in the feed, where a Page token row lives and who may connect the Page
are the host's.

Table-less and router-less variant of the Asas host contract: no session,
no `migrate`, no `seed`, no `build_router`, no `configure`. Everything is an
object the host constructs and owns (the `asas-graph` shape). Dependencies:
httpx, PyJWT with its crypto extra, cryptography.

## Quickstart

```
asas-linkedin @ git+https://github.com/wlootah-a11y/asas.git@asas-linkedin/v0.1.0#subdirectory=packages/asas-linkedin
```

```python
import asas_linkedin as li

# boot: build settings from *your* configuration; the library reads none.
# Built even when an app is missing, so a deployment with a feature off still boots.
linkedin = li.LinkedInClient(li.LinkedInSettings(
    sign_in=li.AppCredentials(settings.linkedin_client_id, settings.linkedin_client_secret,
                              settings.linkedin_callback_url),
    page=li.AppCredentials(settings.linkedin_page_client_id, settings.linkedin_page_client_secret,
                           settings.linkedin_page_callback_url),
    company_id=settings.linkedin_company_id,       # the number in urn:li:organization:{id}
))

# Sign in with LinkedIn: send the browser, then swap the code on the callback
url = await linkedin.authorization_url(app=linkedin.sign_in_app(), state=state)
member = await linkedin.sign_in(code=code)   # member.sub is the stable key; email_verified as LinkedIn says

# Apply with LinkedIn: the widget hands you (profile JSON, signature)
verified = li.verify_profile(raw_json, signature, secret=settings.linkedin_client_secret)
fields = li.keep_mapped_fields(verified.profile)   # only what a hiring product maps, lengths capped

# Post as the Page: an administrator connected it once; you sealed the grant
grant = await linkedin.page_grant(code=code)
sealed = li.seal(li.load_key(settings.page_token_key), grant.access_token,
                 context=f"page_connection:{tenant_id}:access")
...
token = li.unseal(key, sealed, context=f"page_connection:{tenant_id}:access")
urn = await linkedin.create_post(access_token=token, body=li.article_post(
    organization_urn=linkedin.settings.organization_urn,
    title=job.title, summary=job.summary, url=f"{careers}/roles/{job.id}",
    commentary="We're hiring! #jobs",
))

# The Jobs XML feed LinkedIn reads
entries = [job for job in feed_jobs if not li.feed_problems(job)]
xml = li.render_feed(entries, built_at=now, publisher="Example", publisher_url=careers)
```

## What it gets right, so you do not have to

- **The issuer, endpoints and signing keys come from LinkedIn's live
  discovery document**, never hard-coded: LinkedIn's live issuer
  (`https://www.linkedin.com/oauth`) differs from the one its own
  documentation page shows. The keys are cached for an hour; a token signed
  with an unknown key id refetches them once, so a rollover needs no restart,
  then fails closed.
- **The ID token is verified** (RS256 against those keys, the audience is the
  app, `exp`, `iat` and `sub` required) **with a minute of leeway**, because
  LinkedIn's clock and yours are never the same and one second of drift would
  refuse a good sign-in. Nothing LinkedIn issued is kept after the member is
  read.
- **The token request puts the secret in the body** (`client_secret_post`,
  the only form LinkedIn documents) and the code is **never retried**: it is
  single use. "LinkedIn is unreachable while its keys are fetched" is "try
  again later", not "your sign-in was refused".
- **Apply with LinkedIn's signature is checked under your secret** against
  the short, fixed list of serialisations LinkedIn might have signed (as
  sent, compact, compact ASCII, sorted); every candidate is an HMAC under
  your own key, so the list widens nothing for a forger. Anything else is
  one refusal, and nothing from the profile is returned. `keep_mapped_fields`
  then drops everything a hiring product does not map (LinkedIn API Terms
  4.4) and refuses a profile link off linkedin.com.
- **A post's commentary is LinkedIn's "little Text Format"**: fourteen
  reserved characters each need a backslash, "even if not used in one of
  the supported elements", or a person's words become a mention or a
  hashtag, or the post is refused. `fit_commentary` escapes first and then
  cuts to the 3,000-character limit, never leaving half an escape.
- **The Posts API call carries LinkedIn's versioned headers** and is never
  retried: the create is not idempotent, so a lost answer must not post
  twice. The new post's URN comes from the `x-restli-id` header.
- **The Page token is sealed at rest** (AES-256-GCM, a fresh nonce per seal)
  with a context bound into the authenticated data, so a token copied to
  another row or swapped between columns fails to open. A token the key
  will not open is `linkedin_page_not_connected`: reconnect the Page.
- **The Jobs XML feed follows the guide's mandatory-field rules** (a 40
  character `partnerJobId`, a 100 character description in LinkedIn's
  limited HTML, an https apply URL, a company id, a poster email, a
  location), writes every value as CDATA as LinkedIn's example does, and
  `feed_problems` names why a job would be refused before LinkedIn does.
- **Refusals are data.** Unconfigured (503, nothing was called), rejected
  (502: LinkedIn said no), unavailable (502, retry later), signature invalid
  (400) and page not connected (409), each with a stable `code` and
  LinkedIn's machine-readable `reason` in `details`, never its prose.

## Two LinkedIn apps

Sign-in runs on an app with the "Sign In with LinkedIn using OpenID Connect"
product. Posting as a Page runs on a SECOND app, because LinkedIn's Community
Management API must be the only product on its app, and LinkedIn's Marketing
APIs have no app-only flow: a Page administrator signs in once with the
`w_organization_social` scope and the host keeps that grant, sealed. Access
tokens last 60 days; refresh tokens are granted to some apps only, and
`TokenGrant.refresh_token` is `None` when LinkedIn gave none.

## Surface

- `LinkedInSettings(sign_in=, page=, company_id=, discovery_url=,
  api_base_url=, api_version=, timeout_seconds=, user_agent=)`,
  `AppCredentials(client_id, client_secret, redirect_uri)`.
- `LinkedInClient(settings, *, http=None, transport=None)`: `discovery()`,
  `authorization_url(app=, state=, scopes=)`, `exchange_code(app=, code=)`,
  `refresh(app=, refresh_token=)`, `verify_id_token(token, app=)`,
  `sign_in(code=)`, `page_authorization_url(state=)`, `page_grant(code=)`,
  `refresh_page_grant(refresh_token=)`, `create_post(access_token=, body=)`,
  `aclose()`, async context manager.
- `LinkedInMember`, `member_from_claims`; `TokenGrant`; `Discovery`.
- `verify_profile(raw, signature, secret=)`, `keep_mapped_fields`,
  `profile_url`, `MAX_PROFILE_BYTES`.
- `article_post(...)`, `fit_commentary`, `little_text_escape`,
  `COMMENTARY_MAX`.
- `load_key`, `seal`, `unseal`.
- `FeedJob`, `feed_problems`, `feed_description`, `render_feed`.
- `oidc.JwksKeySet`, `oidc.decode_verified`: the verifier, usable for any
  RS256 OpenID Connect provider.

## Not in scope

The account linking rules (what a host does with `member.sub`), the Page
connection row and who may connect it, the choice of jobs that go in the
feed, the Page share's idempotency, and LinkedIn's `userinfo` endpoint (the
ID token carries the same claims).

Extracted from a production platform's candidate sign-in, Apply with
LinkedIn import, Page share and jobs feed, each checked against LinkedIn's
live endpoints and its own published samples.
