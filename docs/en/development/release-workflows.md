---
title: Release Workflows
description: Current Agently repository automation for docs, installers, and PyPI publishing.
keywords: Agently, release, GitHub Actions, PyPI, docs
---

# Release Workflows

> Languages: **English** · [中文](../../cn/development/release-workflows.md)

This page records the current main-repository automation surfaces.

## Development Version And Branching

Agently development work should separate three concepts:

- **Current work version**: the next intended public release batch, recorded in
  `compatibility/in-development.json`. For example, `4.1.3.3` can be the
  current work version even when the larger roadmap target is `4.1.4`.
- **Version target**: the product or architecture goal the current batch moves
  toward, such as AgentTask V1 for `4.1.4`.
- **Task branches**: implementation branches named by work type and scope, not
  by the version number.

Do not create routine development branches named after the version, such as
`4.1.3.3` or `release/4.1.3.3`, unless the work is an actual release-prep
branch. Use task-scoped branch names instead:

- `feature/<scope>`
- `bug-fix/<scope>`
- `update/<scope>`
- `refactor/<scope>`

A current work version may contain several accepted task branches. Each branch
should carry one coherent feature, fix, update, or refactor; after acceptance it
is merged into `dev`. The version release candidate is then assembled from
`dev`, with release notes, compatibility manifests, examples, docs, and
companion repositories reconciled against the current work version.

When starting new version-development work after the previous public version has
already been released, do not infer the next current work version or the task
branch from local history. If either the current work version or the intended
task branch is not explicitly specified, stop and ask the maintainer to specify
both before changing code, specs, docs, examples, or compatibility metadata.

When a branch implements a slice of a larger roadmap target, record both facts:
the feature spec should describe the larger target, while
`compatibility/in-development.json` should describe the current work version
that will publish the accepted slice.

## Documentation

GitHub Pages uses the `docs/` directory from the `main` branch.

The `docs/_config.yml` file records the Pages/Jekyll settings for this branch source, including the site base URL and relative Markdown link handling.

The old `docs` branch is retired and should not be used as a documentation source. Do not add workflows or release steps that publish documentation from the retired branch.

For every framework release, update and review these public documentation
surfaces before merging the release PR:

- final release notes in both languages, for example
  `docs/en/development/release-notes-<version>.md` and
  `docs/cn/development/release-notes-<version>.md`
- the root `README.md`
- the root `README_CN.md`

The README files should keep their existing structure, but they must reflect the
release's final product positioning, current version number, recommended
capability entrypoints, example directories, companion compatibility line, and
business value. Do not leave README updates as a post-release marketing task:
the root README is also the PyPI long description because `[project].readme`
points at `README.md`.

Before opening the release PR, compare release notes with both README files and
check for stale version numbers, outdated companion protocol names, removed
examples, deprecated recommended APIs, and mismatched business claims.

Before merging the release PR, do a separate human release-note review. This is
not a mechanical checklist. The reviewer should confirm that the release notes
describe the final product story, code shape, examples, and business value that
the release is actually shipping. If this review finds wording gaps, stale
claims, missing README updates, confusing examples, or small API/documentation
adjustments, make those changes in the release PR, rerun the relevant
validation, and only then merge. Do not merge first and treat release-note fixes
as follow-up marketing work.

## Acceptance Argument

Before recommending a release, write a coverage-first acceptance argument for
each user-visible feature slice. Start from the target contract, not from the
examples or tests that already exist.

The release reviewer should first list the required behavior from the roadmap,
spec, issue acceptance criteria, compatibility manifest, docs, and example
rules. Then map each requirement to the evidence that proves it:

- scenario examples with real DeepSeek or local Ollama output and stable
  `Expected key output`
- deterministic tests for compatibility, stream/meta shape, route lifecycle,
  budget accounting, errors, and workspace records
- protocol/type tests for public contracts and dependency direction
- docs, compatibility manifests, spec reconciliation, and companion guidance
- DevTools or other companion validation when runtime events, observation
  payloads, lineage, or companion protocols changed

Examples prove that the release solves a real scenario. They must not be the
only proof for compatibility behavior, protocol boundaries, route lifecycle,
error semantics, budget counting, or internal architecture ownership. If a
requirement has no evidence, either implement it before release, mark it as an
explicit deferral in the relevant spec and release notes, or remove the release
claim.

The release PR body or review notes should include this matrix or a concise
link to it. Do not accept a release by pointing directly at existing examples,
tests, or closed issues without first checking that those evidence sources cover
the target contract.

## Release PR Body

The release PR from `dev` to `main` must include enough information for a
reviewer to accept or block the release without reconstructing the work from
commit history.

At minimum, include:

- release version, release level, and current PyPI published version
- change summary grouped by user-visible capability
- coverage-first acceptance argument or matrix
- validation commands and results, including any skipped or failed checks
- clean install smoke environment and result
- compatibility manifest updates and companion repository status
- DevTools version or protocol recommendation when runtime events,
  observation payloads, lineage, or DevTools code changed
- issue closure or follow-up issue status
- known deferred scope and residual release risk
- post-merge companion promotion or publish steps

Do not use a terse PR body that only says "release" or only lists commits.
Release PRs are part of the durable acceptance record.

## Desktop installers

Desktop installers are not part of the current main-repository release flow.

Do not keep or re-enable desktop installer workflows only to support the retired `docs` branch flow.

## PyPI publishing

The main PyPI automation is `Publish on version change`. It runs on pushes to `main` that touch `pyproject.toml`, detects whether the package version changed, and publishes with Poetry only when the version changed.

The PyPI project list page shows the package metadata `Summary`, which comes from `[project].description` in `pyproject.toml`. The full project page renders the README from `[project].readme`.

When preparing a release:

- keep `[project].description` non-empty and concise
- keep `[project].readme` pointed at `README.md`
- do not expect PyPI metadata for an existing uploaded version to update in place; publish a new version when metadata must change publicly

## Clean install smoke

During release testing, validate the package in a brand-new empty environment
created with `conda` or `uv`. Install the release candidate package and only the
necessary runtime dependencies for a minimal script. Do not reuse a developer
environment where optional packages may already be cached.

The smoke script must verify two things:

- A basic installed-package startup path works without optional dependencies
  that are explicitly protected by `agently.utils.LazyImport`.
- At least one LazyImport-protected missing dependency path is triggered on
  purpose, and the user-facing install prompt/error is emitted correctly.

Optional integrations such as DevTools, ChromaDB, FastMCP, SQLModel, Playwright,
or other provider-specific packages should not be installed unless the smoke is
testing that integration directly. Their absence must not break ordinary Agently
imports or minimal Agent/TriggerFlow startup.
