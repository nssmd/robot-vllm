# GitHub CI activation

The complete workflow is checked in as `.github/ci-tests.yml`. It is intentionally
outside `.github/workflows/`, so GitHub does not run it yet. The credential used
for initial publication was allowed to create/push the repository but GitHub
rejected workflow creation because its OAuth `workflow` scope was missing.
Local validation is complete; no remote CI result is claimed.

An authorized maintainer can activate it using a GitHub credential that permits
workflow updates. For the GitHub CLI OAuth login, obtain that authorization with:

```bash
gh auth refresh --hostname github.com --scopes workflow
```

After completing GitHub's login/authorization step:

```bash
mkdir -p .github/workflows
git mv .github/ci-tests.yml .github/workflows/tests.yml
```

Update the workflow filename in `MANIFEST.in` to the new path, commit and push.
The workflow then runs Python 3.10/3.12 contract tests, HTTP smoke, package builds,
a Python container build, ROS transport checks and the actual ROS crash/restart
smoke. It uses no paid model endpoints or training/GPU workers. Each job has a
wall-time limit and uploads its own evidence on completion/failure.

A successful local test does not establish that GitHub runners passed. Verify the
published commit's Actions run and fix runner-specific failures before reporting
CI green. The ROS container recipe and physical deployment commissioning retain
their own validation scope.
