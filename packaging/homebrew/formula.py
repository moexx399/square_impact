"""Write the Homebrew formula for a square_impact release.

The release workflow (.github/workflows/build-exe.yml) runs this after
publishing a release, then pushes the result to the tap repository, so
`brew upgrade` picks up each new version. It can also be run by hand:

    python packaging/homebrew/formula.py --version 1.2.3 --tag v1.2.3 --dist DIR [--out square_impact.rb]

DIR holds the release's macOS and Linux zips (square_impact-macos-arm64.zip,
square_impact-macos-x64.zip, square_impact-linux-x64.zip); their SHA-256
checksums go into the formula. Standard library only.
"""

import argparse
import hashlib
import os

REPO = "moexx399/square_impact"
# Homebrew platform -> release zip (the names release_platform() in square_impact.py uses)
ZIPS = {"macos_arm": "square_impact-macos-arm64.zip",
        "macos_intel": "square_impact-macos-x64.zip",
        "linux_intel": "square_impact-linux-x64.zip"}

TEMPLATE = '''# Generated for each release by packaging/homebrew/formula.py in {repo}: edits are overwritten.
class SquareImpact < Formula
  desc "Instant high-speed replays from the Square Omni launch monitor's own cameras"
  homepage "https://github.com/{repo}"
  version "{version}"
  license "MIT"

  on_macos do
    on_arm do
      url "{url}/{macos_arm}"
      sha256 "{sha_macos_arm}"
    end
    on_intel do
      url "{url}/{macos_intel}"
      sha256 "{sha_macos_intel}"
    end
  end

  on_linux do
    on_intel do
      url "{url}/{linux_intel}"
      sha256 "{sha_linux_intel}"
    end
  end

  def install
    # one self-contained executable, adb built in
    chmod 0755, "square_impact"
    bin.install "square_impact"
    doc.install "README.md", "CHANGELOG.md"
    (share/"square_impact").install "adb-NOTICE.txt"
  end

  def caveats
    s = <<~EOS
      Shots and settings (square_impact.json) are kept in your user data folder:
        #{{OS.mac? ? "~/Library/Application Support/square_impact" : "~/.local/share/square_impact"}}
      Settings page while it runs: http://localhost:8765/settings
      Update with: brew upgrade square_impact
    EOS
    if OS.linux?
      s += <<~EOS

        Linux: adb needs permission to open the Omni's USB device without root.
        Add a udev rule, then unplug and replug the Omni:
          sudo tee /etc/udev/rules.d/51-square-omni.rules <<'RULE'
          SUBSYSTEM=="usb", ATTR{{idVendor}}=="18d1", ATTR{{idProduct}}=="d002", MODE="0660", TAG+="uaccess"
          RULE
          sudo udevadm control --reload-rules
      EOS
    end
    s
  end

  test do
    assert_match "square_impact", shell_output("#{{bin}}/square_impact --help")
  end
end
'''


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def formula(version, tag, shas, repo=REPO):
    """The formula's text. shas: {"macos_arm"|"macos_intel"|"linux_intel": sha256 hex}."""
    missing = set(ZIPS) - set(shas)
    if missing:
        raise ValueError("no checksum for " + ", ".join(sorted(missing)))
    return TEMPLATE.format(repo=repo, version=version.lstrip("v"),
                           url=f"https://github.com/{repo}/releases/download/{tag}",
                           **ZIPS, **{f"sha_{k}": v for k, v in shas.items()})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="release version, e.g. 1.2.3")
    ap.add_argument("--tag", required=True, help="the release's git tag, e.g. v1.2.3 or 1.2.3")
    ap.add_argument("--dist", required=True, help="folder with the release's macOS and Linux zips")
    ap.add_argument("--repo", default=REPO, help=f"GitHub repository (default {REPO})")
    ap.add_argument("--out", default="square_impact.rb", help="where to write the formula")
    a = ap.parse_args()
    shas = {}
    for key, name in ZIPS.items():
        path = os.path.join(a.dist, name)
        if not os.path.exists(path):
            raise SystemExit(f"missing {path}")
        shas[key] = sha256(path)
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(formula(a.version, a.tag, shas, a.repo))
    print(f"wrote {a.out} for {a.version}")
    for key, sha in shas.items():
        print(f"  {ZIPS[key]}: {sha}")


if __name__ == "__main__":
    main()
