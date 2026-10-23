# Releasing

## Versioning

TS Harness follows [Semantic Versioning](https://semver.org/). While the version
is `0.x`, a minor release (`0.2.0`) may include breaking changes and a patch
release (`0.1.1`) only fixes bugs. From `1.0.0`, breaking changes need a major
release.

A change is breaking when an existing install has to do something by hand to
keep working: a renamed or removed environment variable, config key, HTTP
route, workflow definition field, database column, or deployed identifier.
Record every breaking change under **Changed** in `CHANGELOG.md` with the
migration step, and add any kept legacy name to
[`compatibility.md`](compatibility.md).

The version lives in two places and must match:

- `pyproject.toml` (`version`)
- `admin-ui/package.json` (`version`)

## Cutting a release

1. Confirm CI is green on the default branch.
2. Review `.trivyignore` and remove entries that are fixed upstream or expired.
3. In `CHANGELOG.md`, rename **Unreleased** to the new version and date, and
   start a new empty **Unreleased** section.
4. Bump both version fields, commit, and tag:

   ```bash
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

5. The `Release` workflow builds the agent-runtime image, generates an SBOM,
   and runs the vulnerability scan.
6. Create a GitHub release from the tag and paste the changelog section.

## Container images

Images are not published yet. The `Release` workflow builds and scans the
image with `push: false`, and image signing is a stub. Installs build images on
the host from the tagged source.

Publishing later needs a registry (for example GHCR), `push: true` with a
registry login in `release.yml`, and cosign signing against the pushed digest.
