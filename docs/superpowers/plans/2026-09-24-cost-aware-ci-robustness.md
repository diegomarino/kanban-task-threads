# Cost-Aware CI Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Catch deterministic failures locally and validate each release candidate deeply once, while preventing the same source tree from consuming a full GitHub Actions matrix at every release-train hop.

**Architecture:** Split validation into a fast, single-runner PR gate and a full candidate gate that runs only after a reviewed change reaches `pre-release`. Promotion and release steps verify commit identity, ancestry, and tightly constrained metadata diffs instead of rerunning the full matrix. Keep live Discord and cross-repository catalog effects explicit and separate.

**Tech Stack:** Bash, Python 3.11-3.13, pytest 9.1.1, coverage.py, Ruff 0.16.8, actionlint, GitHub Actions, Hermes 0.20.0 and 0.21.3, Release Please.

**Spec:** `docs/testing.md`, `RELEASING.md`, and the cost constraint recorded in this plan: an ordinary release train must run the expensive Hermes/scanner candidate gate once per distinct distributable source tree.

## Global Constraints

- Technical artifacts, code, configuration, test names, and commit messages are in English.
- The plugin supports Python `>=3.11,<3.14`.
- The local default gate must be safe, offline, deterministic, and operate only inside the repository sandbox.
- A GitHub run may be reused only when its exact commit SHA is identified; branch names or a merely green older run are insufficient.
- The full candidate gate must retain both pinned Hermes compatibility refs until the documented support floor changes.
- Discord live delivery and the Hermes catalog push remain explicit external-effect workflows and are never part of routine PR CI.
- Release publication must fail closed if promotion ancestry, candidate SHA, release-only diff, or version synchronization cannot be proven.
- GitHub branch-protection changes are activation work and must be applied only after the replacement checks exist and have passed.

## Target Run Budget

For one ordinary feature progressing through PR, `pre-release`, promotion, Release Please, and publication:

| Stage | Current expensive work | Target work |
|---|---:|---:|
| Feature PR | 3 Python jobs + 2 Hermes jobs + scanner | 1 fast validation job; same-job Hermes smoke only for integration-surface changes |
| Push to `pre-release` | Full matrix + scanner | 1 full candidate gate |
| Promotion PR | Full matrix + scanner | 1 identity/ancestry check |
| Push to `main` | Full matrix + scanner | Release Please only after provenance check |
| Release PR | Full matrix + scanner | 1 release-metadata check |
| Release merge | Full matrix + scanner | Publication plus provenance check |

The target is one expensive gate per distinct candidate, with approximately 80% fewer runner jobs in the normal release path. Do not optimize the separately dispatched catalog workflow by weakening its checks; make its deterministic logic locally executable instead.

## Review Focus

- A promotion PR whose head moved after candidate validation must fail rather than reuse the older green run; Task 4 adds an exact-SHA test.
- A Release Please PR containing runtime code in addition to version/changelog files must fail; Task 5 adds a changed-path allowlist test.
- A workflow-only change that alters the candidate gate must still receive full validation before activation; Task 4 treats CI and release workflow paths as full-gate inputs.
- A superseded PR commit must cancel its in-progress fast run without cancelling candidate or publication work; Task 3 tests the concurrency policy.
- Missing GitHub API data, rate limits, or ambiguous check conclusions must fail closed without retrying an external publication effect; Tasks 4 and 5 add error-path tests.

---

### Task 1: Close the current high-value code coverage gaps

**Files:**
- Modify: `tests/test_reconcile.py`
- Modify: `tests/test_consumer.py`
- Modify: `tests/test_thread_maintenance.py`

**Interfaces:**
- Consumes: the existing `reconcile.main()`, `Consumer.run_once()`, and fake transport/store fixtures.
- Produces: regression coverage for operator CLI behavior and recovery branches that can lose, duplicate, or indefinitely delay delivery.

- [ ] **Step 1: Add reconciliation CLI tests before changing CI**

  Exercise `reconcile.main()` directly with `capsys`, covering:

  Add `test_cli_without_a_verb_prints_usage_and_returns_two`, `test_cli_attention_prints_empty_state_and_returns_zero`, `test_cli_attention_prints_actionable_rows`, `test_cli_rejects_unknown_verbs`, and a parametrized `test_cli_rejects_missing_arguments_without_a_traceback` covering `clear`, `adopt`, `rearm`, and `recreate`.

  Assert exact return codes and stable message fragments. These tests cover the currently unexecuted CLI block without changing operator behavior.

- [ ] **Step 2: Run reconciliation tests and confirm the existing behavior**

  Run: `./scripts/sandbox test tests/test_reconcile.py -q`

  Expected: new tests pass unless they expose a concrete CLI defect. If a defect appears, make the smallest fix in `kanban_task_threads/reconcile.py` and retain the failing test as the regression.

- [ ] **Step 3: Add consumer recovery tests selected by consequence**

  Add focused tests for these unexecuted or partially executed branches:

  - unexpected exception during forum setup becomes a retry warning and publishes nothing;
  - lease loss before a dirty-card repaint prevents the edit and leaves the card dirty;
  - lease loss before metadata audit prevents every bot-side mutation;
  - a dirty post whose task row disappeared is cleared without a transport call;
  - malformed `retry_after` falls back to one second;
  - unexpected exception during repaint leaves the card dirty and emits a warning;
  - unexpected exception during thread maintenance leaves the card dirty and retries later.

  Use observable store/report/transport behavior. Do not write assertions solely to execute lines.

- [ ] **Step 4: Run the focused recovery suites**

  Run:

  ```bash
  ./scripts/sandbox test tests/test_consumer.py tests/test_thread_maintenance.py tests/test_reconcile.py -q
  ```

  Expected: PASS with no network access.

- [ ] **Step 5: Measure coverage and stop at risk coverage**

  Re-run branch coverage and record the per-module result. Do not add tests merely to reach 100%. Acceptance is that `reconcile.py` CLI paths are covered and each listed recovery behavior has an observable assertion.

- [ ] **Step 6: Commit**

  ```bash
  git add tests/test_reconcile.py tests/test_consumer.py tests/test_thread_maintenance.py kanban_task_threads/reconcile.py
  git commit -m "test: cover operator and delivery recovery paths"
  ```

### Task 2: Create one canonical local validation command

**Files:**
- Create: `scripts/check.py`
- Create: `tests/test_check.py`
- Create: `CONTRIBUTING.md`
- Modify: `scripts/sandbox`
- Modify: `docs/testing.md`
- Modify: `RELEASING.md`

**Interfaces:**
- Consumes: existing `./scripts/sandbox test`, Ruff configuration, and repository workflow files.
- Produces: `python3 scripts/check.py fast` and `python3 scripts/check.py full-local`, returning zero only when every selected check passes.

- [ ] **Step 1: Write tests for command construction and failure propagation**

  Add tests that import `scripts/check.py` and assert:

  ```python
  def test_fast_check_contains_one_python_suite_lint_coverage_and_actionlint():
      commands = check.commands_for("fast")
      assert [command.name for command in commands] == [
          "coverage-run",
          "coverage-report",
          "ruff-check",
          "ruff-format",
          "actionlint",
      ]
      assert "--fail-under=85" in commands[1].argv


  def test_full_local_extends_fast_with_offline_plugin_load():
      commands = check.commands_for("full-local")
      assert commands[:5] == check.commands_for("fast")
      assert commands[-1].name == "hermes-doctor"


  def test_runner_stops_at_first_failure_and_returns_its_exit_code():
      executed = []
      result = check.run_commands(
          [
              check.Command("first", ("first",)),
              check.Command("second", ("second",)),
          ],
          run=lambda command: executed.append(command.name) or 17,
      )
      assert result == 17
      assert executed == ["first"]
  ```

- [ ] **Step 2: Run the focused test and confirm RED**

  Run: `./scripts/sandbox test tests/test_check.py -q`

  Expected: FAIL because `scripts/check.py` and its interfaces do not exist.

- [ ] **Step 3: Implement the local runner**

  Implement immutable `Command` records, `commands_for(profile)`, `run_commands(commands, run=subprocess.run)`, and `main(argv)` in `scripts/check.py`. Use argument arrays rather than shell strings. Resolve the actionlint workflow paths with `Path.glob()` and pass each concrete path as an argument; do not rely on shell wildcard expansion. The fast profile must run:

  ```text
  uv run --python 3.13 --with pytest==9.1.1 --with coverage==7.10.7 -- coverage run --branch --source=kanban_task_threads -m pytest tests
  uv run --python 3.13 --with coverage==7.10.7 -- coverage report --fail-under=85
  uvx --from ruff==0.16.8 ruff check .
  uvx --from ruff==0.16.8 ruff format --check .
  actionlint .github/workflows/*.yml
  ```

  Include root `__init__.py` in measured files through a coverage configuration section in `pyproject.toml`, avoiding a `--source=__init__` package name. `full-local` adds `./scripts/sandbox doctor` after the fast commands.

- [ ] **Step 4: Route sandbox entry points through the canonical runner**

  Add `sandbox check` and keep `sandbox test` as the focused pytest command. Do not make unit-test invocations pay for lint or Hermes setup.

- [ ] **Step 5: Run focused and complete verification**

  Run:

  ```bash
  ./scripts/sandbox test tests/test_check.py -q
  python3 scripts/check.py fast
  ```

  Expected: all tests pass, branch coverage is at least 85%, and workflow syntax passes actionlint.

- [ ] **Step 6: Document the local-first contract**

  Update `docs/testing.md` and `RELEASING.md` so contributors run `python3 scripts/check.py fast` before pushing and `full-local` before a publication candidate. State that GitHub repeats only external/platform-specific validation.

- [ ] **Step 7: Add the contributor entry point**

  Create `CONTRIBUTING.md` as the short public entry point for development. It must cover:

  - prerequisites (`uv`, Python 3.11-3.13, `actionlint`, and a Hermes checkout only for integration work);
  - the normal loop: focused pytest during development, `python3 scripts/check.py fast` before pushing, and when `full-local` is warranted;
  - the disposable `.sandbox/` flow for creating events and consuming them without Discord;
  - the difference between unit/fake-transport evidence, real pinned-Hermes evidence, exploratory upstream-Hermes evidence, and live Discord evidence;
  - a statement that an exploratory Hermes pass does not change the supported version floor and does not authorize a release;
  - links to `docs/testing.md` for detail and `RELEASING.md` for maintainer-only publication steps.

  Keep release credentials, branch-protection administration, catalog-token setup, and live Discord destructive helpers out of the normal contributor path; link to maintainer documentation instead.

- [ ] **Step 8: Commit**

  ```bash
  git add scripts/check.py scripts/sandbox tests/test_check.py pyproject.toml CONTRIBUTING.md docs/testing.md RELEASING.md
  git commit -m "test(ci): add canonical local validation gate"
  ```

### Task 3: Replace the repeated PR matrix with one fast required check

**Files:**
- Create: `.github/workflows/pr-validation.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_marketplace.py`
- Modify: `tests/test_release_please.py`

**Interfaces:**
- Consumes: `python3 scripts/check.py fast` from Task 2.
- Produces: one stable required check named `PR validation` for ordinary pull requests.

- [ ] **Step 1: Replace broad textual expectations with event and job-budget assertions**

  Extend the existing `_top_level_block()` and `_job_block()` helpers so the tests inspect exact workflow and job blocks without adding a YAML dependency. Let actionlint own YAML syntax validation. Add assertions equivalent to:

  ```python
  def test_pr_validation_is_one_job_and_cancels_superseded_commits():
      workflow = load_workflow(".github/workflows/pr-validation.yml")
      assert set(workflow["jobs"]) == {"validate"}
      assert workflow["concurrency"]["cancel-in-progress"] is True
      assert workflow["jobs"]["validate"]["name"] == "PR validation"


  def test_pr_validation_runs_the_canonical_fast_gate_once():
      workflow = load_workflow(".github/workflows/pr-validation.yml")
      runs = step_commands(workflow["jobs"]["validate"])
      assert runs.count("python3 scripts/check.py fast") == 1
      assert not any("matrix" in job for job in workflow["jobs"].values())
  ```

  Keep security assertions for immutable action SHAs and read-only default permissions.

- [ ] **Step 2: Run the focused workflow contract tests and confirm RED**

  Run: `./scripts/sandbox test tests/test_marketplace.py tests/test_release_please.py -q`

  Expected: FAIL because the new workflow does not exist and the old PR trigger still launches the full matrix.

- [ ] **Step 3: Add the fast PR workflow**

  Configure `.github/workflows/pr-validation.yml` with:

  ```yaml
  name: PR validation
  on:
    pull_request:
      branches: [pre-release]
  permissions:
    contents: read
  concurrency:
    group: pr-validation-${{ github.event.pull_request.number }}
    cancel-in-progress: true
  jobs:
    validate:
      name: PR validation
      runs-on: ubuntu-latest
  ```

  The job checks out the exact PR SHA, installs Python 3.13 and actionlint, then runs `python3 scripts/check.py fast`. Pin every action and downloaded tool version.

  In the same runner, detect changes to `__init__.py`, `plugin.yaml`, `kanban_task_threads/runtime.py`, `scripts/check_startup.py`, or the Hermes compatibility workflow. For those integration-surface changes only, check out the current supported Hermes ref and run `scripts/check_startup.py` plus `hermes plugins validate`. This keeps the recent launch-policy class of failure visible in the feature PR without paying for two Hermes jobs or a second runner on every PR.

- [ ] **Step 4: Remove ordinary pull requests from the expensive workflow**

  Remove the generic `pull_request` trigger from `.github/workflows/ci.yml`. Rename that workflow and its jobs in Task 4 rather than retaining two definitions of quality commands.

- [ ] **Step 5: Verify the local workflow contracts**

  Run:

  ```bash
  ./scripts/sandbox test tests/test_marketplace.py tests/test_release_please.py -q
  actionlint .github/workflows/*.yml
  ```

  Expected: PASS; ordinary PRs produce one cancellable validation job.

- [ ] **Step 6: Commit**

  ```bash
  git add .github/workflows/pr-validation.yml .github/workflows/ci.yml tests/test_marketplace.py tests/test_release_please.py
  git commit -m "ci: collapse pull request validation to one job"
  ```

### Task 4: Run the expensive matrix once per pre-release candidate

**Files:**
- Create: `scripts/ci/verify_candidate.py`
- Create: `tests/test_verify_candidate.py`
- Rename: `.github/workflows/ci.yml` to `.github/workflows/candidate.yml`
- Modify: `CONTRIBUTING.md`
- Modify: `tests/test_marketplace.py`
- Modify: `tests/test_release_please.py`
- Modify: `RELEASING.md`

**Interfaces:**
- Consumes: a `push` SHA on `pre-release`, GitHub check-run data, and both pinned Hermes refs.
- Produces: a successful `Release candidate` workflow for one exact SHA and an automatically opened/reused promotion PR for that SHA.

- [ ] **Step 1: Add exact candidate identity tests**

  Implement tests around pure functions before GitHub API wiring:

  ```python
  def test_candidate_is_reusable_only_for_exact_successful_sha():
      runs = [
          {"headSha": "old", "conclusion": "success"},
          {"headSha": "candidate", "conclusion": "failure"},
      ]
      assert verify_candidate.has_success(runs, "candidate") is False


  def test_missing_or_ambiguous_candidate_data_fails_closed():
      with pytest.raises(verify_candidate.CandidateError):
          verify_candidate.require_exact_success([], "candidate")


  @pytest.mark.parametrize(
      "path",
      [
          "kanban_task_threads/runtime.py",
          "__init__.py",
          "plugin.yaml",
          ".github/workflows/candidate.yml",
          "scripts/check_startup.py",
      ],
  )
  def test_full_gate_inputs_include_runtime_manifest_and_ci(path):
      assert verify_candidate.requires_full_gate([path])
  ```

- [ ] **Step 2: Run the focused tests and confirm RED**

  Run: `./scripts/sandbox test tests/test_verify_candidate.py -q`

  Expected: FAIL because the candidate verifier does not exist.

- [ ] **Step 3: Implement the candidate verifier as pure logic plus a narrow CLI**

  `verify_candidate.py` must accept JSON through an explicit file argument, never invoke `gh` internally, and return a nonzero exit when no successful run matches the exact SHA. GitHub API acquisition stays visible in the workflow; selection and validation stay unit-testable.

- [ ] **Step 4: Restrict the expensive workflow to `pre-release` pushes**

  Configure `candidate.yml` with:

  ```yaml
  name: Release candidate
  on:
    push:
      branches: [pre-release]
  concurrency:
    group: release-candidate-${{ github.ref }}
    cancel-in-progress: true
  ```

  Preserve:

  - Python 3.11 and 3.13 test jobs. Remove Python 3.12 from this repeated gate because both language boundaries are exercised and the code has no version-specific dependency surface.
  - Both pinned Hermes startup checks.
  - `hermes plugins validate` only on the supporting Hermes ref.
  - The HOL scanner.
  - A stable aggregate check named `Release candidate`.

  The promotion job depends on the aggregate check and confirms that the remote `pre-release` SHA still equals `github.sha` before creating or updating the PR.

- [ ] **Step 5: Preserve an opt-in upstream Hermes probe**

  Add `workflow_dispatch` with a required `hermes_ref` input to the candidate workflow or a narrowly separate compatibility workflow. A manual invocation checks out exactly that ref from `NousResearch/hermes-agent`, runs `scripts/check_startup.py`, and runs `hermes plugins validate` only when the selected CLI supports it. It never opens a promotion PR and never invokes Release Please.

  Document examples in `CONTRIBUTING.md` for a commit SHA and `refs/pull/<number>/head`, how to find the resulting check, and how to interpret a skip when an older CLI lacks `plugins validate`. This is the path for testing an interesting upstream Hermes change; it consumes GitHub time only when explicitly invoked.

- [ ] **Step 6: Make promotion PR validation identity-only**

  Add a separate lightweight workflow for PRs from `pre-release` to `main`. It verifies:

  - head repository is this repository;
  - head branch is exactly `pre-release`;
  - head SHA equals the current remote `pre-release` SHA;
  - the exact head SHA has a successful `Release candidate` run;
  - the candidate workflow definition itself came from that SHA.

  It must not run pytest, Ruff, the Python matrix, Hermes, or the scanner again.

- [ ] **Step 7: Verify job counts and exact-SHA behavior**

  Run:

  ```bash
  ./scripts/sandbox test tests/test_verify_candidate.py tests/test_marketplace.py tests/test_release_please.py -q
  actionlint .github/workflows/*.yml
  ```

  Expected: PASS. Contract tests assert two Python jobs, two Hermes jobs, one scanner job, and no expensive jobs in the promotion workflow.

- [ ] **Step 8: Update release documentation**

  Describe `pre-release` as the single expensive validation boundary. State that later stages reuse only an exact successful candidate SHA and fail closed if it cannot be proven.

- [ ] **Step 9: Commit**

  ```bash
  git add .github/workflows scripts/ci/verify_candidate.py tests/test_verify_candidate.py tests/test_marketplace.py tests/test_release_please.py CONTRIBUTING.md RELEASING.md
  git commit -m "ci: validate each release candidate once"
  ```

### Task 5: Keep Release Please intact and make its prerequisites provenance-only

**Files:**
- Create: `scripts/ci/verify_release.py`
- Create: `tests/test_verify_release.py`
- Create: `.github/workflows/release.yml`
- Modify: `tests/test_release_please.py`
- Modify: `RELEASING.md`

**Interfaces:**
- Consumes: the promoted candidate SHA, merge parents, changed paths, synchronized version files, and Release Please outputs.
- Produces: permission to open/update a release PR or publish a tag without rerunning the candidate matrix.

- [ ] **Step 1: Define the release-only allowlist in tests**

  Add tests for these exact paths:

  ```python
  RELEASE_ONLY_PATHS = {
      ".release-please-manifest.json",
      "CHANGELOG.md",
      "plugin.yaml",
      "pyproject.toml",
      "uv.lock",
  }


  def test_release_pr_accepts_only_release_metadata():
      verify_release.require_release_only(RELEASE_ONLY_PATHS)


  def test_release_pr_rejects_runtime_or_workflow_changes():
      with pytest.raises(verify_release.ReleaseError):
          verify_release.require_release_only(RELEASE_ONLY_PATHS | {"kanban_task_threads/runtime.py"})


  def test_all_version_surfaces_must_match():
      assert (
          verify_release.require_one_version(
              {"manifest": "0.5.0", "plugin": "0.5.0", "project": "0.5.0", "lock": "0.5.0"}
          )
          == "0.5.0"
      )
  ```

- [ ] **Step 2: Run the focused tests and confirm RED**

  Run: `./scripts/sandbox test tests/test_verify_release.py -q`

  Expected: FAIL because `verify_release.py` does not exist.

- [ ] **Step 3: Implement deterministic release verification**

  Implement pure functions for path allowlisting, version equality, stable tag parsing, and merge-parent ancestry. The CLI accepts explicit JSON/files supplied by the workflow. Any missing parent, mismatched SHA, unknown path, missing version, or GitHub API ambiguity returns nonzero.

- [ ] **Step 4: Change prerequisites without rewriting the release mechanism**

  Move the existing Release Please job from `ci.yml` into `.github/workflows/release.yml`, preserving the `googleapis/release-please-action` step, pinned SHA, token, config path, manifest path, outputs, `main` push trigger, and write permissions byte-for-byte. Change only the validation jobs it depends on.

  The resulting workflow must ensure:

  - a push to `main` first proves the commit is either the validated promotion merge or the exact Release Please merge;
  - Release Please runs only after that provenance check;
  - a Release Please PR runs one lightweight `Release metadata` job checking the allowlist, version synchronization, changelog structure, action pins, and workflow syntax;
  - merging the release PR publishes only after the same provenance and version checks;
  - no Python matrix, Hermes checkout, scanner, or full pytest suite runs at these stages.

  Preserve the existing promotion behavior as well: a successful exact `pre-release` candidate opens or reuses the `pre-release` to `main` PR, and that PR must still be merged with a merge commit. Do not replace this with direct pushes, squash merges, rebases, or an alternate release branch.

- [ ] **Step 5: Add negative workflow-contract tests**

  Assert that the release workflow contains no `strategy.matrix`, no Hermes repository checkout, no HOL scanner, and no direct release step that can run without `verify_release.py` succeeding. Add regression assertions for the unchanged Release Please action SHA, inputs, outputs, permissions, and `main` push trigger, plus the unchanged `pre-release` to `main` merge-commit topology.

- [ ] **Step 6: Run release verification**

  Run:

  ```bash
  ./scripts/sandbox test tests/test_verify_release.py tests/test_release_please.py -q
  actionlint .github/workflows/*.yml
  python3 scripts/check.py fast
  ```

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add scripts/ci/verify_release.py tests/test_verify_release.py .github/workflows/release.yml tests/test_release_please.py RELEASING.md
  git commit -m "ci: verify release provenance without repeating candidate CI"
  ```

### Task 6: Add local semantic tests for catalog handoff without extra GitHub runs

**Files:**
- Create: `scripts/ci/catalog_handoff.py`
- Create: `tests/test_catalog_handoff.py`
- Modify: `.github/workflows/sync-hermes-catalog.yml`
- Modify: `tests/test_release_please.py`
- Modify: `docs/testing.md`

**Interfaces:**
- Consumes: explicit release metadata, PR listings, remote-ref listings, and catalog file content.
- Produces: a deterministic handoff decision (`create`, `reuse`, `already-merged`, or fail-closed) consumed by the manual workflow.

- [ ] **Step 1: Add table-driven handoff state tests**

  Cover:

  ```python
  @pytest.mark.parametrize(
      ("open_pr", "remote_branch", "upstream_pin", "expected"),
      [
          (None, None, None, "create"),
          ("same-release", "matching", None, "reuse"),
          (None, "matching", "matching", "already-merged"),
      ],
  )
  def test_supported_handoff_states(open_pr, remote_branch, upstream_pin, expected):
      assert catalog_handoff.decide(open_pr, remote_branch, upstream_pin).action == expected
  ```

  Add failing cases for a different open plugin PR, a closed-unmerged PR, a mismatched remote branch, multiple matching PRs, and missing API data.

- [ ] **Step 2: Run the focused test and confirm RED**

  Run: `./scripts/sandbox test tests/test_catalog_handoff.py -q`

  Expected: FAIL because the decision module does not exist.

- [ ] **Step 3: Extract deterministic state decisions from workflow shell**

  Implement the tested decision module. Keep `gh api`, checkout, push, and PR creation in the workflow as explicit effects. Pass their JSON outputs to the module through files. Do not retry a failed or ambiguous push automatically.

- [ ] **Step 4: Add a local bare-repository push rehearsal**

  In `tests/test_catalog_handoff.py`, create local bare Git repositories and exercise branch creation plus `--force-with-lease` for new and existing matching branches. This verifies Git behavior without credentials or network access. It does not claim to test GitHub token scopes.

- [ ] **Step 5: Keep catalog publication manual and single-run**

  Retain `workflow_dispatch`, the protected environment, `cancel-in-progress: false`, and cross-repository credentials. Do not add the catalog workflow to PR or release CI. Improve its preflight error so a rejected push reports the required fork permission/scope and leaves a clear manual recovery path.

- [ ] **Step 6: Verify locally**

  Run:

  ```bash
  ./scripts/sandbox test tests/test_catalog_handoff.py tests/test_release_please.py -q
  actionlint .github/workflows/*.yml
  ```

  Expected: PASS without network access.

- [ ] **Step 7: Commit**

  ```bash
  git add scripts/ci/catalog_handoff.py tests/test_catalog_handoff.py .github/workflows/sync-hermes-catalog.yml tests/test_release_please.py docs/testing.md
  git commit -m "test(release): rehearse catalog handoff locally"
  ```

### Task 7: Activate the cheaper check topology safely

**Files:**
- Modify: `RELEASING.md`
- Modify: repository branch-protection settings after all local and PR checks pass

**Interfaces:**
- Consumes: successful runs of `PR validation`, `Release candidate`, promotion identity, and `Release metadata` on the implementation branch.
- Produces: branch protection that requires the new stable check names and no longer waits for retired matrix checks.

- [ ] **Step 1: Run the complete local gate**

  Run:

  ```bash
  python3 scripts/check.py full-local
  ./scripts/sandbox test -q
  ```

  Expected: all checks pass and the worktree is clean except for the intended implementation commits.

- [ ] **Step 2: Open the implementation PR and observe one fast run**

  Confirm the PR starts exactly one `PR validation` job. Push a harmless follow-up commit while the first run is active and verify the older run is cancelled.

- [ ] **Step 3: Promote to `pre-release` and inspect one full candidate run**

  Confirm exactly one candidate workflow runs for the resulting SHA, with two Python boundary jobs, two Hermes jobs, one scanner, and the aggregate gate. Record the exact successful SHA.

- [ ] **Step 4: Verify lightweight downstream stages**

  Confirm the promotion PR, main push, Release Please PR, and release merge do not repeat the Python/Hermes/scanner matrix. Confirm each stage verifies the exact candidate or release SHA before mutation.

- [ ] **Step 5: Update branch protection**

  Replace retired required check names only after their replacements have produced successful runs. Require `PR validation` for feature PRs and the promotion identity check for `main`. Read back the persisted ruleset.

- [ ] **Step 6: Measure the result**

  Add a short table to `RELEASING.md` recording the observed job count and wall time for one pre-change and one post-change release train. Acceptance criteria:

  - at most one expensive candidate workflow per distinct source SHA;
  - no full matrix on promotion or Release Please PRs;
  - at least 70% fewer GitHub runner jobs over the complete release train;
  - no reduction in supported Hermes refs, scanner threshold, or release provenance checks.

- [ ] **Step 7: Commit the measured documentation**

  ```bash
  git add RELEASING.md
  git commit -m "docs(release): record cost-aware CI acceptance"
  ```

## Self-Review Record

- Spec coverage: local deterministic validation, unit/branch coverage, Hermes compatibility, workflow validation, promotion, Release Please, catalog handoff, and activation are each owned by a task.
- Placeholder scan: no deferred implementation placeholders remain.
- Interface consistency: Tasks 3-5 consume `scripts/check.py` and exact candidate/release SHAs; Task 6 remains independent and manual.
- Review focus: each listed failure mode has an explicit negative test in Tasks 3-6, while Task 1 adds risk-selected product tests.
- Cost boundary: only Task 4 retains the expensive Python/Hermes/scanner gate, and it runs on `pre-release` once per candidate SHA; the upstream Hermes probe is manual.
