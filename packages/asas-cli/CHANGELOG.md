# Changelog — `asas-cli`

Versions follow semver, and the git tag matches this file: `asas-cli/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.10 (unreleased)

- `asas add cv_profile` and `asas new` know `asas-cv-profile`: a registry entry, a boot snippet and the offline fallback tag `asas-cv-profile/v0.1.0`.

## 0.1.9 (unreleased)

- `asas add cv` and `asas new` know `asas-cv`: a registry entry, a boot snippet and the offline fallback tag `asas-cv/v0.1.0`.

## 0.1.8 (unreleased)

- The offline fallback pin for `asas-agent` moves to `v0.4.0`, the release with
  release stores, per-agent settings and tracing, a typed answer and the guarded model.

## 0.1.7 (unreleased)

- The offline fallback pin for `asas-agent` moves to `v0.3.1`, the release whose
  read-only tools of one model answer run together.

## 0.1.6 (unreleased)

- The offline fallback pin for `asas-agent` moves to `v0.3.0`, the release that
  carries the SDK loop and its host seams.

## 0.1.5 (unreleased)

- The offline fallback pin for `asas-agent` moves to `v0.2.1`, the release
  that adds the production providers and the Langfuse tracing processor.

## 0.1.4 (unreleased)

- The offline fallback pin for `asas-oracle` moves to `v0.1.1`, the release
  that gives Fusion's lifecycle refusals their own error classes.

## 0.1.3 (unreleased)

- The offline fallback pin for `asas-notifications` moves to `v0.20.0`, the
  release that joins the opaque-identity line (0.17.0 to 0.19.0) with the async
  host support.

## 0.1.2 (unreleased)

- `asas add agent` and `asas new --with agent` know `asas-agent` 0.2.0, the agent
  framework: its registry entry, its offline fallback tag and a boot snippet
  that imports it and shows where a release is loaded.

## 0.1.1 — 2026-09-13

- New command `asas outdated`: reads a consumer project's `pyproject.toml`,
  compares each `asas-*` pin against the newest release tags and against
  `ADVISORIES.json` on main, and reports the drift tier per package. With
  `--ci` it exits 0 when current or patch-behind, 1 when minor-behind
  (breaking pre-1.0 — deliberate upgrade owed), 2 when a pin is affected by
  an advisory (mandatory refresh). An unreachable advisories file warns and
  never fails a build on its own.
- New command `asas release <package>`: verifies the three version
  declarations agree (pyproject, `__version__`, newest CHANGELOG heading),
  refuses a tag that already exists locally or on origin, then creates and
  pushes the annotated `asas-<pkg>/vX.Y.Z` tag. `--no-push` for a dry cut.

## 0.1.0 — 2026-08-27

- Initial release: `asas add <package>` pins one Asas package into an existing
  project's `pyproject.toml`; `asas new <name> --with <packages>` scaffolds a
  new FastAPI project with a working boot sequence pre-wired for the chosen
  packages; `asas list` enumerates known packages.
- Born after the per-package tag scheme (`RELEASING.md`) landed, so it resolves
  and pins each selected package's own `asas-<pkg>/vX.Y.Z` tag — never a single
  shared tag across a multi-package `asas new --with a,b,c`.
- Licensed under **Apache 2.0**, matching every other package in this repo.
