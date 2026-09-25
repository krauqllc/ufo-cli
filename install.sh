#!/bin/sh
# Install UFO CLI for the current user on Linux x86_64, without sudo.
#
#   curl -fsSL https://universalfileopener.com/install.sh | sh
#
# Downloads the app image from the latest release of github.com/krauqllc/ufo-cli
# (or the release named by UFO_VERSION), checks it against that release's
# SHA256SUMS, unpacks it to ~/.local/share/ufo-cli/VERSION and links
# ~/.local/bin/ufo to it. Nothing else is changed. Running it again installs the
# newest release and removes the one before. To uninstall:
#
#   rm -rf ~/.local/share/ufo-cli ~/.local/bin/ufo
set -eu

# Everything runs from main, called on the last line, so a download cut short runs nothing.
main() {
  repo=https://github.com/krauqllc/ufo-cli
  share=$HOME/.local/share/ufo-cli
  bin=$HOME/.local/bin
  link=$bin/ufo

  say() { printf 'ufo install: %s\n' "$*" >&2; }
  fail() { say "$*"; exit 1; }

  [ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] ||
    fail "UFO CLI runs on Linux x86_64 for now, and this is $(uname -s) $(uname -m)."
  command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is needed to check the download."
  if command -v curl >/dev/null 2>&1; then
    if [ -t 2 ]; then progress=-#; else progress=-sS; fi
    download() { curl -fL --proto '=https' --tlsv1.2 --retry 3 "$3" -o "$2" "$1"; }
  elif command -v wget >/dev/null 2>&1; then
    progress=
    download() { wget -q -O "$2" "$1"; }
  else
    fail "curl or wget is needed to download the release."
  fi

  # Refuse before downloading anything if ~/.local/bin/ufo is someone else's file.
  if [ -e "$link" ] || [ -L "$link" ]; then
    case $(readlink "$link" || true) in
      "$share"/*) ;;
      *) fail "$link exists and was not installed by this script. Move it away and run again." ;;
    esac
  fi

  mkdir -p "$share" "$bin"
  # Unpack beside the destination so the final move is a rename on one file system.
  tmp=$(mktemp -d "$share/.install.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT
  trap 'exit 130' INT TERM

  if [ -n "${UFO_VERSION:-}" ]; then
    sums_url=$repo/releases/download/v${UFO_VERSION#v}/SHA256SUMS
  else
    sums_url=$repo/releases/latest/download/SHA256SUMS
  fi
  download "$sums_url" "$tmp/SHA256SUMS" -sS || fail "could not download $sums_url"
  line=$(grep -E '^[0-9a-f]{64}  universal-file-opener-[0-9]+\.[0-9]+\.[0-9]+-linux-x64\.tar\.gz$' "$tmp/SHA256SUMS") ||
    fail "the release's SHA256SUMS lists no Linux app image."
  name=${line#*  }
  version=${name#universal-file-opener-}
  version=${version%-linux-x64.tar.gz}

  say "downloading UFO CLI $version"
  download "$repo/releases/download/v$version/$name" "$tmp/$name" "$progress" || fail "could not download $name"
  (cd "$tmp" && printf '%s\n' "$line" | sha256sum -c --status) ||
    fail "$name does not match the release's SHA256SUMS; nothing was installed."

  tar -xzf "$tmp/$name" -C "$tmp"
  image=$tmp/universal-file-opener-$version-linux-x64
  "$image/bin/ufo" --version >/dev/null || fail "the unpacked ufo did not start; nothing was installed."

  target=$share/$version
  [ ! -e "$target" ] || mv "$target" "$tmp/replaced"
  mv "$image" "$target"
  ln -sfn "$target/bin/ufo" "$link"
  for old in "$share"/*; do
    case ${old##*/} in
      "$version") ;;
      [0-9]*.[0-9]*.[0-9]*) rm -rf "$old" ;;
    esac
  done

  say "installed UFO CLI $version as $link"
  case ":$PATH:" in
    *":$bin:"*)
      found=$(command -v ufo || true)
      [ "$found" = "$link" ] || say "another ufo comes first on your PATH: $found"
      ;;
    *) say "add $bin to your PATH, for example: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac
  # Reading and editing need nothing else. Page rendering and conversion to PDF load libGL, libX11
  # and fontconfig, which a desktop has and a minimal server or container may not.
  if command -v ldd >/dev/null 2>&1 && ldd "$target/lib/app/libskiko-linux-x64.so" 2>/dev/null | grep -q 'not found'; then
    say "to render pages and convert to PDF, also install libGL, libX11 and fontconfig"
    say "  (Debian or Ubuntu: sudo apt install libgl1 libx11-6 libfontconfig1)"
  fi
  say "next: ufo doctor, then ufo skill install --agent claude-code (or codex, cursor, ...)"
  say "license terms: $target/ENGINE-LICENSE.txt"
}

main "$@"
