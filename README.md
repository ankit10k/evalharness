# Semiflow EvalBench

**Measure whether your AI coding setup is actually getting better.**

**Built for semiconductor design and verification teams.** RTL, testbenches,
architecture, and synthesis, with your own simulator and your own flow.

`[•_•] EvalScout`

Stdlib-only Python. No dependencies, no server, no account, no network calls.
Your RTL never leaves your machine.

> Nothing here is hardware-only at the mechanical level, so it works on any
> codebase. The defaults, eval categories, benchmark integrations, and design
> decisions are aimed squarely at silicon teams.

---

## Table of contents

- [What is this?](#what-is-this)
- [Why you need it](#why-you-need-it)
- [**Built for silicon flows**](#built-for-silicon-flows)
- [How it works](#how-it-works)
- [**Two ways to use it**](#two-ways-to-use-it)
- [Requirements](#requirements)
- [**Option A: With your coding agent (MCP)**](#option-a-with-your-coding-agent-mcp) - recommended
- [**Option B: Command line**](#option-b-command-line)
- [The dashboard](#the-dashboard)
- [Command reference](#command-reference)
- [CVDP benchmark support](#cvdp-benchmark-support)
- [SiliconCrew integration](#siliconcrew-integration)
- [FAQ](#faq)
- [Giving feedback](#giving-feedback)
- [License and attribution](#license-and-attribution)

---

## What is this?

Your team uses an AI coding agent (Claude Code, Cursor, Codex, or similar) on
RTL, testbenches, and debug. Over time you tune how it works: you write a
`CLAUDE.md` describing your clocking conventions, add a skill document for
your AXI rules, connect an EDA MCP server, switch models. All of that together
is what we call your **harness**.

Here is the problem: you have no idea whether any of it helps.

You write a document explaining your reset methodology. Does the agent
actually produce better RTL now? You switch models. Better or worse on
constrained-random testbenches? You connect a lint tool. Did that move the
needle, or just add noise?

Most teams answer this with vibes. Semiflow EvalBench answers it with a number,
measured on your own designs, inside your own environment.

It works in two steps:

1. **It captures your real work.** When you fix a bug and verify the fix, the
   tool quietly records it: the starting code, the task, and the command that
   proves it works. That becomes an *eval*.
2. **It replays that work against your current setup.** It rewinds your repo to
   the state before you fixed the bug, hands the task to your agent, lets the
   agent solve it from scratch, then checks the result. Do this across a set of
   evals and you have a score. Change your harness, run it again, and you can
   see whether the score moved.

No synthetic benchmarks. No writing test cases by hand. The evals come from
work you were already doing.

---

## Why you need it

**You are flying blind.** Prompt engineering and context engineering are
guesswork without measurement. A document that feels helpful may do nothing.
A change that seems minor may help a lot. You cannot tell without evidence.

**Your RTL never leaves your machine.** Proprietary designs, customer IP, and
anything under NDA cannot be uploaded to a cloud benchmarking service. For most
semiconductor teams that rules out every hosted option. Semiflow EvalBench runs
entirely locally, makes zero network calls, and stores everything in a
`.evalbench/` folder inside your own repo. It works on an air-gapped
workstation.

**It fits your existing flow.** No new simulator, no new methodology, no change
to how your team works. You give it the shell command you already use to sign
off on a change: a `make regression`, an `iverilog`/`vvp` run, Verilator, a
VCS/Xcelium script, a cocotb suite, a formal proof, or a synthesis check. If it
exits `0`, it passed.

**It runs where your tools run.** Python standard library only, no pip install,
Python 3.9 compatible. That matters on locked-down EDA workstations where you
cannot freely install packages.

**It catches overfitting.** Tune your context files enough and you will fit them
to one block's quirks rather than making the agent genuinely better at your
designs. Semiflow EvalBench tracks your own evals and public hardware benchmark
problems as two separate scores, so you can see when one improves and the other
does not.

---

## Built for silicon flows

Concretely, not just in positioning:

**Evals are categorised by design activity.** Every eval is typed as
`rtl`, `testbench`, `architecture`, `synthesis`, or `other`, inferred from the
files you touched and correctable by hand. Your score breaks down by category,
so "the agent got better at testbenches but worse at synthesis constraints" is
something you can actually see.

**Hardware benchmarks are first-class.** Built-in support for NVIDIA's
[CVDP](#cvdp-benchmark-support) benchmark (RTL design, testbench authoring, and
debug problems) as a public calibration point next to your private evals, plus
an importer for [SiliconCrew](#siliconcrew-integration) results.

**Optional EDA tool integration.** If you run an RTL/DV MCP server alongside it
(for example SiliconCrew's `rtl-codex`, which exposes lint, simulation, and
formal), the agent is prompted to run those checks before an eval is approved,
and the results are recorded on the eval as `supplementary_checks`.

**Designed around long verification loops.** Attempts run in isolated git
worktrees, so a multi-minute regression runs against a clean checkout without
disturbing your working tree.

**The pilot design.** This was built and dogfooded against an AI-generated L1
data cache: RTL, three testbenches, SDC constraints, and a Sky130 synthesis
flow.

---

## How it works

### The one concept that matters: attempt vs regression

There are two very different things you can measure. Confusing them is the
main way people fool themselves.

**Regression** (`evalbench replay`)

Re-runs an eval's success command against your **current** code, where the fix
already exists.

This tells you the repo still works. It says nothing about your harness.
Editing `CLAUDE.md` cannot change the outcome, because the code is already
fixed. Useful for catching breakage. Useless for measuring your setup.

**Attempt** (`evalbench attempt` then `evalbench grade`)

Checks out the **original, pre-fix** code into an isolated workspace, hands
your agent the task, lets it solve the problem from scratch, then runs the
success command and records the verdict.

This is the measurement that actually moves when your harness changes.

> **Rule of thumb:** "Is my repo healthy?" use `replay`. "Is my setup getting
> better?" use `attempt` and `grade`.

Semiflow EvalBench never calls an AI model itself. It prepares the workspace and
grades the outcome. Your agent does the work in between.

### Two numbers: score and consistency

| Metric | Meaning |
|---|---|
| **Score** | The latest verdict for each eval. "Where do I stand right now." |
| **Consistency** | Passed divided by total across an eval's whole run history. "Does this behave the same way every time." |

Consistency shows `n/a` until an eval has at least 2 runs, because 100 percent
off a single run is noise, not information.

If you attempt the same eval several times under one harness, consistency is
effectively **Pass@k**: how reliably your setup solves that task rather than
whether it got lucky once.

### Harness versions

Your harness gets a content hash covering your context files, declared model,
and declared tools. Change any of them and the hash changes, so silent drift
gets detected even if you forget to record it.

You can also give versions readable names:

```bash
evalbench harness v2 "added cache-debug skill doc"
```

That turns your trend chart into `v1 -> v2 -> v3` instead of a row of opaque
hashes.

---

## Two ways to use it

Semiflow EvalBench works in two modes. They share the same data, the same
`.evalbench/` folder, and the same scores, so you can mix them freely.

| | **Option A: With your agent (MCP)** | **Option B: Command line** |
|---|---|---|
| Setup | Drop in one config file | Clone, optionally add an alias |
| Who runs it | Your agent, on its own | You, by typing commands |
| Best for | Everyday use, hands off | Scripting, CI, agents without MCP |
| You must remember | Nothing | To run `check` after each task |

**Start with Option A if your agent supports MCP.** That covers Claude Code,
Cursor, and most modern coding agents. The agent captures your work and reports
scores without you having to remember anything, which is the entire point of
the tool. Option B is there when you want direct control, are scripting, or
your agent does not speak MCP.

---

## Requirements

- **Python 3.9 or newer** (standard library only, nothing to install)
- **git**
- **Whatever command you already use to verify your work**

Docker is not required. It appears only in the optional CVDP grading path
described later.

```bash
git clone https://github.com/ankit10k/semiflow-evalbench.git
```

That is the whole installation. There is no build step and no package to
install.

---

## Option A: With your coding agent (MCP)

**Recommended.** Your agent drives the tool, so evals get captured as a natural
part of finishing work rather than as a chore you have to remember.

### 1. Add the config

Copy `.mcp.json.template` from this repository into the root of the project you
want to track, rename it to `.mcp.json`, and fill in the two absolute paths:

```json
{
  "mcpServers": {
    "semiflow-evalbench": {
      "command": "python3",
      "args": ["/absolute/path/to/semiflow-evalbench/mcp_server.py"],
      "env": {
        "EVALBENCH_REPO": "/absolute/path/to/your/project"
      }
    }
  }
}
```

### 2. Restart your agent session

MCP servers load at session start, so a session that is already running will
not pick up the change.

To confirm it connected in Claude Code, run `/mcp` and look for
`semiflow-evalbench`.

### 3. Just work

Ask your agent to do something real and let it verify the work as usual. From
there it handles the rest:

- After your tests pass, it calls `check` on its own
- It tells you what changed and asks whether to keep it as an eval
- It records your answer, updates the score, and gives you a dashboard link

A typical exchange:

```
You:   Fix the FIFO overflow when depth is 1.

Agent: Fixed it in rtl/fifo.sv and make test passes.

       [*_*] EvalScout: nice, that is real work - Fix FIFO overflow
       (rtl/fifo.sv). Logged as eval_a1b2c3d4. Want to keep it as a
       quality check going forward?

You:   Yes, tag it fifo.

Agent: Locked in. Tracking 6 evals now. Board: file:///.../dashboard.html
```

The first time in a repo, the agent asks for your success command, context
globs, model, and tools, then sets everything up.

### 4. Ask it things in plain language

No commands to memorise:

- *"How is our eval score looking?"*
- *"Re-run the evals and see if that skill doc helped"*
- *"I changed CLAUDE.md, call this harness v3"*
- *"Try eval_a1b2c3d4 from scratch and see if you can still solve it"*

### Available tools

Thirteen MCP tools: `check`, `decide`, `start_attempt`, `grade_attempt`,
`set_harness`, `replay`, `score`, `list_pending`, `get_eval`, `import_cvdp`,
`import_baseline`, `feedback`, and `init`.

This works with any MCP-capable agent, not only Claude Code. Cursor and others
use the same `mcpServers` configuration shape.

### Customising the persona

Add these to the `env` block of your `.mcp.json`:

```json
"EVALBENCH_PET_NAME": "Scout",
"EVALBENCH_PET_ICON": "(o_o)"
```

### Testing the server without an agent

```bash
python3 mcp_test_client.py /path/to/your/project
```

An interactive prompt where you can run `score`, `list`, `check <cmd>`,
`get <eval_id>`, or `raw <tool> <json>` straight against the server. Useful for
confirming things work before wiring up an agent.

---

## Option B: Command line

Use this when you want direct control, are scripting something, or your agent
does not support MCP. Everything Option A does is available here as a command.

Optionally add a shortcut so you can type `evalbench` anywhere:

```bash
echo "alias evalbench='python3 /absolute/path/to/semiflow-evalbench/evalbench.py'" >> ~/.zshrc
source ~/.zshrc
```

Use `~/.bashrc` if you use bash. The rest of this section assumes the alias.

### Step 1: Initialize inside the repo you work in

```bash
cd /path/to/your/project
evalbench init
```

It asks four questions:

| Question | What to answer | Example |
|---|---|---|
| Success command | The command you already use to sign off a change | `make regression TEST=fifo` |
| Context globs | Files that make up your harness | `CLAUDE.md,skills/**,docs/methodology.md` |
| Model | Which model you use | `claude-sonnet-5` |
| Tools | MCP servers or tools available to your agent | `rtl-codex` |

This creates `.evalbench/config.json` and adds `.evalbench/` to your
`.gitignore`.

### Step 2: Do real work

Fix a bug. Add a feature. Write a testbench. Whatever you were going to do
anyway.

### Step 3: Capture it

When you would normally run your test to confirm the work, run it through
`check` instead:

```bash
evalbench check "make regression TEST=fifo" --prompt "Fix the FIFO overflow when depth is 1"
```

`check` runs your command, notices you changed real code, and proposes an eval:

```
eval_a1b2c3d4  [rtl]  Fix FIFO overflow (rtl/fifo.sv)
  rtl/fifo.sv  (+4/-2)  commit 8a31f2c9de
  success: make regression TEST=fifo
  prompt: Fix the FIFO overflow when depth is 1
  [a]pprove / [e]dit / [r]eject / [t]ag / [p]rompt / [c]omment / [y]pe / [d]iff / [s]kip?
```

Press `a` to keep it.

> **Always pass `--prompt`.** It is stored as the task statement. Without it the
> eval can never be attempted, and you are left with a plain regression test.

### Step 4: Measure your harness

Pick an eval and make your agent redo it from scratch:

```bash
evalbench attempt eval_a1b2c3d4
```

This prints an isolated workspace path and the task. The workspace contains
your code **before** the fix. Let your agent solve the task in that workspace,
then grade it:

```bash
evalbench grade att_5e6f7a8b
```

You get `PASS` or `FAIL`. Repeat a few times on the same eval to build a
consistency number.

### Step 5: Change something, then measure again

Edit your `CLAUDE.md`, add a skill document, or switch models. Record the new
version:

```bash
evalbench harness v2 "added RTL debugging guide"
```

Run your attempts again, then compare:

```bash
evalbench score
```

```
Harness: v2 - added RTL debugging guide  (a5f11bcfac5a)

[local] score 4/5 (80%)   consistency 73% (over 5 eval(s); 0 need more runs)
  by type - rtl: 3/3  testbench: 1/2
```

That difference between v1 and v2 is the thing you could not see before.

---
## The dashboard

```bash
evalbench dashboard
```

This writes `.evalbench/dashboard.html` and opens it in your browser. It is a
single static file. There is no server and no network request.

What you get:

- **Suite cards** showing score and consistency for your own evals and for
  benchmark problems, side by side but never blended into one figure.
- **A trend chart** plotting your score across every harness version, so
  improvement or regression is visible at a glance.
- **Filter chips** for eval type (`rtl`, `testbench`, `architecture`,
  `synthesis`, `other`) and suite.
- **Expandable eval rows.** Click any eval to see its purpose, task prompt,
  success command, links to the touched files, a colour-coded diff, your
  comment thread, and every run it has ever had with the mode of each run.
- **A warning banner** when a suite has no attempt-backed runs, because that
  means the numbers are regression results and do not reflect harness quality.

The dashboard regenerates automatically whenever you approve an eval, replay,
or check the score, so it never goes stale.

---

## Command reference

### Everyday commands

| Command | What it does |
|---|---|
| `evalbench init` | One-time setup in a repo |
| `evalbench check "<cmd>" --prompt "<task>"` | Run your test, capture the work as an eval |
| `evalbench attempt <eval_id>` | Open a pre-fix workspace for your agent to solve |
| `evalbench grade <attempt_id>` | Grade a finished attempt |
| `evalbench harness <version> "<note>"` | Name your current harness version |
| `evalbench score` | Print score and consistency per suite |
| `evalbench dashboard` | Build and open the visual dashboard |

### Occasional commands

| Command | What it does |
|---|---|
| `evalbench replay` | Re-run all evals against current code (repo health only) |
| `evalbench ask "<question>"` | Local Q and A over your data: `score`, `failing`, `trend`, `tags` |
| `evalbench feedback "<text>"` | Log a note about the tool itself |
| `evalbench import-cvdp --dataset <path>` | Import CVDP benchmark problems |
| `evalbench import-baseline --manifest <path>` | Import already-graded reference verdicts |

### Advanced commands

| Command | What it does |
|---|---|
| `evalbench wrap "<cmd>"` | Capture a command without proposing an eval |
| `evalbench propose` | Turn the last capture into a draft eval |
| `evalbench review` | Review pending evals one by one |
| `evalbench run <eval_id>` | Re-run one eval against current code |

Add `--help` to any command for its full options.

## CVDP benchmark support

**CVDP** (Comprehensive Verilog Design Problems) is NVIDIA's public benchmark
for hardware design and verification agents. It contains 749 problems across
several categories, each with a task prompt, starting context, and a hidden
test harness that grades the solution.

Semiflow EvalBench can work with CVDP in three ways, with very different costs.

### Option 1: Import problem definitions (free)

```bash
evalbench import-cvdp --dataset /path/to/cvdp_v1.0.2_agentic_code_generation_no_commercial.jsonl --limit 20
```

Reads the dataset and creates eval definitions you can browse and filter. Costs
nothing, runs no agent, needs no Docker. This solves the cold-start problem: a
brand new install has something meaningful in it immediately.

Useful flags: `--limit`, `--ids`, `--difficulty easy|medium|hard`.

### Option 2: Import an existing graded baseline (free)

If you have a results file from a previous CVDP run, import its verdicts as an
external reference line:

```bash
evalbench import-baseline --manifest /path/to/FINAL_MANIFEST.json
```

This gives you a real calibration number at zero cost. See the SiliconCrew
section below for where such a file comes from.

### Option 3: Run CVDP problems yourself (expensive)

Actually solving CVDP problems with your own agent and grading them in NVIDIA's
official reference container is the gold standard, but it is costly. Based on
recorded runs, expect roughly **7 US dollars and 22 minutes per problem** at
API list prices. A full 92-problem sweep took about 40 hours.

This path also requires Docker, the `ghcr.io/hdl/sim/osvb` reference image, and
a working agent setup. If you are on a subscription plan rather than API
billing, the real constraint is your usage limits rather than money, and a full
sweep will exhaust them.

Start with options 1 and 2. Reach for option 3 only when you have a specific
reason.

### You supply the dataset

This repository contains **no CVDP data**. The dataset is licensed by NVIDIA
and is not redistributed here. Obtain it yourself from NVIDIA's CVDP benchmark
distribution and pass the path with `--dataset`, or set the `CVDP_DATASET`
environment variable.

---

## SiliconCrew integration

[SiliconCrew](https://siliconcrew-frontend-psp2dkllmq-uc.a.run.app/) is an
open-source AI hardware design platform by Naman Ranka. It automates RTL
generation, verification, synthesis, and physical design, and it includes a
benchmark orchestrator that can run CVDP problems end to end and grade them in
NVIDIA's official reference container.

Repository: <https://github.com/naman-ranka/siliconcrew> (MIT licensed)

### How Semiflow EvalBench relates to it

**It does not depend on it.** Semiflow EvalBench is standalone and works fully without
SiliconCrew installed. No SiliconCrew code is bundled or vendored here.

The connection is one optional, read-only adapter. If you have run CVDP through
SiliconCrew's benchmark orchestrator, it produces a results file at
`bench-orchestrator/final_runs/FINAL_MANIFEST.json`. Semiflow EvalBench can import
those verdicts:

```bash
evalbench import-baseline --manifest /path/to/siliconcrew/bench-orchestrator/final_runs/FINAL_MANIFEST.json
```

You get output like:

```
Imported 92 reference verdict(s) - 60/92 PASS (65%)
  easy     15/17 (88%)
  medium   36/55 (65%)
  hard      9/20 (45%)
```

### These are reference numbers, not your score

Verdicts imported this way were produced by **SiliconCrew's** harness: its
agent, its model, its tools. Not yours.

Semiflow EvalBench stamps them with a fixed synthetic harness identifier so they can
never be averaged into a measurement of your own system. They appear in the
dashboard with a `reference` badge, they are reported separately in `score`
output, and they are never marked stale, because they were never yours to
refresh.

Think of them as a calibration line: a marker of what a known-good hardware
design agent achieves on a public benchmark, sitting next to your own numbers
for comparison.

The adapter reads a **file**, not a repository. You do not need SiliconCrew
checked out anywhere. If you do not have such a file, this one command is
unavailable and everything else works normally.

---

## FAQ

**Do I need to change how I work?**

No. Do your work as usual. The only difference is running your verification
command through `evalbench check` instead of directly.

**Does any of my code leave my machine?**

No. Core functionality makes zero network calls. There is no telemetry, no
analytics, and no update check. Everything lives in `.evalbench/` inside your
repo, which the tool adds to your `.gitignore` automatically.

**Is this only for semiconductor work?**

It is built and tuned for it. The eval categories (`rtl`, `testbench`,
`architecture`, `synthesis`), the CVDP and SiliconCrew integrations, and the
no-dependency, air-gapped design all target silicon teams.

That said, nothing in the mechanism is hardware-specific. The success command
is just a shell command, so `pytest`, `npm test`, or `cargo test` work fine if
you point it at a software repo. You would simply not use the hardware-specific
parts.

**Does it need an API key?**

No. Semiflow EvalBench never calls a model. Your agent does the work; this tool
prepares the workspace and grades the result.

**Why does my score say 100 percent when I have only one eval?**

Because one eval passing is 100 percent of one eval. That is not a meaningful
signal. Consistency deliberately reports `n/a` until an eval has at least two
runs, for the same reason. Build up several evals and several runs before
reading anything into the numbers.

**What is the difference between score and consistency again?**

Score is your latest result. Consistency is how reliably you get that result.
An eval that passes now but passed only 3 of its last 10 attempts is not
solid, and score alone would hide that.

**My score did not change after I edited `CLAUDE.md`. Is it broken?**

Check whether you ran `replay` or `attempt`. `replay` re-runs your test against
already-fixed code, so it cannot change when your harness changes. Only
`attempt` measures your harness. The dashboard shows a warning when a suite has
no attempt-backed runs.

**Can I use this with Cursor, Codex, or another agent?**

Yes. The MCP server follows the standard protocol. Any MCP-capable agent can
connect using the same configuration shape. The CLI works with any agent, or
with none at all.

**What if my project has no tests?**

You need some command that distinguishes working code from broken code. It does
not have to be a full regression suite. A lint run that fails on error, an
elaboration or compile step, a single directed test, a formal property check, or
a script that greps a simulation log for `TEST PASSED` all work.

**Can my whole team share one set of evals?**

Not yet. Evals live in a gitignored local folder. You could commit
`.evalbench/` deliberately to share them, but this has not been tested and
the harness hash is machine-specific, so treat that as unsupported for now.

**How do I delete an eval I no longer want?**

Edit `.evalbench/evals.json` and remove the entry, or set its `status` to
`rejected`. The files are plain JSON and safe to edit by hand.

---

## Giving feedback

If you are testing this for someone, log friction as you hit it:

```bash
evalbench feedback "the review prompt options were confusing"
```

Then send back `.evalbench/feedback.jsonl`.

If you are willing, `.evalbench/evals.json` and `.evalbench/runs.jsonl` are
also useful. They contain eval metadata, diffs of changes you approved, and
verdicts. Review them first if your code is sensitive. Your source files are
never included, and nothing is ever sent automatically.

---

## License and attribution

Semiflow EvalBench is MIT licensed. Copyright (c) 2026 Ankit Kumar. See
[LICENSE](LICENSE).

Third-party components are documented in [NOTICE](NOTICE). In short:

- **SiliconCrew** (MIT, Copyright (c) 2026 Naman Ranka) is an optional adapter
  target. No SiliconCrew code is bundled here.
  <https://github.com/naman-ranka/siliconcrew>
- **NVIDIA CVDP benchmark dataset** is licensed separately by NVIDIA and is not
  redistributed here. You supply your own copy.
- **`ghcr.io/hdl/sim/osvb`** container image is licensed separately and is not
  bundled. It is needed only for the optional CVDP grading path.

Contributing guidelines, including the constraints that keep this tool
dependency-free and offline, are in [CONTRIBUTING.md](CONTRIBUTING.md).
