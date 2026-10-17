# Contributing

Thanks for helping improve TS Harness. Read
[`docs/architecture/vision.md`](docs/architecture/vision.md) and
[`docs/architecture/harness-blueprint.md`](docs/architecture/harness-blueprint.md)
before proposing a larger change. A change that only works by adding a
domain-specific branch to the gateway or orchestrator usually belongs in
configuration, a skill, a hook, or a policy rule instead.

Everyone taking part is expected to follow the
[code of conduct](CODE_OF_CONDUCT.md). Report security problems privately as
described in [`SECURITY.md`](SECURITY.md).

## Setup

Python 3.11 or newer, and Node.js for the admin console.

```bash
pip install -e ".[dev]"
cp config/organization.example.yaml config/organization.yaml  # optional
```

To run the whole stack locally with Docker, use `scripts/quickstart.sh up`
(see the README).

## Tests

Python tests run without network access or a model API key:

```bash
export GATEWAY_TOKEN=test-token
export RUNTIME_FAKE_PROVIDER=true
python -m pytest tests -q
```

Admin console:

```bash
cd admin-ui
npm ci
npm test
npm run lint
```

Workspace templates must render with no unresolved placeholders:

```bash
python3 scripts/render-workspace.py --config config/organization.example.yaml \
  --src workspace --dst .rendered-workspace/validate --strict
bash scripts/validate-placeholders.sh .rendered-workspace/validate
bash scripts/validate-placeholders.sh skills
```

Live evals call a real model, so CI does not run them. Maintainers trigger the
**Live evals** workflow by hand; it needs an `OPENAI_API_KEY` repository secret
and skips with a notice when the secret is missing.

Lint:

```bash
ruff check runtime tests scripts
shellcheck --severity=warning scripts/*.sh scripts/phases/*.sh scripts/ops/*.sh
```

## Pull requests

- Keep each pull request to one change, with tests for new behavior.
- Do not commit secrets, generated files (`__pycache__`, `*.egg-info`,
  `admin-ui/dist`), or a filled-in `config/organization.yaml`.
- Update the runbook or architecture doc that describes the behavior you changed.
- Add a line under **Unreleased** in [`CHANGELOG.md`](CHANGELOG.md) for any
  user-visible change. Releases and versioning are described in
  [`docs/releasing.md`](docs/releasing.md).
- Commit messages follow Conventional Commits, for example
  `fix(workflows): keep cancelled programs cancelled`.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License, Version 2.0](LICENSE), as described in section 5 of the license.
