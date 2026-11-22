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

## Branches and promotion

- `dev` takes every merged PR. Test hosts (and athena) run `dev`.
- `main` holds released code only. It moves forward by a release PR from `dev`, never by direct
  pushes. Production hosts run a release tag (`v0.2.0`), set as `runtime.enterprise_repo_ref`.
- A change reaches production in this order: PR into `dev` → deployed and checked on a test or
  staging host → release PR `dev` → `main` → tag → production hosts roll to the tag.

## Cutting a release

1. Confirm CI is green on `dev` and the staging host has run the build (the nightly end-to-end
   regression, when configured, passed).
2. Review `.trivyignore` and remove entries that are fixed upstream or expired.
3. Prepare the release PR:

   ```bash
   python3 scripts/ops/release.py prepare 0.2.0
   ```

   It branches `release/v0.2.0` from `origin/dev`, renames **Unreleased** in `CHANGELOG.md` to
   the version and date (with a new empty **Unreleased** above it), bumps `pyproject.toml` and
   `admin-ui/package.json`, commits, pushes and opens a PR into `main`.
4. Merge the release PR (a merge commit, not a squash, so `main` keeps `dev`'s history).
5. Tag and publish:

   ```bash
   python3 scripts/ops/release.py tag 0.2.0
   ```

   It tags the merged commit on `main` as `v0.2.0` (which starts the `Release` workflow: image
   build, SBOM, vulnerability scan) and creates the GitHub release with the changelog section.
   `python3 scripts/ops/release.py notes 0.2.0` prints the section on its own.
6. Roll production hosts to the tag (below).

## Deploying a release and rolling back

On a host, move to any released version, forward or back:

```bash
sudo ADMIN_USER=<admin-user> bash scripts/ops/rollback.sh v0.2.0
```

It fetches tags, checks out the tag and runs `redeploy.sh`, which waits for running coding jobs
to finish before restarting anything.

**Database migrations only move forward and only add.** An older release must run on the newer
schema, so a new migration adds tables, columns (nullable or with a default), indexes and
constraints, and never drops, renames or retypes. `tests/test_release_process.py` refuses a
migration that does, unless it carries a `-- destructive: <reason>` line; such a release says in
its changelog entry how to roll back (usually: roll forward with a fix instead). Rolling back
the code never rolls back the database.

## Hotfixes

Fix on a branch from `main`, PR into `main`, release a patch version (`0.2.1`) with the steps
above (prepare from that branch instead of `dev`: check it out and edit `CHANGELOG.md` and both
version fields by hand), then merge `main` back into `dev`.

## Container images

Images are not published or signed yet. The `Release` workflow builds and scans
the image with `push: false`. Installs build images on the host from the tagged
source.

Publishing later needs a registry (for example GHCR), `push: true` with a
registry login in `release.yml`, and cosign signing against the pushed digest.
