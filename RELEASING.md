# Releasing

Releases are automated on the public `main` lineage by Release Please. Source
development and public publication have deliberately disjoint histories:
Release Please starts only after a reviewed, sanitized change has reached the
public tree. It does not select, copy, or reconcile internal source commits.

## Source-to-public boundary

1. Develop and review the change on the source lineage.
2. Create a publication branch from `public-main`/`origin/main`, then port only
   the intended distributable changes. Resolve differences deliberately; do
   not merge the source history into the public history or accept one side of
   a conflict wholesale.
3. Run every publication gate below against that exact public candidate.
4. Merge the technical public PR. A push to public `main` lets Release Please
   open or update its release PR from the Conventional Commits now present on
   that lineage.

The automation in this repository is therefore port-ready configuration. A
copy in an internal source worktree cannot activate GitHub Actions and must not
be treated as evidence that the public workflow is installed or running.

## Automated release flow

The manifest is bootstrapped at the current public release, `0.2.3`. For the
single root component, the Python strategy owns `CHANGELOG.md` and the version
in `pyproject.toml`; explicit extra-file rules update `plugin.yaml` and the
root `kanban-task-threads` package entry in `uv.lock`. A release PR must contain
the same SemVer in all four places. Tags and GitHub releases use `vX.Y.Z` with
no component prefix.

Review the entire generated diff, not only the values: the YAML updater
reserializes `plugin.yaml` and does not retain comments, while the changelog
updater inserts the new release section ahead of the existing `[Unreleased]`
section. Those are expected behaviors of the pinned action's release-please
version, not evidence that unrelated source or runtime files were selected.

Release Please derives the next version and release notes from Conventional
Commits on public `main`: `fix:` produces a patch, `feat:` a minor, and a
breaking change a major under normal SemVer rules. Changes that must appear in
release notes need an appropriate user-facing Conventional Commit. State-DB
schema changes must also describe the in-place migration and any operator
action in the resulting changelog entry before the release PR is merged.

For each release:

1. Review the generated release PR. Confirm its changelog is complete and its
   versions agree in `.release-please-manifest.json`, `pyproject.toml`,
   `plugin.yaml`, and `uv.lock`.
2. Run the publication gates below against the exact release-PR tree.
3. Merge the release PR only after that review. The next workflow run creates
   the `vX.Y.Z` tag and GitHub Release at the merged public commit.
4. Read back the tag and release. Do not blindly retry an ambiguous create.
5. Open the separate SHA/version bump PR for
   `NousResearch/hermes-agent`'s `plugin-catalog/`. The catalog must pin the
   40-character public release commit; Release Please does not update it.

## Publication gates

Run these without pointing Hermes at the live fleet:

```bash
./scripts/sandbox test
./scripts/sandbox lint
./scripts/sandbox doctor
HERMES_HOME="$PWD/.sandbox" \
  HERMES_KANBAN_HOME="$PWD/.sandbox" \
  hermes plugins validate . --json
actionlint .github/workflows/release-please.yml
```

The tests include the Release Please contract: the public bootstrap version,
all synchronized version targets, the `main`-only trigger, the pinned action,
and the workflow's exact permission set. Public CI and the marketplace scanner
must also pass on the candidate tree.

Never add a self-update mechanism or update-check ping (catalog rule 3), or a
`capabilities`/`provides_hooks` declaration that drifts from what `register()`
registers (rule 6 — treated as a security issue; the test suite pins it).

## Activation

Activation is a separate public-repository operation; none of these local
files changes GitHub by itself.

1. Port `.github/workflows/release-please.yml`,
   `.release-please-manifest.json`, `release-please-config.json`, this document,
   and the contract test onto a branch based on public `main`.
2. Before merge, verify that public `main` still has release/tag `v0.2.3` and
   that `pyproject.toml`, `plugin.yaml`, and `uv.lock` all say `0.2.3`. If a
   newer release exists, update the manifest bootstrap to that exact version
   instead of replaying or replacing a release.
3. In repository **Settings → Actions → General**, an owner must enable
   **Allow GitHub Actions to create and approve pull requests** if it is not
   already enabled. The repository's default workflow token can remain
   restricted: this workflow declares its own three write permissions. Record
   and review any settings change separately.
4. Merge the activation PR. Then read back the installed files and the first
   workflow result before calling the automation active.

The workflow intentionally uses only `secrets.GITHUB_TOKEN`; it introduces no
PAT, GitHub App, or new secret. GitHub suppresses recursive workflow events
created by `GITHUB_TOKEN`, so CI does **not** automatically run on release PRs
opened by this workflow. The exact release-PR tree must therefore receive the
manual publication-gate run above before merge. Moving to automatically
triggered release-PR CI requires a separately reviewed credential and policy
decision; it is outside this setup.

## Rollback

- Before activation is merged, close the activation PR; local/configuration
  files have no external effect.
- After activation, use a reviewed public PR to remove or revert the workflow.
  Close any pending Release Please PR after confirming it has not been merged.
- Keep the manifest at or above every version already published. Removing the
  workflow does not remove an existing tag or GitHub Release, and rollback does
  not authorize deleting or rewriting either one.
- If an action run has an ambiguous external outcome, inspect the PR, tag, and
  GitHub Release first. Do not retry until their state is known.
