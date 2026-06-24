# Releasing dowse

dowse uses a GitHub Actions release workflow (`.github/workflows/release.yml`)
that builds wheel + sdist, validates them, and publishes to **TestPyPI** then
**PyPI** — all via **PyPI Trusted Publishing** (OIDC), so no API tokens are
stored as repository secrets.

## One-time setup: PyPI Trusted Publishing

Trusted Publishing lets GitHub Actions authenticate to PyPI without API tokens.
Instead, PyPI verifies the workflow's OIDC token against a configured
publisher. Set this up once on both PyPI and TestPyPI:

### 1. PyPI (production)

1. Go to https://pypi.org/manage/account/publishing/
2. Add a new pending publisher:
   - **PyPI Project Name:** `dowse`
   - **Owner:** `perezdap`
   - **Repository:** `Dowse`
   - **Workflow filename:** `release.yml`
   - **Environment name:** `pypi`
3. Save. The first release will create the project; subsequent releases publish
   new versions.

### 2. TestPyPI (rehearsal)

1. Go to https://test.pypi.org/manage/account/publishing/
2. Add a new pending publisher with the same settings, except:
   - **Environment name:** `testpypi`
3. Save.

### 3. GitHub environments

Create two required environments in the repository settings
(Settings → Environments):

- `testpypi` — used by the `publish-testpypi` job
- `pypi` — used by the `publish-pypi` job

Optionally add required reviewers to the `pypi` environment so a human must
approve before production publishes.

## Cutting a release

1. Ensure `CHANGELOG.md` has an entry under a versioned heading (e.g.
   `## [0.2.0] - 2026-06-24`) and `pyproject.toml` `version` matches.
2. Commit and merge to `main`.
3. Tag and push:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

4. The `Release` workflow fires automatically:
   - **build** — `python -m build` + `twine check dist/*`
   - **publish-testpypi** — publishes to TestPyPI (rehearsal)
   - **publish-pypi** — publishes to PyPI after TestPyPI succeeds

5. Verify on TestPyPI first: https://test.pypi.org/project/dowse/
6. Verify on PyPI: https://pypi.org/project/dowse/

## Release rehearsal (without PyPI)

To rehearse without publishing to production, push a pre-release tag
(e.g. `v0.2.0-rc1`). The workflow publishes to TestPyPI; the **PyPI** job is
skipped automatically when the tag name contains `-rc`.

## Verifying the published package

```bash
# Install from TestPyPI (rehearsal)
pip install -i https://test.pypi.org/simple/ dowse

# Install from PyPI (production)
pip install dowse

# Verify the entry points work
dowse --help
dowse serve --help
dowse status
```

## Notes

- The workflow **does not trigger on pull requests** — only on `v*` tag pushes.
- `skip-existing: true` on both publish jobs means re-running a tag won't fail
  if the version already exists on PyPI/TestPyPI.
- **Build** runs on `windows-latest` (matches CI). **Publish** jobs run on
  `ubuntu-latest` because `pypa/gh-action-pypi-publish` requires GNU/Linux.
- No API tokens are used anywhere. All authentication is via OIDC Trusted
  Publishing.
