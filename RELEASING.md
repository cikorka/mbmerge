# Versioning and publishing mbmerge

**English** | [Čeština](RELEASING.cs.md)

The distribution name, repository name and installed command are **mbmerge**. There is no requirement to populate GitHub Packages: Python distributions go to PyPI, while GitHub Releases identify source snapshots and provide release notes.

## One version, three entry points

`VERSION` in [merge_mbtiles.py](merge_mbtiles.py) is the single version source. Setuptools reads that literal for wheel/sdist metadata; the CLI prints it and `mbmerge.__version__` exposes it. Do not duplicate a version in pyproject.toml. The on-disk `SCHEMA_VERSION` is independent and follows the compatibility rules in [ARCHITECTURE.md](ARCHITECTURE.md).

The first public release can be `1.1.0`; the earlier working versions need not have public releases. Use tags `v1.1.0`, `v1.1.1`, etc. After publication, never replace a version or move its release tag. Publish a new version for changes.

- Patch, e.g. `1.1.1`: compatible fixes.
- Minor, e.g. `1.2.0`: compatible new functionality.
- Major, e.g. `2.0.0`: incompatible public CLI/data-contract changes.

Python prerelease versions use forms such as `1.2.0rc1`; a matching tag is `v1.2.0rc1`. Mark these as prereleases in GitHub. The production workflow skips GitHub prereleases and rejects non-X.Y.Z versions even if a preview was accidentally marked stable. TestPyPI is available for trial publishing without changing the production index.

## Workflows included

| File | Trigger | What it does |
|---|---|---|
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | Push, pull request, reusable workflow | Test on Linux/macOS with Python 3.11–3.14. Build a wheel/sdist, check metadata/version, install the exact wheel, exercise it outside the checkout, rebuild/install from sdist, and store validated artifacts. |
| [.github/workflows/testpypi.yml](.github/workflows/testpypi.yml) | Manual Run workflow | Run the same checks, then publish to TestPyPI using the `testpypi` environment. |
| [.github/workflows/release.yml](.github/workflows/release.yml) | Published stable GitHub Release | Run the same checks with an exact `v<VERSION>` tag check, then publish to PyPI using the `pypi` environment. |

Build/test jobs have read-only repository permissions. Only publishing jobs request `id-token: write`. Publishing jobs do not check out code or build it: they download the artifacts from their own workflow run after all checks succeed. No API token/password is stored in the repository. A tag push alone does not publish to PyPI.

## Build outputs and Node.js in CI

The build produces a Python wheel (`py3-none-any`) and source archive, not a standalone native executable. Installation creates an `mbmerge` command in `bin` that uses Python and Pillow. The Linux/macOS matrix checks Python, SQLite, file-locking and image-library compatibility; it does not compile native executables for those operating systems.

GitHub Actions uses Node.js internally for checkout, setup-python and artifact transfer. The directly selected JavaScript actions use Node.js 24; artifact transfer uses `upload-artifact@v7.0.2` and `download-artifact@v8.0.2`. Node.js is not an mbmerge runtime dependency and is not included in the wheel.

## Private evaluation before publishing

The repository is [cikorka/mbmerge](https://github.com/cikorka/mbmerge). While it is private, both PyPI and TestPyPI publishing jobs are skipped. CI, building and wheel-installation checks still run, with artifacts retained in the private repository. Validate representative real inputs and project value before making it public.

After the owner's separate decision to make the repository public, set repository Settings → Secrets and variables → Actions → Variables as appropriate: `TESTPYPI_PUBLISH_ENABLED=true` first, and later `PYPI_PUBLISH_ENABLED=true`. Without the corresponding variable, publishing also remains skipped. These variables are not API tokens. Creating a release alone cannot publish a private project to a public package index.

## One-time setup by the repository owner

1. Create the GitHub repository as `cikorka/mbmerge` and push this project's files. Keep `.github/workflows/` in the repository root. Enable GitHub Actions if necessary.
2. Create accounts on PyPI and TestPyPI and complete their required account/email/2FA setup. They have separate accounts and separate project namespaces. An HTTP 404 for a name does not reserve it or guarantee registration; confirm the name during setup.
3. In GitHub repository Settings → Environments, create `pypi` and `testpypi`. For production `pypi`, require a maintainer review before deployment and restrict deployments to release tags. Configure reviewers appropriate to your ownership/organization.
4. On [PyPI publishing settings](https://pypi.org/manage/account/publishing/), add a **pending publisher** for the new project:
   - PyPI project name: `mbmerge`.
   - Owner: `cikorka`.
   - Repository: `mbmerge`.
   - Workflow filename: `release.yml`.
   - Environment: `pypi`.
5. On [TestPyPI publishing settings](https://test.pypi.org/manage/account/publishing/), add a separate pending publisher with project `mbmerge`, the same owner/repository, workflow `testpypi.yml`, environment `testpypi`.
6. The actual `cikorka/mbmerge` URLs are already set under `[project.urls]` in pyproject.toml. README.pypi.md is the package description and has no broken relative image links; GitHub's README retains the illustrated guide.

The first successful trusted publication creates the project. A pending publisher does not reserve the name. Names, owner, repository, workflow filename and environment must match the identity configured on each index exactly. An initial release may ask you to approve the protected environment before upload.

## First publish: TestPyPI, then PyPI

1. Push the prepared repository and confirm **Tests and distributions** passes.
2. In Actions, select **Publish manually to TestPyPI** and use **Run workflow** on the commit/ref to try. It builds and validates the artifacts before upload. Verify the resulting TestPyPI project and installation.
   In a fresh virtual environment, install runtime dependencies from the checkout first, then the project from TestPyPI only:

```bash
python -m pip install -r requirements.txt
python -m pip install --index-url https://test.pypi.org/simple/ --no-deps 'mbmerge==1.1.0'
mbmerge --version
```

3. Set the intended `VERSION` if changing it, commit all release files, and push. For the current version:

```bash
git tag -a v1.1.0 -m "mbmerge 1.1.0"
git push origin v1.1.0
```

4. On GitHub create and **publish** a Release for that existing tag. Use **Set as a pre-release** only for previews; those are skipped by production publishing. Write release notes describing changes and compatibility. The release workflow re-runs validation, requires the tag and built version to match, and uploads the wheel/sdist to PyPI after environment approval.
5. Verify installation from PyPI in a clean environment:

```bash
pipx install mbmerge
mbmerge --version
```

For an existing pipx installation, use `pipx upgrade mbmerge`. GitHub automatically offers source archives for the tag; the built wheel and source distribution are also available in the workflow's `python-distributions` artifact and on PyPI. This workflow does not attach extra binary assets to the GitHub Release.

TestPyPI versions are separate from PyPI, so testing `1.1.0` there does not consume `1.1.0` on production. On either index you cannot overwrite an already uploaded distribution file. Re-running a successful publishing job can fail for that reason; do not work around it with changed artifacts under the same version. A failed build/check before upload is safe to retry after fixing the cause.

## Local verification before creating a release

Use Python 3.11+ in a virtual environment:

```bash
python -m pip install '.[release]'
python -m unittest discover -s tests -v
python -m build
python -m twine check --strict dist/*
python tools/check_release.py --dist-dir dist --tag v1.1.0
python -m pip install --force-reinstall --no-deps dist/*.whl
python -I tools/smoke_installed.py
```

Run in a fresh checkout or clear only old generated files in `dist/` first: the validator requires exactly one wheel and one sdist for the intended version. To validate source distribution completeness too:

```bash
python -m pip wheel --no-deps dist/*.tar.gz --wheel-dir rebuilt
python -m pip install --force-reinstall --no-deps rebuilt/*.whl
python -I tools/smoke_installed.py
```

The smoke script uses an installed distribution and runs the CLI from a temporary directory, so local source imports cannot hide missing wheel files. It exercises both encoders, real spawned workers, mixed inputs, exact pixels, metadata, replay and output verification. `check_release.py` rejects tag/version mismatches, unexpected wheel data and databases outside the four named demo fixtures in an sdist. Build artifacts stay ignored by Git.

A normal `python -m build` uses an isolated build environment. `--no-isolation` is only for offline/local builds when the declared backend requirements are already installed. Runtime Pillow remains a dependency, not vendored code.

## References

[PyPA packaging guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/), [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/), [publishing with GitHub Actions](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/), [TestPyPI](https://packaging.python.org/en/latest/guides/using-testpypi/), [Semantic Versioning](https://semver.org/).
