## What and why

<!-- What does this change, and what problem does it solve? Link issues with "Fixes #123". -->

## How it was tested

<!-- Offline tests, and/or on a real Omni (which board: r1 launch / r2 spin; what shot types). -->

## Checklist

- [ ] **Read-only:** nothing in this change writes to, pushes to, or reconfigures the Omni
- [ ] `python -m unittest discover -s tests` passes (and new logic has tests where it can run offline)
- [ ] `ruff check .` passes
- [ ] Changelog entry added with `changie new` (or the PR is labelled `skip-changelog` for docs/CI-only changes)
- [ ] README / CONTRIBUTING updated if behaviour or commands changed
- [ ] No new runtime dependencies (standard library only)
