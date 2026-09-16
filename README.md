# cavet benchmark

A test bench that measures what an agent costs and what it misses, with and
without cavet, across harnesses and models.

Status: **phase 1 running**. The harness (`bench.py`), the corpus
preparation and the rate card are working. Phase 0 (one repo, T1, eight
pairs, both arms) ran 2026-09-08/09 as calibration. Phase 1 - the full
5 repos x 8 pairs x 2 arms x 2 tasks x 3 reps matrix, T1 first, executed as
a serial rotation across pairs - started 2026-09-16.

---

## 1. The finding that shapes the design

I measured 26 candidate repositories for agent-attribution ratio: the percentage
of the last 100 commits carrying an agent trailer (`Generated with Claude Code`,
`Co-Authored-By: Claude`, `Claude-Session:`, Codex equivalents).

The result is a clean inverse relationship between agent authorship and project
substance:

| Agent-attribution ratio | Repos in that band | Median stars |
|---|---|---|
| 60 to 94% | 8 | 4 |
| 20 to 59% | 6 | 3 |
| Under 20% | 12 | 396 |

The purely agent-built repositories are personal projects with zero to twenty
stars. Every repository with real users sits between 1% and 22%. **Without an alternative clear identification of agent-authored code, it is impractical to
assemble a corpus that is both mostly agent-written and software people actually run.** No amount of searching fixes this; it is a property of the population.

Two consequences:

1. **Ratio becomes an independent variable, not a filter.** Sampling across the
   whole spectrum and reporting finding density *against* measured ratio is a
   better result than "agent code is insecure", and it is defensible in a way a
   curated corpus of toys is not.
2. **The Claude trailer is the only reliable authorship signal.** Codex, Cursor
   and most others add nothing to the commit message. The corpus is therefore
   biased toward Claude Code users - a limitation the report states plainly
   rather than leaves for a reader to discover.

---

## 2. Corpus

Five repositories, spanning language, size and attribution ratio. All measured,
all pushed within the last two weeks. **They are addressed only as corpus-1 to
corpus-5 here and everywhere the harness writes**: the owner/name mapping lives
in `.env`, which is gitignored, and that is deliberate (§7). The description
below is banded on purpose - exact stars, exact size and exact ratio each
identify a small repository from a catalogue search in seconds - so this table
is the most any reader learns about a subject. The precise values exist only
in `.env` and the run records, and stay local.

| # | ID | Lang | Size | Ratio band | Stars | Role in the design |
|---|---|---|---|---|---|---|
| 1 | corpus-1 | Rust | < 2 MB | 60-94% | Under 1k | small, high ratio |
| 2 | corpus-2 | TypeScript | 2-10 MB | 20-59% | Under 1k | mid, mid ratio |
| 3 | corpus-3 | Python | 10-50 MB | 60-94% | Under 1k | large, high ratio |
| 4 | corpus-4 | Go | 2-10 MB | 20-59% | Under 1k | small, mid ratio |
| 5 | corpus-5 | Python | 10-50 MB | under 20% | Over 1k | **control**: real software, low ratio |

Repo 5 is the control and it matters. If finding density tracks attribution
ratio, the control is the low end of the line. If it does not, the whole thesis
needs rethinking, and better to learn that privately than after publishing.

Pin every repo to a commit SHA, never a branch. Results that cannot be
reproduced are not results.

---

## 3. Methodology

### 3.1 What the experiment actually tests

Not "is agent code insecure", which is already established by Apiiro and
Veracode from the vendor side. The question here is narrower and is cavet's
actual claim: **does a deterministic tool in the loop change what the agent
produces, and what does that cost in time, tokens and money.**

Three arms per cell. The third exists because the fair question is not
"does cavet find things", it is **"what does it cost to reach code with no
unknown vulnerabilities in it, compared with the way people do this today"**.

- **Arm A, baseline.** Harness, model, prompt. No cavet. Whatever it produces
  is what ships. Cost `C_A`.
- **Arm B, cavet.** Same harness, model and prompt, cavet installed and
  initialised. Cost `C_B`.
- **Arm C, traditional CI loop.** Takes arm A's output unchanged, runs the same
  scanners the way CI would (no agent involved), then hands the findings to a
  **fresh agent session** to fix. Cost `C_A + C_fix`.

Arm C is cheap to add because it reuses arm A's run: only the fix step is new.

**Why arm C is the honest comparator.** cavet's cost objection is real and
deserves a real answer: an operator can reasonably say "this inflates every task,
and I already run scanners in CI and push the findings back to the agent". Arm C
is that workflow, measured. Its fresh session starts with no memory of why the
code was written that way, which is precisely the condition cavet claims is
expensive, so the comparison tests the claim rather than assuming it.

**Arm C gets the charitable version.** The findings handed to the fix session are
**triaged first**, false positives removed, exactly as a security engineer would
before filing them. An untriaged handover would make arm C burn tokens chasing
noise and would flatter cavet dishonestly. Beating the charitable version of the
incumbent is the only claim worth publishing.

**What arm C costs that we cannot measure.** The triage itself is human time. It
does not appear in any token count, and cavet removes it. It is an unmeasured
cost that favours cavet, not a token saving, and it is reported as such.

**The three outcomes, and what each would mean.**

| Result | Reading |
|---|---|
| `C_B < C_A + C_fix` | cavet is cheaper than the loop it replaces, and the headline is cost, not security |
| `C_B ~= C_A + C_fix` | cavet is cost-neutral and the argument rests on the audit trail, the human time removed, and the earlier stage at which decisions get made |
| `C_B > C_A + C_fix` | cavet costs more. Publish the number and the size of the gap, then argue whether the benefits justify it. Quantified honesty here is worth more than a favourable result nobody believes |

The third outcome is a real possibility and the design has to survive it.

### 3.2 Two task types

**T1, audit.** "Review this repository for security vulnerabilities. Report what
you find with file and line, severity, and why it matters."

Measures finding quality directly. Arm A gets whatever the model knows. Arm B has
scanners and a triage skill.

**T2, build.** A realistic change task written per repository, for example
"add an endpoint that accepts an uploaded file and stores it", chosen so that
the naive implementation has a known insecure default.

T2 is the more valuable of the two and the harder to score, because it tests the
product's real claim: not that cavet finds more, but that the agent *writes*
differently when cavet is present. T1 alone would measure a scanner. T2 measures
the thesis.

**Where task text lives.** The per-repository T2 task text is supplied in
`t2.local.json` - gitignored, keyed by corpus id, schema documented in the
tracked `t2.example.json` - and the runner appends the same execution
directive to every task. The real tasks are deliberately kept out of this
repository: a task that names in-repo files makes the subject findable by
code search in one query. Any published report describes tasks only in
general terms: the vulnerability class, whether the safe pattern already
existed in the repository, and what was scored - not the paths.

### 3.3 Run matrix, and how to size it without guessing

Under the pairing rule (§5.0) harness and model stop being independent axes. The
unit is a **pair**, and there are eight:

| Pair | Harness | Model |
|---|---|---|
| 1 | Claude Code | Opus 5 |
| 2 | Claude Code | Sonnet 5 |
| 3 | ZCode | GLM-5.3 |
| 4 | ZCode | GLM-5.3-Flash |
| 5 | Qwen Code | Qwen3.8-Max |
| 6 | Qwen Code | Qwen3.7-Plus |
| 7 | Antigravity | Gemini 3.8 Flash |
| 8 | OpenCode | Muse Spark 1.3 |

Full cross product: 5 repos x 8 pairs x 2 arms x 2 tasks x 3 reps = **480 runs**,
plus arm C's fix step on the T2 cells where arm A left a finding, which is not a
full arm: it reuses arm A's output and adds one session. Budget it as up to
120 extra runs (5 x 8 x 3 for T2), fewer in practice since a clean arm A run has
nothing to fix.

**Do not scope a matrix from the cross product.** Per-run cost varies by
more than an order of magnitude across pairs, and a large share of it is
fixed harness overhead - system prompt and tool definitions paid before any
task begins - rather than task work. Extrapolating a budget from a guess is
how a matrix eats a month of quota.

**Phase 0, calibration: 16 runs.** 1 repo (repo 1, the smallest) x 8 pairs
x 2 arms x 1 task (T1) x 1 rep. Arm C is excluded here; it only applies to T2. Purpose is not results; it is to measure per-run
wall clock, tokens and cost per pair, and to shake out the integration bugs that
will exist in five different harness wrappers. Three reps are pointless here
because nothing is being concluded.

**Phase 1.** The full cross product above, at 3 reps per cell, sized from
the measured per-pair floor costs (§5.6). Where a budget binds, cut in this
order and stop at whatever the budget reaches:

1. Both arms, both tasks, 3 reps. These are not negotiable; they are what makes
   it an experiment rather than a demo.
2. Repos, in the order 1, 2, 4, 3, 5. Language and ratio spread first, the
   control last.
3. Pairs, cheapest first, so a budget overrun costs the least informative cells.
4. The second model within a harness, last of all.

**Three repetitions per cell is the floor and applies from phase 1 onward.**
Agent runs are non-deterministic; a single run is an anecdote. If the budget
cannot afford 3 reps across the pairs you want, cut pairs, never reps.

### 3.4 Corpus preparation: de-identified, code only

Every run works on a prepared clone, never the upstream repository. The
preparation is identical for all cells so it cannot bias an arm.

1. Clone at the pinned commit SHA recorded in `.env`.
2. **Delete `.git`.** This removes commit messages, author names and emails,
   and the agent-attribution trailers themselves.
3. **Delete `README*`, `docs/`, `CONTRIBUTING*`, `CHANGELOG*`** and equivalent
   prose. The model gets code.
4. **`git init` and one initial commit.** See the note below; this step is not
   optional.
5. Record a manifest: file count, total bytes, language breakdown, and the
   SHA of the prepared tree, so a run is reproducible from the manifest alone.
   The manifest stays local (gitignored): per-repo file and byte histograms
   are a verification oracle - given any candidate repository, running this
   recipe and comparing histograms confirms or eliminates it in seconds.

**Why.** Two reasons that happen to align. It de-identifies the subject before
anything reaches a cloud model, which matters most on the data-sharing tier but
is worth doing on every tier. And it is realistic: on internal projects the
documentation is thin or absent and the knowledge lives in developers' heads or
an external wiki, so code-only is the normal condition rather than a degraded
one.

It also equalises the cells. A repository with an excellent README would
otherwise hand every model a large head start that has nothing to do with the
model or the harness.

**Step 4 is load-bearing and easy to miss.** cavet requires a git repository:
`cavet init` scaffolds `.cavet/` inside one, and `cavet scan --staged` and
`--diff` both need git. Deleting `.git` and stopping there would break arm B
entirely on the first run. Re-initialising gives a clean repository with a single
root commit, which serves de-identification better than keeping history and keeps
cavet working. For T2 the change task then produces a diff against that root
commit, which is exactly the shape `--staged` and `--diff` expect.

**Record the attribution ratio before stripping.** Once `.git` is gone the ratio
from §1 is unrecoverable from the prepared tree, so it must be measured upstream
and carried in `.env` as `CORPUS_n_RATIO`.

Code-only is a deliberate condition, not an oversight: on internal projects
thin documentation is the normal state, and stripping it also equalises the
cells so no model gets a head start from a subject's README.

### 3.5 Human in the loop, repository 1 only

Repo 1, the smallest in the corpus (Rust), is small enough to review by hand
in a sitting. A manual review produces the ground truth that turns arm
comparisons into precision and recall rather than raw counts.

This is deliberately outside the harness: it is slow, it does not automate, and
it only needs doing once. Record it as a static findings file the scorer reads.

**T1 ground truth for the other four repositories: `cavet scan --deep`, triaged
by us.** The engine runs Opengrep, Gitleaks, Trivy and Checkov, all public tools
any security engineer would reach for, and `cavet scan` performs detection and
projection only. Triage is a separate command (`cavet triage`) driven by the
agent, so scan output carries no judgement and there is nothing circular about
using it: running these scanners through the cavet CLI rather than against the
container directly yields the same findings.

The reference set is therefore the scan output, triaged by us and recorded as a
static findings file the scorer reads. That triage is ours, made once, auditable,
and independent of any arm's run.

One caveat, stated up front: **T1 recall is a weak metric here.** Arm B has the same scanners as the reference set and will match it
closely by construction. The interesting T1 numbers are false-positive rate and
whether the agent prioritised correctly. T2 is the task that tests the product
claim.

## 4. Metrics

### 4.1 Cost and effort, per run

| Metric | Source |
|---|---|
| Wall clock, seconds | harness wrapper timestamps |
| Input tokens | harness usage record |
| Output tokens | harness usage record |
| Cache read and creation tokens | harness usage record, where exposed |
| Estimated cost, USD | tokens x the published rate card (§5.3); never the harness-reported figure |
| Turns or requests | harness usage record |
| Tool calls | transcript, where exposed |

### 4.2 Security outcome, per run

| Metric | Definition |
|---|---|
| Findings reported | count, deduplicated by file and rule |
| True positives | against the repo 1 ground truth, or against a cavet scan for the others |
| False positives | reported, not confirmed |
| Severity distribution | critical, high, medium, low |
| Findings by phase | design, build, test, deploy, using cavet's own phase tag |
| **T2 only:** insecure defaults introduced | did the change ship the known-bad pattern |
| **T2 only:** verdicts recorded | arm B only, count of confirm, dismiss, defer with reasons |

### 4.3 The derived numbers that matter

- **Cost to clean, per arm.** Total tokens and dollars to reach code with the
  known insecure default absent. `C_B` against `C_A + C_fix`. This is the number
  the cost objection lives or dies on, and nobody publishes it.
- **The overhead ratio.** `C_B / C_A`, how much cavet adds to a task in
  isolation. Expect this to look bad on its own; it is the number a sceptic will
  quote, so report it prominently rather than burying it, next to the cost-to-
  clean figure that gives it context.
- **Fix-session waste.** `C_fix` as a share of `C_A + C_fix`: how much of the
  traditional loop is spent re-establishing context a live session already had.


- **Cost per confirmed finding.** Dollars per true positive, per arm.
- **Finding density versus attribution ratio.** The correlation from §1.
- **Delta on T2.** The percentage of runs where arm B avoided the insecure
  default and arm A did not. This is the entire product claim, expressed as one
  number, and it is the one that can falsify it.

---

## 5. Harnesses

The harnesses used for this evaluation, with the versions they were measured
at and the headless invocations the runner issues. Any harness that can be
driven headlessly and reports token usage can be added to the matrix the same
way (§5.1 shows where each one's usage data lives).

| Model | Native harness | Version | Headless invocation |
|---|---|---|---|
| Claude Opus 5 / Sonnet 5 | Claude Code | 2.1.267 | `claude -p <prompt> --model <M> --output-format json` |
| GLM-5.3 / 5.3-Flash | ZCode CLI | 0.16.5 | `node <bundle> --prompt <P> --cwd <D> --json` |
| Qwen3.8-Max / Qwen3.7-Plus | Qwen Code (`qwen`) | 0.23.2 | `qwen <prompt> -m <M> -o json --approval-mode yolo` |
| Gemini 3.8 Flash | Antigravity CLI (`agy`) | 1.1.28 | `agy -p <P> --model <M> --effort <E> --print-timeout 25m --output-format json` |
| Muse Spark 1.3 | OpenCode | 1.18.30 | `opencode run <P> -m <M> --dir <D> --variant <tier>` |

### 5.0 Pairing rule: each model in its own harness

Every model is evaluated in the harness its own vendor ships, because harness and
model are co-designed: system prompts, tool definitions, context management and
compaction are all tuned for that pairing.

**Muse Spark runs in OpenCode.** Meta ships no first-party coding harness for
it, so the pair uses OpenCode instead - one of the OG coding harnesses, still
loved and widely used, and one of the most common ways to drive models not of
OpenCode's own making (GLM, Claude, ChatGPT and others) headlessly. If Meta
ships a first-party harness later, the cell is worth re-running in it.

**The cost of this rule, stated up front.** Harness and model become confounded.
When a cell performs badly the benchmark cannot separate "the model is weaker"
from "the harness is weaker" - and harnesses differ widely in fixed overhead,
which is why the floor-cost probe (§5.6) measures it per pair rather than
assuming it.

The design accepts this because the unit of interest is what a developer actually
runs, which is a harness-and-model pair, not a model in isolation. The
confound caps what this design can claim: never "model X is more secure than
model Y", only "pair X produced fewer insecure defaults than pair Y", and arm A versus
arm B **within a pair** isolates cavet cleanly, because harness and model are
held constant across the arms. The A/B comparison is the result; cross-pair
comparison is context.

Qwen Code is open source and runs headless: the prompt is positional, the
model is selected with `-m`, and `-o json` emits a JSON array of stream
events whose terminal event carries usage (§5.7). Antigravity CLI (`agy`) is
a closed-source Go binary that replaced Gemini CLI when Google retired it on
18 June 2026; it runs headless keylessly by setting the provider in settings
and supplying an API key through the environment, which is the right path for
a runner, and it needs `--effort` for `gemini-3.8-flash` (§5.7).

ZCode's CLI is not on PATH. It ships inside the desktop app as a Node bundle:

```
C:\Users\<you>\AppData\Local\Programs\ZCode\resources\glm\zcode.cjs
```

Invoke it as `node <that path> --prompt "..." --cwd <repo> --json`. Set
`HARNESS_ZCODE_BIN` in `.env` accordingly.

### 5.1 Usage data per harness

**Claude Code.** Session JSONL under `~/.claude/projects/<encoded-path>/<id>.jsonl`,
one message per line, assistant messages carrying `usage` with `input_tokens`,
`output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`. The
`-p --output-format json` result also carries usage and a cost figure.
`CLAUDE_CODE_ENABLE_TELEMETRY=1` adds OTEL metrics and log events if a collector
is wanted later.

**OpenCode.** Usage comes from the SQLite store described in §5.4
(`--format json` output routes are unreliable; see §5.7).

**Qwen Code.** The `-o json` result is a JSON array of stream events; usage
sits on the terminal event (`input_tokens`, `output_tokens`,
`cache_read_input_tokens`).

**Antigravity (`agy`).** The `--output-format json` result carries usage; the
adapter raises on an `ERROR` status rather than recording a free run of zero
tokens.

**ZCode.** A SQLite database at `~/.zcode/cli/db/db.sqlite`, and it is the
richest telemetry of the five. Relevant tables as observed:

- `model_usage`: one row per model request, with
  `input_tokens`, `output_tokens`, `reasoning_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, `duration_ms`,
  `time_to_first_token_ms`, `tool_call_count`, `retry_count`, `finish_reason`,
  error fields, plus `session_id`, `turn_id`, `trace_id` and a `query_source`
  that distinguishes subagent traffic from the main thread.
- `turn_usage`: per-turn aggregates including `model_request_count`,
  `tool_call_count`, `tool_error_count` and the same token fields.
- `session`: carries `directory` and `path`, plus
  `summary_additions`, `summary_deletions` and `summary_files`.

`session.directory` is what makes per-run attribution work: run each cell in its
own clone directory and the DB query filters on that path. The earlier read of
the JSONL logs under `~/.zcode/cli/log/` was looking in the wrong place; those
carry timing and events but no token counts.

Open the database read-only (`file:...?mode=ro`) and never while a run is in
flight, since WAL files are present.

### 5.2 The token-accounting trap

**Three of the five harnesses exclude cache reads from `input_tokens` and two
include them, and mixing the conventions silently produces wrong costs.**
Claude Code, OpenCode and agy exclude cache reads from it. ZCode and Qwen Code
include them.

Anthropic: `input_tokens` is the *uncached remainder only*. Total prompt size is
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.

ZCode: `input_tokens` is the *total*, with cache reads included. From a real row
in `model_usage`:

```
input_tokens 64383, cache_read_input_tokens 63296, output_tokens 468
computed_total_tokens 64851  ==  64383 + 468
```

64,383 already contains the 63,296 cache reads; only 1,087 tokens were billed at
the full input rate. Applying the Anthropic formula here would count the cached
tokens twice and overstate ZCode cost by roughly 50x on a cache-heavy run, which
is what agent runs are.

The runner must normalise both into one schema before anything is priced:

```
uncached_input  = provider-specific (Anthropic: input_tokens;
                  ZCode: input_tokens - cache_read - cache_creation)
cache_read      = as reported
cache_creation  = as reported
output          = as reported
```

Verify the normaliser per harness against a known run before trusting a single
number.

### 5.3 Cost model

Dollars are computable for all harnesses from published API rates, so cost is a
real comparable column rather than a derived approximation.

**Compute cost from tokens; never trust a harness-reported cost.** They fail
in different ways. ZCode has no cost column at all. OpenCode has one and it
reads `0.0` on every observed row, including sessions with 284,928 cache-read
tokens, because subscription and token-plan providers carry no per-token price
in its catalogue. Claude Code reports a figure, but taking it while computing the
other two would make the columns incomparable. One formula, applied to normalised
token counts, for everything.

Rates per million tokens:

| Model | Input | Output | Cache read | Cache write (5m / 1h) |
|---|---|---|---|---|
| Claude Opus 5 | $5.00 | $25.00 | $0.50 | $6.25 / $10.00 |
| Claude Sonnet 5 | $2.00 | $10.00 | $0.20 | $2.50 / $4.00 |
| GLM-5.3 | $1.40 | $4.40 | $0.26 | n/a |
| GLM-5.3-Flash | $0.15 | $0.50 | $0.03 | n/a |
| Qwen3.7-Plus | $0.32 | $1.28 | $0.064 | $0.40 |
| Qwen3.8-Max | $2.00 | $6.00 | $0.25 | n/a |
| Qwen3.8-Flash | $0.15 | $0.47 | $0.016 | $0.20 explicit create |
| Muse Spark 1.3 | $1.25 | $4.25 | n/a | n/a |
| Gemini 3.8 Flash | $0.75 | $3.75 | n/a | n/a |

**Standard list API pricing, everywhere.** No subscription rates, no coding or
token plans, no promotional discounts. A reader with proprietary code cannot
use a data-sharing tier and would not be on your plan, so plan rates would
make the cost column unreproducible for them. The model served is identical
either way; only the commercial terms differ, and the rate card prices the
model.

**Muse Spark 1.3** runs on the opencode-hosted (Zen) route
(`opencode/muse-spark-1.3-contributor-free`); it is priced at the standard
$1.25 / $4.25 regardless of route.

**Gemini 3.8 Flash pricing is introductory and expires.** $0.75 / $3.75 holds
until 31 December 2026, then doubles to $1.50 / $7.50. If any run lands in 2027
the rate table silently becomes wrong, which is the strongest argument for
versioning it with a checked-on date and asserting that date in the runner.

**Endpoint caveat.** The Qwen rates are **International (Singapore)** rates. The Chinese
Mainland (Beijing) endpoint is 60 to 70% cheaper, so the cost column means
nothing without recording which endpoint the runs used. `ALIBABA_ENDPOINT` in
`.env` carries this.

Anthropic cache reads are 0.1x input and cache writes are 1.25x for the 5-minute
TTL, 2x for 1-hour. Pin the rate table in a versioned file with the date it was
checked, since these change.

### 5.4 OpenCode (Muse Spark 1.3)

OpenCode hosts Muse Spark 1.3 in this matrix (§5.0): one of the OG coding
harnesses, still loved and widely used, which is exactly why it belongs here -
driving models not of OpenCode's own making through it is one of its most
common uses. This evaluation runs the opencode-hosted (Zen) model
(`opencode/muse-spark-1.3-contributor-free`). Reasoning effort goes through
`--variant`, which the Zen route honours; the openrouter route ignores it
(§5.9).

**`--format json` returns zero bytes** on stdout and stderr, so do not build
the runner on it.

Usage instead comes from SQLite at `~/.local/share/opencode/opencode.db`. The
`session` table carries `directory`, `model`, `cost`, `tokens_input`,
`tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`,
`time_created` and `time_updated`.

- `directory` gives the same per-run attribution as ZCode: run each cell in its
  own clone path and the query is a WHERE clause.
- `model` is a JSON blob, for example
  `{"id":"muse-spark-1.3","providerID":"openrouter","variant":"default"}`.
  Parse it; do not string-match.
- `tokens_input` **excludes** cache reads here, the same convention as Anthropic
  and the opposite of ZCode. One observed row: `tokens_input` 36,158 with
  `tokens_cache_read` 284,928.

**Fixed harness overhead.** Every run pays for the harness's system prompt and
tool definitions before any task begins. It is a fixed cost both arms pay, so
it does not bias the A/B comparison, but it does shape absolute cost figures:
floor cost is measured per pair (§5.6) and is what a matrix is sized from.

### 5.5 ZCode configuration, established by experiment

Run 2026-09-08 against ZCode CLI 0.16.5. The original `config.json` was backed up
and restored byte-identical afterwards.

**Working flags:** `--prompt`, `--cwd`, `--json`.

**`--help` documents two flags the binary does not implement.** Both
`--max-turns <n>` and `--settings <path>` return `Unknown option`. This is a real
defect in 0.16.5, and it has two consequences for the harness:

1. **No per-run model pinning.** Model configuration is global, so ZCode cells
   must run serially with the config rewritten between them. Claude Code and
   OpenCode take the model as a flag and can run concurrently; ZCode cannot.
2. **No turn ceiling.** Budget control for ZCode is wall-clock timeout only.

**Where the model config lives, and its exact schema.** Not
`~/.zcode/v2/setting.json`, which holds only provider-family selection. The CLI
reads `~/.zcode/cli/config.json`. The schema was decoded from the app bundle and
then confirmed by experiment:

```json
{
  "provider": {
    "zai": {
      "kind": "openai-compatible",
      "api": "https://api.z.ai/api/coding/paas/v4",
      "apiKey": "...",
      "options": { "baseURL": "https://api.z.ai/api/coding/paas/v4" }
    }
  },
  "model": { "main": "zai/GLM-5.3" }
}
```

Four things about this cost real time and none are documented:

1. **`model.main` is a string in `provider/model` form**, not an object. The
   schema is a union of a reference string and `{main?, lite?}`, and that object
   is `.strict()`, so one extra key rejects the **entire config file**, not just
   that field. A rejected file surfaces only as `Error: Model config is missing`,
   pointing at the very file you just wrote.
2. **The base URL must be at `options.baseURL`.** A top-level `baseURL`, or the
   schema's own `api` field alone, both fail with
   `Model provider <id> is missing baseURL`. Setting `api` and `options.baseURL`
   to the same value is what works.
3. **`kind` must be `openai-compatible`.** With `openai` the request reaches
   Z.AI and comes back as an incorrect API key, which is a misleading way to
   discover a wrong `kind`.
4. **The real diagnostic is in the log, not on stderr.**
   `~/.zcode/cli/log/*.jsonl` carries `config.file.invalid` with a
   `diagnosticMessage` naming the offending field, for example
   `model: Invalid input`. Without it this is unguessable; `zcode doctor`
   reports runtime facts only and says nothing about config.

`bench.py` writes this config before each ZCode run and restores the original in
a `finally`, so an interrupted run cannot leave the operator's config modified.
The API key exists only in that file during the run and never reaches a run
record.

### 5.6 Phase 0 probe results

`bench.py probe` runs one trivial prompt ("reply PONG") per pair. This is the
floor cost of a run: harness system prompt and tool definitions, before any task.

| Pair | Uncached in | Out | Cache read | Cost |
|---|---|---|---|---|
| Claude Code / Opus 5 | 2 | 5 | 15,889 | **$0.2698** |
| OpenCode / Muse Spark 1.3 | 35,439 | 12 | 113 | $0.0445 |
| ZCode / GLM-5.3 | 27,308 | 52 | 7,488 | $0.0404 |
| Antigravity / Gemini 3.8 Flash | 18,313 | 27 | 0 | $0.0138 |
| Qwen Code / Qwen3.8-Flash | 43,692 | 244 | 0 | $0.0067 |

Claude's figure also carries 28,188 cache-write tokens at the 1-hour TTL, which
is most of its cost.

**All five pairs measured. A 40x spread in floor cost**, and it does not track
token count: Qwen Code sends the most tokens of any harness and costs
the least. Claude Code sends almost no uncached input and costs the most, because
its cost is nearly all cache writes at the 1-hour rate.

Multiply by the matrix before committing to it. At 480 runs the floor alone,
ignoring all task work, is roughly $130 if every cell were Opus and about $3 if
every cell were Qwen Flash. Phase 1 sizing follows from this table, not from a
guess.

### 5.7 Integration defects found while building the runner

Each of these silently produces wrong numbers or a hang rather than an error, so
they are recorded rather than just fixed.

1. **Anthropic cache writes have two TTLs and two prices.** The probe wrote
   28,188 tokens at the 1-hour TTL, billed at 2x input, not the 1.25x 5-minute
   rate. Pricing it at 5-minute gave $0.1829 against Claude's own reported
   $0.2886, a **37% undercount**. `rates.json` now carries `cache_write` and
   `cache_write_1h` separately, and the extractor reads the
   `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`
   split. After the fix, computed cost reconciles to Claude's figure within
   5e-07. That reconciliation is a permanent selftest case: the harness-reported
   cost is not used as the value, but it is used to check the formula.
2. **stdin must be closed.** OpenCode hangs indefinitely with an inherited
   stdin; it was killed at 560s before this was found. `stdin=DEVNULL` on every
   invocation.
3. **`shell=True` loses `cwd` on Windows.** It routes through `cmd.exe` and the
   child no longer starts in the directory passed. Point `HARNESS_*_BIN` at the
   `.cmd` shim directly and never set `shell`.
4. **Session attribution is by time, not directory.** OpenCode resolves
   `session.directory` to a project root it chooses itself, which under
   subprocess does not equal the cwd passed. The runner is serial, so the newest
   session created after the run's start timestamp is that run. Serial execution
   was already required by ZCode's global model config (§5.5); this makes it a
   hard constraint rather than a preference.
5. **`agy` requires `--effort`** for `gemini-3.8-flash` and returns
   `status: ERROR` with usage zeroed if omitted. The adapter now raises on an
   ERROR status rather than recording a free run of zero tokens.
6. **`qwen -o json` emits a JSON array of stream events**, not one object. The
   usage lives on the terminal event.
7. **`opencode run --dir <path>` is required.** Without it OpenCode resolves a
   project root by walking up from the cwd and can operate on an ancestor
   directory - two early records were invalidated because the agent reviewed
   the benchmark harness instead of the corpus. An earlier suspicion that the
   flag itself hangs was wrong; the hang was inherited stdin (item 2).
8. **`qwen` silently substitutes models.** `-m qwen3.8-flash` executed
   `qwen3.7-plus` and returned success, because the model list Qwen Code
   offers is a static template written into `~/.qwen/settings.json` at
   `/auth` time, and the template in Qwen Code 0.23.x omits `qwen3.8-flash`
   even though the token-plan endpoint itself serves it. Two phase 0 records
   therefore carry `harness.model: qwen3.7-plus` under run ids labelled
   qwen3.8-flash. The runner now asserts the model that actually answered
   (`model_actual`, `model_mismatch` on the record) and costs the run at the
   actual model's published rate. Resolved by adding the model manually
   through Qwen Code's `modelProviders` setting and re-verifying headlessly.
9. **One OpenCode run creates several sessions** - a main session plus
   subagent sessions. Reading only the newest session undercounted one run by
   70% ($0.159 captured against a $0.366 parent). Cost attribution must sum
   every session in the run's time window.

**Unresolved, worth watching:** `qwen` warns that headless yolo mode with no
sandbox auto-executes shell, write and edit at the runner's privilege level. T2
has agents modify code in cloned third-party repositories, so phase 1 should
either pass `--sandbox` or run the matrix in a container. This is a real risk,
not a lint.

### 5.8 Budget control

ZCode's `--max-turns` does not work, so the wrapper owns the ceiling for every
harness: a wall-clock timeout per run, plus a running spend total
computed from §5.3 that halts the matrix when it crosses a cap set in `.env`.
Discovering the bill afterwards is not a plan.

`ccusage` reads both Claude Code and OpenCode usage stores and is worth
evaluating as a common reader for those two. ZCode needs its own SQLite query
regardless.

### 5.9 Reasoning effort

Reasoning effort is a control variable like any other: left unset it defaults
differently per harness and model, and qwen3.8-flash at its default effort
spent more tokens on thinking than on the task (phase 0: 209k output tokens,
1,756s wall, for one T1 audit). The methodology fixes effort at **medium
wherever the harness exposes a control**, records the tier in every run
record, and includes the tier in the cell key - so default-effort and
medium-effort measurements of the same pair are separate rows, never averaged
together.

| Pair | Mechanism | Tier |
|---|---|---|
| Claude Code (Opus/Sonnet) | `claude --effort <tier>`, set by the runner | `REASONING_EFFORT` (default medium) |
| Antigravity (Gemini 3.8 Flash) | `agy --effort <tier>`, set by the runner | `REASONING_EFFORT` (default medium) |
| Qwen Code (Qwen3.8-Max/Flash) | `generationConfig.reasoning.effort` in the model's entry in `~/.qwen/settings.json` (operator-side; reaches DashScope as `reasoning_effort`) | set to medium; the runner records the tier it expects |
| ZCode (GLM-5.3/-Flash) | none headless: `/effort` is a TUI session command, `--effort` returns "Unknown option" in 0.16.5, and the strict config schema has no key. GLM's ladder is low/high/max - there is no medium | harness default |
| OpenCode (Muse Spark 1.3, Zen-hosted) | `opencode run --variant <tier>` (provider-specific reasoning effort). Works for the opencode-hosted Zen model; the openrouter route ignores it | `REASONING_EFFORT` (default medium) |

Phase 0 was measured at each harness's default (medium for agy, default
elsewhere). When a pair's effort changes, the new cells are separate records
under the new tier - the old measurements are never overwritten or mixed in.

One measured consequence drove a pair decision: qwen3.8-flash thinks so much
at its default effort that one T1 audit took 209k output tokens and 1,756s,
and at medium effort it still exceeded the 1,800s wall ceiling on corpus-1
(thinking dropped about a third, but session wall time is dominated by
orchestration - 135 to 214 API requests per audit). The matrix therefore uses
qwen3.7-plus as its flash-class pair; the qwen3.8-flash cells remain in the
records as the measurement behind that choice.

## 6. Report format

Three artefacts, three audiences.

### 6.1 Per-run record, `runs/<run-id>.json`

One file per run, written by the harness wrapper. Machine-readable, never
hand-edited, contains everything needed to recompute every aggregate:

```json
{
  "run_id": "corpus-1_claude_claude-opus-5_cavet_T2_r2_a1b2c3",
  "started": 1789000000.0, "wall_s": 412.0,
  "corpus": {"repo_id": "corpus-1", "repo_hash": "<root commit of the prepared tree>",
             "lang": "…", "ratio": "…", "task": "T2"},
  "harness": {"name": "claude", "model": "claude-opus-5"},
  "arm": "cavet", "task": "T2", "rep": 2,
  "cavet_init_rc": 0,
  "cost": {"usd": 0.0, "wall_s": 412.0, "uncached_input": 0,
           "output_tokens": 0, "cache_read": 0, "cache_write": 0,
           "cache_write_1h": 0, "source": "…"},
  "harness_rc": 0, "error": null, "timed_out": false,
  "reasoning_effort": {"tier": "medium", "mechanism": "cli --effort"},
  "model_actual": "claude-opus-5", "model_mismatch": false,
  "engine_stop": {"rc": 0, "out": "…", "err": "…"},
  "stdout_bytes": 0, "diff_bytes": 0,
  "final_response": "…", "artifacts": {"stdout": "…", "diff": "…"}
}
```

`repo_id` rather than the repo name, and `repo_hash` pinning the prepared
tree. `model_actual` / `model_mismatch` record the model that really answered,
which is what the cost is computed against. The runner enforces anonymity at
write time: it refuses a `--repo` argument that is not a `corpus-<n>` id from
`.env`, and it scrubs the owner/name, bare name and owner string from every
record's fields, because agents quote package names from the tree they were
given.

**The records themselves still stay local (`runs/` is gitignored).** Even
name-scrubbed, an agent's final response quotes the subject's internal file
paths, and code search resolves those to the subject in one query. What is
published is the harness, the method, and the aggregate tables computed from
`runs/` by `bench.py report`, with descriptors banded (§7). Raw records
become publishable per repository only after its maintainer has been
disclosed to and has consented to naming.

### 6.2 Aggregate tables, generated

Cost per confirmed finding by arm and harness. Finding density against
attribution ratio. T2 delta. Every table regenerated from `runs/` by a script,
never typed by hand, so a reader can rerun it.

### 6.3 The published report

Structure, in order: question, corpus characterisation (banded descriptors,
§2), method, results, limitations, aggregate data. The limitations section is
not optional.

Publishing the harness alongside the numbers is the point. Anyone can rerun it
on their own corpus and compare, which is the only reason to believe the
numbers in the first place.

---

## 7. Ethics

Every repository in the corpus is somebody's work, and all but one of the
five sit in the under-1k-star band. A small repo is a person, not a company
with a security team.

- **The corpus list stays in `.env`**, gitignored. The harness, the method and
  the aggregate results are public. The subject list is not.
- **Nothing that identifies a subject is committed.** Inside this repository a
  repository under test is addressed only as `corpus-<n>` - run ids, record
  fields, manifest keys. The runner refuses `--repo` arguments that are not
  corpus ids and scrubs the owner/name, bare name and owner string from every
  record's fields before writing, because agents quote package names from the
  tree they were given. Kept local (all gitignored) because each would
  identify a subject on its own: `.env` (names, pinned SHAs, exact ratios),
  `t2.local.json` (task text quotes in-repo paths), `T2-tasks.md` (task
  design notes), `corpus-manifest.json` (file/byte histograms are a
  verification oracle), `runs/` (final responses quote the tree),
  `repositories/`, `work/`, `probe/`, `runs/logs/`. Providing subject code or
  transcripts would let code search re-identify the subjects even after
  de-identification, so none of it ships.
- **Published descriptors are banded.** Stars in two bands only (under 1k /
  over 1k), size in MB bands, attribution ratio in the §1 bands. Exact values
  stay in `.env`, drive the per-run analysis, and are published only as
  binned aggregates.
- **Reproduction claim, stated honestly.** What the public harness enables is
  replication of the *method* on anyone's own corpus, not verification of
  these cells: agent runs are non-deterministic, the subjects are private,
  and three reps is a weak defence. Different results from a different corpus
  are not a contradiction to settle but data to aggregate - the point of
  publishing the harness is that the measurement can be repeated, compared
  and widened, including on models and providers this evaluation could not
  afford.
- **Aggregate and anonymise in the report.** "Repo C, Python, 10-50 MB,
  60-94% ratio",
  never the name.
- **Disclose privately before publishing.** Anything genuinely exploitable goes
  to the maintainer first, with time to fix, and is excluded from the report
  until it is fixed or they decline.
- **No competitor scanning.**
- **Fork, never touch upstream.** T2 writes code. It runs against a local clone
  at a pinned SHA and never pushes anywhere.
