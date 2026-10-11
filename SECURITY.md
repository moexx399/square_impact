# Security

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them
privately through GitHub:
[Security → Report a vulnerability](https://github.com/moexx399/square_impact/security/advisories/new).

Include what you found, how to reproduce it, and what an attacker could do
with it. You'll get a reply as soon as possible, and credit in the release
notes if you'd like it.

## What's in scope

- **The read-only guarantee.** Any way square_impact could write to, change, or
  disrupt the Omni (its storage, configuration, or the running sqocam process)
  is treated as a security bug.
- **The OBS overlay server.** It listens on `127.0.0.1` only. Anything that
  exposes it to the network, or lets a web page abuse it, is in scope.
- **The exe and release pipeline.** Problems with the bundled adb or the
  GitHub Actions build that could deliver tampered binaries.

## Good to know

- The Omni's camera boards expose a **root ADB shell over USB** to anything
  plugged into them. That's how the device ships, not something square_impact
  sets up, and it applies whether or not you use this tool.
- The exe bundles Google's `adb` from the Android SDK platform-tools, downloaded
  from `dl.google.com` at build time.

Only the latest release is supported with fixes.
