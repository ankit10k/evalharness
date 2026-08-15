#!/usr/bin/env python3
"""evalctl MCP server — the "QA pet" as an MCP tool, usable by any MCP-capable
coding agent (Claude Code, Cursor, etc.), not just Claude Code hooks.

Hand-rolled against the MCP stdio transport (newline-delimited JSON-RPC 2.0)
instead of the official `mcp` SDK, because that SDK requires Python 3.10+ and
this stays stdlib-only/Python-3.9-compatible on purpose — same reason evalctl
itself has zero dependencies: it has to run on whatever's already on an EDA
workstation, no pip install required beyond what ships with Python.

Design: this server does NOT try to block and wait for a human answer inside
a tool call (there's no terminal to prompt from an MCP call). Instead:
  - `check` captures + proposes an eval, saved as pending_review, and returns
    it to the calling agent to relay conversationally ("this looks
    eval-worthy, want me to add it?").
  - `decide` is how the agent (after talking to the user) finalizes it —
    approve/reject/tag, called as a *separate* tool call once the user has
    actually answered.
The agent is the pet's voice; this server is its hands.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evalctl as ec  # noqa: E402  (local module, path set above)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "evalharness", "version": "0.1.0"}

# --------------------------------------------------------------- the "pet"
#
# Every tool response carries a `pet_says` line in a fixed persona. This is
# deliberate: it's the one thing that shows up the same way regardless of
# which agent/model is calling the tool, since we can't control how a given
# agent chooses to narrate a bare JSON result. Tool descriptions tell the
# calling agent to surface this verbatim rather than paraphrase it away.

PET_ICON = os.environ.get("EVALHARNESS_PET_ICON", "[•_•]")
PET_NAME = os.environ.get("EVALHARNESS_PET_NAME", "EvalScout")

# Personality guide — keep pet_says lines consistent with this, not generic
# assistant-speak. Scout is a quiet tracker, not a cheerleader: it notices
# things and reports them plainly, short sentences, dry rather than peppy.
# It says "logged" / "tracking N" / "heads up" / "worth checking" — never
# "Great job!" / "Awesome!" / "I'd be happy to help with that!". It flags
# problems as plainly as wins; it doesn't perform enthusiasm.

SERVER_INSTRUCTIONS_STATIC = f"""\
IMPORTANT — read this now, before doing anything else in this session: you are connected to \
evalharness, an MCP server whose whole purpose depends on you calling it WITHOUT being asked. \
If you only call these tools when the user explicitly requests it, the tool provides zero \
value — that failure mode is the default to actively avoid. Call its persona {PET_ICON} {PET_NAME}. \
Its job is to turn real, verified work in this repo into a standing set of quality regressions, \
and to track whether the harness (context files, skills, model, tools) is actually getting \
better over time.

1. Right after you run and verify a real code change with the user's test/build/sim command, \
call `check` — every single time, unprompted, as a normal part of finishing that step, the same \
way you'd naturally report the test result itself. Do not wait to be asked. Do not treat this as \
optional or as something to do "if it seems relevant" — treat it as mandatory follow-through on \
any verified change, same tier of importance as running the test in the first place.
2. If `check` returns a `proposed_eval`, it is NOT saved as a tracked regression yet. Tell the \
user what changed in one short line (relay the `pet_says` field — keep its tone: brief, \
friendly, not corporate) and ask if they want to keep it. Only call `decide` (approve/reject) \
after they answer, unless they've given you standing permission to auto-approve in this repo.
3. If `check` reports no config yet, ask the user for a success command, context file globs, \
model, and available tools, then call `init` before proceeding.
4. When the user changes context/skill files, tools, or model: call `set_harness` to name the new \
version (v2, v3...), then measure it. Know which measurement you want:
   - `replay` re-runs each eval's success command against the CURRENT tree. The fix is already \
committed there, so this only tells you the repo is still green — it does NOT measure harness quality.
   - `start_attempt` + `grade_attempt` checks out the ORIGINAL pre-fix state and makes you solve the \
task again from scratch. This is the only mode that actually measures whether the system got better. \
Prefer it whenever the user asks about harness/system quality or progress over time. Repeat the same \
eval a few times to build a consistency (Pass@k) number.
When the user asks how things are going, call `score` and relay `pet_says`.
5. Every response includes a `pet_says` line — surface it to the user (your own phrasing is \
fine) instead of just reporting raw JSON fields back at them. Several tools also return a \
`dashboard_url` (a local file:// link to a visual scoreboard) — share it as a clickable link \
whenever it's present, don't paraphrase it into prose.
6. Two suites are tracked and scored separately, never blended: `local` (evals captured from the \
user's own work) and `cvdp` (imported NVIDIA benchmark problems, via `import_cvdp` — only on explicit \
request). If a harness change lifts the local score but leaves cvdp flat, that's an overfitting signal \
worth telling the user about.
7. If another MCP server for RTL/DV tooling (e.g. SiliconCrew's rtl-codex — lint, formal/sby, \
simulation) is also connected in this session, consider running its lint/formal checks before \
calling `decide` on an eval, and pass the results in `decide`'s optional `supplementary_checks` \
argument (e.g. {{"lint": "clean", "formal": "proved"}}) so they're recorded alongside the eval. \
Not required if no such server is connected — evalharness works standalone.
"""


def _memory_digest(cwd: Path) -> str:
    """Short-term-memory bootstrap for a fresh session: a few lines summarizing
    what's on disk in .evalharness/, computed fresh on every connect, so a new
    session (or one that just got context-compacted) doesn't start blind."""
    cfg = ec.read_json(ec.eh_path(ec.CONFIG_FILE), None)
    if cfg is None:
        return "\nCurrent state in this repo: no .evalharness/config.json yet — call `init` first.\n"

    evals = ec.read_evals()
    pending = sum(1 for e in evals if e["status"] == "pending_review")
    board = ec._compute_scoreboard(cwd, cfg)
    if board is None:
        return f"\nCurrent state in this repo ({cfg.get('repo_name', 'this repo')}): no runs recorded yet. {pending} eval(s) pending review.\n"

    o = board["overall"]
    pct = 100 * o["passed"] / o["total"] if o["total"] else 0
    stale = sum(1 for r in board["rows"] if r["stale"])
    lines = [f"\nCurrent state in this repo ({cfg.get('repo_name', 'this repo')}): score {o['passed']}/{o['total']} ({pct:.0f}%)."]
    if pending:
        lines.append(f"{pending} eval(s) pending review — consider calling `list_pending` early to catch up.")
    if stale:
        lines.append(f"{stale} eval(s) ran under a previous harness version — the context/tools/model changed since; call `replay` if you want a fresh score.")
    return " ".join(lines) + "\n"


def build_server_instructions(cwd: Path) -> str:
    return SERVER_INSTRUCTIONS_STATIC + _memory_digest(cwd)

TIPS = [
    "changed CLAUDE.md or a skill file recently? run `replay` and see if the score moved.",
    "if a file keeps showing up across evals, that's usually worth turning into a skill doc.",
    "tags are free — use them to group evals by the pattern they represent, not just the file.",
    "a stale run just means the harness changed since it last ran — `replay` refreshes it.",
    "rejecting a bad proposal is as useful as approving a good one — keeps the regression set honest.",
]


def _tip(seed: int) -> str:
    return TIPS[seed % len(TIPS)]


def _say(text: str) -> str:
    return f"{PET_ICON} {PET_NAME}: {text}"


def _dashboard_link(cwd: Path, cfg: dict) -> str:
    path = ec._write_dashboard(cwd, cfg)
    return f"file://{path}"


def _stale_note(cwd: Path, cfg: dict) -> str | None:
    """State-keyed nudge, not static boilerplate — only fires when the harness
    actually changed since some eval last ran."""
    board = ec._compute_scoreboard(cwd, cfg)
    if board is None:
        return None
    stale = sum(1 for r in board["rows"] if r["stale"])
    if not stale:
        return None
    return f"heads up, {stale} tracked eval(s) ran under a previous harness — run `replay` to see if your recent changes actually helped."


# ------------------------------------------------------------------ tools

def _cwd() -> Path:
    return Path.cwd()


def _cfg() -> dict:
    return ec.load_config()


def tool_check(args: dict) -> dict:
    """Run the user's verification command; if it changed real code, propose
    an eval (saved as pending_review — NOT auto-approved) and return it."""
    cwd = _cwd()
    cfg_path = ec.eh_path(ec.CONFIG_FILE)
    if not cfg_path.exists():
        return {
            "ok": False,
            "error": (
                "No .evalharness/config.json in this repo yet. Ask the user for a "
                "success command, context file globs, model, and tools, then call "
                "the `init` tool with them before calling `check` again."
            ),
            "pet_says": _say(
                "hey, we haven't met on this repo yet! Get me a success command, "
                "context files, and your model/tools, and call `init` to get started."
            ),
        }
    cfg = _cfg()
    command = args.get("command") or cfg.get("success_command_template")
    if not command:
        return {"ok": False, "error": "No command given and no success_command_template configured."}

    capture = ec._capture(cwd, command)
    if args.get("prompt"):
        capture["input_prompt"] = args["prompt"]
    ec.append_jsonl(ec.eh_path(ec.CAPTURES_FILE), capture)
    verdict = "PASS" if capture["exit_code"] == 0 else "FAIL"

    result = {
        "ok": True,
        "verdict": verdict,
        "duration_sec": capture["duration_sec"],
        "output_tail": capture["output_tail"] if verdict == "FAIL" else None,
        "diff_present": capture["diff_present"],
        "proposed_eval": None,
        "nudge": None,
    }
    if not capture["diff_present"]:
        stale_note = _stale_note(cwd, cfg)
        say = f"ran it, still {verdict.lower()}, nothing new to log."
        say = f"{say} {stale_note}" if stale_note else f"{say} {_tip(int(capture['timestamp']))}"
        result["pet_says"] = _say(say)
        return result

    if verdict == "FAIL":
        first_line = (capture["output_tail"] or "").splitlines()[-1:] or [""]
        result["pet_says"] = _say(f"that broke — {first_line[0].strip()[:80]}. Want me to dig in before we call it an eval?")
        return result

    ev = ec._propose_from_capture(cwd, cfg, capture)
    ev["status"] = "pending_review"
    evals = ec.read_evals()
    evals.append(ev)
    ec.write_json(ec.eh_path(ec.EVALS_FILE), evals)

    nudge = ec._nudge_for(cwd, ev)
    result["proposed_eval"] = {
        "eval_id": ev["eval_id"],
        "type": ev["type"],
        "suite": ev["suite"],
        "summary": ev["summary"],
        "input_prompt": ev["input_prompt"],
        "success_command": ev["success_command"],
        "diffstat": ev["diffstat"],
        "code_links": ev["code_links"],
        "starting_commit": ev["starting_commit"][:10],
    }
    result["nudge"] = nudge
    result["instructions_for_agent"] = (
        "This eval is pending_review — it is NOT part of the tracked regression set yet. "
        "Relay `pet_says` to the user (verbatim tone, your own words are fine) and ask if they "
        "want to keep it as a quality check going forward. Only call `decide` with approve after "
        "they say yes (or you have clear standing permission to auto-approve for this repo)."
    )
    say = f"nice, that's real work — {ev['summary']}. Logged as {ev['eval_id']} (use `decide` to keep or discard this)."
    if len(capture["diff"]) > ec._MAX_DIFF_CHARS:
        say += f" (heads up — this diff was huge, {len(capture['diff'])} chars, I truncated what I stored. Worth checking that wasn't an accidental generated-file commit.)"
    if nudge:
        say += f" Also: {nudge}"
    result["pet_says"] = _say(say)
    return result


def tool_list_pending(args: dict) -> dict:
    cwd = _cwd()
    cfg = _cfg()
    evals = ec.read_evals()
    pending = [e for e in evals if e["status"] == "pending_review"]
    if not pending:
        stale_note = _stale_note(cwd, cfg)
        pet_says = _say(f"all caught up — nothing waiting on you.{' ' + stale_note if stale_note else ''}")
    else:
        pet_says = _say(f"{len(pending)} eval(s) waiting on a decision: {', '.join(e['eval_id'] for e in pending)}.")
    return {
        "ok": True,
        "pet_says": pet_says,
        "pending": [
            {
                "eval_id": e["eval_id"],
                "type": e.get("type", "other"),
                "input_prompt": e.get("input_prompt", ""),
                "summary": e.get("summary", e["eval_id"]),
                "diffstat": e.get("diffstat", {}),
                "success_command": e.get("success_command", ""),
            }
            for e in pending
        ],
    }


def tool_decide(args: dict) -> dict:
    """Finalize a pending eval: approve (optionally with edits/tags), reject."""
    cwd = _cwd()
    cfg = _cfg()
    eval_id = args.get("eval_id")
    decision = (args.get("decision") or "").lower()
    if decision not in ("approve", "reject"):
        return {"ok": False, "error": "decision must be 'approve' or 'reject'."}

    evals = ec.read_evals()
    ev = next((e for e in evals if e["eval_id"] == eval_id), None)
    if ev is None:
        return {"ok": False, "error": f"No eval {eval_id}."}

    if args.get("task_text"):
        ev["task_text"] = args["task_text"]
    if args.get("success_command"):
        ev["success_command"] = args["success_command"]
    if args.get("tags"):
        ev["tags"] = sorted(set(ev.get("tags", [])) | set(args["tags"]))
    if args.get("purpose"):
        ev["purpose"] = args["purpose"]
    if args.get("input_prompt"):
        ev["input_prompt"] = args["input_prompt"]
    if args.get("type") in ec.EVAL_TYPES:
        ev["type"] = args["type"]
    if args.get("comment"):
        ev.setdefault("comments", []).append({"text": args["comment"], "timestamp": ec.time.time()})
    if args.get("supplementary_checks"):
        ev["supplementary_checks"] = {
            **ev.get("supplementary_checks", {}),
            **args["supplementary_checks"],
        }

    if decision == "reject":
        ev["status"] = "rejected"
        ec.write_json(ec.eh_path(ec.EVALS_FILE), evals)
        return {
            "ok": True, "eval_id": eval_id, "status": "rejected",
            "pet_says": _say("tossed it — not every diff needs to be a regression."),
        }

    ev["status"] = "approved"
    ec.write_json(ec.eh_path(ec.EVALS_FILE), evals)
    try:
        run_record = ec._execute_eval(cwd, cfg, ev, commit=None)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    ec.append_jsonl(ec.eh_path(ec.RUNS_FILE), run_record)
    approved_count = sum(1 for e in evals if e["status"] == "approved")
    tag_note = f" tagged {', '.join(ev['tags'])}." if ev.get("tags") else ""
    checks_note = ""
    if ev.get("supplementary_checks"):
        checks_str = ", ".join(f"{k}={v}" for k, v in ev["supplementary_checks"].items())
        checks_note = f" Also recorded: {checks_str}."
    link = _dashboard_link(cwd, cfg)
    return {
        "ok": True,
        "eval_id": eval_id,
        "status": "approved",
        "verdict": run_record["verdict"],
        "tags": ev.get("tags", []),
        "supplementary_checks": ev.get("supplementary_checks", {}),
        "dashboard_url": link,
        "pet_says": _say(f"locked in ✅ — tracking {approved_count} eval(s) now.{tag_note}{checks_note} Board: {link}"),
    }


def tool_replay(args: dict) -> dict:
    cwd = _cwd()
    cfg = _cfg()
    evals = ec.read_evals()
    approved = [e for e in evals if e["status"] == "approved"]
    results = []
    for ev in approved:
        try:
            run_record = ec._execute_eval(cwd, cfg, ev, commit=None)
        except RuntimeError as exc:
            results.append({"eval_id": ev["eval_id"], "error": str(exc)})
            continue
        ec.append_jsonl(ec.eh_path(ec.RUNS_FILE), run_record)
        results.append({"eval_id": ev["eval_id"], "verdict": run_record["verdict"]})
    board = ec._compute_scoreboard(cwd, cfg)
    if board is None:
        pet_says = _say("nothing approved yet to replay.")
        link = None
    else:
        o = board["overall"]
        link = _dashboard_link(cwd, cfg)
        pet_says = _say(f"replayed {len(results)} eval(s) — {_suite_phrase(board)}. Board: {link}")
    return {"ok": True, "results": results, "scoreboard": board, "dashboard_url": link, "pet_says": pet_says}


def _suite_phrase(board: dict) -> str:
    """Per-suite phrasing that never presents an external reference baseline as
    if it were a measurement of the user's own harness."""
    parts = []
    for name, s in (board.get("suites") or {}).items():
        o = s["overall"]
        if not o["total"]:
            continue
        pct = 100 * o["passed"] / o["total"]
        n_ref = sum(1 for r in s["rows"] if r.get("reference"))
        if n_ref == o["total"]:
            parts.append(f"{name} {o['passed']}/{o['total']} ({pct:.0f}%) — external reference, not your harness")
        elif n_ref:
            own = [r for r in s["rows"] if not r.get("reference")]
            op = sum(1 for r in own if r["verdict"] == "PASS")
            parts.append(f"{name} yours {op}/{len(own)}, reference {o['passed']-op}/{n_ref}")
        else:
            cons = s.get("consistency")
            c = "" if cons is None else f", consistency {100*cons:.0f}%"
            parts.append(f"{name} {o['passed']}/{o['total']} ({pct:.0f}%){c}")
    return "; ".join(parts) or "nothing scored yet"


def tool_score(args: dict) -> dict:
    cwd = _cwd()
    cfg = _cfg()
    board = ec._compute_scoreboard(cwd, cfg)
    if board is None:
        return {"ok": True, "scoreboard": None, "message": "No runs recorded yet.", "pet_says": _say("no runs yet — call `check` on some real work to get started.")}
    o = board["overall"]
    link = _dashboard_link(cwd, cfg)
    pet_says = _say(f"{_suite_phrase(board)}. Board: {link}. {_tip(o['total'])}")
    return {"ok": True, "scoreboard": board, "dashboard_url": link, "pet_says": pet_says}


def tool_start_attempt(args: dict) -> dict:
    """Open an isolated worktree at the eval's starting commit + hand back the
    task. THIS is the real eval mode — the agent must actually solve it."""
    cwd = _cwd()
    cfg = _cfg()
    ev = next((e for e in ec.read_evals() if e["eval_id"] == args.get("eval_id")), None)
    if ev is None:
        return {"ok": False, "error": f"No eval {args.get('eval_id')}."}
    try:
        att = ec.start_attempt(cwd, cfg, ev)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc),
                "pet_says": _say(f"can't attempt that one yet — {exc}")}
    return {
        "ok": True,
        "attempt_id": att["attempt_id"],
        "workspace": att["workspace"],
        "task": att["input_prompt"],
        "starting_commit": att["starting_commit"][:10],
        "instructions_for_agent": (
            "A clean worktree is checked out at the eval's ORIGINAL pre-fix state — the solution is "
            f"NOT present. Do the work described in `task` inside {att['workspace']} (and only there; "
            "do not touch the main working tree). When you believe it's solved, call `grade_attempt` "
            "with this attempt_id. Do not run the success command yourself to peek — grading does that "
            "and records the verdict."
        ),
        "pet_says": _say(
            f"attempt {att['attempt_id']} open on a clean worktree at {att['starting_commit'][:10]}. "
            "Solve it in there, then call grade_attempt."
        ),
    }


def tool_grade_attempt(args: dict) -> dict:
    cwd = _cwd()
    cfg = _cfg()
    try:
        rec = ec.grade_attempt(cwd, cfg, args.get("attempt_id"), keep_workspace=bool(args.get("keep_workspace")))
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    stat = ec._diffstat(rec.get("solution_diff", ""))
    link = _dashboard_link(cwd, cfg)
    verdict = rec["verdict"]
    say = (f"attempt graded: {verdict}. The agent changed {len(stat['files'])} file(s) "
           f"(+{stat['added']}/-{stat['removed']}).")
    say += (" That's a real harness signal — it solved the task from scratch." if verdict == "PASS"
            else " It couldn't solve it from the original state — that's the honest signal.")
    return {
        "ok": True,
        "verdict": verdict,
        "eval_id": rec["eval_id"],
        "duration_sec": rec["duration_sec"],
        "output_tail": rec["output_tail"] if verdict == "FAIL" else None,
        "solution_diffstat": stat,
        "dashboard_url": link,
        "pet_says": _say(f"{say} Board: {link}"),
    }


def tool_import_cvdp(args: dict) -> dict:
    """Import NVIDIA CVDP problems as eval definitions. Zero cost — no agent
    runs, no Docker. The dataset is user-supplied; this tool ships no data."""
    cwd = _cwd()
    _cfg()
    dataset = args.get("dataset") or os.environ.get("CVDP_DATASET", "")
    if not dataset:
        return {
            "ok": False,
            "error": "No dataset path. Pass `dataset`, or set CVDP_DATASET in the server env.",
            "pet_says": _say(
                "I don't ship CVDP data — it's NVIDIA's, licensed separately. Point me at a "
                "CVDP JSONL you've downloaded and I'll import the problems as evals."
            ),
        }
    ds = Path(dataset).expanduser()
    if not ds.is_absolute():
        ds = (cwd / ds).resolve()
    if not ds.exists():
        return {"ok": False, "error": f"Dataset not found: {ds}",
                "pet_says": _say(f"can't find a CVDP dataset at {ds}.")}

    try:
        res = ec.import_cvdp(
            cwd, ds,
            limit=int(args.get("limit") or 0),
            ids=args.get("ids"),
            difficulty=args.get("difficulty"),
            success_command=args.get("success_command", ""),
        )
    except Exception as exc:
        return {"ok": False, "error": f"Import failed: {exc}"}

    imported = res["imported"]
    by_diff: dict[str, int] = {}
    for ev in imported:
        d = ev["cvdp"]["difficulty"]
        by_diff[d] = by_diff.get(d, 0) + 1
    needs_grading = sum(1 for e in imported if e["status"] == "needs_grading")
    say = f"imported {len(imported)} CVDP problem(s) ({', '.join(f'{k}: {v}' for k, v in sorted(by_diff.items())) or 'none'})."
    if needs_grading:
        say += (f" {needs_grading} are `needs_grading` — browsable and attemptable, but they don't "
                "count toward the score until they have a grading command (faithful CVDP grading "
                "needs Docker + the osvb reference container).")
    return {
        "ok": True,
        "imported": len(imported),
        "skipped": res["skipped"],
        "by_difficulty": by_diff,
        "needs_grading": needs_grading,
        "eval_ids": [e["eval_id"] for e in imported],
        "pet_says": _say(say),
    }


def tool_import_baseline(args: dict) -> dict:
    """Import already-graded CVDP verdicts as an external reference line.
    Free: reads a JSON file, runs no agent, pulls no image."""
    cwd = _cwd()
    _cfg()
    manifest = args.get("manifest") or os.environ.get("CVDP_BASELINE_MANIFEST", "")
    if not manifest:
        return {"ok": False,
                "error": "No manifest path. Pass `manifest`, or set CVDP_BASELINE_MANIFEST in the server env.",
                "pet_says": _say("point me at a FINAL_MANIFEST.json and I'll pull in its verdicts as a reference line.")}
    mp = Path(manifest).expanduser()
    if not mp.is_absolute():
        mp = (cwd / mp).resolve()
    try:
        res = ec.import_baseline(cwd, mp, label=args.get("label") or "SiliconCrew CVDP baseline")
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}

    if not res["runs"]:
        return {"ok": True, "imported": 0, "skipped": res["skipped"],
                "pet_says": _say("nothing new — that baseline is already imported.")}
    pct = 100 * res["passed"] / res["runs"]
    diffs = ", ".join(f"{d} {b['passed']}/{b['total']}" for d, b in res["by_difficulty"].items() if b["total"])
    return {
        "ok": True,
        "imported": res["runs"],
        "created_stubs": res["created"],
        "passed": res["passed"],
        "by_difficulty": res["by_difficulty"],
        "attribution": {"source": res["label"], "url": ec.SILICONCREW_URL, "repo": ec.SILICONCREW_REPO},
        "dashboard_url": _dashboard_link(cwd, _cfg()),
        "pet_says": _say(
            f"pulled in {res['runs']} reference verdicts — {res['passed']}/{res['runs']} PASS ({pct:.0f}%)"
            + (f" ({diffs})" if diffs else "") +
            f". That's {res['label']} ({ec.SILICONCREW_URL}), not your harness — it's a calibration line "
            "and won't move when you change your setup."
        ),
    }


def tool_set_harness(args: dict) -> dict:
    """Name the current harness state so progress is trackable as v1/v2/v3
    rather than a series of opaque hashes."""
    cwd = _cwd()
    cfg = _cfg()
    version = (args.get("version") or "").strip()
    if not version:
        return {"ok": False, "error": "version is required (e.g. 'v3')."}
    cfg["harness_version"] = version
    cfg["harness_note"] = args.get("note", "")
    hid = ec._harness_id(cwd, cfg)
    history = cfg.setdefault("harness_history", [])
    if not any(h["harness_id"] == hid and h["version"] == version for h in history):
        history.append({"version": version, "note": args.get("note", ""),
                        "harness_id": hid, "declared_at": ec.time.time()})
    ec.write_json(ec.eh_path(ec.CONFIG_FILE), cfg)
    return {
        "ok": True, "harness_version": version, "harness_id": hid,
        "pet_says": _say(f"harness is now {version} ({hid}). Runs from here on are stamped with it — "
                         "call `replay` to score the new version against the existing evals."),
    }


def tool_get_eval(args: dict) -> dict:
    eval_id = args.get("eval_id")
    evals = ec.read_evals()
    ev = next((e for e in evals if e["eval_id"] == eval_id), None)
    if ev is None:
        return {"ok": False, "error": f"No eval {eval_id}."}
    runs = [r for r in ec.read_jsonl(ec.eh_path(ec.RUNS_FILE)) if r["eval_id"] == eval_id]
    return {"ok": True, "eval": ev, "runs": runs}


def tool_feedback(args: dict) -> dict:
    text = args.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "text is required."}
    ec.append_jsonl(ec.eh_path(ec.FEEDBACK_FILE), {
        "target_type": "general", "target_id": None, "question": None,
        "answer": text, "timestamp": ec.time.time(),
    })
    return {"ok": True}


def tool_init(args: dict) -> dict:
    cwd = _cwd()
    cfg_path = ec.eh_path(ec.CONFIG_FILE)
    if cfg_path.exists() and not args.get("force"):
        return {"ok": False, "error": f"{cfg_path} already exists. Pass force=true to overwrite."}
    config = {
        "repo_name": args.get("repo_name") or cwd.name,
        "success_command_template": args.get("success_command", ""),
        "context_globs": args.get("context_globs", []),
        "model": args.get("model", ""),
        "tools": args.get("tools", []),
        "capture_backend": "mcp",
        "instance_axes": {},
    }
    ec.write_json(cfg_path, config)
    ec._ensure_gitignored(cwd)
    return {"ok": True, "config": config}


TOOLS = {
    "check": {
        "fn": tool_check,
        "description": (
            "Call this WITHOUT being asked, right after you (the agent) have just run and verified "
            "a real code change with the user's test/build/simulation command — this is how quality "
            "tracking stays up to date without the user having to remember to trigger it. Captures the "
            "change and, if it's real, proposes it as a pending eval for you to discuss with the user "
            "(do not auto-approve without telling them what it is). Every response includes a "
            f"`pet_says` field ({PET_ICON} {PET_NAME}, this tool's persona) — surface it to the user "
            "as a short spoken-aloud line (your own phrasing is fine, keep the tone: brief, friendly, "
            "not corporate) instead of just reporting raw JSON fields."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Verification command to run; omit to reuse the configured default."},
                "prompt": {"type": "string", "description": "The task the user asked for, in their words. Stored as input_prompt so this eval can later be re-attempted from scratch by an agent — pass it whenever you know it."},
            },
        },
    },
    "list_pending": {
        "fn": tool_list_pending,
        "description": "List evals awaiting a decision (proposed by `check` but not yet approved/rejected).",
        "schema": {"type": "object", "properties": {}},
    },
    "start_attempt": {
        "fn": tool_start_attempt,
        "description": (
            "THE REAL EVAL MODE. Checks out a clean worktree at the eval's ORIGINAL pre-fix commit and "
            "returns the task prompt for you to solve from scratch. Use this — not `replay` — when the "
            "question is 'is my system/harness actually good', because it makes the agent redo the work. "
            "(`replay` only re-runs the test against already-fixed code, which measures repo health, not "
            "harness quality.) Solve the task in the returned workspace, then call `grade_attempt`. "
            "Run it several times on the same eval to measure consistency / Pass@k."
        ),
        "schema": {"type": "object", "properties": {"eval_id": {"type": "string"}}, "required": ["eval_id"]},
    },
    "grade_attempt": {
        "fn": tool_grade_attempt,
        "description": "Grade an open attempt: runs the success command inside its worktree, records the verdict (mode=attempt), cleans up. Relay `pet_says` and the dashboard link.",
        "schema": {
            "type": "object",
            "properties": {
                "attempt_id": {"type": "string"},
                "keep_workspace": {"type": "boolean", "description": "Keep the worktree for debugging instead of removing it."},
            },
            "required": ["attempt_id"],
        },
    },
    "import_baseline": {
        "fn": tool_import_baseline,
        "description": (
            "Import already-graded CVDP verdicts from a bench-orchestrator FINAL_MANIFEST.json as an "
            "EXTERNAL reference baseline. Free and instant — reads a local JSON file, runs no agent, "
            "pulls no Docker image. Use this to give the user a real CVDP calibration number without "
            "spending their usage limits. Always make clear when relaying it that these verdicts came "
            "from another system's harness, not theirs, and so will not move when they change their setup."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "manifest": {"type": "string", "description": "Path to FINAL_MANIFEST.json. Falls back to the CVDP_BASELINE_MANIFEST env var."},
                "label": {"type": "string", "description": "Name for this reference harness (default: SiliconCrew CVDP baseline)."},
            },
        },
    },
    "import_cvdp": {
        "fn": tool_import_cvdp,
        "description": (
            "Import NVIDIA CVDP benchmark problems as eval definitions from a user-supplied dataset "
            "JSONL. Free and instant — no agent runs, no Docker, nothing downloaded. Good for giving a "
            "new repo an external calibration set on day one, alongside the user's own captured evals. "
            "Only call when the user asks for CVDP/a baseline/external benchmark; it is never automatic."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "description": "Path to the CVDP JSONL. Falls back to the CVDP_DATASET env var."},
                "limit": {"type": "integer", "description": "Max problems to import (0/omit = all matched)."},
                "ids": {"type": "array", "items": {"type": "string"}, "description": "Specific problem ids."},
                "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
                "success_command": {"type": "string", "description": "Grading command. Without it problems import as needs_grading and stay out of the score."},
            },
        },
    },
    "set_harness": {
        "fn": tool_set_harness,
        "description": (
            "Give the current harness state a name/version (e.g. v3, 'added cache-debug skill'). Call this "
            "when the user has meaningfully changed context files, skills, tools, or model — it makes the "
            "trend readable as v1→v2→v3 instead of opaque hashes."
        ),
        "schema": {
            "type": "object",
            "properties": {"version": {"type": "string"}, "note": {"type": "string"}},
            "required": ["version"],
        },
    },
    "decide": {
        "fn": tool_decide,
        "description": (
            "Approve or reject a pending eval, after checking with the user. Approving also runs it "
            "once and records the result. Relay `pet_says` — approving includes a `dashboard_url` "
            "(file:// link) to the local scoreboard; share it as a clickable link, don't just describe "
            "it. If another RTL/DV MCP server (e.g. SiliconCrew's rtl-codex) is connected, consider "
            "running its lint/formal tools first and passing the results via `supplementary_checks`."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "eval_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["approve", "reject"]},
                "tags": {"type": "array", "items": {"type": "string"}},
                "task_text": {"type": "string", "description": "Optional edited task description."},
                "success_command": {"type": "string", "description": "Optional edited success command."},
                "purpose": {"type": "string", "description": "Why this eval matters — one line, shown in the UI."},
                "input_prompt": {"type": "string", "description": "Task statement an agent must solve. Required before `start_attempt` will work."},
                "comment": {"type": "string", "description": "Free-text note from the user, appended to the eval's comment thread."},
                "type": {"type": "string", "enum": ["testbench", "rtl", "architecture", "synthesis", "other"],
                          "description": "Correct the inferred type if the heuristic got it wrong."},
                "supplementary_checks": {
                    "type": "object",
                    "description": "Optional results from other tools run before approving, e.g. {\"lint\": \"clean\", \"formal\": \"proved\"}. Merged with any already recorded.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["eval_id", "decision"],
        },
    },
    "replay": {
        "fn": tool_replay,
        "description": "Re-run every approved eval against the current tree. Call after a context/skill/tool/model change to see if quality moved. Relay `pet_says` and the `dashboard_url` link to the user.",
        "schema": {"type": "object", "properties": {}},
    },
    "score": {
        "fn": tool_score,
        "description": "Get the current quality score: overall pass rate + consistency, per-type and per-suite breakdown, harness trend. If the user asks how things are going / quality-related questions, call this and relay `pet_says` including the `dashboard_url` link.",
        "schema": {"type": "object", "properties": {}},
    },
    "get_eval": {
        "fn": tool_get_eval,
        "description": "Get full detail (diff, task, run history) for one eval.",
        "schema": {"type": "object", "properties": {"eval_id": {"type": "string"}}, "required": ["eval_id"]},
    },
    "feedback": {
        "fn": tool_feedback,
        "description": "Log free-text feedback about this tool itself (not about the codebase).",
        "schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    "init": {
        "fn": tool_init,
        "description": (
            "One-time setup for a repo that has no .evalharness/config.json yet. Ask the user for a "
            "success command, context file globs, model, and available tools first."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "success_command": {"type": "string"},
                "context_globs": {"type": "array", "items": {"type": "string"}},
                "model": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}},
                "repo_name": {"type": "string"},
                "force": {"type": "boolean"},
            },
        },
    },
}


# --------------------------------------------------------------- protocol

def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(msg_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": build_server_instructions(Path.cwd()),
        })
        return

    if method == "notifications/initialized" or method == "initialized":
        return  # notification, no response

    if method == "tools/list":
        _result(msg_id, {
            "tools": [
                {"name": name, "description": spec["description"], "inputSchema": spec["schema"]}
                for name, spec in TOOLS.items()
            ]
        })
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            _error(msg_id, -32601, f"Unknown tool: {name}")
            return
        try:
            output = spec["fn"](args)
        except Exception as exc:  # tool errors surface to the agent, not a protocol crash
            _result(msg_id, {
                "content": [{"type": "text", "text": f"Error: {exc}\n{traceback.format_exc()}"}],
                "isError": True,
            })
            return
        _result(msg_id, {"content": [{"type": "text", "text": json.dumps(output, indent=2)}]})
        return

    if method in ("resources/list", "prompts/list"):
        _result(msg_id, {"resources": []} if method == "resources/list" else {"prompts": []})
        return

    if method == "ping":
        _result(msg_id, {})
        return

    if msg_id is not None:
        _error(msg_id, -32601, f"Method not found: {method}")


def main() -> None:
    repo = os.environ.get("EVALHARNESS_REPO")
    if len(sys.argv) > 1:
        repo = sys.argv[1]
    if repo:
        os.chdir(repo)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(msg)


if __name__ == "__main__":
    main()
