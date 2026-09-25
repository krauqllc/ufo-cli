# UFO CLI

UFO CLI lets an AI agent or a script read 274 file types and edit Word, Excel,
PowerPoint and PDF files on your own machine. An edit changes only what it is
asked to, writes a new copy and returns a JSON receipt of what it did. It makes
no network connection.

Website and comparison with other agent tools: https://universalfileopener.com/cli/

## Install

Linux x86_64:

```sh
curl -fsSL https://universalfileopener.com/install.sh | sh
ufo doctor
ufo skill install --agent claude-code
```

[`install.sh`](https://github.com/krauqllc/ufo-cli/blob/main/install.sh) downloads the app image from the latest
[release](https://github.com/krauqllc/ufo-cli/releases), checks it against the
release's `SHA256SUMS` and installs it under `~/.local`, without sudo. The same
release has a Debian package (`sudo apt install ./universal-file-opener_1.5.0_amd64.deb`)
and a container image (`docker load -i ufo-cli-1.5.0-docker.tar`; run it with
networking disabled and your inputs mounted read-only).

For an MCP client instead of the skill, `ufo mcp print-config --client claude-code`
prints the server registration.

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
python3 tools/cli/evaluate_sample.py --ufo $UFO --expect-version 1.5.0 --output sample-result
python3 tools/cli/own_file_evaluation.py --files ~/my-files --ufo $UFO --expect-version 1.5.0 --output my-files-report
python3 tools/corpus/download_realworld_corpus.py --download --output corpus
python3 tools/corpus/compare_agent_tools.py --download --corpus tools/corpus/realworld_corpus_manifest.tsv --corpus-root corpus --ufo $UFO
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
