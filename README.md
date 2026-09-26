# UFO CLI

Let your agent update the files your clients already use. UFO reads and edits
Word reports, Excel workbooks, PowerPoint decks, PDFs and supporting files on
your machine. It keeps originals, writes new copies and returns JSON receipts.
It makes no network connection.

**Free for individuals and small teams.** Every command and supported file type
is included, with no signup or watermark. Organizations with fewer than ten
people and under USD 1 million in annual revenue qualify; any organization can
evaluate it free. See the [eligibility details](https://universalfileopener.com/cli/#license).

Website and comparison with other agent tools: https://universalfileopener.com/cli/

## Install

Linux x86_64:

```sh
curl -fsSL https://universalfileopener.com/install.sh | sh
ufo doctor
ufo skill install --agent claude-code --dir "$PWD"
```

[`install.sh`](https://github.com/krauqllc/ufo-cli/blob/main/install.sh) downloads the app image from the latest
[release](https://github.com/krauqllc/ufo-cli/releases), checks it against the
release's `SHA256SUMS` and installs it under `~/.local`, without sudo. The same
release has a Debian package (`sudo apt install ./universal-file-opener_1.5.0_amd64.deb`)
and `ufo-cli-1.5.0-docker.tar`, an archive of the container image for offline transfer.

### Container

From a directory containing `report.docx`, download and run the public image:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --user "$(id -u):$(id -g)" --security-opt no-new-privileges \
  --pids-limit 128 --cpus 2 --memory 1g \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --mount type=bind,src="$PWD",dst=/input,readonly \
  ghcr.io/krauqllc/ufo-cli:1.5.0 text /input/report.docx
```

No GitHub login is needed. The temporary mount is required. For edits, mount a
separate writable output folder at `/output` and select a new file there. The
container reads, edits and recognizes text; page rendering and Office-to-PDF
conversion need the application package. For a pinned deployment, the 1.5.0
image digest is `sha256:6642403388fda95c597706c8cf57fe63d37a0931fd67126d62d86982e7a50a24`.

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

## Try a client update

Run the [working example](https://universalfileopener.com/cli/#example): changing
design hours from 40 to 48 recalculates a workbook's fee, then updates a Word
report with a tracked change and a PowerPoint slide. It creates fictional
files, needs only Python 3 and UFO, and checks original hashes, dry-run hashes
and every untouched package part. It needs no agent, model API or account.

## Working efficiently

Read the help for the operation you need: `ufo edit paragraphs --help`. Locate
text with `ufo find --text 'budget' --compact report.docx`, then read selected
paragraphs, cells or slides with `ufo text --format structure --compact`.
Batch independent changes to one file, and reuse the output receipt's hash for
the next edit. Store full receipts when you need an audit record.

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
| `tools/cli/own_file_evaluation.py` | Runs UFO on your own files: reads, reversible edits and an independent check of every output |
| `tools/cli/evaluate_sample.py` | The end-to-end evaluation on generated sample files |
| `tools/corpus/compare_agent_tools.py` | The comparison with other agent file tools, on the same tasks and checks |
| `tools/corpus/realworld_corpus_manifest.tsv` | The public files that comparison uses, with their sources and SHA-256 |
| `tools/corpus/download_realworld_corpus.py` | Downloads those files and verifies each one |

The other files under `tools/` are the helpers these import. They need Python
3.10 or newer, `jsonschema` for receipt validation, and an installed UFO build.

## Check it yourself

```sh
UFO=$(command -v ufo)
python3 tools/cli/evaluate_sample.py --ufo "$UFO" --expect-version 1.5.0 --output sample-result
python3 tools/cli/own_file_evaluation.py --files ~/my-files --ufo "$UFO" --expect-version 1.5.0 --output my-files-report
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
million in annual revenue. Larger organizations need a
[license](https://universalfileopener.com/cli/pricing/).

## Security

Report a security problem to support@krauq.com with "Security" in the subject.
