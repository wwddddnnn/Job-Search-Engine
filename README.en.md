# Job Search Assistant

> Chinese original: [README.md](README.md)

A local-first personal job search assistant. It keeps the whole chain — **find jobs → verify career facts →
match → prepare applications** — on your own machine: **Python 3.12 standard library + SQLite**, with
**zero third-party runtime dependencies** (no package installs, no Node.js, no frontend build), everything
auditable and replayable, and LLMs involved at controlled, reversible points.

Quick orientation:

- **Current work** — Phase 2.5, manual curation & verification UI: S3a delivered, S3b / S4 not started
- **Runnable baseline** — `main` carries all of Phase 2 (S1–S5); Phase 2.5 lives on feature branches, not yet merged
- **Stack** — Python 3.12+ standard library · SQLite · plain modular JS (no framework / no build / no CDN)
- **Tests** — standard-library `unittest` (currently 130 tests green; no network / LLM / MCP / browser access)
- **How it is built** — a two-agent builder + reviewer loop (`codex-hermes-loop.sh`, see below)

## What problem this solves

Job search material is scattered across resumes, notes and all sorts of web pages. Hand it to a model to
"help summarise" and you can no longer tell which statements describe what you actually did from the ones the
model wrote because they sounded good — and you are the one who pays when such a sentence ends up in an
application.

So this project does it the other way round: **a fact must first be traceable to source text and be confirmed
item by item by the user; models may only produce drafts.** Original files, extracted text, every re-run and
every confirmation leave records that cannot be overwritten, so any trusted conclusion can be replayed back
to where it came from.

## Core invariants

An implementation that breaks these gets sent back by the reviewer; they are the reason this project exists,
not a matter of style.

1. **LLM output is always a draft**; facts the user has not explicitly confirmed never enter a published
   version and never appear in a `VerifiedEvidencePack`.
2. Only **explicitly confirmed items** are `verified`; user additions with no document source are recorded as
   `user_assertion` and never disguised as coming from the resume.
3. Original files and extracted text are **kept long-term and replayable**; replacing a file produces a
   **new** document and does not overwrite history.
4. Re-running extraction **creates a new** run; historical runs and their output are never rewritten or deleted.
5. Every confirmation produces a referenceable `ProfileVersion`, with before/after revision audit that can be
   traced back.
6. **Every write path must go through an Application Service** (carrying a `RequestContext` + idempotency key,
   with the service layer writing the audit); adapters must not touch domain tables directly.

## Where the project stands

| Phase | Goal | Status |
|---|---|---|
| **Phase 0** Foundation | Project skeleton and architecture guardrails (module boundaries, migrations, error model, idempotency, audit) | ✅ accepted |
| **Phase 1** Job Discovery | Reliably discover, store and view jobs (raw responses retained, dedup, immutable snapshots) | ✅ accepted |
| **Phase 2** Career Foundation | A verifiable personal career knowledge base (import / extraction / confirmation / evidence pack / snapshot) | ✅ S1–S5 accepted |
| **Phase 2.5** Manual curation & verification UI | Curate, verify and publish career facts in the browser | 🚧 see below |
| **Step B** Real LLM | Integrate a real provider (today only a deterministic fake provider exists) | ⏳ not started |
| **Phase 3** Job Matching | Low-cost, explainable, invalidatable matching results | ⏳ not started |
| **Phase 4–7** | Application Core, Agent-ready Contracts, MCP Adapter, controlled browser submission | ⏳ not started |

Current Phase 2.5 slices:

| Slice | Content | Status |
|---|---|---|
| S1 | Review draft service, source retention, autosave, partial publication and version semantics | ✅ |
| S2a | Single-command startup, JSON HTTP adapter, plain frontend skeleton, Chinese/English, Markdown import, read-only dual view | ✅ |
| S2b | Two-pane editing: create items from selections, edit / verify / reject / delete / undo, autosave state machine, publish and version conflicts | ✅ passed review |
| S3a | Multiple LLM API configs, controlled local credentials, config panel and contract | ✅ delivered |
| S3b | Reference / prompt panel, Mock optimization, one-click replace and undo | ⏳ not started |
| S4 | Add to existing experiences from more documents, automatic Mock merge, complete manual acceptance notes | ⏳ not started |

Branch convention: **one feature branch per slice, one builder + reviewer round per slice, one commit per
round.** The per-phase DoD (verifiable definition of done) and acceptance criteria are not stacked up in this
README — see the [architecture doc §11](docs/Job%20Search%20Assistant%20技术架构文档.md) and the per-phase docs
(see "Docs" at the end).

## Quick start

One prerequisite only: **Python 3.12+** (`requires-python = ">=3.12"`). No dependency installs, no Node.js, no
database server.

```bash
# 1. Clone the code
git clone https://github.com/wwddddnnn/Job-Search-Engine.git
cd Job-Search-Engine

# 2. Point at an interpreter: put the absolute path to python3.12 into the PYBIN= line of dev.env at the repo root
#    On this machine the existing conda env (3.12.14) works:
#    conda create -n Job-Search-Engine python=3.12 -y --solver classic   # --solver classic is required on this machine
#    PYBIN=/opt/homebrew/Caskroom/miniconda/base/envs/Job-Search-Engine/bin/python

# 3. Initialise the database + run the tests
source dev.env
PYTHONPATH=src "$PYBIN" -m job_search_assistant init-db
PYTHONPATH=src "$PYBIN" -m job_search_assistant db-info
PYTHONPATH=src "$PYBIN" -m unittest discover -s tests

# 4. Start the local UI (available since S2a)
PYTHONPATH=src "$PYBIN" -m job_search_assistant serve --port 8000 --host 127.0.0.1
#    → open http://127.0.0.1:8000/ , press Ctrl-C in the terminal to stop
```

A few easy mistakes:

- `dev.env` holds an **absolute path from the original machine**; on a new machine you must change `PYBIN=`.
  A same-named environment variable wins, so you can override it for one run:
  `PYBIN=/path/to/python ./codex-hermes-loop.sh "task"`.
- The default database is `.job-search-assistant/job-search-assistant.sqlite` (already in `.gitignore`);
  delete the whole `.job-search-assistant/` directory to reset.
- `--database` / `--migrations` are **global options and must come before the subcommand**:
  `… python -m job_search_assistant --database /tmp/x.sqlite init-db`.
- To use the command name `job-search-assistant` instead of `python -m job_search_assistant`:
  `pip install -e .` — zero third-party dependencies, so it pulls nothing from the network.
- To try a slice that is still in flight: `git checkout phase2.5/s2a-web-entry` (check the branch has been
  pushed to the remote first).

**Running details and manual acceptance steps** (including the three things automatic tests cannot cover and a
human must click through): [docs/manual-acceptance.md](docs/manual-acceptance.md). The HTTP contracts for each
slice live in [docs/phase2.5-career-review-ui.md](docs/phase2.5-career-review-ui.md).

## How it is built: the builder + reviewer loop

A slice lives on one feature branch and `codex-hermes-loop.sh` runs it as a series of rounds. Every round has
the same rhythm: **the reviewer's verdict from the previous round sets this round's work → the builder writes
it → the test gate → the reviewer inspects and rules → commit and push.** The two agents are independent: the
builder only writes code and only touches the working tree, never git; the reviewer only emits verdict text and
has no ability to write files or run commands; collecting changes, running tests, writing the log, committing
and pushing are all done by the script.

A human appears twice in this pipeline: handing over a task brief at the start (that is where round 1's work
comes from), and making the call when the loop ends with `ESCALATE`.

```text
feature branch
  └─ round N
       ├─ builder  writes code from the task brief
       ├─ collect  stages every change, produces this round's diff
       ├─ gate     runs unit tests — no PASS without them
       ├─ reviewer inspects «architecture / phase docs + this round's diff + test results»
       ├─ log      appends this round's verdict
       └─ commit & push (committed either way, so it stays revertible)
```

### The seven steps of a round

1. **Assemble the builder's input.** The script appends three blocks to the task brief: this project's
   interpreter and test command, the full builder conventions, and **the previous round's review comments plus
   the previous round's test results**. Round 1 has no third block — its work comes entirely from the human's
   task brief; from round 2 on, what to change and how far to go is decided by the reviewer's previous verdict.
   The review comments are the next round's work order.
2. **The builder develops.** By default it runs headless Codex, and its output stream is rendered into
   human-readable lines in real time by a local script; it only touches working-tree files and never commits or
   pushes.
3. **Collect the changes.** The script stages every change in the working tree (including new, untracked files,
   but excluding the development log) and produces a diff. That diff is the only thing under review this round,
   so the reviewer only ever sees the current round's changes.
4. **The test gate.** The project's unit tests run. If they are red, this round cannot reach `PASS`: even when
   the reviewer rules a pass, the script downgrades it to `NEEDS_FIX` and records the reason in the log.
5. **The reviewer inspects and rules.** The script assembles "this task + the full architecture and phase docs +
   this round's diff + the test results" into one input and hands it to Hermes. The first three lines of the
   reviewer's answer are three fixed fields — the verdict STATUS, the commit message COMMIT_MSG, and the log
   note LOG_NOTE; get a field name wrong and the whole round's review is void. The body then lists problems as
   "file and line / clause violated / symptom / why it is a problem / suggested direction".
6. **Persist it.** The script appends the log note to the development log and prefixes the commit message
   according to the verdict: unchanged for a pass, `[第N轮/待修改]` (round N, needs fixes) for `NEEDS_FIX`, and
   `[需人工确认]` (needs human confirmation) for `ESCALATE`. Then it commits and pushes — pass or fail, so the
   branch always keeps a revertible point.
7. **Decide what is next.** `PASS` ends the run (exit code 0); `NEEDS_FIX` goes back to step 1 and hands the
   fresh review comments to the builder for another round (3 rounds by default); `ESCALATE` stops and waits for
   a human.

| STATUS | What the reviewer means | What the script does next |
|---|---|---|
| `PASS` | no violation of the invariants / acceptance criteria found | commit and push, exit code 0 |
| `NEEDS_FIX` | specific problems with a clear direction for fixing them | commit and push, feed the comments back to the builder for the next round (capped by `MAX_ATTEMPTS`, 3 by default) |
| `ESCALATE` | direction in doubt / an unrevertible decision / the same problem recurring | commit and push, exit code 2, stop and wait for a human |

There are a few situations where the script stops and waits for a human on its own (all of them exit with code
2 rather than silently continuing):

- `MAX_ATTEMPTS` rounds are used up without a pass, or the reviewer's output contains no legal `STATUS`.
- This round's diff is empty — the builder never actually did anything.
- The builder itself reports a quota / authentication problem. In that case the script first prints a **residue
  report** for the working tree (number of changed entries, number of untracked files), reminding you that
  「本轮没有可审查的产物」 ("nothing reviewable this round") does not mean the working tree is clean: a
  half-finished piece of work may already be sitting in it.
- The reviewer's output echoes the prompt but the current round's nonce boundary cannot be found (the CLI
  output format changed).

### Usage

```bash
git checkout -b phase2.5/s2b-editing          # the script refuses to run on main/master
./codex-hermes-loop.sh "implement two-pane editing and the autosave state machine"
```

### Why it is split this way

- **Write authority and review authority sit in two separate processes.** The reviewer is invoked as
  `hermes chat -Q -t vision --ignore-rules -c <REVIEWER_SESSION> --create-if-missing --query-file <prompt>`:
  `-t vision` injects exactly one tool, `vision_analyze`, so the ability to write files, run bash or read files
  simply does not exist on its side — independence is bought by removing capability. `--ignore-rules` stops it
  from absorbing machine-local memory or `AGENTS.md`, so it judges only from the architecture docs and this
  round's diff. `-Q` leaves nothing but the final answer plus one `session_id` line, so format instructions never
  collide with STATUS parsing.
- **Both sides look at the same file state.** The builder and the reviewer read and write the same checkout in
  place: no `git worktree`, no second clone for development, no editing a copy and copying it back. That way the
  diff under review is the very file the builder just touched, and conclusions about dedup / merging can be
  re-checked later (this still holds after moving machines).
- **One commit per round, so review scope stays this round only.** The diff the reviewer receives contains only
  this round's changes; historical changes never bleed into this judgement.
- **The cost side.** Each round = one builder invocation + one review invocation + one commit; in practice every
  slice from S2 to S5 needed a `NEEDS_FIX` round to close out, so a slice usually takes two rounds to pass. That
  is why the builder side has hard efficiency conventions (see `docs/builder-conventions.md`: batched reads, scan
  before reading, combined verification, no re-confirming the same fact — reading files one `cat` at a time is
  this project's single biggest source of token spend).

### Key files and knobs

| File / variable | Purpose |
|---|---|
| `codex-hermes-loop.sh` | the orchestration script (builder / reviewer are pluggable: `BUILDER=codex\|hermes\|cmd`, `REVIEWER=hermes\|opencode`) |
| `.opencode/prompts/reviewer.md` | the reviewer's system prompt (the read-only agent definition lives in `opencode.json`) |
| `docs/builder-conventions.md` | the conventions the builder must follow, injected into the builder's prompt every round |
| `DEVELOPMENT_LOG.md` | per-round time / round number / review verdict / test result / note, written by the script |
| `REVIEWER_SESSION` | the **named session** the reviewer reuses (default `jse-reviewer`). Hermes auto-compresses its context when it grows; **renaming it resets the context**: `REVIEWER_SESSION=jse-reviewer-2026q4 ./codex-hermes-loop.sh "…"` |
| `FILE_REVIEWER_SESSION` | fills in the reviewer session's project ownership (writes Hermes `state.db`); `=0` disables it, and you then have to check the session's ownership by hand |
| `ARCH_DOCS` | the architecture / phase docs handed to the reviewer (5 by default; use newline separation when a path contains spaces) |
| `MAX_CONTEXT_KB` | reviewer input cap (300KB by default); past it the script trims the diff file by file and lists what was left out |
| `TEST_CMD` / `PYBIN` | the acceptance test command and interpreter (`PYBIN` defaults to reading `dev.env`) |
| `KEEP_RUNS=1` / `SKIP_TESTS=1` / `DRY_RUN=1` | keep this round's intermediate artefacts / skip the test gate (not advised) / rehearse without committing or pushing |
| `scripts/codex-stream-filter.py` | renders the `codex exec --json` event stream into human-readable lines in real time (no extra token cost) |
| `scripts/cap-diff.py` | trims a diff to a byte budget **file by file** and lists the files it dropped |

The full list of differences from the imported upstream kit, with the evidence recorded at the time, is in
[docs/hermes-reviewer-kit.md](docs/hermes-reviewer-kit.md).

## Project layout

```text
src/job_search_assistant/
├── app_services/       # cross-domain application services and startup wiring (build_foundation, build_local_ui)
├── adapters/web/       # Phase 2.5's local HTTP entry point and static pages (page assets under static/)
├── career/             # Career / Experience Library domain (from Phase 2)
├── core/               # errors, request context, idempotency and audit abstractions
├── discovery/          # Job Discovery domain types, providers and persistence ports
├── infrastructure/     # SQLite and controlled file storage implementations
├── applications/       # Application domain (later phases)
└── matching/           # Job Matching domain (later phases)

migrations/             # ordered, checksum-protected SQLite migrations (add new ones, never edit old files)
tests/                  # standard-library unittest tests (HTTP touches loopback only)
docs/                   # architecture, per-phase design docs, and the run / acceptance manual
scripts/                # development helper scripts
codex-hermes-loop.sh    # builder + reviewer loop orchestration
```

Dependency direction: `core` ← domain modules ← `app_services` ← adapters. Domain code does not import the
SQLite implementation, and HTTP / UI adapters neither talk to the database directly nor call internal store
write methods. For how to wire the Career application services by hand (there is no CLI business command yet),
see [docs/manual-acceptance.md §4](docs/manual-acceptance.md#4-手工装配调用career-应用服务).

## Docs

- [docs/Job Search Assistant 技术架构文档.md](docs/Job%20Search%20Assistant%20技术架构文档.md) — the overall
  architecture: §5 invariants, §6 module boundaries, §9 state machines, §11 phased plan and DoD; **it wins in
  any conflict**
- [docs/manual-acceptance.md](docs/manual-acceptance.md) — running details, S2a / S2b / S3a manual acceptance
  steps, the boundary of automatic test coverage, and hand-wiring the Career application services
- [docs/phase0-foundation.md](docs/phase0-foundation.md) — Phase 0 design notes and acceptance criteria
- [docs/phase1-job-discovery.md](docs/phase1-job-discovery.md) — Phase 1 scope, pipeline, test strategy
- [docs/phase2-career-foundation.md](docs/phase2-career-foundation.md) — Phase 2 design notes, exit criteria,
  the slice table and each slice's review leftovers
- [docs/phase2.5-career-review-ui.md](docs/phase2.5-career-review-ui.md) — Phase 2.5 interactions, LLM config and
  Mock scope, version rules, acceptance criteria and each slice's contract
- [docs/builder-conventions.md](docs/builder-conventions.md) — the builder's hard constraints, efficiency
  conventions, self-test and commit rules
- [docs/hermes-reviewer-kit.md](docs/hermes-reviewer-kit.md) — the loop's local adaptations and the interrupted-run
  recovery rules
- `DEVELOPMENT_LOG.md` — the result and commit message of every builder / reviewer round

> Note: the design docs under `docs/` are currently written in Chinese only.
