# Closing dependency security alerts (Dependabot, `uv.lock`)

> Use when a Dependabot alert is open on `uv.lock`, or before bumping a dependency for a security fix.

The image and CI install from `uv.lock` (`uv sync --locked`), so an alert closes only when **no locked copy** of the package is in the vulnerable range. Bumping the version you run is not enough.

## What went wrong before

- **A second copy kept for a Python nobody runs.** uv locks the whole `requires-python` window. With `>=3.10`, the lock kept Pillow 12.3.0 next to 13.0.0 (which needs 3.11): 43 alerts stayed open after the bump. The fixed releases of filelock, dnspython, virtualenv and pymongo need 3.11 too, so they could not be locked at all. Fix the window, not the symptom.
- **A fix capped by another package.** `opentelemetry-api` 1.45.2 needed `google-adk` 2.14.1 (2.12 capped it at 1.42.1). Read what uv answers instead of guessing.
- **A fix that drags a stack.** `litellm` 1.100.4 requires `boto3>=1.43`, which moves `aiobotocore` to 3.x and `s3fs`/`fsspec` with it. Say it in the PR; the tests of that stack are the evidence.

## Do

1. List every locked copy before and after: a package can appear twice with `resolution-markers`.
   ```bash
   grep -n -A3 '^name = "pillow"' uv.lock
   ```
2. Take the smallest release that closes the alert (`first_patched_version`), one package at a time:
   ```bash
   uv lock --upgrade-package 'pypdf==6.21.4'
   ```
   If uv answers "No solution found", the message names the package that blocks: bump that one, or narrow `requires-python`.
3. For a direct dependency, raise its floor in `pyproject.toml` too, with the advisory id in a comment: a fresh `pip install apowerb` must not resolve to the vulnerable release.
4. Never hand-edit `uv.lock`. `uv lock --check` must pass.

## Check

- Each open alert against **all** locked versions of its package (the alert gives `vulnerable_version_range`): zero match.
- `DB_SCHEMA="" uv run pytest -q -m "not integration"` compared with the same run on `main`: a major bump of a transitive package (oauthlib 4, filelock 4, aiobotocore 3) has no other proof.
- GitHub re-reads the lock after the merge: the alert count is confirmed there, not locally.
