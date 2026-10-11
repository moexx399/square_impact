# Changelog

All notable changes to square_impact are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are managed with [changie](https://changie.dev): add one with
`changie new` (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## v0.3.0 - 2026-10-11
### Added
* The exe checks GitHub for a newer release at startup (in the background, at most daily) and prints a link; turn off with --no-update-check or SQUARE_IMPACT_NO_UPDATE_CHECK=1
* `version` command reports whether a newer release is available
### Changed
* Release tags work with or without a leading v (v1.2.3 or 1.2.3)

## v0.2.0 - 2026-10-11
### Added
* Contributor docs: CONTRIBUTING guide, Code of Conduct, security policy, and issue and pull request templates
* docs/DEVICE_NOTES.md documenting the Omni camera boards: memory layout, shot log sequence, timings and adb quirks
* Offline test suite (no device needed), including a guard that fails if any device command could write to the Omni
* CI runs lint and tests on Python 3.10-3.13 and requires a changelog entry on pull requests
* Changelog managed with changie; GitHub Releases use the changelog notes for each version
### Changed
* Release builds check the tag matches the code version and include CHANGELOG.md in the zip

## v0.1.0 - 2026-10-11
### Added
* Capture the high-speed frames around each shot from the Square Omni launch cameras over USB, read-only (adb + /dev/mem)
* `obs` command: OBS browser-source overlay that pops up each shot, plays it twice in slow motion, then disappears
* `live` desktop viewer that re-arms after every shot and opens each clip on the contact frame
* Automatic contact detection using sub-pixel ball tracking; detections with no ball in view are ignored
* Show the Omni's own ball speed, launch angle, direction and spin with each shot
* `shot`, `view`, `status`, `sample`, `grab`, `impact` and `version` commands
* Keep only the newest 5 captures in `shots/`
* Single-file Windows exe with adb bundled, built and released by GitHub Actions
