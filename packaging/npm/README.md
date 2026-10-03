# UFO CLI

Useful information for any accessible regular file. Deeper readers screen
supported formats for hidden content and privacy risks, extract content, and
make targeted changes in new copies. UFO runs locally as a CLI or stdio MCP
server. Reports state what was inspected and what remains unknown.

This package installs the exact UFO 1.5.1 runtime. Direct
[Linux downloads](https://github.com/krauqllc/ufo-cli/releases/tag/v1.5.1)
are also available. Install and check your first file:

```sh
npm install --global @krauq/ufo-cli@1.5.1
ufo --version
ufo doctor
ufo inspect --json ./your-file
```

Linux x86_64, glibc 2.35+, Node.js 18.17+ and GNU tar are required. Ubuntu
24.04 and Debian 12 are tested. Native macOS, Windows, ARM64 and Alpine/musl
are unsupported. Java is included. `ufo doctor` also checks optional rendering
dependencies on your host.

Installation downloads the pinned 202 MB release archive over HTTPS and checks
its hash and complete runtime identity. npm install scripts must be enabled.
Document processing sends no network requests or telemetry. A connected agent
can send tool results to its model provider.

For a supported document, try `ufo inspect --deep ./report.docx`. Basic file
information does not promise a reader, editor or security inspector for every
format. Review findings and coverage gaps; `ufo capabilities` and command help
describe the operations available in this release.

## Connect an agent

Install before registering MCP. An uncached `npx` startup can exceed a client's
handshake timeout while it downloads the runtime. An installed server starts
without a download. Print a client template:

```sh
ufo mcp print-config --client claude-code
```

Use the installed `ufo` executable and replace allowed-root placeholders with
existing absolute task directories. Printing does not register a server. UFO
grants no directories by default; use separate input and output folders when
possible. The same local server can be started directly:

```sh
ufo mcp --allow-root /absolute/input --allow-root /absolute/output
```

For a project guide instead, run
`ufo skill install --agent claude-code --dir "$PWD"` and start a new session. See the
[agent setup guide](https://universalfileopener.com/cli/#agents) for other clients.

## License and maintenance

Every command is included. Individuals and eligible small organizations can
use UFO free; larger commercial organizations need a
[business license](https://universalfileopener.com/cli/pricing/).
Wrapper source is Apache 2.0; the full binary terms are in
`ENGINE-LICENSE.txt`. Install a delivered license
with `ufo license install ./license.json`, then check `ufo license status`.
License checks are offline.

The wrapper is pinned to its exact runtime version and never upgrades it
silently. Update by installing a reviewed newer wrapper tarball. `npm install --global @krauq/ufo-cli@VERSION` selects an exact
published version. Rebuilding an intact installation verifies and reuses
it. To check all files in a downloaded runtime, run:

```sh
npm run verify --prefix "$(npm root --global)/@krauq/ufo-cli"
```

Normal launches of that runtime check the launcher hash.

Use `npm uninstall --global @krauq/ufo-cli` to remove the wrapper and downloaded
runtime. It leaves your documents and per-user license in place. The optional
`ufo license remove` command removes that license separately.

To reuse an existing matching 1.5.1 installation, set
`UFO_CLI_SKIP_DOWNLOAD=1` and `UFO_CLI_BINARY` to its absolute `bin/ufo` path
during installation. The resolved path is saved and reused on later launches
and rebuilds. The wrapper rejects another version and its own launcher.
External installations are checked for the matching version, rather than the
downloaded runtime's complete file identity.
