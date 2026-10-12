# Contributing to square_impact

Thanks for helping! Bug reports, shot captures from setups we haven't seen,
fixes, and features are all welcome. By taking part you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

These rules aren't negotiable. Pull requests that break them can't be merged.

1. **Never write to the Omni.** square_impact only *reads* from the device:
   `/dev/mem`, `/tmp/sqocam.cfg` and `/var/log/messages`. No `adb push`, no files
   in `/tmp`, no redirects (`>`), no `rm`/`mv`/`mount`/`setprop`, no killing or
   restarting sqocam, no config changes. The only exceptions are `stty -opost` on
   our own temporary adb session terminal, and the log follower stopping or
   checking the `tail` it started itself (`kill $tp`, `kill -0 $tp`).
   `tests/test_square_impact.py` has a guard test for the generated device
   commands. If you add a new device command, add it to that test.
3. **Keep work off the Omni.** Parse logs and process images on the PC. The
   Omni should only run the one log follower and plain `dd` reads of the
   buffers a shot needs.
2. **Standard library only.** Users run it with a bare Python or the exe, so
   there are no runtime dependencies. Dev tools (ruff, PyInstaller) are fine.

## Getting set up

```bash
git clone https://github.com/moexx399/square_impact
cd square_impact
python -m unittest discover -s tests -v   # offline tests, no device needed
pip install ruff && ruff check .          # lint (config in pyproject.toml)
```

To run it against hardware you need Windows, Python 3.10+,
`adb` (`winget install Google.PlatformTools`) and a Square Omni connected by
USB. Start with `python square_impact.py status`.

You'll also want [changie](https://changie.dev) for changelog entries:
`winget install miniscruff.changie` (or `brew install changie`, or
`go install github.com/miniscruff/changie@latest`).

## Working without an Omni

Most of the code can be developed and tested offline:

- **Image logic** (contact detection, ball tracking, ring seam): the tests
  synthesise frames. Add a case to `tests/test_square_impact.py` that
  reproduces the situation.
- **Viewer and OBS overlay:** `python square_impact.py view <shot folder>` opens any
  saved capture. For the overlay, a captured shot folder is enough to drive it
  through `/replay`.
- **Real captures:** if you have an Omni, shot folders (`shots/...`) attached to
  issues are the most valuable thing you can contribute. Please zip the whole
  folder.

## Making a change

1. **Open an issue first** for anything bigger than a small fix, so we can agree
   on the approach.
2. Create a branch, make the change, and add tests for logic that can run offline.
3. **Add a changelog entry:**
   ```bash
   changie new
   ```
   Pick the kind (Added, Changed, Deprecated, Removed, Fixed, Security) and write
   one line from a *user's* point of view. This creates a small file in
   `.changes/unreleased/`. Commit it with your change. CI checks for it on pull
   requests. Docs- or CI-only PRs can use the `skip-changelog` label instead.
4. Run the tests and `ruff check .`.
5. Open a pull request and fill in the template. Say how you tested it,
   especially whether it was on a real Omni and which board.

### Code style

- Match the surrounding code: small functions, comments that explain *why*
  (device quirks, timing), not what.
- Line length 110. `ruff check .` must pass.
- Device facts (memory addresses, log lines, timings) are documented in
  [docs/DEVICE_NOTES.md](docs/DEVICE_NOTES.md). Update it if you find out
  something new.

## Releasing (maintainers)

The version lives in `square_impact.py` (`VERSION`) and `pyproject.toml`.
changie updates both.

```bash
changie batch auto      # collects .changes/unreleased into .changes/vX.Y.Z.md, bumps VERSION
changie merge           # regenerates CHANGELOG.md
git add -A && git commit -m "Release vX.Y.Z"
git tag vX.Y.Z && git push && git push --tags
```

Pushing the tag runs `.github/workflows/build-exe.yml`, which checks the tag
matches `VERSION`, builds `square_impact.exe` with adb bundled, and publishes a
GitHub Release using `.changes/vX.Y.Z.md` as the notes.

While the project is `0.x`, breaking changes bump the minor version. See
`.changie.yaml`.
