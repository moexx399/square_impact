# Changelog

All notable changes to square_impact are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are managed with [changie](https://changie.dev): add one with
`changie new` (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## v0.16.0 - 2026-10-29
### Added
* Homebrew: brew install moexx399/tap/square_impact on macOS and Linux. Each release updates the tap's formula automatically; Homebrew installs keep shots and settings in the user data folder and update with brew upgrade

## v0.15.0 - 2026-10-29
### Changed
* New defaults: the bottom launch camera (was top) and denoise off (was on). Set cam: top or denoise: true on the settings page to go back; --denoise turns denoise on for one run
### Fixed
* Settings page preview: picking a different saved shot froze the preview until the page was reloaded. The current clip now keeps playing while the new one renders, the new shot starts from its first frame, and playback can no longer stop on a drawing error
* Driver shots: a ball that leaves the camera's view within a frame or two (a driver off a tee) was found 13-15 frames late, so the replay showed the ball sitting on the tee. Contact now also counts the ball's spot going empty

## v0.14.1 - 2026-10-26
### Fixed
* The ball crop no longer snaps to the left edge: the low bottom camera sees the resting ball flatter than the tracker allowed, so it wasn't found at the start of the clip. The flatter ball is now accepted, and the crop falls back to the resting ball the capture finds

## v0.14.0 - 2026-10-26
### Added
* macOS (Apple Silicon and Intel) and Linux builds: every release now ships square_impact-macos-arm64.zip, square_impact-macos-x64.zip and square_impact-linux-x64.zip next to the Windows zip, each with adb built in, and square_impact update fetches the right one
### Fixed
* Two square_impacts could both listen on the overlay port on Windows, and OBS's requests landed on either; the port is now exclusive, and obs says clearly when it's taken

## v0.13.0 - 2026-10-26
### Added
* Settings: every option can be set in square_impact.json next to the exe, and on a settings page (http://localhost:8765/settings, a Settings button in live, or square_impact settings) that saves to it. The page previews a saved shot with your changes as you make them, before saving. Most changes apply without a restart; command-line flags still win for that run

## v0.12.1 - 2026-10-19
### Fixed
* The OBS replay no longer needs a cache refresh after square_impact restarts or updates: the page reconnects by itself (keep-alive pings every 5 s, reconnect after 15 s of silence). The README explains starting square_impact before OBS

## v0.12.0 - 2026-10-19
### Changed
* The club stands out from the turf again in enhanced replays: moving things get the full shadow lift and the still turf and room a gentler one (the club is ~3.7x brighter than the turf behind it, up from ~2.3x)

## v0.11.0 - 2026-10-17
### Added
* Clearer launch-camera replays: the dark scene's shadows are lifted (the ball keeps its shading) and edges are sharpened without boosting grain, so the club and shaft are easier to see through the swing. --no-enhance turns it off
### Fixed
* The impact frame is now the frame nearest the moment of contact: the ball's flight is timed back to its launch, and fast balls are no longer tracked late. Previously it could be a frame or two before the club reached the ball

## v0.10.0 - 2026-10-16
### Changed
* The ball crop (the default) now centres on the ball where it starts and then holds still, so the club and ball move through the frame; --crop follow keeps the old window that pans with the ball
### Fixed
* Console colour is only used when the terminal can show it: NO_COLOR and TERM=dumb now turn off all colour, and consoles that can't process colour codes get plain text instead of raw codes

## v0.9.0 - 2026-10-12
### Added
* The console header now shows the logo beside the title (on terminals with colour; plain text when output is redirected or NO_COLOR is set)

## v0.8.0 - 2026-10-12
### Added
* App logo: shown on the viewer window, in the taskbar and Alt+Tab, as the OBS overlay page's favicon, and as the exe's icon

## v0.7.0 - 2026-10-12
### Added
* Spin camera support: with the spin board (r2) USB port connected, each shot shows a sharp, strobe-lit close-up of the ball leaving the club
* Both boards at once: what is captured and shown follows which USB ports are connected (launch, spin, or both), re-checked every few seconds; with both, a shot plays the launch swing then the spin close-up
* Launch-camera frames are denoised for display (frame averaging that keeps the club and ball sharp); `--no-denoise` turns it off
* OBS overlay `fps2` option sets the spin close-up playback speed (default 10)
* `status` lists every connected board; `grab`/`sample` take `--board r1|r2`
* Impact snapshot: each shot saves impact.png (launch camera's contact frame over the spin camera's first lit frame); the overlay freezes on the impact frame (hold, default 700 ms) and serves the latest snapshot at /impact.png
### Changed
* The overlay and viewers show a 640x192 window that follows the ball on both cameras (`--crop ball`, the new default), keeping it centred
* Shots are saved as `shots/<time>_shot_<boards>/` with one segment per board; older shot folders still open
* The Omni does much less: one log follower per board instead of one per capture session; all parsing happens on the PC, and the Omni only does plain memory reads when a shot is logged
### Fixed
* Closed or crashed sessions no longer leave adb clients and log followers running on the PC and the Omni

## v0.6.0 - 2026-10-11
### Changed
* The OBS overlay and viewers also skip the last 20% of each clip (ball rolling away), so clips are contact +/-18 frames; `--trim-back` adjusts it and saved clips keep every frame

## v0.5.0 - 2026-10-11
### Changed
* The OBS overlay and viewers skip the first 20% of each clip (club not yet moving), so playback starts closer to the action; `--trim-front` adjusts it and saved clips keep every frame

## v0.4.0 - 2026-10-11
### Added
* `obs` and `live` re-arm every 5 minutes without a shot and wait for the Omni to reconnect if it sleeps, reboots or is unplugged
* `--crop left|right|full` for the OBS overlay and viewers; defaults to the left half (640x192), where the ball and club are. Saved clips keep the full frame
* `square_impact.exe update` downloads the latest release from GitHub, verifies its SHA-256, and replaces the exe
### Fixed
* Long sessions stopped catching shots after ~25 minutes: the Omni rotates its log file and the shot trigger kept reading the old one. The log is now followed across rotations

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
