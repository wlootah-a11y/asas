# Changelog: `asas-linkedin`

Versions follow semver, and the git tag matches this file: `asas-linkedin/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release. Extracted from a production platform's candidate sign-in,
Apply with LinkedIn import, Page share and jobs feed, and made independent
of it: the client and the pure pieces move here, the account linking, the
Page connection row and the choice of jobs stay with the host.

- **`LinkedInClient`** over `LinkedInSettings` (two apps: sign-in and Page):
  discovery read live and cached for an hour, `authorization_url`,
  `exchange_code` (secret in the body, never retried), `refresh`,
  `verify_id_token` (RS256 against the published keys, one refetch on an
  unknown key id, a minute of leeway), `sign_in`, `page_authorization_url`,
  `page_grant`, `refresh_page_grant`, `create_post` (versioned headers, the
  URN from `x-restli-id`, never retried).
- **`oidc`**: `JwksKeySet` and `decode_verified`, usable for any RS256
  provider, refusing with a stable `OidcError.reason`.
- **`identity`**: `member_from_claims` into `LinkedInMember`, both locale
  shapes, an optional and separately-stated verified email.
- **`awli`**: `verify_profile` (HMAC-SHA256 under the app secret, four
  serialisations, one refusal) and `keep_mapped_fields` (the mapped fields
  only, lengths capped, a profile link off linkedin.com dropped).
- **`little_text`** and **`posts`**: the fourteen reserved characters
  escaped, `fit_commentary` to 3,000 without half an escape, `article_post`
  in the Posts API's shape.
- **`sealing`**: AES-256-GCM with a context in the authenticated data,
  `load_key` from base64, an unopenable token reported as the Page not
  connected.
- **`jobs_feed`**: `FeedJob`, `problems` (the guide's mandatory rules),
  `description` (LinkedIn's limited HTML, escaped), `render` (CDATA
  everywhere, terminators and control characters neutralised).
- Five errors with a stable `code`, an `http_status` and LinkedIn's
  machine-readable `reason` in `details`, never its prose.
