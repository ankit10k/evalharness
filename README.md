# evalharness — `evalctl` `[•_•] EvalScout`

Turn your real work into evals, then measure whether your **harness** is
actually improving.

"Harness" here means the whole system you code with: your context files
(`CLAUDE.md`, skill docs, specs), your model, and your tools/MCP servers. Most
people change those constantly and have no idea whether any of it helped.
evalharness gives you a number, tracked over named versions.

Stdlib-only Python. No dependencies, no `pip install`, no server, no account.
**Core functionality makes zero network calls** — no telemetry, no analytics,
no update checks. Everything lives in a `.evalharness/` directory inside the
repo you point it at. Nothing leaves your machine unless you send it yourself.
That matters if your RTL is proprietary.

---

## The one concept you must not skip: attempt vs regression

There are two completely different things you can measure, and confusing them
is the main way people fool themselves.

**Regression** — `evalctl replay`, `evalctl run <eval_id>`

> Re-runs an eval's `success_command` against the **current tree**, where the
> fix already exists.

This tells you the repo is still green. It says **nothing** about your harness.
Editing `CLAUDE.md` cannot change the result, because the code is already
fixed. Useful for catching breakage; useless for measuring whether your system
got better.

**Attempt** — `evalctl attempt <eval_id>` → do the work → `evalctl grade <attempt_id>`

> Checks out the **original pre-fix state** into an isolated worktree (the fix
> is *not* there), hands the agent the eval's `input_prompt`, lets it solve the
> task from scratch, then runs the success command and records the verdict.

This is the measurement that actually moves when your harness changes. Same
shape as a real benchmark: (prompt, context, harness) → verdict. Runs are
stamped `mode=attempt` so you can tell them apart forever.

Rule of thumb: *"is my repo healthy?"* → `replay`. *"is my system getting
better?"* → `attempt` + `grade`. The dashboard warns you in yellow when a
suite has no attempt-backed runs at all.

evalharness never calls an LLM itself. It sets the stage (`attempt`) and grades
(`grade`); your agent does the work in between.

---

## Score vs consistency

Two numbers, deliberately never merged:

- **score** — the **latest** verdict for each eval, summed. "Where do I stand
  right now."
- **consistency** — `passed / total` across an eval's **entire run history**.
  "Does this behave the same way every time." Shown as `n/a` until an eval has
  at least 2 runs, because 100% off a single run is noise.

If you repeatedly `attempt` the same eval under the same harness, consistency
is effectively **Pass@k** — how reliably your system solves that task, not
whether it got lucky once. If you accumulate runs across weeks of harness
changes, it reads as stability over time instead. Same formula either way.

---

## Two suites, never blended

| suite | what's in it |
|---|---|
| `local` | evals captured from **your own** verified work |
| `cvdp` | imported NVIDIA CVDP benchmark problems, and/or external reference verdicts |

They are scored identically but reported separately, on purpose. If a harness
change lifts `local` but leaves `cvdp` flat, that's an **overfitting signal**:
you tuned your context files to your own repo's quirks rather than to general
capability. Blending the two into one number would hide exactly the thing you
most want to see.

External reference verdicts (imported from someone else's graded runs) are
stamped with a fixed synthetic harness id. They show as a calibration line and
are never counted as a measurement of *your* system — and they're never marked
"stale", because they were never yours to refresh.

---

## Requirements

- **Python 3.9+** (stdlib only)
- **git**
- **whatever build/sim/EDA command you already use** to verify your own work

Explicitly toolchain-agnostic. `make regression`, `iverilog … && vvp …`,
Verilator, a Synopsys or Cadence flow script, cocotb, a Python unit test — it
does not matter, because the success command is always your own string, run
through your own shell. evalharness has no idea what a simulator is.

Docker is **not** required. It is only mentioned in the optional CVDP Tier-2
path below.

---

## Quickstart

```bash
git clone <your-fork-url> evalharness
cd /path/to/your/repo

# optional convenience alias
echo "alias evalctl='python3 /abs/path/to/evalharness/evalctl.py'" >> ~/.zshrc
source ~/.zshrc
```

**1. Initialize** (once per repo). Asks for your success command, context/skill
globs, model, and tools:

```bash
evalctl init
```

**2. Do real work.** Fix a bug, write a testbench, whatever you were going to
do anyway.

**3. Capture it.** Run your normal verification command *through* `check`
instead of directly:

```bash
evalctl check "<your test cmd>" --prompt "<the task you were solving>"
```

`check` runs the command, notices you changed real code, infers a candidate
eval (diffstat, not a diff dump), flags files that have come up in prior evals,
and asks approve / edit / reject / tag / prompt / comment / type / diff / skip.

**Pass `--prompt`.** It's stored as `input_prompt`, and without it the eval can
never be `attempt`ed — you'd have a regression test and nothing more. `check`
prints a yellow warning when it's missing.

**4. Measure the harness.** Pick an eval and make the agent redo it from
scratch:

```bash
evalctl attempt eval_a1b2c3d4      # prints an isolated pre-fix workspace + the task
# ... your agent solves the task inside that workspace ...
evalctl grade att_5e6f7a8b         # runs the success command there, records mode=attempt
```

Repeat on the same eval a few times to get a consistency / Pass@k number.

**5. Name your harness version** whenever you change context files, skills,
tools, or model:

```bash
evalctl harness v2 "added cache-debug skill doc"
evalctl harness                     # show current + full history
```

The harness id itself is a content hash of everything matching `context_globs`
plus your declared model and tools — so silent drift is detected even if you
forget to bump the version. The version label just makes the trend readable as
v1 → v2 → v3 instead of opaque hashes.

**6. Look at the number.**

```bash
evalctl score        # terminal: per-suite score, consistency, by-type breakdown
evalctl dashboard    # local one-page web app, no server — opens in your browser
```

The dashboard shows suite cards, a trend line across harness versions, and every
eval as a clickable row that expands into its diff, prompt, success command,
tags, comments, and full run history (with each run's mode and solution diff).
It's written to `.evalharness/dashboard.html` and regenerated on every
check/decide/replay, so it's never stale.

---

## Command reference

| Command | What it does |
|---|---|
| `check "<cmd>" [--prompt "..."]` | **Everyday entrypoint.** Run your command; if it changed real code, propose/review/score an eval in one step. First run also does setup. |
| `attempt <eval_id>` | **Real eval mode.** Open a pre-fix worktree + task prompt for an agent to solve. |
| `grade <attempt_id> [--keep]` | Grade an open attempt; records `mode=attempt`. |
| `replay` | Re-run every approved eval against the current tree. Repo health, *not* harness quality. |
| `harness [<version> [<note>]]` | Name the current harness state, or show current + history. |
| `score` | Print per-suite score + consistency, no re-running. |
| `dashboard [--no-open]` | Build and open the local web scoreboard. |
| `ask "<question>"` | Local keyword Q&A over your own data — **not** an LLM call. Understands score / failing / trend / tags / `category <name>` / `eval <id>`. |
| `feedback "<text>"` | Log a free-text note about the tool itself. |
| `import-baseline --manifest <path>` | Import already-graded CVDP verdicts as an external reference line. Free. |
| `import-cvdp --dataset <path>` | Import CVDP problems as eval definitions from a JSONL you supply. |
| `init` | Explicit setup instead of `check`'s first-run prompt. |
| `wrap "<cmd>"` | (advanced) Capture one command without proposing an eval. |
| `propose` | (advanced) Turn the last capture into a draft eval. |
| `review` | (advanced) Work through pending evals interactively. |
| `run <eval_id> [--commit <sha>]` | (advanced) Replay one eval, optionally against an old commit in a throwaway worktree. |

Quote commands containing `&&`, `|`, or quotes as a **single argument** —
otherwise your shell splits them before evalctl ever sees them.

---

## MCP setup

`mcp_server.py` exposes the same functionality as MCP tools, so your agent can
call `check` / `start_attempt` / `grade_attempt` / `score` on its own instead of
you typing commands. The server is hand-rolled JSON-RPC over stdio — it does
**not** use the official `mcp` SDK, because that requires Python 3.10+ and would
break the zero-dependency design.

### Claude Code

Copy `.mcp.json.template` from this repo into the **root of the repo you want
to track**, rename it to `.mcp.json`, and substitute the two placeholders:

```jsonc
{
  "mcpServers": {
    "evalharness": {
      "command": "python3",
      "args": ["<ABSOLUTE_PATH_TO_EVALHARNESS>/mcp_server.py"],
      "env": {
        "EVALHARNESS_REPO": "<ABSOLUTE_PATH_TO_YOUR_REPO>"
      }
    }
  }
}
```

- `<ABSOLUTE_PATH_TO_EVALHARNESS>` — where you cloned this repo.
- `<ABSOLUTE_PATH_TO_YOUR_REPO>` — the repo whose work you're capturing. This
  is what tells the server which `.evalharness/` to read and write.

Both must be **absolute**; the agent launches the server with an unpredictable
working directory. (You can also pass the repo as `argv[1]` instead of using
the env var.)

**Restart your agent after editing this file.** MCP servers are loaded at
session start — an already-running session will not pick up the new server.

### Any other MCP-capable agent

Cursor, Windsurf, Cline, and anything else that speaks MCP use the same
`mcpServers` config shape — only the file it lives in differs. Point it at
`python3 <path>/mcp_server.py` with `EVALHARNESS_REPO` set and it works
identically. Nothing in the server is Claude-specific.

### Optional env vars

| Variable | Effect |
|---|---|
| `EVALHARNESS_REPO` | Repo the server operates on (or pass it as `argv[1]`) |
| `CVDP_DATASET` | Default dataset path for the `import_cvdp` tool |
| `CVDP_BASELINE_MANIFEST` | Default manifest path for the `import_baseline` tool |
| `EVALHARNESS_PET_NAME` | Rename the persona (default `EvalScout`) |
| `EVALHARNESS_PET_ICON` | Change the persona icon (default `[•_•]`) |

### Testing the server without an agent

```bash
python3 mcp_test_client.py /path/to/your/repo
```

Launches the server, does the handshake, prints the instructions a real agent
would load, and gives you a prompt where `check`, `list`, `decide`, `score`,
`get`, `feedback`, and `raw <tool> <json>` work as plain typed commands.

---

## CVDP: bring your own dataset

evalharness **ships no CVDP data.** No problems, no prompts, no contexts, no
patches. Same posture as SiliconCrew's own cvdp-pipeline README: *contains no
CVDP data — reads a dataset JSONL you supply.* The dataset is NVIDIA's and is
licensed separately; obtaining it and complying with its terms is on you.

### The free path — `import-baseline` (recommended first)

If you have an already-graded CVDP results file — a bench-orchestrator
`FINAL_MANIFEST.json`, e.g. from a SiliconCrew run — you can import its verdicts
as an external reference line:

```bash
evalctl import-baseline --manifest /path/to/bench-orchestrator/final_runs/FINAL_MANIFEST.json
```

**Cost: zero.** It reads one local JSON file. No Docker, no container pull, no
agent runs, no network, no usage burned. You get a real CVDP calibration number
immediately, broken down by difficulty.

Those verdicts are stamped with a fixed synthetic harness id and labelled as an
external reference throughout the UI, because they were produced by *someone
else's* harness. They give you a line to compare against; they will not move
when you change your own setup.

### Importing problems to attempt yourself

```bash
evalctl import-cvdp --dataset /path/to/cvdp.jsonl --difficulty easy --limit 20
```

This imports problems as eval definitions with their prompts and context
materialized to disk, so you can browse and `attempt` them. Without
`--success-command` they import as `needs_grading`: attemptable, but kept **out
of your score** rather than faking a verdict.

### The expensive path — faithful Tier-2 grading

A faithful CVDP verdict requires the official reference container
(**Docker + `ghcr.io/hdl/sim/osvb`**, licensed separately, not bundled here)
plus a real agent run per problem.

**Be honest with yourself about the cost.** Based on SiliconCrew's recorded
runs, a Tier-2 CVDP problem costs roughly **$7 median and ~22 minutes per
problem**. A 50-problem sweep is therefore on the order of several hundred
dollars and most of a day. That is why `import-baseline` exists and why it's
the default recommendation: take the free calibration line first, and spend
real money only on the specific problems you actually care about.

---

## Feedback (for friends testing this)

Please log friction as you hit it — a one-liner is fine, don't polish it:

```bash
evalctl feedback "check picked a confusing task description"
evalctl feedback "wasn't obvious that replay doesn't measure the harness"
```

Then send back:

- `.evalharness/feedback.jsonl` — the notes above (**this is the one I need**)
- `.evalharness/evals.json` and `.evalharness/runs.jsonl` — optional, but much
  more useful; they show what got captured and how it scored

**No source code needs to leave your machine.** `evals.json` does contain
captured diffs of your work, so skim it before sending, or send only
`feedback.jsonl` if the diffs are sensitive. `runs.jsonl` holds verdicts,
timings, harness ids, and command output tails.

Things I especially want to know: did `check` propose evals that felt worth
keeping? Did you understand attempt vs replay without reading this README
twice? Did the score ever move in a way that matched your gut?

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version — these are hard
constraints, not preferences:

- **stdlib only, no dependencies**, ever (must run on locked-down EDA
  workstations with stock Python)
- **Python 3.9 compatible** (why the MCP server is hand-rolled instead of using
  the 3.10+ `mcp` SDK)
- **zero network calls** in core functionality; no telemetry
- **ship no third-party data**; read user-supplied paths instead
- **toolchain-agnostic**; the success command is always the user's own

## License

MIT — see [LICENSE](LICENSE). Third-party attributions (SiliconCrew, NVIDIA
CVDP, the osvb container image) are in [NOTICE](NOTICE); none of them are
bundled or redistributed here.
