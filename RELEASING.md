# Releasing

Releases are automated on the public `main` lineage by Release Please. Source
development and public publication have deliberately disjoint histories:
Release Please starts only after a reviewed, sanitized change has reached the
public tree. It does not select, copy, or reconcile internal source commits.

## Source-to-public boundary

1. Develop and review the change on the source lineage.
2. Create a publication branch from public `pre-release`, then port only the
   intended distributable changes. Resolve differences deliberately; do not
   merge the source history into the public history or accept one side of a
   conflict wholesale.
3. Run `python3 scripts/check.py fast` before pushing and
   `python3 scripts/check.py full-local` against that exact public candidate,
   then run every publication gate below and
   merge its technical PR into `pre-release`. Pushes to `pre-release` run the
   single expensive **Release candidate** gate, but cannot run Release Please
   or publish a tag. Later stages reuse only that exact successful candidate
   SHA and fail closed when the identity cannot be proven.
4. After every validation job passes on a `pre-release` push, CI opens or
   reuses one promotion PR from `pre-release` to `main`. Merge it with **Create
   a merge commit** so `pre-release` remains an ancestor of `main`; do not
   squash or rebase the release train.
5. A push to public `main` lets Release Please open or update its release PR
   from the Conventional Commits now present on that lineage.

The automation in this repository is therefore port-ready configuration. A
copy in an internal source worktree cannot activate GitHub Actions and must not
be treated as evidence that the public workflow is installed or running.

## Public branch setup

`main` remains the default and release branch. Create `pre-release` from an
exact, released `main` commit and protect both branches. Normal publication PRs
target `pre-release`; only reviewed promotion PRs target `main`. The branch name
does not imply prerelease SemVer: no beta tag or GitHub prerelease is created,
and consumers who want the integrated candidate can explicitly pull that
branch.

Keep the **Main PR policy** CI check required. It rejects ordinary PRs aimed at
`main`; only the repository's `pre-release` promotion and the exact
`release-please--branches--main` PR authored by `github-actions[bot]` pass. The
remote branch protections must also prevent direct pushes, because checked-in
CI cannot undo a commit that has already reached `main`.

Configure the promotion merge method to preserve the commits already reviewed
on `pre-release`. After a stable release, synchronize `main` back before the
next promotion so the Release Please version and changelog commit becomes the
new integration baseline. Creating or protecting the remote branch is an
activation operation, not an effect of these checked-in files.

## Automated release flow

The manifest is bootstrapped at the current public release, `0.3.0`. For the
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
5. Synchronize the resulting version/changelog commit from `main` back into
   `pre-release` before the next promotion. If no new integration commits have
   landed, fast-forward `pre-release`; otherwise merge `main` into it and
   resolve deliberately. Never let a later promotion revert release metadata.
6. Decide separately when Hermes should learn about the release. From GitHub
   Actions, run **Sync Hermes Catalog** on `main`. Leave `release_tag` blank to
   select the latest stable release, or enter an exact `vX.Y.Z` tag. The manual
   workflow updates the fork's deterministic
   `catalog/kanban-task-threads-vX.Y.Z` branch and opens the separate release
   metadata PR for `NousResearch/hermes-agent`'s `plugin-catalog/`. The entry's
   `image` URL is updated to the banner at that same immutable release SHA. It may skip
   intermediate plugin releases: the catalog moves directly from its current
   pin to the stable release selected by the operator. The automation never
   merges upstream.

The CI workflow starts Release Please only on `main`, after the Python
3.11–3.13 matrix, Hermes validation, and the HOL scanner have all passed. It
does not invoke the catalog workflow. The manual catalog workflow accepts only
a published, non-draft, non-prerelease `vX.Y.Z` release, resolves its tag to the
exact commit, and verifies that commit's release manifest before touching the
fork. It then checks out current upstream `main`, changes only `version`,
   `sha`, and the SHA-pinned `image`, runs the upstream catalog validator, and
   checks for an existing PR before creating one. Catalog dispatches are
   serialized, and any open catalog PR from this plugin's fork blocks a
   different release handoff, so two releases cannot create simultaneous
   upstream PRs. A same-release retry reuses an exact, validated automation
   branch. An
unexpected branch or a prior closed-but-unmerged PR fails closed for manual
resolution; a merged PR is accepted only after current upstream `main`
contains the exact pin. New branches use an explicit empty-value lease and the
resulting PR starts as a draft.

## Publication gates

Run these without pointing Hermes at the live fleet. `full-local` is the
canonical offline gate; GitHub repeats only external or platform-specific
validation:

```bash
python3 scripts/check.py full-local
HERMES_HOME="$PWD/.sandbox" \
  HERMES_KANBAN_HOME="$PWD/.sandbox" \
  hermes plugins validate . --json
actionlint .github/workflows/*.yml
```

After candidate validation, promotion and release stages verify exact commit
identity, merge parents, release-only paths, and synchronized versions. They do
not repeat the Python, Hermes, or scanner matrix. Release Please remains the
only mechanism that opens the release PR and publishes the merged release.

The tests include the Release Please contract: the public bootstrap version,
all synchronized version targets, the CI gates, the pinned action, the release
outputs, the catalog updater's confinement, and the workflow's permission
boundaries. Public CI and the marketplace scanner must also pass on the
candidate tree.

Never add a self-update mechanism or update-check ping (catalog rule 3), or a
`capabilities`/`provides_hooks` declaration that drifts from what `register()`
registers (rule 6 — treated as a security issue; the test suite pins it).

## Catalog credential setup

The release itself uses only the repository's `GITHUB_TOKEN`. The cross-repo
catalog handoff uses a separate credential because a repository token cannot
push to `diegomarino/hermes-agent` or open a PR against
`NousResearch/hermes-agent`.

1. Create the `hermes-catalog` GitHub Environment and restrict it to the public
   `main` branch. The workflow itself is manually dispatched; required-reviewer
   protection remains optional as a second human gate.
2. Add `HERMES_CATALOG_TOKEN` as an environment secret. The credential must be
   able to push a branch to `diegomarino/hermes-agent` and create a pull request
   from that fork into the public `NousResearch/hermes-agent` repository. Give
   it no unrelated repository or organization access. The workflow exposes it
   only to the branch-push and PR-creation steps; upstream validation receives
   no cross-repository credential.
3. In repository **Settings → Actions → General**, an owner must enable
   **Allow GitHub Actions to create and approve pull requests** if it is not
   already enabled. The repository's default workflow token can remain
   restricted. The promotion job adds only `pull-requests: write` while keeping
   `contents: read`; the Release Please job separately declares `contents`,
   `issues`, and `pull-requests` write access. No other CI job receives a
   write-capable token.
4. Read back the environment configuration and inspect the first catalog job.
   A green local suite or installed workflow file does not prove that the
   credential works or that an upstream PR was created.

GitHub suppresses most recursive workflow events created by `GITHUB_TOKEN`.
The release PR may therefore require explicit workflow approval. Irrespective
of how that PR's checks start, merging it creates a normal push to `main`; CI
reruns every publication gate before it creates the release. That release has
no catalog side effect until an operator separately dispatches **Sync Hermes
Catalog**.

## Rollback

- Before this automation is merged, close its PR; local/configuration files
  have no external effect.
- After activation, use a reviewed public PR to remove or revert the manual
  catalog workflow. Removing `HERMES_CATALOG_TOKEN` prevents future cross-repo
  handoffs without disabling Release Please.
  Close any pending Release Please PR after confirming it has not been merged.
- Keep the manifest at or above every version already published. Removing the
  workflow does not remove an existing tag or GitHub Release, and rollback does
  not authorize deleting or rewriting either one.
- If an action run has an ambiguous external outcome, inspect the PR, tag, and
  GitHub Release first. Do not retry until their state is known.
