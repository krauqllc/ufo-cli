# UFO CLI

**Your agent. Any file.** UFO gives accessible regular files useful basic
information, including unfamiliar formats. Dedicated readers go deeper on
supported documents, images, archives, media and text/data. Screen supported
files for hidden content and privacy risks, then make targeted changes in new,
reviewable copies. One offline CLI and local MCP server, with JSON receipts.

Inspection reports its coverage gaps. Basic information for an unknown file
does not imply a dedicated reader, editor or security inspector for its format.
File and resource limits apply.

**Free for individuals and small teams.** Every command and supported file type
is included, with no signup or watermark. Organizations with fewer than ten
people and under USD 1 million in annual revenue, including funding, qualify;
any organization can evaluate it free. See the
[eligibility details](https://universalfileopener.com/cli/#license).

Website and comparison with other agent tools: https://universalfileopener.com/cli/

## Install

Native Linux x86_64 installation needs glibc 2.35 or later (Ubuntu 24.04 and
Debian 12 tested). Native Alpine/musl installation is unsupported. The Java
runtime is included; the container option below uses its own runtime.

```sh
curl -fsSL https://universalfileopener.com/install.sh | sh
ufo doctor
ufo skill install --agent claude-code --dir "$PWD"
```

[`install.sh`](https://github.com/krauqllc/ufo-cli/blob/main/install.sh) downloads the app image from the latest
[release](https://github.com/krauqllc/ufo-cli/releases), checks it against the
release's `SHA256SUMS` and installs it under `~/.local`, without sudo. The same
release has a Debian package (`sudo apt install ./universal-file-opener_1.5.1_amd64.deb`)
and `ufo-cli-1.5.1-docker.tar`, an archive of the container image for offline transfer.

### Container

From a directory containing `report.docx`, download and run the public image:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --user "$(id -u):$(id -g)" --security-opt no-new-privileges \
  --pids-limit 128 --cpus 2 --memory 1g \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --mount type=bind,src="$PWD",dst=/input,readonly \
  ghcr.io/krauqllc/ufo-cli:1.5.1 text /input/report.docx
```

No GitHub login is needed. The temporary mount is required. For edits, mount a
separate writable output folder at `/output` and select a new file there. The
container reads, edits and recognizes text; page rendering and Office-to-PDF
conversion need the application package. For a deployment pinned to an immutable
image, use the digest in the [release notes](https://github.com/krauqllc/ufo-cli/releases/tag/v1.5.1).

### Connect an agent

Run in the project folder. For Cursor use
`ufo skill install --agent cursor --dir "$PWD"`. For Codex use
`ufo skill install --agent codex --dir "$PWD" --replace`; it adds or updates
only the marked UFO section of the project's `AGENTS.md`, keeping the rest.
Start a new session after installing the guide.

For an MCP client instead of the skill, `ufo mcp print-config --client claude-code`
prints a template. Replace its executable and folder placeholders, then merge
it into the client configuration. Printing does not register a server. Follow
the [complete setup instructions](https://universalfileopener.com/cli/#agents).

## Get your first useful result

From a clone of this repository, with Python 3.10+ and `jsonschema` installed:

```sh
python3 tools/cli/first_run_demo.py --ufo "$(command -v ufo)" \
  --expect-version 1.5.1 --output first-look --mcp
```

The short example generates two fictional inputs. It collects basic information
for an unfamiliar file, finds document properties, a reviewer comment and hidden
text in a supported DOCX, and writes a clean copy. It independently checks the
saved ZIP/XML bytes, original hashes, full receipt schemas and refusal to replace
an existing output. Open `first-look/START_HERE.txt` to review the results.

Every `notInspected` gap and skipped clean category remains in the evidence.
The optional MCP check discovers tools and calls inspection over local stdio;
it does not establish that an installed assistant has connected successfully.

<details>
<summary>See a complete mixed-file editing workflow</summary>

[Watch the 64-second film](https://universalfileopener.com/cli/#film), then
[inspect and replay the workflow](https://universalfileopener.com/cli/#example).
A fictional change brief updates a spreadsheet model, a Word memo with native
tracked revisions, and two slides in a board deck.

The film uses actual Microsoft Office exports of the downloadable files. The
replay needs only Python 3 and UFO, with no agent or model API. It checks all
20 recomputed saved results, existing formulas, earlier periods, native Word
revisions, original hashes and untouched package parts. Every edit matches its
dry-run output hash; an intentionally stale source hash refuses. This is an
edited script demonstration, not an agent recording or a timing benchmark.

</details>

## Working efficiently

Read the help for the operation you need: `ufo edit paragraphs --help`. Locate
text with `ufo find --text 'budget' --compact report.docx`, then read selected
paragraphs, cells or slides with `ufo text --format structure --compact`.
Batch independent changes to one file, and reuse the output receipt's hash for
the next edit. Store full receipts when you need an audit record.

`--compact` applies to `text`, `find`, `inspect --deep` and `edit`; `intake`,
`convert` and `unpack` do not accept it. For MCP, read `tools/list` schemas:
`find` takes one file and `report` requires an output path.

Only operations that declare `--expect-sha256` accept revision guards.
`replace-text`, PDF page edits, image transforms and archive edits do not.
Check each operation's help rather than copying another operation's flags.
Always review what a receipt skipped and inspect the output before handoff.

## What is in this repository

The binaries are on the Releases page. This repository holds what you need to
check them:

| Path | What it is |
| --- | --- |
| `docs/cli/schemas/` | JSON schemas for every receipt and report the CLI writes |
| `tools/cli/first_run_demo.py` | The short unfamiliar-file and inspection/clean-copy example, with independent checks |
| `tools/cli/own_file_evaluation.py` | Runs UFO on your own files: reads, reversible edits and an independent check of every output |
| `tools/cli/evaluate_sample.py` | The end-to-end evaluation on generated sample files |
| `tools/corpus/compare_agent_tools.py` | The comparison with other agent file tools, on the same tasks and checks |
| `tools/corpus/realworld_corpus_manifest.tsv` | The public files that comparison uses, with their sources and SHA-256 |
| `tools/corpus/download_realworld_corpus.py` | Downloads those files and verifies each one |

The other files under `tools/` are the helpers these import. They need Python
3.10 or newer, `jsonschema` for receipt validation, and an installed UFO build.

Reviewed distribution source is also available under `packaging/npm/`,
`packaging/linux-cli/registry/` and `packaging/homebrew/`. These wrappers and
descriptors target the same Linux x86_64 release. The
[Homebrew tap](https://github.com/krauqllc/homebrew-ufo) is public:
`brew install krauqllc/ufo/ufo`. The release also provides a complete local
MCPB bundle for compatible Linux clients, listed in the
[official MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.krauqllc%2Fufo-cli/versions/1.5.1-1).
Directory revision `1.5.1-1` keeps engine and bundle version `1.5.1` unchanged.
Choose only existing task folders when installing the bundle; no directories
are granted by default. npm publication awaits the publisher's 2FA; Smithery
and Glama account submission are separate pending steps.

## Check it yourself

```sh
UFO=$(command -v ufo)
python3 tools/cli/evaluate_sample.py --ufo "$UFO" --expect-version 1.5.1 --output sample-result
python3 tools/cli/own_file_evaluation.py --files ~/my-files --ufo "$UFO" --expect-version 1.5.1 --output my-files-report
python3 tools/corpus/download_realworld_corpus.py --download --output corpus
python3 tools/corpus/compare_agent_tools.py --download --corpus tools/corpus/realworld_corpus_manifest.tsv --corpus-root corpus --ufo "$UFO"
```

`--download` fetches the pinned releases of the other tools, and
`--setup-libraries` creates the Python library environment the comparison uses.
Both need the network once; the runs themselves do not.

## License

The files here are Apache 2.0 (`LICENSE`, `NOTICE`). The binaries are licensed
under `ENGINE-LICENSE.txt`: free for individuals, evaluation, education,
non-commercial use, and organizations with fewer than ten people and under USD 1
million in annual revenue, including funding. Larger organizations need a
[license](https://universalfileopener.com/cli/pricing/).

Install a delivered file with `ufo license install ./license.json`, then run
`ufo license status`. A fresh file arrives for each paid period; install
renewals with `ufo license install ./license.json --replace`. Checks are offline.
For delivery or recovery help, contact support@krauq.com with the order number.

## Security

Report a security problem to support@krauq.com with "Security" in the subject.
