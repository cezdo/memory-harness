# Memory Harness

[中文](README.md)

> “Some things, I don't want to start over from scratch next time.”

Memory Harness lets an AI assistant remember what it has been through with its user — across sessions, tools and projects — and every memory traces back to the original words.

It wraps the assistants you already use: Claude Code, Codex and dsh today. The assistant works as usual; in the background, Memory Harness reads the conversations, turns them into memories that can be recalled, and brings them back when the next conversation starts.

```mermaid
flowchart LR
  A[Original conversations<br/>append-only] -->|observe| B[Events<br/>what happened]
  B -->|form| C[Session records · topics · lines]
  C -->|cognize| D[Cognitions]
  C & D -->|Synapse| H[Next conversation<br/>standing context · recall · traces]
  R((rhythm)) -.-> A & B & C
```

## What makes it different

### One “I” across hosts and projects

Whatever model or tool is in use today, the “I” in memory is the same assistant. The program stamps every record with two layers of context:

- **the “I” back then**: model, platform, working directory and time of the original conversation;
- **the “I” looking back**: the model, prompt and isolation used to process it.

An agent reading the record months later knows the conditions under which that judgment was made.

### Every statement has a source

Three invariants hold the system together, and `mh engram check` verifies them by program:

1. **Originals are append-only**: conversations are kept verbatim, with addresses back to the host's original files;
2. **Derived records cite their sources**: every observation points to the text it read and the model and prompt version that produced it;
3. **Supersede, never overwrite**: a new understanding points to the old one, and the old one stays readable.

Recall is therefore more than a remembered conclusion. From a handle such as `L3:T5:S1` you can walk all the way back to what was actually said.

### History is replayed in order, not flattened into “now”

Memory is formed by replaying sessions from the earliest, each step seeing only the history before it. What formed first appears first; later revisions and reversals are kept in the order they happened, so the supersession chain is a real timeline. With a better model you can re-read the same history and keep both readings.

### Reading with different depths

Not every conversation deserves a word-by-word read. observe previews first, then decides on a precise read, a rough read, or none, spending model quota where it matters. rhythm drives the observe, form and tidy loops on wall-clock time and picks models by remaining quota.

### Strict inside, human outside

The further in, the stricter and more mechanical: the program locates citations, supersession is auditable, and model outputs are never scored. The further out, the more human: memories arrive as the assistant's own recollection, and a word that memory has never seen gets an honest “doesn't ring a bell” instead of a guess:

```text
<memory-harness>
「thinkterm」没印象，记忆里还没有，当新的看。
</memory-harness>
```

(“No memory of ‘thinkterm’ yet — treat it as new.”) Writing and recall share one set of expression rules that keep authors, addressees and readers apart and preserve who said each quote.

### Start with the core and recent progress

Each session starts with the core text (`core.md`), recall tool instructions, workspace status and the current environment, in that order. Workspace status lists recently paused sessions with topic handles to help resume work. With core-editing hooks enabled, the core is normally read-only; the user can authorize edits for one turn with `/mhcore` (`$mhcore` in Codex), and changes are appended to a kernel log. See [the usage guide](docs/usage.en.md#core-and-session-start-context) for installation and host support.

### Complements the host's own memory

A host's memory is RAM: current rules, preferences and project state, loaded whole every time. Memory Harness is the SSD: the full history — how things happened, why, the back-and-forth, the original words — across hosts and workspaces, accumulating over time. It does not compete on recalling today's rules; it does what host memory cannot: long-span reading (how something came to be, phase summaries, timelines) and recall across projects and tools.

### Mistakes are correctable and leave a trail

The system expects to make mistakes, but keeps them bounded, correctable and not repeated once traced. A mistake is corrected at the layer where it happened, as a new record citing the original text — never a silent overwrite. Evaluation avoids scores too: the program finds moments where the user had to explain something again and leaves them for a person to look at.

## Three ways to recall

| Tool | What it does |
|---|---|
| `evoke` | Give a few words; recall related topics and statements |
| `relive` | Expand a handle, down to the original words when needed |
| `roam` | Browse by workspace, line and topic when you don't know which word to try |

They plug into hosts as MCP tools and also work from the command line: `mh evoke`, `mh relive`, `mh roam`.

## Status

Under active development; mainly for Linux, Chinese-language conversations and local use.

- **Working**: collection, observation, session records and topic lines, standing context at session start, MCP recall, traces and feedback, integrity checks.
- **In progress**: automatic cognition from session records and topic lines, Atlas map rebuilding and unified scheduling. Per-turn association is recorded but not delivered; “no memory of this word” hints are delivered.

The bundled Claude Code, Codex and dsh adapters are ready-to-use examples. To support another harness, write your own adapter following them.

## Getting started

```sh
python3 -m pip install --no-cache-dir --no-build-isolation .
mh --help
```

Requires Python 3.11 or later. Instance configuration, model calls, host integration and optional tools are covered in [Install, configure and integrate](docs/usage.en.md).

Development and tests: `python3 tools/test.py`. Tests run in temporary directories with synthetic data and no real model calls. Contributor rules are in [AGENTS.md](AGENTS.md).

Licensed under Apache-2.0; see [LICENSE](LICENSE).
