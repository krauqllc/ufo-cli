# Pinned to the published, signed CLI 1.6.0 release. The URL, size and
# digest were verified against GitHub and the owner's signed SHA256SUMS.
#
# This belongs in a Krauq-maintained tap (for example, krauqllc/homebrew-tap,
# installed as `brew tap krauqllc/tap`), not in homebrew-core. Core does not
# accept a binary-only formula for software that is not open source, and the
# engine binaries are not: see packaging/linux-cli/ENGINE-LICENSE.txt. The
# Apache 2.0 command layer is a subset of the source tree, not of the shipped
# binary.
#
# Linux x86_64 is the supported target. Native macOS needs its own accepted,
# signed and notarized artifact before a formula can offer it.

class Ufo < Formula
  desc "Inspect files, extract content, and make guarded edits locally"
  homepage "https://universalfileopener.com/cli/"
  version "1.6.0"
  # The shipped binaries are not under an SPDX-expressible license. The command
  # layer source is Apache-2.0; the distributed executable is not.
  license :cannot_represent

  depends_on :linux
  depends_on arch: :x86_64

  on_linux do
    on_intel do
      url "https://github.com/krauqllc/ufo-cli/releases/download/v1.6.0/universal-file-opener-1.6.0-linux-x64.tar.gz"
      sha256 "50a70b90fd474a88f090dda1aafd3c59efe476b36f1d1d1e83b5703ad1584ca4"
    end
  end

  def install
    # The artifact is an unpacked application directory with an embedded Java
    # runtime, not a single relocatable executable. Keep the tree intact under
    # libexec and expose only the launcher, so the runtime, the jlink image and
    # the AppCDS archive stay next to each other.
    libexec.install Dir["*"]
    bin.install_symlink libexec/"bin/ufo"

    # Homebrew promotes standard metafiles out of libexec when the prefix
    # has none. These links retain the complete approved application tree.
    prefix.install_symlink libexec/"LICENSE", libexec/"NOTICE"

    # Expose the shipped notices without moving any pinned runtime files.
    doc.install_symlink libexec/"ENGINE-LICENSE.txt", libexec/"THIRD-PARTY-NOTICES.txt",
                        libexec/"NOTICE", libexec/"LICENSE"
  end

  def caveats
    <<~EOS
      UFO processes files locally. It makes no network request on any document
      path and sends no telemetry. A connected agent can still forward results
      to its model provider.

      The engine binaries are licensed under business use terms, not an open
      source license. Free for individuals, evaluation, education,
      non-commercial use and commercial organizations with fewer than ten
      people and less than USD 1 million in annual revenue plus funding.
      Read the terms before commercial use in a larger organization.

      The embedded runtime needs no system Java. Rendering pages and conversion
      to PDF also need libGL, libX11 and fontconfig; run ufo doctor on this host.
      Linux x86_64 with glibc 2.35 or later is required; Alpine/musl is unsupported.
      Ubuntu 24.04 and Debian 12 are the tested installation targets.

      Put UFO in an assistant's path with:
        ufo skill install --agent claude-code
        ufo mcp print-config --client claude-code
      Replace the printed allowed-root placeholders with existing absolute
      task directories. No directories are granted by default.
    EOS
  end

  test do
    # doctor is the real installation check: it creates a private temporary
    # directory, round-trips generated text through the bounded parser worker,
    # verifies source preservation and proves that a second write to the same
    # name is refused. It reads no user document and makes no network request.
    assert_match version.to_s, shell_output("#{bin}/ufo --version")
    system bin/"ufo", "doctor"
    assert_match "\"schema\"", shell_output("#{bin}/ufo capabilities")
  end
end
