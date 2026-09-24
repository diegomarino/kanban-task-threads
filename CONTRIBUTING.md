# Contributing

Develop with Python 3.11-3.13, [uv](https://docs.astral.sh/uv/), and
`actionlint`. A Hermes checkout is needed only for integration work.

Use focused tests while developing:

```bash
./scripts/sandbox test tests/test_consumer.py -q
```

Before pushing, run `python3 scripts/check.py fast`. Run
`python3 scripts/check.py full-local` for a publication candidate or when an
integration-facing change needs the offline plugin-load check.

For a disposable local event flow that never contacts Discord, use
`./scripts/sandbox up`, `./scripts/sandbox task`, and
`./scripts/sandbox consume`. Everything is contained in the ignored
`.sandbox/` directory.

Unit and fake-transport evidence proves deterministic plugin behavior. Real
pinned-Hermes evidence proves compatibility with a supported Hermes version.
Exploratory upstream-Hermes evidence is useful investigation only: it does not
change the supported version floor or authorize a release. Live Discord
evidence proves delivery to an explicitly authorized test forum and is never a
routine contributor check.

See [testing details](docs/testing.md). Maintainers should use
[the release guide](RELEASING.md) for publication and its separate procedures
for credentials, branch protection, catalog setup, and live Discord helpers.
