---
title: Release Workflows
description: Current Agently repository automation for docs, installers, and PyPI publishing.
keywords: Agently, release, GitHub Actions, PyPI, docs
---

# Release Workflows

> Languages: **English** · [中文](../../cn/development/release-workflows.md)

This page records the current main-repository automation surfaces.

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

Before opening or merging the release PR, compare release notes with both README
files and check for stale version numbers, outdated companion protocol names,
removed examples, deprecated recommended APIs, and mismatched business claims.

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
