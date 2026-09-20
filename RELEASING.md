# Releasing

The sequence, kept honest (the catalog pins a 40-char SHA, so order matters):

1. `./scripts/sandbox test` · `./scripts/sandbox lint` · `./scripts/sandbox doctor`
   — all green, plus `hermes plugins validate . --json` all-ok.
2. Move the `[Unreleased]` notes in CHANGELOG.md under the new version, dated.
   If the release changes the state-DB schema, the notes must describe the
   in-place migration and any operator action.
3. Bump `version:` in `plugin.yaml` **and** `[project]` in `pyproject.toml`
   (they must match; semver).
4. Commit, tag `vX.Y.Z`, push with tags, create the GitHub Release with the
   changelog section as its notes.
5. Catalog listing (once it exists): open the SHA-bump PR against
   `NousResearch/hermes-agent` `plugin-catalog/` — entries update only via
   that PR, `hermes plugins update` covers direct-from-repo installs.

Never: a self-update mechanism or update-check pings (catalog rule 3), or a
`capabilities`/`provides_hooks` declaration that drifts from what `register()`
registers (rule 6 — treated as a security issue; our test suite pins it).
