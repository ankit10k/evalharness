#!/usr/bin/env python3
"""evalbench - minimal local eval harness for hardware coding agents.

Stdlib only. Runs entirely on local files under .evalbench/ inside
whatever repo you invoke it from. No server, no network calls, no
assumptions about which simulator/EDA tool/agent you're using - the
success command is always yours.
"""
from __future__ import annotations

import argparse
import glob as globmod
import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

EH_DIR = ".evalbench"
EH_DIR_LEGACY = ".evalharness"  # pre-rename name; still read so old data keeps working
CONFIG_FILE = "config.json"
CAPTURES_FILE = "captures.jsonl"
EVALS_FILE = "evals.json"
RUNS_FILE = "runs.jsonl"
FEEDBACK_FILE = "feedback.jsonl"


def eh_dir_name(cwd: Path | None = None) -> str:
    """Active state directory for this repo.

    Prefers `.evalbench/`, but keeps using an existing `.evalbench/` if the
    repo was set up before the rename. Nothing is moved automatically, so no
    one loses history by upgrading.
    """
    base = cwd or Path.cwd()
    if (base / EH_DIR).exists():
        return EH_DIR
    if (base / EH_DIR_LEGACY).exists():
        return EH_DIR_LEGACY
    return EH_DIR


def eh_path(*parts: str) -> Path:
    return Path.cwd() / eh_dir_name() / Path(*parts)


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def run_cmd(cmd: str, cwd: Path) -> tuple[int, str, float]:
    start = time.time()
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    duration = time.time() - start
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(output.splitlines()[-40:])
    return proc.returncode, tail, duration


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.stdout.strip()


def short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def strip_leading_separator(tokens: list[str]) -> list[str]:
    if tokens and tokens[0] == "--":
        return tokens[1:]
    return tokens


# ---------------------------------------------------------------- commands

def cmd_init(args) -> int:
    cwd = Path.cwd()
    cfg_path = eh_path(CONFIG_FILE)
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} already exists. Use --force to overwrite.")
        return 1
    repo_name = args.repo_name or cwd.name
    success_cmd = args.success_command or input(
        "Success command (e.g. `make regression`, `cd tb && iverilog ... && vvp ...`): "
    ).strip()
    globs_raw = args.context_globs
    if globs_raw is None:
        globs_raw = input(
            "Context/skill file globs, comma-separated (e.g. CLAUDE.md,skills/**) [blank = none]: "
        ).strip()
    model = args.model
    if model is None:
        model = input(
            "Model/agent identity (e.g. claude-sonnet-5, codex) [blank = unspecified]: "
        ).strip()
    tools_raw = args.tools
    if tools_raw is None:
        tools_raw = input(
            "Tools/MCP servers available to the agent, comma-separated [blank = unspecified]: "
        ).strip()
    config = {
        "repo_name": repo_name,
        "success_command_template": success_cmd,
        "context_globs": [g.strip() for g in globs_raw.split(",") if g.strip()],
        "model": model,
        "tools": [t.strip() for t in tools_raw.split(",") if t.strip()],
        "capture_backend": "generic_shell_wrapper",
        "instance_axes": {},
    }
    write_json(cfg_path, config)
    _ensure_gitignored(cwd)
    print(f"Initialized {cfg_path}")
    return 0


def _ensure_gitignored(cwd: Path) -> None:
    gitignore = cwd / ".gitignore"
    entry = f"{eh_dir_name(cwd)}/"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if entry in existing.splitlines():
        return
    with gitignore.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{entry}\n")


def load_config() -> dict:
    cfg = read_json(eh_path(CONFIG_FILE), None)
    if cfg is None:
        print("No .evalbench/config.json found. Run `evalbench init` first.", file=sys.stderr)
        sys.exit(1)
    return cfg


# Both state-dir names are noise: a repo may hold either, and neither is the
# user's actual work.
_NOISE_PATHS = (EH_DIR, EH_DIR_LEGACY, ".gitignore")


def _repo_changed(cwd: Path, post_diff: str) -> bool:
    """True if there's a real tracked-file diff, or untracked files other than
    evalbench's own scaffolding (.evalbench/, .gitignore), which always shows
    up in `git status` and isn't the user's actual work."""
    if post_diff:
        return True
    status_lines = git(cwd, "status", "--porcelain").splitlines()
    real_changes = [
        line for line in status_lines
        if not line.split(maxsplit=1)[-1].strip().startswith(_NOISE_PATHS)
    ]
    return bool(real_changes)


def _capture(cwd: Path, command: str) -> dict:
    pre_commit = git(cwd, "rev-parse", "HEAD")
    returncode, output_tail, duration = run_cmd(command, cwd)
    post_diff = git(cwd, "diff")
    return {
        "capture_id": short_id("cap"),
        "timestamp": time.time(),
        "command": command,
        "pre_commit": pre_commit,
        "exit_code": returncode,
        "duration_sec": round(duration, 2),
        "output_tail": output_tail,
        "diff": post_diff,
        "diff_present": _repo_changed(cwd, post_diff),
    }


def cmd_wrap(args) -> int:
    cwd = Path.cwd()
    load_config()
    command = args.cmd
    if not command:
        print('Usage: evalbench wrap "<command>"  (quote it as one argument if it uses &&, |, etc.)', file=sys.stderr)
        return 1
    capture = _capture(cwd, command)
    append_jsonl(eh_path(CAPTURES_FILE), capture)
    verdict = "PASS" if capture["exit_code"] == 0 else "FAIL"
    print(f"[wrap] {verdict} in {capture['duration_sec']:.1f}s - capture {capture['capture_id']} recorded.")
    return 0


def _commit_subject(cwd: Path, commit: str) -> str:
    if not commit:
        return ""
    return git(cwd, "log", "-1", "--format=%s", commit)


# Eval type taxonomy. Order matters - first match wins per file, so the more
# specific patterns (testbench, synthesis) must precede the generic RTL
# extensions, otherwise a file like tb/foo_tb.sv would be typed as "rtl".
_TYPE_RULES = (
    ("testbench", ("tb/", "_tb.", "test", "verif", "cocotb", "_test.")),
    ("synthesis", (".sdc", "synth", "orfs", "openroad", "config.mk", ".lib", ".lef", "pdk")),
    ("architecture", ("spec/", "docs/", ".md", ".yaml", ".yml", "arch")),
    ("rtl", (".sv", ".v", ".vhd", "rtl/")),
)

EVAL_TYPES = ("testbench", "rtl", "architecture", "synthesis", "other")


def _diffstat(diff_text: str) -> dict:
    """Cheap diffstat from unified diff text: touched files + net +/- lines.
    No shelling out to `git diff --stat` since we already have the text."""
    files: list[str] = []
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/"):
            # "diff --git a/path b/path"
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1])
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"files": files, "added": added, "removed": removed}


def _infer_type(files: list[str]) -> str:
    """Best-effort type from touched file paths. A heuristic, not judgment -
    the agent/user can override it at decide time."""
    if not files:
        return "other"
    votes: dict[str, int] = {}
    for f in files:
        low = f.lower()
        for type_name, needles in _TYPE_RULES:
            if any(n in low for n in needles):
                votes[type_name] = votes.get(type_name, 0) + 1
                break
        else:
            votes["other"] = votes.get("other", 0) + 1
    return max(votes, key=votes.get)


_MAX_DIFF_CHARS = 20_000  # ~a few hundred lines - caps storage growth and, more importantly,
                          # what lands in an agent's context window when it reads an eval back


def _cap_diff(diff_text: str) -> str:
    if len(diff_text) <= _MAX_DIFF_CHARS:
        return diff_text
    kept = diff_text[:_MAX_DIFF_CHARS]
    dropped = len(diff_text) - _MAX_DIFF_CHARS
    return f"{kept}\n\n... [truncated, {dropped} more chars - this diff was unusually large, worth checking why]"


def _code_links(cwd: Path, files: list[str]) -> list[dict]:
    """file:// references to the touched files, rather than embedding their
    contents. CVDP has to embed context because it ships as a standalone
    dataset; we have the actual repo on disk, so a reference is both smaller
    and always current."""
    return [{"path": f, "url": f"file://{cwd / f}"} for f in files]


def _propose_from_capture(cwd: Path, cfg: dict, capture: dict) -> dict:
    subject = _commit_subject(cwd, capture["pre_commit"]) or "(no commit message)"
    success_command = cfg.get("success_command_template") or capture["command"]
    stat = _diffstat(capture["diff"])
    eval_type = _infer_type(stat["files"])
    files_summary = ", ".join(stat["files"][:4]) + (" ..." if len(stat["files"]) > 4 else "")
    return {
        "eval_id": short_id("eval"),
        "capture_id": capture["capture_id"],
        "suite": "local",
        "starting_commit": capture["pre_commit"],
        "type": eval_type,
        "summary": f"{subject} ({files_summary or 'no files touched'})",
        "purpose": "",            # why this eval matters - user-supplied at decide time
        "input_prompt": capture.get("input_prompt", ""),  # the task an agent must solve
        "task_text": f"Reproduce/extend work from: {subject}\n\nCommand run: {capture['command']}",
        "success_command": success_command,
        "expected_diff": _cap_diff(capture["diff"]),
        "diffstat": stat,
        "code_links": _code_links(cwd, stat["files"]),
        "comments": [],
        "tags": [],
        "status": "pending_review",
        "created_at": time.time(),
    }


def migrate_eval(ev: dict) -> dict:
    """Bring a pre-schema-change record up to the current shape. Old records
    used `category`; new ones use `type` with a different taxonomy. Called on
    read so existing data keeps working without a migration script."""
    if "type" not in ev:
        legacy = ev.get("category", "other")
        ev["type"] = {"verification": "testbench", "spec": "architecture"}.get(legacy, legacy)
    ev.setdefault("suite", "local")
    ev.setdefault("purpose", "")
    ev.setdefault("input_prompt", "")
    ev.setdefault("comments", [])
    ev.setdefault("code_links", [])
    ev.setdefault("tags", [])
    return ev


def read_evals() -> list[dict]:
    return [migrate_eval(e) for e in read_json(eh_path(EVALS_FILE), [])]


def _eval_summary(ev: dict) -> str:
    """One-line label. Guards against empty summary/task_text - reference stubs
    imported from a manifest have no task text at all."""
    for candidate in (ev.get("summary"), ev.get("task_text")):
        if candidate:
            first = candidate.splitlines()
            if first and first[0].strip():
                return first[0]
    return ev["eval_id"]


def _nudge_for(cwd: Path, ev: dict) -> str | None:
    """Local, deterministic pattern-spotting - no LLM call. Looks at prior
    evals for signals worth flagging to the person reviewing: a file that
    keeps coming back, or a fresh FAIL streak on this category."""
    files = set(ev.get("diffstat", {}).get("files", []))
    if not files:
        return None
    prior = [e for e in read_evals() if e["eval_id"] != ev["eval_id"]]
    hits = [e for e in prior if set(e.get("diffstat", {}).get("files", [])) & files]
    if len(hits) >= 2:
        touched = files & {f for e in hits for f in e.get("diffstat", {}).get("files", [])}
        return (
            f"note: {', '.join(sorted(touched))} has come up in {len(hits)} prior eval(s) "
            f"({', '.join(h['eval_id'] for h in hits[-3:])}) - might be worth a tag or a skill doc "
            "if this is a recurring pattern, not a one-off."
        )
    return None


def cmd_propose(args) -> int:
    cwd = Path.cwd()
    cfg = load_config()
    captures = read_jsonl(eh_path(CAPTURES_FILE))
    if not captures:
        print('No captures found. Run `evalbench wrap "<command>"` first.', file=sys.stderr)
        return 1
    capture = captures[-1]
    if args.capture_id:
        capture = next((c for c in captures if c["capture_id"] == args.capture_id), None)
        if capture is None:
            print(f"Capture {args.capture_id} not found.", file=sys.stderr)
            return 1
    eval_record = _propose_from_capture(cwd, cfg, capture)
    evals = read_evals()
    evals.append(eval_record)
    write_json(eh_path(EVALS_FILE), evals)
    print(f"Proposed {eval_record['eval_id']} - review with `evalbench review`.")
    return 0


def _review_one(ev: dict, nudge: str | None = None) -> None:
    stat = ev.get("diffstat", {"files": [], "added": 0, "removed": 0})
    files = ", ".join(stat["files"]) or "(no files)"
    print("\n" + "-" * 60)
    print(f"{ev['eval_id']}  [{ev.get('type', 'other')}]  {_eval_summary(ev)}")
    print(f"  {files}  (+{stat['added']}/-{stat['removed']})  commit {ev['starting_commit'][:10]}")
    print(f"  success: {ev['success_command']}")
    if ev.get("purpose"):
        print(f"  purpose: {ev['purpose']}")
    if ev.get("input_prompt"):
        print(f"  prompt: {ev['input_prompt'][:100]}")
    else:
        print("  \033[33mprompt: (none - needed before this eval can be attempted)\033[0m")
    if ev.get("tags"):
        print(f"  tags: {', '.join(ev['tags'])}")
    if ev.get("comments"):
        print(f"  comments: {len(ev['comments'])}")
    if nudge:
        print(f"  \033[33m{nudge}\033[0m")
    while True:
        choice = input(
            "  [a]pprove / [e]dit / [r]eject / [t]ag / [p]rompt / [c]omment / [y]pe / [q]uestion / [d]iff / [s]kip? "
        ).strip().lower()
        if choice == "d":
            print(ev["expected_diff"])
            continue
        if choice == "t":
            new_tags = input("  Tags to add, comma-separated: ").strip()
            added = [t.strip() for t in new_tags.split(",") if t.strip()]
            ev["tags"] = sorted(set(ev.get("tags", [])) | set(added))
            print(f"  tags now: {', '.join(ev['tags'])}")
            continue
        if choice == "p":
            prompt = input("  Task prompt (what an agent must accomplish): ").strip()
            if prompt:
                ev["input_prompt"] = prompt
            continue
        if choice == "c":
            text = input("  Comment: ").strip()
            if text:
                ev.setdefault("comments", []).append({"text": text, "timestamp": time.time()})
                print(f"  {len(ev['comments'])} comment(s)")
            continue
        if choice == "y":
            print(f"  types: {', '.join(EVAL_TYPES)}")
            new_type = input(f"  Type [{ev.get('type', 'other')}]: ").strip().lower()
            if new_type in EVAL_TYPES:
                ev["type"] = new_type
            elif new_type:
                print(f"  '{new_type}' is not a known type - keeping {ev.get('type')}")
            continue
        if choice == "a":
            ev["status"] = "approved"
            return
        elif choice == "e":
            new_purpose = input("Purpose - why this eval matters (blank = keep): ").strip()
            if new_purpose:
                ev["purpose"] = new_purpose
            new_task = input("New task_text (blank = keep): ").strip()
            if new_task:
                ev["task_text"] = new_task
            new_cmd = input("New success_command (blank = keep): ").strip()
            if new_cmd:
                ev["success_command"] = new_cmd
            ev["status"] = "approved"
            return
        elif choice == "r":
            ev["status"] = "rejected"
            return
        elif choice == "q":
            question = input("Question for the expert: ").strip()
            answer = input("Answer: ").strip()
            append_jsonl(eh_path(FEEDBACK_FILE), {
                "target_type": "eval",
                "target_id": ev["eval_id"],
                "question": question,
                "answer": answer,
                "timestamp": time.time(),
            })
            print("Recorded. Decide approve/reject now that you've answered:")
            continue
        else:
            return  # leaves status as pending_review - shows up again next time


def cmd_review(args) -> int:
    evals = read_evals()
    pending = [e for e in evals if e["status"] == "pending_review"]
    if not pending:
        print("Nothing pending review.")
        return 0
    cwd = Path.cwd()
    for ev in pending:
        _review_one(ev, nudge=_nudge_for(cwd, ev))
    write_json(eh_path(EVALS_FILE), evals)
    return 0


def _harness_id(cwd: Path, cfg: dict) -> str:
    """Identity of 'the system': context/skill files + declared model + declared
    tools. Two runs only count as the same harness if all three match - a tool
    or model change is as much a harness change as editing CLAUDE.md."""
    h = hashlib.sha256()
    globs = cfg.get("context_globs", [])
    files: set[str] = set()
    for pattern in globs:
        for f in sorted(globmod.glob(str(cwd / pattern), recursive=True)):
            files.add(f)
    for f in sorted(files):
        p = Path(f)
        if p.is_file():
            h.update(p.read_bytes())
    if not globs:
        h.update(b"no-context-globs-configured")
    h.update(b"|model:" + (cfg.get("model") or "unspecified").encode())
    h.update(b"|tools:" + ",".join(sorted(cfg.get("tools") or [])).encode())
    return h.hexdigest()[:12]


def _harness_label(cfg: dict) -> str:
    """Human-facing name for the current harness. Prefers the user's declared
    version + note; falls back to the raw model/tools description. A content
    hash alone can't express 'v3 beat v2' - ordering and intent have to be
    declared, not derived."""
    version = cfg.get("harness_version") or ""
    note = cfg.get("harness_note") or ""
    if version:
        return f"{version} - {note}" if note else version
    model = cfg.get("model") or "unspecified"
    tools = ",".join(cfg.get("tools") or []) or "unspecified"
    return f"model={model} tools={tools}"


def cmd_harness(args) -> int:
    """Name the current harness state, e.g. `evalbench harness v3 "added cache-debug skill"`.
    The content hash still detects silent drift; this gives it an ordered, human name."""
    cwd = Path.cwd()
    cfg = load_config()
    if not args.version:
        print(f"Current harness: {_harness_label(cfg)}  ({_harness_id(cwd, cfg)})")
        history = cfg.get("harness_history", [])
        if history:
            print("\nHistory:")
            for h in history:
                print(f"  {h['version']:8s} {h['harness_id']}  {h.get('note', '')}")
        return 0

    cfg["harness_version"] = args.version
    cfg["harness_note"] = args.note or ""
    hid = _harness_id(cwd, cfg)
    history = cfg.setdefault("harness_history", [])
    if not any(h["harness_id"] == hid and h["version"] == args.version for h in history):
        history.append({
            "version": args.version,
            "note": args.note or "",
            "harness_id": hid,
            "declared_at": time.time(),
        })
    write_json(eh_path(CONFIG_FILE), cfg)
    print(f"Harness is now {args.version} ({hid})" + (f" - {args.note}" if args.note else ""))
    return 0


def _execute_eval(cwd: Path, cfg: dict, ev: dict, commit: str | None) -> dict:
    """Run ev's success_command (against `commit` in a throwaway worktree if given,
    otherwise the current tree) and return a run record. Does not append it."""
    target_cwd = cwd
    worktree_dir = None
    if commit:
        worktree_dir = eh_path("worktrees", ev["eval_id"])
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=cwd, capture_output=True)
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_dir), commit],
            cwd=cwd, capture_output=True, text=True,
        )
        if add.returncode != 0:
            raise RuntimeError(f"Failed to create worktree at {commit}:\n{add.stderr}")
        target_cwd = worktree_dir

    returncode, output_tail, duration = run_cmd(ev["success_command"], target_cwd)

    if worktree_dir is not None:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=cwd, capture_output=True)

    verdict = "PASS" if returncode == 0 else "FAIL"
    return {
        "run_id": short_id("run"),
        "eval_id": ev["eval_id"],
        "harness_id": _harness_id(cwd, cfg),
        "harness_label": _harness_label(cfg),
        "harness_version": cfg.get("harness_version", ""),
        "commit": commit or git(cwd, "rev-parse", "HEAD"),
        "verdict": verdict,
        "mode": "regression",  # ran the test only; see start_attempt for real eval mode
        "duration_sec": round(duration, 2),
        "output_tail": output_tail,
        "timestamp": time.time(),
    }


# --------------------------------------------------------------- attempts
#
# The difference between a REGRESSION and an EVAL:
#
#   regression (_execute_eval): run success_command against the current tree.
#     Answers "is the repo still green". Changing CLAUDE.md cannot affect this
#     - the fix is already committed - so it says nothing about harness quality.
#
#   attempt (below): check out starting_commit into a worktree (the fix is NOT
#     there), hand the agent input_prompt, let it do the work, THEN grade.
#     Answers "can my system solve this task", which is what actually moves
#     when the harness changes. Same shape as CVDP: (prompt, context, harness).
#
# Semiflow EvalBench makes no LLM calls, so it can't drive the agent itself. It sets
# the stage (start_attempt) and grades (grade_attempt); the calling agent does
# the work in between - same split as check/decide.

ATTEMPTS_FILE = "attempts.json"


def _attempt_dir(attempt_id: str) -> Path:
    return eh_path("attempts", attempt_id)


def start_attempt(cwd: Path, cfg: dict, ev: dict) -> dict:
    """Set up an isolated pre-fix workspace and return the task for an agent to
    solve. Nothing is graded until grade_attempt.

    Two source kinds, because not every eval comes from a git repo:
      - "git"  (local evals): worktree detached at starting_commit
      - "files" (imported CVDP problems): copy of a materialized context dir,
        since CVDP ships file dictionaries, not commits
    """
    if not ev.get("input_prompt"):
        raise RuntimeError(
            f"{ev['eval_id']} has no input_prompt - an attempt needs a task statement to give "
            "the agent. Set one via `decide`/`review` (or the MCP `decide` tool) first."
        )
    attempt_id = short_id("att")
    wt = _attempt_dir(attempt_id)
    wt.parent.mkdir(parents=True, exist_ok=True)
    source = ev.get("source", "git")

    if source == "files":
        src = Path(ev["source_path"])
        if not src.is_absolute():
            src = cwd / src
        if not src.exists():
            raise RuntimeError(
                f"{ev['eval_id']}'s context snapshot is missing at {src} - re-import it."
            )
        shutil.copytree(src, wt)
    else:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt), ev["starting_commit"]],
            cwd=cwd, capture_output=True, text=True,
        )
        if add.returncode != 0:
            raise RuntimeError(f"Failed to create worktree at {ev['starting_commit']}:\n{add.stderr}")

    attempt = {
        "source": source,
        "attempt_id": attempt_id,
        "eval_id": ev["eval_id"],
        "workspace": str(wt),
        "starting_commit": ev.get("starting_commit", ""),
        "source_path": ev.get("source_path", ""),
        "input_prompt": ev["input_prompt"],
        "success_command": ev["success_command"],
        "harness_id": _harness_id(cwd, cfg),
        "harness_label": _harness_label(cfg),
        "harness_version": cfg.get("harness_version", ""),
        "status": "open",
        "started_at": time.time(),
    }
    attempts = read_json(eh_path(ATTEMPTS_FILE), [])
    attempts.append(attempt)
    write_json(eh_path(ATTEMPTS_FILE), attempts)
    return attempt


def _workspace_diff(att: dict, wt: Path) -> str:
    """What the agent actually changed. Git-backed attempts use `git diff`;
    file-snapshot attempts (CVDP) diff against the pristine source dir with
    `git diff --no-index`, which works outside a repo."""
    if att.get("source") == "files":
        src = att.get("source_path")
        if not src:
            return ""
        proc = subprocess.run(
            ["git", "diff", "--no-index", "--", str(src), str(wt)],
            capture_output=True, text=True,
        )
        return proc.stdout  # exit code 1 just means "differences found"
    return git(wt, "diff")


def grade_attempt(cwd: Path, cfg: dict, attempt_id: str, keep_workspace: bool = False) -> dict:
    """Run the success command inside the attempt's worktree, record a run
    stamped mode=attempt, and clean up."""
    attempts = read_json(eh_path(ATTEMPTS_FILE), [])
    att = next((a for a in attempts if a["attempt_id"] == attempt_id), None)
    if att is None:
        raise RuntimeError(f"No attempt {attempt_id}.")
    if att["status"] != "open":
        raise RuntimeError(f"Attempt {attempt_id} is already {att['status']}.")

    wt = Path(att["workspace"])
    if not wt.exists():
        raise RuntimeError(f"Attempt workspace {wt} is gone - cannot grade.")

    if not att["success_command"]:
        raise RuntimeError(
            f"{att['eval_id']} has no success_command - nothing to grade against. "
            "Imported CVDP problems need a grading command (see `evalbench import-cvdp` notes on "
            "Docker/osvb grading) or a locally-defined one before they can be scored."
        )

    returncode, output_tail, duration = run_cmd(att["success_command"], wt)
    verdict = "PASS" if returncode == 0 else "FAIL"
    solution_diff = _workspace_diff(att, wt)

    run_record = {
        "run_id": short_id("run"),
        "eval_id": att["eval_id"],
        "attempt_id": attempt_id,
        "harness_id": att["harness_id"],
        "harness_label": att["harness_label"],
        "harness_version": att.get("harness_version", ""),
        "commit": att.get("starting_commit", ""),
        "verdict": verdict,
        "mode": "attempt",  # the agent actually did the work - this measures the harness
        "duration_sec": round(duration, 2),
        "output_tail": output_tail,
        "solution_diff": _cap_diff(solution_diff),
        "timestamp": time.time(),
    }
    append_jsonl(eh_path(RUNS_FILE), run_record)

    att["status"] = "graded"
    att["verdict"] = verdict
    att["graded_at"] = time.time()
    write_json(eh_path(ATTEMPTS_FILE), attempts)

    if not keep_workspace:
        if att.get("source") == "files":
            shutil.rmtree(wt, ignore_errors=True)
        else:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=cwd, capture_output=True)
    return run_record


# ------------------------------------------------------------ CVDP import
#
# Imports NVIDIA's CVDP benchmark problems as eval DEFINITIONS. Ships no data:
# you supply the dataset JSONL (same posture as SiliconCrew's cvdp-pipeline,
# which states "contains no CVDP data - reads a dataset JSONL you supply").
# Cost of this step is zero - no agent runs, no Docker. Grading imported
# problems faithfully needs the reference container (see README, Tier 2).
#
# CVDP row schema (verified against the HF datasets-server):
#   id, categories[], system_message, prompt, context{path:content},
#   patch{path:content}, harness{path:content}

CVDP_DIR = "cvdp"

# Attribution for the reference baseline imported from SiliconCrew's runs.
SILICONCREW_URL = "https://siliconcrew-frontend-psp2dkllmq-uc.a.run.app/"
SILICONCREW_REPO = "https://github.com/naman-ranka/siliconcrew"
REFERENCE_HARNESS_ID = "siliconcrew-ref"


def _cvdp_eval_id(problem_id: str) -> str:
    """Normalize a CVDP problem id to a stable eval_id.

    Both sources must agree so a dataset import and a baseline import land on
    the SAME eval - that's what lets you see 'SiliconCrew PASSed this, my
    harness FAILed it' side by side instead of two disconnected records.
      dataset:  cvdp_agentic_AES_encryption_decryption_0003
      manifest: AES_encryption_decryption_0003
      both ->   cvdp_aes_encryption_decryption_0003
    """
    pid = problem_id.strip()
    for prefix in ("cvdp_agentic_", "cvdp_"):
        if pid.lower().startswith(prefix):
            pid = pid[len(prefix):]
            break
    return f"cvdp_{pid.lower()}"


def _cvdp_iter(dataset: Path):
    with dataset.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            yield row.get("row", row)  # tolerate {"row": {...}} wrapping


def _cvdp_difficulty(categories: list) -> str:
    for c in categories or []:
        if c in ("easy", "medium", "hard"):
            return c
    return "unknown"


def _materialize_cvdp(eval_id: str, files: dict) -> Path:
    base = eh_path(CVDP_DIR, eval_id)
    if base.exists():
        shutil.rmtree(base)
    for relpath, content in (files or {}).items():
        p = base / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    base.mkdir(parents=True, exist_ok=True)
    return base


def import_cvdp(cwd: Path, dataset: Path, limit: int = 0, ids: list | None = None,
                difficulty: str | None = None, success_command: str = "") -> dict:
    existing = read_evals()
    existing_ids = {e["eval_id"] for e in existing}
    imported, skipped = [], 0

    for row in _cvdp_iter(dataset):
        pid = row.get("id")
        if not pid:
            continue
        cats = row.get("categories") or []
        if ids and pid not in ids and pid.replace("cvdp_agentic_", "") not in ids:
            continue
        if difficulty and _cvdp_difficulty(cats) != difficulty:
            continue

        eval_id = _cvdp_eval_id(pid)
        if eval_id in existing_ids:
            skipped += 1
            continue

        context = row.get("context") or {}
        patch = row.get("patch") or {}
        snapshot = _materialize_cvdp(eval_id, context)
        targets = sorted(patch.keys())

        ev = {
            "eval_id": eval_id,
            "suite": "cvdp",
            "source": "files",
            "source_path": str(snapshot.relative_to(cwd)) if snapshot.is_relative_to(cwd) else str(snapshot),
            "starting_commit": "",
            "type": _infer_type(targets) if targets else _infer_type(list(context.keys())),
            "summary": f"{pid} ({_cvdp_difficulty(cats)})",
            "purpose": f"CVDP benchmark problem {pid} - external calibration point, not homegrown.",
            "input_prompt": row.get("prompt", ""),
            "task_text": row.get("prompt", ""),
            "system_message": row.get("system_message", ""),
            "success_command": success_command,
            "expected_diff": "",
            "diffstat": {"files": targets, "added": 0, "removed": 0},
            "code_links": [{"path": p, "url": f"file://{snapshot / p}"} for p in sorted(context.keys())],
            "patch_targets": targets,
            "cvdp": {
                "problem_id": pid,
                "categories": cats,
                "difficulty": _cvdp_difficulty(cats),
                "has_harness": bool(row.get("harness")),
            },
            "comments": [],
            "tags": ["cvdp", _cvdp_difficulty(cats)],
            # Without a grading command these are browsable/attemptable but not
            # scorable - keep them out of the score rather than faking a verdict.
            "status": "approved" if success_command else "needs_grading",
            "created_at": time.time(),
        }
        imported.append(ev)
        existing_ids.add(eval_id)
        if limit and len(imported) >= limit:
            break

    if imported:
        write_json(eh_path(EVALS_FILE), existing + imported)
    return {"imported": imported, "skipped": skipped}


def import_baseline(cwd: Path, manifest: Path, label: str = "SiliconCrew CVDP baseline") -> dict:
    """Import already-graded CVDP verdicts from a bench-orchestrator
    FINAL_MANIFEST.json as a REFERENCE baseline.

    These runs were produced by someone else's harness (SiliconCrew's agent +
    model + MCP tools) and graded in the official CVDP reference container.
    They're stamped with a fixed synthetic harness id so they can never be
    mistaken for - or averaged into - a measurement of YOUR system. They give
    you an external calibration line at zero cost and zero agent runs.
    """
    records = read_json(manifest, None)
    if records is None:
        raise RuntimeError(f"Manifest not found: {manifest}")
    if not isinstance(records, list):
        raise RuntimeError("Manifest must be a JSON list of run records.")

    evals = read_evals()
    by_id = {e["eval_id"]: e for e in evals}
    existing_runs = read_jsonl(eh_path(RUNS_FILE))
    already = {r["eval_id"] for r in existing_runs if r.get("harness_id") == REFERENCE_HARNESS_ID}

    created, run_rows, skipped = 0, [], 0
    for rec in records:
        pid = rec.get("problem")
        verdict = rec.get("verdict")
        if not pid or verdict not in ("PASS", "FAIL"):
            continue
        eval_id = _cvdp_eval_id(pid)
        if eval_id in already:
            skipped += 1
            continue

        ev = by_id.get(eval_id)
        if ev is None:
            # No dataset import yet - create a reference-only stub. It has no
            # prompt or context, so it is deliberately NOT attemptable; import
            # the real CVDP dataset to make it runnable.
            ev = {
                "eval_id": eval_id,
                "suite": "cvdp",
                "source": "reference",
                "reference": True,
                "starting_commit": "",
                "type": "other",
                "summary": f"{pid} ({rec.get('difficulty', 'unknown')})",
                "purpose": (
                    f"CVDP problem {pid} - external reference verdict from {label}. "
                    "Calibration only; import the CVDP dataset to attempt it yourself."
                ),
                "input_prompt": "",
                "task_text": "",
                "success_command": "",
                "expected_diff": "",
                "diffstat": {"files": [], "added": 0, "removed": 0},
                "code_links": [],
                "comments": [],
                "tags": ["cvdp", "reference", rec.get("difficulty", "unknown")],
                "cvdp": {
                    "problem_id": pid,
                    "categories": [c for c in [rec.get("cid"), rec.get("difficulty")] if c],
                    "difficulty": rec.get("difficulty", "unknown"),
                    "has_harness": True,
                },
                "attribution": {
                    "source": label,
                    "url": SILICONCREW_URL,
                    "repo": SILICONCREW_REPO,
                    "note": "Graded in the official CVDP reference container (ghcr.io/hdl/sim/osvb).",
                },
                "status": "approved",
                "created_at": time.time(),
            }
            evals.append(ev)
            by_id[eval_id] = ev
            created += 1
        else:
            ev.setdefault("attribution", {
                "source": label, "url": SILICONCREW_URL, "repo": SILICONCREW_REPO,
            })

        run_rows.append({
            "run_id": short_id("run"),
            "eval_id": eval_id,
            "harness_id": REFERENCE_HARNESS_ID,
            "harness_label": label,
            "harness_version": "siliconcrew-baseline",
            "reference": True,
            "commit": "",
            "verdict": verdict,
            # SiliconCrew's agent solved each problem from scratch and it was
            # container-graded - that is a genuine attempt, not a regression.
            "mode": "attempt",
            "duration_sec": round((rec.get("duration_ms") or 0) / 1000, 2),
            "output_tail": "",
            "timestamp": time.time(),
            "source_run_dir": rec.get("source_run_dir", ""),
        })

    if run_rows:
        write_json(eh_path(EVALS_FILE), evals)
        for r in run_rows:
            append_jsonl(eh_path(RUNS_FILE), r)

    passed = sum(1 for r in run_rows if r["verdict"] == "PASS")
    by_diff: dict[str, dict] = {}
    for rec in records:
        eid = _cvdp_eval_id(rec.get("problem", ""))
        if not any(r["eval_id"] == eid for r in run_rows):
            continue
        d = rec.get("difficulty", "unknown")
        b = by_diff.setdefault(d, {"passed": 0, "total": 0})
        b["total"] += 1
        if rec.get("verdict") == "PASS":
            b["passed"] += 1

    return {"created": created, "runs": len(run_rows), "skipped": skipped,
            "passed": passed, "by_difficulty": by_diff, "label": label}


def cmd_import_baseline(args) -> int:
    cwd = Path.cwd()
    load_config()
    manifest = Path(args.manifest).expanduser()
    if not manifest.is_absolute():
        manifest = (cwd / manifest).resolve()
    try:
        res = import_baseline(cwd, manifest, label=args.label)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not res["runs"]:
        print(f"Nothing new to import (skipped {res['skipped']} already present).")
        return 0
    pct = 100 * res["passed"] / res["runs"]
    print(f"Imported {res['runs']} reference verdict(s) - {res['passed']}/{res['runs']} PASS ({pct:.0f}%)")
    print(f"  {res['created']} new eval stub(s) created" + (f", {res['skipped']} skipped" if res["skipped"] else ""))
    for d in ("easy", "medium", "hard", "unknown"):
        b = res["by_difficulty"].get(d)
        if b:
            print(f"  {d:8s} {b['passed']}/{b['total']} ({100*b['passed']/b['total']:.0f}%)")
    print(f"\nSource: {res['label']} - {SILICONCREW_URL}")
    print("These are an EXTERNAL reference: produced by SiliconCrew's harness, not yours.")
    print("They give you a calibration line; they will not move when you change your own harness.")
    return 0


def cmd_import_cvdp(args) -> int:
    cwd = Path.cwd()
    load_config()
    dataset = Path(args.dataset).expanduser()
    if not dataset.is_absolute():
        dataset = (cwd / dataset).resolve()
    if not dataset.exists():
        print(f"Dataset not found: {dataset}\n\n"
              "evalbench ships no CVDP data - obtain the JSONL from NVIDIA's CVDP benchmark\n"
              "distribution and pass its path with --dataset.", file=sys.stderr)
        return 1

    res = import_cvdp(
        cwd, dataset,
        limit=args.limit or 0,
        ids=[i.strip() for i in args.ids.split(",")] if args.ids else None,
        difficulty=args.difficulty,
        success_command=args.success_command or "",
    )
    n = len(res["imported"])
    print(f"Imported {n} CVDP problem(s)" + (f", skipped {res['skipped']} already present" if res["skipped"] else ""))
    by_diff: dict[str, int] = {}
    for ev in res["imported"]:
        by_diff[ev["cvdp"]["difficulty"]] = by_diff.get(ev["cvdp"]["difficulty"], 0) + 1
    if by_diff:
        print("  " + "  ".join(f"{k}: {v}" for k, v in sorted(by_diff.items())))
    if n and not args.success_command:
        print("\nThese are imported as `needs_grading` - they have prompts and context you can\n"
              "browse and attempt, but no grading command, so they do not count toward your score.\n"
              "A faithful CVDP verdict needs the reference container (Docker + ghcr.io/hdl/sim/osvb).\n"
              "To score them locally with your own command, re-run with --success-command.")
    return 0


def cmd_attempt(args) -> int:
    cwd = Path.cwd()
    cfg = load_config()
    ev = next((e for e in read_evals() if e["eval_id"] == args.eval_id), None)
    if ev is None:
        print(f"eval {args.eval_id} not found.", file=sys.stderr)
        return 1
    try:
        att = start_attempt(cwd, cfg, ev)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Attempt {att['attempt_id']} open.")
    print(f"  workspace: {att['workspace']}")
    print(f"  task: {att['input_prompt']}")
    print(f"\nDo the work in that workspace, then: evalbench grade {att['attempt_id']}")
    return 0


def cmd_grade(args) -> int:
    cwd = Path.cwd()
    cfg = load_config()
    try:
        rec = grade_attempt(cwd, cfg, args.attempt_id, keep_workspace=args.keep)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"[attempt] {rec['eval_id']} -> {rec['verdict']} ({rec['duration_sec']:.1f}s)")
    if rec["verdict"] == "FAIL":
        print(rec["output_tail"])
    return 0 if rec["verdict"] == "PASS" else 1


def cmd_run(args) -> int:
    cwd = Path.cwd()
    cfg = load_config()
    evals = read_evals()
    ev = next((e for e in evals if e["eval_id"] == args.eval_id), None)
    if ev is None:
        print(f"eval {args.eval_id} not found.", file=sys.stderr)
        return 1
    if ev["status"] != "approved":
        print(f"eval {args.eval_id} is not approved (status={ev['status']}). Approve it via `evalbench review` first.")
        return 1
    try:
        run_record = _execute_eval(cwd, cfg, ev, args.commit)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    append_jsonl(eh_path(RUNS_FILE), run_record)
    print(f"[run] {ev['eval_id']} -> {run_record['verdict']} ({run_record['duration_sec']:.1f}s) harness={run_record['harness_id']}")
    if run_record["verdict"] == "FAIL":
        print(run_record["output_tail"])
    return 0 if run_record["verdict"] == "PASS" else 1


MIN_RUNS_FOR_CONSISTENCY = 2  # below this, "100%" off a single run is noise, not signal
_SCORE_ROW_LIMIT = 15         # terminal listing cap; the dashboard shows everything


def _suite_stats(runs: list[dict], evals_by_id: dict, current_harness: str) -> dict:
    """Score + consistency for one suite's runs.

    Two distinct numbers, deliberately not conflated:
      - pass rate: latest verdict per eval - "where do I stand right now"
      - consistency: passed/total across an eval's FULL run history - "does
        this eval behave the same way every time". For CVDP (N samples in one
        pass) this is literally Pass@k; for local evals (runs accumulated
        across weeks of harness changes) it's stability over time. Same
        formula, because the run records have the same shape.
    """
    latest_by_eval: dict[str, dict] = {}
    history: dict[str, list[dict]] = {}
    for r in runs:
        latest_by_eval[r["eval_id"]] = r
        history.setdefault(r["eval_id"], []).append(r)

    rows = []
    by_type: dict[str, dict] = {}
    consistencies = []
    for eval_id, r in sorted(latest_by_eval.items()):
        ev = evals_by_id.get(eval_id, {})
        eval_type = ev.get("type", "other")
        hist = history[eval_id]
        passed_runs = sum(1 for h in hist if h["verdict"] == "PASS")
        enough = len(hist) >= MIN_RUNS_FOR_CONSISTENCY
        consistency = (passed_runs / len(hist)) if enough else None
        if consistency is not None:
            consistencies.append(consistency)

        bucket = by_type.setdefault(eval_type, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if r["verdict"] == "PASS":
            bucket["passed"] += 1

        is_ref = bool(ev.get("reference")) or r.get("harness_id") == REFERENCE_HARNESS_ID
        rows.append({
            "eval_id": eval_id,
            "type": eval_type,
            "suite": ev.get("suite", "local"),
            "summary": ev.get("summary", eval_id),
            "tags": ev.get("tags", []),
            "verdict": r["verdict"],
            "harness_id": r["harness_id"],
            "harness_label": r.get("harness_label", r["harness_id"]),
            "reference": is_ref,
            # An external reference is not "stale" - it's from another system
            # entirely and is never expected to match your current harness.
            "stale": (not is_ref) and r["harness_id"] != current_harness,
            "duration_sec": r["duration_sec"],
            "run_count": len(hist),
            "passed_runs": passed_runs,
            "consistency": consistency,
            "history": [
                {"verdict": h["verdict"], "timestamp": h.get("timestamp"),
                 "harness_id": h["harness_id"], "mode": h.get("mode", "regression")}
                for h in hist
            ],
        })

    total = len(latest_by_eval)
    passed = sum(1 for r in latest_by_eval.values() if r["verdict"] == "PASS")

    # Trend: one point per harness version, in first-seen order. Each eval
    # counts once per harness (its last verdict under that harness).
    harness_order: list[str] = []
    harness_totals: dict[str, dict] = {}
    for r in runs:
        hid = r["harness_id"]
        if hid not in harness_totals:
            harness_order.append(hid)
            harness_totals[hid] = {"passed": 0, "total": 0,
                                   "label": r.get("harness_label", hid),
                                   "version": r.get("harness_version")}
    latest_per_eval_per_harness: dict[tuple, dict] = {}
    for r in runs:
        latest_per_eval_per_harness[(r["eval_id"], r["harness_id"])] = r
    for (_, hid), r in latest_per_eval_per_harness.items():
        harness_totals[hid]["total"] += 1
        if r["verdict"] == "PASS":
            harness_totals[hid]["passed"] += 1

    return {
        "overall": {"passed": passed, "total": total},
        "consistency": (sum(consistencies) / len(consistencies)) if consistencies else None,
        "consistency_sample": len(consistencies),
        "insufficient_history": total - len(consistencies),
        "by_type": by_type,
        "rows": rows,
        "trend": [{"harness_id": hid, **harness_totals[hid]} for hid in harness_order],
    }


def _compute_scoreboard(cwd: Path, cfg: dict) -> dict | None:
    runs = read_jsonl(eh_path(RUNS_FILE))
    if not runs:
        return None
    evals_by_id = {e["eval_id"]: e for e in read_evals()}
    current_harness = _harness_id(cwd, cfg)

    def suite_of(r: dict) -> str:
        return evals_by_id.get(r["eval_id"], {}).get("suite", "local")

    # Local and CVDP are scored identically but reported separately - never
    # blended. Divergence between them is the overfitting signal.
    suites = {}
    for suite_name in ("local", "cvdp"):
        suite_runs = [r for r in runs if suite_of(r) == suite_name]
        if suite_runs:
            suites[suite_name] = _suite_stats(suite_runs, evals_by_id, current_harness)

    combined = _suite_stats(runs, evals_by_id, current_harness)
    return {
        "current_harness": current_harness,
        "current_harness_label": _harness_label(cfg),
        "current_harness_version": cfg.get("harness_version", ""),
        "suites": suites,
        # Back-compat top-level keys (used by `ask`, older callers).
        "overall": combined["overall"],
        "consistency": combined["consistency"],
        "by_type": combined["by_type"],
        "rows": combined["rows"],
        "trend": combined["trend"],
    }


def _fmt_consistency(c) -> str:
    return "n/a" if c is None else f"{100 * c:.0f}%"


def _print_score(cwd: Path, cfg: dict) -> None:
    board = _compute_scoreboard(cwd, cfg)
    if board is None:
        print("No runs recorded yet.")
        return
    # label already embeds the version when one is declared - don't print it twice
    print(f"Harness: {board['current_harness_label']}  ({board['current_harness']})")
    for suite_name, s in board["suites"].items():
        o = s["overall"]
        pct = 100 * o["passed"] / o["total"] if o["total"] else 0
        n_ref = sum(1 for r in s["rows"] if r["reference"])
        ref_note = f"   [{n_ref} external reference]" if n_ref else ""
        print(f"\n[{suite_name}] score {o['passed']}/{o['total']} ({pct:.0f}%)   "
              f"consistency {_fmt_consistency(s['consistency'])}"
              f" (over {s['consistency_sample']} eval(s); {s['insufficient_history']} need more runs)"
              f"{ref_note}")
        if len(s["by_type"]) > 1:
            print("  by type - " + "  ".join(
                f"{t}: {b['passed']}/{b['total']}" for t, b in sorted(s["by_type"].items())
            ))
        # Own evals first and in full; reference rows are a calibration line,
        # not a to-do list, so they're summarized rather than enumerated.
        own = [r for r in s["rows"] if not r["reference"]]
        ref = [r for r in s["rows"] if r["reference"]]
        for row in own[:_SCORE_ROW_LIMIT]:
            marker = "stale" if row["stale"] else "current"
            cons = _fmt_consistency(row["consistency"])
            print(f"  {row['eval_id']:14s} {row['verdict']:4s} [{row['type']:12s}] "
                  f"{row['summary'][:44]:44s} {row['passed_runs']}/{row['run_count']} runs ({cons}) ({marker})")
        if len(own) > _SCORE_ROW_LIMIT:
            print(f"  ... and {len(own) - _SCORE_ROW_LIMIT} more (see `evalbench dashboard`)")
        if ref:
            rp = sum(1 for r in ref if r["verdict"] == "PASS")
            label = next((r["harness_label"] for r in ref if r["harness_label"]), "external")
            print(f"  reference: {rp}/{len(ref)} PASS ({100*rp/len(ref):.0f}%) - {label}")


def cmd_score(args) -> int:
    cwd = Path.cwd()
    cfg = load_config()
    _print_score(cwd, cfg)
    return 0


def cmd_check(args) -> int:
    """One-shot: capture -> propose -> review -> record score. This is the
    everyday entrypoint - run it right after you verify a real change."""
    cwd = Path.cwd()
    cfg_path = eh_path(CONFIG_FILE)
    if not cfg_path.exists():
        print("First time in this repo - quick one-time setup:\n")
        init_args = argparse.Namespace(
            repo_name=None,
            success_command=args.cmd,
            context_globs=None,
            model=None,
            tools=None,
            force=False,
        )
        if cmd_init(init_args) != 0:
            return 1
        print()

    cfg = load_config()
    command = args.cmd or cfg.get("success_command_template")
    if not command:
        print("No command given and no success_command_template configured.", file=sys.stderr)
        return 1

    capture = _capture(cwd, command)
    if getattr(args, "prompt", None):
        capture["input_prompt"] = args.prompt
    append_jsonl(eh_path(CAPTURES_FILE), capture)
    verdict = "PASS" if capture["exit_code"] == 0 else "FAIL"
    print(f"[check] {verdict} in {capture['duration_sec']:.1f}s")
    if verdict == "FAIL":
        print(capture["output_tail"])

    if not capture["diff_present"]:
        print("\nNo code changes since the last commit - nothing new to turn into an eval.")
        print()
        _print_score(cwd, cfg)
        return 0 if verdict == "PASS" else 1

    ev = _propose_from_capture(cwd, cfg, capture)
    print(f"\nThis change looks eval-worthy - starting commit {ev['starting_commit'][:10]}:")
    _review_one(ev, nudge=_nudge_for(cwd, ev))
    evals = read_evals()
    evals.append(ev)
    write_json(eh_path(EVALS_FILE), evals)

    if ev["status"] == "approved":
        run_record = {
            "run_id": short_id("run"),
            "eval_id": ev["eval_id"],
            "harness_id": _harness_id(cwd, cfg),
            "harness_label": _harness_label(cfg),
            "commit": git(cwd, "rev-parse", "HEAD"),
            "verdict": verdict,
            "duration_sec": capture["duration_sec"],
            "output_tail": capture["output_tail"],
            "timestamp": time.time(),
        }
        append_jsonl(eh_path(RUNS_FILE), run_record)
        print(f"\nSaved as {ev['eval_id']} - future `evalbench check`/`evalbench replay` runs will track it.")
    else:
        print(f"\n{ev['eval_id']} not approved (status={ev['status']}) - not added to your regression set.")

    print()
    _print_score(cwd, cfg)
    return 0 if verdict == "PASS" else 1


def cmd_replay(args) -> int:
    """Re-run every approved eval against the current tree - use this after
    you change context/skill files to see if the score actually moved."""
    cwd = Path.cwd()
    cfg = load_config()
    evals = read_evals()
    # Reference/imported evals with no success_command can't be re-run - an
    # empty shell command exits 0 and would score a bogus PASS.
    approved = [e for e in evals if e["status"] == "approved" and e.get("success_command")]
    skipped = sum(1 for e in evals if e["status"] == "approved" and not e.get("success_command"))
    if not approved:
        print("No runnable approved evals yet - run `evalbench check \"<cmd>\"` on some real work first.")
        if skipped:
            print(f"({skipped} approved eval(s) have no success command - reference/imported entries aren't replayable.)")
        return 0
    print(f"Replaying {len(approved)} eval(s) against the current tree"
          + (f" ({skipped} reference eval(s) skipped)" if skipped else "") + "...\n")
    for ev in approved:
        try:
            run_record = _execute_eval(cwd, cfg, ev, commit=None)
        except RuntimeError as exc:
            print(f"  {ev['eval_id']}: ERROR - {exc}")
            continue
        append_jsonl(eh_path(RUNS_FILE), run_record)
        print(f"  {ev['eval_id']:12s} -> {run_record['verdict']} ({run_record['duration_sec']:.1f}s)")
    print()
    _print_score(cwd, cfg)
    return 0


def _build_web_payload(cwd: Path, cfg: dict) -> dict:
    """Full data for the local web app: every eval with its complete run
    history (not just the latest), so the UI can drill into any eval and
    show how it's scored across every harness version it's been run under."""
    evals = read_evals()
    runs = read_jsonl(eh_path(RUNS_FILE))
    feedback = read_jsonl(eh_path(FEEDBACK_FILE))
    current_harness = _harness_id(cwd, cfg)

    runs_by_eval: dict[str, list[dict]] = {}
    for r in runs:
        runs_by_eval.setdefault(r["eval_id"], []).append(r)

    eval_payload = []
    by_type: dict[str, dict] = {}
    for ev in evals:
        ev_runs = sorted(runs_by_eval.get(ev["eval_id"], []), key=lambda r: r["timestamp"], reverse=True)
        latest = ev_runs[0] if ev_runs else None
        if latest and ev["status"] == "approved":
            eval_type = ev.get("type", "other")
            bucket = by_type.setdefault(eval_type, {"passed": 0, "total": 0})
            bucket["total"] += 1
            if latest["verdict"] == "PASS":
                bucket["passed"] += 1
        passed_runs = sum(1 for r in ev_runs if r["verdict"] == "PASS")
        attempt_runs = [r for r in ev_runs if r.get("mode") == "attempt"]
        enough = len(ev_runs) >= MIN_RUNS_FOR_CONSISTENCY
        eval_payload.append({
            "eval_id": ev["eval_id"],
            "type": ev.get("type", "other"),
            "suite": ev.get("suite", "local"),
            "reference": bool(ev.get("reference")),
            "attribution": ev.get("attribution", {}),
            "cvdp": ev.get("cvdp", {}),
            "summary": _eval_summary(ev),
            "purpose": ev.get("purpose", ""),
            "input_prompt": ev.get("input_prompt", ""),
            "task_text": ev.get("task_text", ""),
            "success_command": ev.get("success_command", ""),
            "starting_commit": ev.get("starting_commit", ""),
            "status": ev.get("status", "pending_review"),
            "tags": ev.get("tags", []),
            "comments": ev.get("comments", []),
            "code_links": ev.get("code_links", []),
            "created_at": ev.get("created_at"),
            "diffstat": ev.get("diffstat", {"files": [], "added": 0, "removed": 0}),
            "expected_diff": ev.get("expected_diff", ""),
            "latest_verdict": latest["verdict"] if latest else None,
            "latest_stale": bool(latest and latest["harness_id"] != current_harness),
            "run_count": len(ev_runs),
            "passed_runs": passed_runs,
            "attempt_count": len(attempt_runs),
            "consistency": (passed_runs / len(ev_runs)) if enough else None,
            "runs": [
                {
                    "run_id": r["run_id"],
                    "harness_id": r["harness_id"],
                    "harness_label": r.get("harness_label", ""),
                    "harness_version": r.get("harness_version", ""),
                    "mode": r.get("mode", "regression"),
                    "verdict": r["verdict"],
                    "duration_sec": r["duration_sec"],
                    "timestamp": r["timestamp"],
                    "commit": r.get("commit", "")[:10],
                    "output_tail": r.get("output_tail", ""),
                    "solution_diff": r.get("solution_diff", ""),
                }
                for r in ev_runs
            ],
        })

    # Trend: pass rate per harness_id, ordered by when that harness was first run.
    harness_seen: dict[str, dict] = {}
    for r in sorted(runs, key=lambda r: r["timestamp"]):
        hid = r["harness_id"]
        if hid not in harness_seen:
            harness_seen[hid] = {
                "harness_id": hid,
                "harness_label": r.get("harness_label", ""),
                "harness_version": r.get("harness_version", ""),
                "first_seen": r["timestamp"],
                "passed": 0,
                "total": 0,
            }
    latest_per_eval_per_harness: dict[tuple, dict] = {}
    for r in runs:
        latest_per_eval_per_harness[(r["eval_id"], r["harness_id"])] = r
    for (_, hid), r in latest_per_eval_per_harness.items():
        ev = next((e for e in evals if e["eval_id"] == r["eval_id"]), None)
        if not ev or ev["status"] != "approved":
            continue
        harness_seen[hid]["total"] += 1
        if r["verdict"] == "PASS":
            harness_seen[hid]["passed"] += 1
    trend = [harness_seen[hid] for hid in sorted(harness_seen, key=lambda h: harness_seen[h]["first_seen"])]

    def suite_summary(suite_name: str) -> dict:
        scored = [e for e in eval_payload
                  if e["status"] == "approved" and e["latest_verdict"] and e["suite"] == suite_name]
        cons = [e["consistency"] for e in scored if e["consistency"] is not None]
        return {
            "suite": suite_name,
            "passed": sum(1 for e in scored if e["latest_verdict"] == "PASS"),
            "total": len(scored),
            "consistency": (sum(cons) / len(cons)) if cons else None,
            "consistency_sample": len(cons),
            "insufficient_history": len(scored) - len(cons),
            "attempt_backed": sum(1 for e in scored if e["attempt_count"] > 0),
            "reference_count": sum(1 for e in scored if e["reference"]),
        }

    suites = [suite_summary(s) for s in ("local", "cvdp")]
    suites = [s for s in suites if s["total"] > 0]

    approved_evals = [e for e in eval_payload if e["status"] == "approved" and e["latest_verdict"]]
    total = len(approved_evals)
    passed = sum(1 for e in approved_evals if e["latest_verdict"] == "PASS")

    return {
        "repo_name": cfg.get("repo_name", "repo"),
        "generated_at": time.time(),
        "current_harness": {
            "id": current_harness,
            "label": _harness_label(cfg),
            "version": cfg.get("harness_version", ""),
            "note": cfg.get("harness_note", ""),
            "model": cfg.get("model") or "unspecified",
            "tools": cfg.get("tools") or [],
            "context_globs": cfg.get("context_globs") or [],
            "history": cfg.get("harness_history", []),
        },
        "overall": {"passed": passed, "total": total},
        "suites": suites,
        "by_type": by_type,
        "trend": trend,
        "evals": sorted(eval_payload, key=lambda e: e.get("created_at") or 0, reverse=True),
        "feedback": feedback,
    }


_DASHBOARD_SHELL = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>EvalScout</title>
<style>
  :root { --bg:#0b0e14; --panel:#131822; --panel2:#161c28; --border:#232a38; --text:#e8eaed; --muted:#8a92a3;
          --green:#4ade80; --red:#f87171; --yellow:#facc15; --blue:#60a5fa; --purple:#a78bfa;
          --add-bg:rgba(74,222,128,.10); --del-bg:rgba(248,113,113,.10); }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg);
         color: var(--text); margin: 0; padding: 28px 20px 80px; }
  .wrap { max-width: 980px; margin: 0 auto; }
  header { margin-bottom: 24px; }
  h1 { font-size: 19px; margin: 0 0 6px; font-weight: 600; }
  h1 .icon { color: var(--blue); font-family: ui-monospace, Menlo, monospace; }
  .sub { color: var(--muted); font-size: 12.5px; line-height: 1.6; }
  .sub code { background: var(--panel2); padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }

  .suites { display: flex; gap: 14px; flex-wrap: wrap; margin: 22px 0; }
  .suite { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
           padding: 16px 18px; flex: 1; min-width: 240px; }
  .suite h2 { margin: 0 0 12px; font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
              color: var(--muted); font-weight: 600; }
  .metrics { display: flex; gap: 24px; }
  .metric .val { font-size: 30px; font-weight: 700; line-height: 1.1; }
  .metric .lbl { font-size: 11px; color: var(--muted); margin-top: 3px; }
  .metric .hint { font-size: 10.5px; color: var(--muted); opacity: .75; margin-top: 2px; }
  .good { color: var(--green); } .mid { color: var(--yellow); } .bad { color: var(--red); }
  .na { color: var(--muted); }

  .section-label { font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
                   color: var(--muted); font-weight: 600; margin: 26px 0 10px; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }
  .empty { color: var(--muted); font-size: 13px; padding: 16px 0; }

  svg.trend { width: 100%; height: 130px; display: block; }
  .trend-line { fill: none; stroke: var(--blue); stroke-width: 2; }
  .trend-line.cvdp { stroke: var(--purple); stroke-dasharray: 4 3; }
  .trend-pt { fill: var(--blue); } .trend-pt.cvdp { fill: var(--purple); }
  .trend-lbl { fill: var(--muted); font-size: 10px; text-anchor: middle; }
  .axis { stroke: var(--border); stroke-width: 1; }

  .filters { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0 10px; align-items: center; }
  .filters .lbl { font-size: 11px; color: var(--muted); margin-right: 2px; }
  .chip { background: var(--panel2); border: 1px solid var(--border); color: var(--muted);
          border-radius: 999px; padding: 4px 11px; font-size: 11.5px; cursor: pointer; }
  .chip.on { background: var(--blue); border-color: var(--blue); color: #06121f; font-weight: 600; }

  .row { display: flex; align-items: center; gap: 9px; padding: 11px 12px; border: 1px solid var(--border);
         border-radius: 8px; margin-bottom: 6px; cursor: pointer; background: var(--panel); }
  .row:hover { border-color: #33405a; }
  .row.open { border-color: var(--blue); border-bottom-left-radius: 0; border-bottom-right-radius: 0; margin-bottom: 0; }
  .badge { font-size: 10px; padding: 2px 7px; border-radius: 4px; font-weight: 600; white-space: nowrap; }
  .badge.PASS { background: rgba(74,222,128,.15); color: var(--green); }
  .badge.FAIL { background: rgba(248,113,113,.15); color: var(--red); }
  .badge.NEW  { background: var(--panel2); color: var(--muted); }
  .badge.type { background: var(--panel2); color: var(--muted); font-weight: 500; }
  .badge.suite { background: rgba(167,139,250,.14); color: var(--purple); font-weight: 500; }
  .badge.stale { background: rgba(250,204,21,.12); color: var(--yellow); font-weight: 500; }
  .badge.ref { background: rgba(138,146,163,.16); color: var(--muted); font-weight: 500; }
  .badge.tag { background: rgba(96,165,250,.12); color: var(--blue); font-weight: 500; }
  .row .summary { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .spark { display: flex; gap: 2px; align-items: center; }
  .spark i { width: 5px; height: 13px; border-radius: 1px; display: block; }
  .spark i.PASS { background: var(--green); } .spark i.FAIL { background: var(--red); }
  .cons { font-size: 11px; color: var(--muted); min-width: 56px; text-align: right; font-variant-numeric: tabular-nums; }
  .caret { color: var(--muted); font-size: 10px; }

  .detail { display: none; border: 1px solid var(--blue); border-top: none; border-radius: 0 0 8px 8px;
            padding: 16px 18px; margin-bottom: 6px; background: var(--panel2); }
  .detail.open { display: block; }
  .field { margin-bottom: 14px; }
  .field > label { display: block; font-size: 10px; text-transform: uppercase; letter-spacing: .07em;
                   color: var(--muted); margin-bottom: 5px; font-weight: 600; }
  .field .body { font-size: 12.5px; line-height: 1.55; white-space: pre-wrap; }
  .field .missing { color: var(--yellow); font-size: 12px; }
  code.cmd { display: block; background: var(--bg); padding: 8px 10px; border-radius: 6px;
             font-size: 11.5px; overflow-x: auto; white-space: pre; }
  a.link { color: var(--blue); text-decoration: none; font-size: 12px; display: block; margin-bottom: 3px; }
  a.link:hover { text-decoration: underline; }
  button.toggle { background: var(--panel); border: 1px solid var(--border); color: var(--text);
                  border-radius: 6px; padding: 5px 11px; font-size: 11.5px; cursor: pointer; }
  button.toggle:hover { border-color: var(--blue); }

  .diff { display: none; margin-top: 8px; background: var(--bg); border-radius: 6px; overflow-x: auto;
          font-family: ui-monospace, Menlo, monospace; font-size: 11px; line-height: 1.45; }
  .diff.open { display: block; }
  .diff .dl { padding: 0 10px; white-space: pre; }
  .diff .dl.add { background: var(--add-bg); color: var(--green); }
  .diff .dl.del { background: var(--del-bg); color: var(--red); }
  .diff .dl.hunk { color: var(--blue); background: rgba(96,165,250,.07); }
  .diff .dl.meta { color: var(--muted); }

  table.runs { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  table.runs th { text-align: left; color: var(--muted); font-weight: 500; padding: 5px 8px 5px 0;
                  border-bottom: 1px solid var(--border); font-size: 10.5px; text-transform: uppercase; }
  table.runs td { padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border); }
  .mode { font-size: 10px; padding: 1px 5px; border-radius: 3px; }
  .mode.attempt { background: rgba(74,222,128,.13); color: var(--green); }
  .mode.regression { background: var(--panel); color: var(--muted); }
  .comment { background: var(--panel); border-left: 2px solid var(--blue); padding: 7px 10px;
             border-radius: 0 5px 5px 0; margin-bottom: 5px; font-size: 12px; }
  .comment .when { color: var(--muted); font-size: 10px; margin-top: 3px; }
</style></head>
<body><div class="wrap">
<header>
  <h1><span class="icon">[&bull;_&bull;]</span> EvalScout &mdash; <span id="repo"></span></h1>
  <div class="sub" id="harness"></div>
</header>

<div class="suites" id="suites"></div>

<div class="section-label">Score by harness version</div>
<div class="panel"><svg class="trend" id="trend"></svg><div id="trend-legend" class="sub"></div></div>

<div class="section-label">Evals</div>
<div class="filters" id="filters"></div>
<div id="list"></div>
</div>

<script>
const DATA = __EVALBENCH_DATA__;
const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const pctClass = p => p >= 0.8 ? 'good' : p >= 0.5 ? 'mid' : 'bad';
const ago = ts => {
  if (!ts) return '';
  const s = Date.now()/1000 - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
};

document.getElementById('repo').textContent = DATA.repo_name;
const h = DATA.current_harness;
document.getElementById('harness').innerHTML =
  'Harness ' + (h.version ? '<b>' + esc(h.version) + '</b> ' : '') +
  '<code>' + esc(h.id) + '</code>' + (h.note ? ' &mdash; ' + esc(h.note) : '') +
  '<br>model <code>' + esc(h.model) + '</code>' +
  ' &middot; tools <code>' + esc((h.tools || []).join(', ') || 'none') + '</code>' +
  ' &middot; context <code>' + esc((h.context_globs || []).join(', ') || 'none') + '</code>' +
  '<br>generated ' + new Date(DATA.generated_at * 1000).toLocaleString();

// ---- suite summary cards (local and cvdp reported separately, never blended)
const suitesEl = document.getElementById('suites');
if (!DATA.suites.length) {
  suitesEl.innerHTML = '<div class="empty">No scored evals yet.</div>';
}
DATA.suites.forEach(s => {
  const pass = s.total ? s.passed / s.total : 0;
  const consTxt = s.consistency == null ? 'n/a' : Math.round(s.consistency * 100) + '%';
  const consCls = s.consistency == null ? 'na' : pctClass(s.consistency);
  const d = document.createElement('div');
  d.className = 'suite';
  d.innerHTML =
    '<h2>' + esc(s.suite) + '</h2><div class="metrics">' +
    '<div class="metric"><div class="val ' + pctClass(pass) + '">' + Math.round(pass*100) + '%</div>' +
    '<div class="lbl">score</div><div class="hint">' + s.passed + '/' + s.total + ' passing</div></div>' +
    '<div class="metric"><div class="val ' + consCls + '">' + consTxt + '</div>' +
    '<div class="lbl">consistency</div><div class="hint">' +
      (s.consistency == null ? 'needs ≥2 runs' : 'over ' + s.consistency_sample + ' eval(s)') +
      (s.insufficient_history ? ' &middot; ' + s.insufficient_history + ' pending' : '') +
    '</div></div></div>' +
    (s.reference_count
      ? '<div class="hint" style="margin-top:10px">' + s.reference_count + ' of these are an external ' +
        'reference baseline (not your harness).</div>'
      : '') +
    (s.attempt_backed ? '' :
      '<div class="hint" style="margin-top:10px;color:var(--yellow)">No attempt-backed runs - ' +
      'these are regression results, which do not measure harness quality.</div>');
  suitesEl.appendChild(d);
});

// ---- trend
const svg = document.getElementById('trend');
const trend = DATA.trend.filter(t => t.total > 0);
if (trend.length < 1) {
  svg.outerHTML = '<div class="empty">No history yet. Run <code>evalbench replay</code> after a harness change to start a trend.</div>';
} else {
  const W = 940, H = 130, PADX = 34, PADY = 20;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  const NS = 'http://www.w3.org/2000/svg';
  const n = trend.length, step = n > 1 ? (W - 2*PADX) / (n - 1) : 0;
  [0, 0.5, 1].forEach(f => {
    const y = H - PADY - f * (H - 2*PADY);
    const l = document.createElementNS(NS, 'line');
    l.setAttribute('x1', PADX-6); l.setAttribute('x2', W-PADX);
    l.setAttribute('y1', y); l.setAttribute('y2', y); l.setAttribute('class', 'axis');
    svg.appendChild(l);
    const t = document.createElementNS(NS, 'text');
    t.setAttribute('x', 12); t.setAttribute('y', y + 3);
    t.setAttribute('class', 'trend-lbl'); t.textContent = Math.round(f*100) + '%';
    svg.appendChild(t);
  });
  const pts = trend.map((t, i) => {
    const p = t.total ? t.passed / t.total : 0;
    return { x: PADX + i*step, y: H - PADY - p*(H - 2*PADY), t, p };
  });
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', pts.map((p,i) => (i?'L':'M') + p.x.toFixed(1) + ' ' + p.y.toFixed(1)).join(' '));
  path.setAttribute('class', 'trend-line');
  svg.appendChild(path);
  pts.forEach(p => {
    const c = document.createElementNS(NS, 'circle');
    c.setAttribute('cx', p.x); c.setAttribute('cy', p.y); c.setAttribute('r', 4);
    c.setAttribute('class', 'trend-pt');
    const ti = document.createElementNS(NS, 'title');
    ti.textContent = (p.t.harness_version || p.t.harness_id) + ' - ' +
                     p.t.passed + '/' + p.t.total + ' (' + Math.round(p.p*100) + '%)' +
                     (p.t.harness_label ? '\\n' + p.t.harness_label : '');
    c.appendChild(ti); svg.appendChild(c);
    const lb = document.createElementNS(NS, 'text');
    lb.setAttribute('x', p.x); lb.setAttribute('y', H - 6);
    lb.setAttribute('class', 'trend-lbl');
    lb.textContent = p.t.harness_version || p.t.harness_id.slice(0, 6);
    svg.appendChild(lb);
  });
  document.getElementById('trend-legend').textContent =
    trend.length === 1 ? 'Only one harness version recorded so far - change context/skills and replay to see movement.' : '';
}

// ---- filters + list
let filterType = 'all', filterSuite = 'all';
const types = Array.from(new Set(DATA.evals.map(e => e.type))).sort();
const suites = Array.from(new Set(DATA.evals.map(e => e.suite))).sort();
const filtersEl = document.getElementById('filters');

function chip(label, active, onClick) {
  const b = document.createElement('span');
  b.className = 'chip' + (active ? ' on' : '');
  b.textContent = label;
  b.onclick = onClick;
  return b;
}
function renderFilters() {
  filtersEl.innerHTML = '';
  const l1 = document.createElement('span'); l1.className = 'lbl'; l1.textContent = 'type'; filtersEl.appendChild(l1);
  filtersEl.appendChild(chip('all', filterType === 'all', () => { filterType = 'all'; render(); }));
  types.forEach(t => filtersEl.appendChild(chip(t, filterType === t, () => { filterType = t; render(); })));
  if (suites.length > 1) {
    const l2 = document.createElement('span'); l2.className = 'lbl'; l2.style.marginLeft = '10px';
    l2.textContent = 'suite'; filtersEl.appendChild(l2);
    filtersEl.appendChild(chip('all', filterSuite === 'all', () => { filterSuite = 'all'; render(); }));
    suites.forEach(s => filtersEl.appendChild(chip(s, filterSuite === s, () => { filterSuite = s; render(); })));
  }
}

function diffHtml(text) {
  if (!text) return '<div class="dl meta">(no diff recorded)</div>';
  return text.split('\\n').map(line => {
    let cls = '';
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ')) cls = 'meta';
    else if (line.startsWith('@@')) cls = 'hunk';
    else if (line.startsWith('+')) cls = 'add';
    else if (line.startsWith('-')) cls = 'del';
    return '<div class="dl ' + cls + '">' + esc(line || ' ') + '</div>';
  }).join('');
}

const listEl = document.getElementById('list');
function render() {
  renderFilters();
  listEl.innerHTML = '';
  const shown = DATA.evals.filter(e =>
    (filterType === 'all' || e.type === filterType) &&
    (filterSuite === 'all' || e.suite === filterSuite));
  if (!shown.length) {
    listEl.innerHTML = '<div class="empty">No evals match. Run <code>evalbench check "&lt;your test command&gt;"</code> on real work to create some.</div>';
    return;
  }
  shown.forEach(ev => {
    const row = document.createElement('div');
    row.className = 'row';
    const verdict = ev.latest_verdict || 'NEW';
    const spark = ev.runs.slice().reverse().map(r =>
      '<i class="' + r.verdict + '" title="' + r.verdict + ' · ' + (r.harness_version || r.harness_id) + '"></i>').join('');
    const consTxt = ev.consistency == null
      ? (ev.run_count ? ev.run_count + '/1 run' : 'no runs')
      : ev.passed_runs + '/' + ev.run_count + ' · ' + Math.round(ev.consistency*100) + '%';
    row.innerHTML =
      '<span class="badge ' + verdict + '">' + verdict + '</span>' +
      '<span class="badge type">' + esc(ev.type) + '</span>' +
      (suites.length > 1 ? '<span class="badge suite">' + esc(ev.suite) + '</span>' : '') +
      (ev.reference ? '<span class="badge ref">reference</span>' : '') +
      '<span class="summary">' + esc(ev.summary) + '</span>' +
      (ev.tags || []).map(t => '<span class="badge tag">' + esc(t) + '</span>').join('') +
      (ev.latest_stale ? '<span class="badge stale">stale</span>' : '') +
      '<span class="spark">' + spark + '</span>' +
      '<span class="cons">' + consTxt + '</span><span class="caret">&#9656;</span>';

    const det = document.createElement('div');
    det.className = 'detail';
    const st = ev.diffstat || { files: [], added: 0, removed: 0 };
    const did = 'd_' + ev.eval_id;
    det.innerHTML =
      '<div class="field"><label>purpose</label><div class="body">' +
        (ev.purpose ? esc(ev.purpose) : '<span class="missing">not set - add one so this eval explains itself later</span>') +
      '</div></div>' +
      '<div class="field"><label>input prompt (what an agent must solve)</label><div class="body">' +
        (ev.input_prompt ? esc(ev.input_prompt)
          : '<span class="missing">not set - required before this eval can be attempted (evalbench attempt)</span>') +
      '</div></div>' +
      (ev.reference
        ? '<div class="field"><label>external reference</label><div class="body">Verdict produced by <b>' +
          esc(ev.attribution.source || 'another system') + '</b>, not your harness - a calibration point that ' +
          'will not move when you change your setup.' +
          (ev.attribution.note ? '<br>' + esc(ev.attribution.note) : '') +
          (ev.attribution.url ? '<br><a class="link" href="' + esc(ev.attribution.url) + '" target="_blank">' +
             esc(ev.attribution.url) + '</a>' : '') +
          '</div></div>'
        : '') +
      (ev.cvdp && ev.cvdp.problem_id
        ? '<div class="field"><label>cvdp problem</label><div class="body">' + esc(ev.cvdp.problem_id) +
          ' &middot; difficulty <b>' + esc(ev.cvdp.difficulty || 'unknown') + '</b>' +
          (ev.cvdp.categories && ev.cvdp.categories.length ? ' &middot; ' + esc(ev.cvdp.categories.join(', ')) : '') +
          '</div></div>'
        : '') +
      '<div class="field"><label>success command</label><code class="cmd">' +
        (ev.success_command ? esc(ev.success_command)
          : '<span style="color:var(--yellow)">none - not runnable locally</span>') + '</code></div>' +
      '<div class="field"><label>code (' + st.files.length + ' file(s), +' + st.added + '/-' + st.removed +
        ', from ' + esc((ev.starting_commit||'').slice(0,10)) + ')</label>' +
        (ev.code_links || []).map(l => '<a class="link" href="' + esc(l.url) + '">' + esc(l.path) + '</a>').join('') +
        '<button class="toggle" onclick="var d=document.getElementById(\\'' + did + '\\');d.classList.toggle(\\'open\\')">show diff</button>' +
        '<div class="diff" id="' + did + '">' + diffHtml(ev.expected_diff) + '</div>' +
      '</div>' +
      '<div class="field"><label>comments (' + (ev.comments||[]).length + ')</label>' +
        ((ev.comments||[]).length
          ? ev.comments.map(c => '<div class="comment">' + esc(c.text) +
              '<div class="when">' + ago(c.timestamp) + '</div></div>').join('')
          : '<div class="body" style="color:var(--muted)">none yet</div>') +
      '</div>' +
      '<div class="field"><label>run history (' + ev.runs.length + ')</label>' +
        (ev.runs.length ? '<table class="runs"><tr><th>verdict</th><th>mode</th><th>harness</th><th>when</th><th>time</th></tr>' +
          ev.runs.map(r =>
            '<tr><td><span class="badge ' + r.verdict + '">' + r.verdict + '</span></td>' +
            '<td><span class="mode ' + (r.mode||'regression') + '">' + esc(r.mode||'regression') + '</span></td>' +
            '<td>' + esc(r.harness_version || r.harness_id) + '</td>' +
            '<td>' + ago(r.timestamp) + '</td><td>' + r.duration_sec + 's</td></tr>').join('') +
          '</table>'
          : '<div class="body" style="color:var(--muted)">never run</div>') +
      '</div>';

    row.onclick = () => { row.classList.toggle('open'); det.classList.toggle('open'); };
    listEl.appendChild(row);
    listEl.appendChild(det);
  });
}
render();
</script>
</body></html>
"""


def _write_dashboard(cwd: Path, cfg: dict) -> Path:
    """Regenerate .evalbench/dashboard.html and return its path. Cheap
    enough (no external calls, just local file reads) to call after every
    check/decide/replay so the file is never stale."""
    payload = _build_web_payload(cwd, cfg)
    out_path = eh_path("dashboard.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = _DASHBOARD_SHELL.replace("__EVALBENCH_DATA__", json.dumps(payload))
    out_path.write_text(html)
    return out_path


def cmd_dashboard(args) -> int:
    cwd = Path.cwd()
    cfg = load_config()
    out_path = _write_dashboard(cwd, cfg)
    print(f"Wrote {out_path}")
    if not args.no_open:
        import webbrowser
        webbrowser.open(f"file://{out_path}")
        print("Opened in your browser.")
    else:
        print(f"Open it with: open {out_path}")
    return 0


def cmd_ask(args) -> int:
    """Local, deterministic Q&A over your own .evalbench data - keyword
    routing, not an LLM. Answers only what's derivable from evals/runs/tags
    already on disk. For real natural-language questions, point an agent
    (e.g. this Claude Code session) at .evalbench/*.json directly - that's
    where actual understanding should live, not a second model call baked
    into this offline tool."""
    cwd = Path.cwd()
    cfg = load_config()
    question = " ".join(strip_leading_separator(args.text)).strip().lower()
    board = _compute_scoreboard(cwd, cfg)
    evals = read_evals()

    if not question:
        print("Usage: evalbench ask \"<question>\"")
        print("Understands: score / failing / trend / tags / category <name> / eval <id>")
        return 1

    if board is None:
        print("No runs recorded yet - nothing to ask about. Run `evalbench check \"<cmd>\"` first.")
        return 0

    if any(w in question for w in ("fail", "broken", "red")):
        bad = [r for r in board["rows"] if r["verdict"] == "FAIL"]
        if not bad:
            print("Nothing failing right now.")
        else:
            print(f"{len(bad)} failing:")
            for r in bad:
                print(f"  {r['eval_id']}  [{r['type']}]  {r['summary']}")
        return 0

    if any(w in question for w in ("trend", "improve", "history", "over time")):
        if not board["trend"]:
            print("Not enough history yet - run `evalbench replay` after a harness change.")
        else:
            for t in board["trend"]:
                pct = 100 * t["passed"] / t["total"] if t["total"] else 0
                # _suite_stats writes these as `version`/`label` (not `harness_label`)
                name = t.get("version") or t.get("label") or t["harness_id"]
                print(f"  {name}: {t['passed']}/{t['total']} ({pct:.0f}%)")
        return 0

    if "tag" in question:
        tagged: dict[str, list[str]] = {}
        for e in evals:
            for tag in e.get("tags", []):
                tagged.setdefault(tag, []).append(e["eval_id"])
        if not tagged:
            print("No evals tagged yet - use [t]ag during `evalbench check` review.")
        else:
            for tag, ids in sorted(tagged.items()):
                print(f"  {tag}: {', '.join(ids)}")
        return 0

    if question.startswith("category ") or question.startswith("cat "):
        name = question.split(maxsplit=1)[1].strip()
        matches = [r for r in board["rows"] if r["type"] == name]
        if not matches:
            print(f"No evals in category '{name}'. Categories present: {', '.join(sorted(board['by_type']))}")
        else:
            for r in matches:
                print(f"  {r['eval_id']}  {r['verdict']}  {r['summary']}")
        return 0

    if question.startswith("eval "):
        eval_id = question.split(maxsplit=1)[1].strip()
        ev = next((e for e in evals if e["eval_id"] == eval_id), None)
        if ev is None:
            print(f"No eval {eval_id}.")
        else:
            print(f"{ev['eval_id']}  [{ev.get('type')}]  status={ev['status']}")
            print(f"  {ev.get('summary')}")
            print(f"  success: {ev['success_command']}")
            print(f"  tags: {', '.join(ev.get('tags', [])) or '(none)'}")
        return 0

    if any(w in question for w in ("score", "how are we", "quality", "doing")):
        o = board["overall"]
        pct = 100 * o["passed"] / o["total"] if o["total"] else 0
        print(f"Score: {o['passed']}/{o['total']} ({pct:.0f}%) on harness {board['current_harness']}")
        for cat, b in sorted(board["by_type"].items()):
            print(f"  {cat}: {b['passed']}/{b['total']}")
        return 0

    print("Not sure how to answer that locally. I understand: score / failing / trend / tags / category <name> / eval <id>.")
    print("For anything else, ask the agent you're running this alongside - point it at .evalbench/*.json.")
    return 0


def cmd_feedback(args) -> int:
    tokens = strip_leading_separator(args.text)
    text = " ".join(tokens)
    if not text:
        print('Usage: evalbench feedback "<text>"', file=sys.stderr)
        return 1
    append_jsonl(eh_path(FEEDBACK_FILE), {
        "target_type": "general",
        "target_id": None,
        "question": None,
        "answer": text,
        "timestamp": time.time(),
    })
    print("Recorded.")
    return 0


# ------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evalbench", description="Semiflow EvalBench: measure whether your AI coding setup is actually improving.")
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Scaffold .evalbench/config.json in the current repo.")
    p_init.add_argument("--repo-name")
    p_init.add_argument("--success-command")
    p_init.add_argument("--context-globs", help="Comma-separated globs, e.g. CLAUDE.md,skills/**")
    p_init.add_argument("--model", help="Model/agent identity, e.g. claude-sonnet-5")
    p_init.add_argument("--tools", help="Comma-separated tool/MCP server names available to the agent")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_check = sub.add_parser(
        "check",
        help="Everyday entrypoint: capture -> propose -> review -> score, in one step.",
    )
    p_check.add_argument(
        "cmd", nargs="?", default=None,
        help='Your verification command, quoted as one argument. Omit to reuse the configured success command.',
    )
    p_check.add_argument(
        "--prompt",
        help="The task you were solving. Stored as input_prompt so this eval can later be re-attempted by an agent.",
    )
    p_check.set_defaults(func=cmd_check)

    p_attempt = sub.add_parser(
        "attempt",
        help="Open an attempt: worktree at the eval's starting commit + the task prompt, for an agent to solve.",
    )
    p_attempt.add_argument("eval_id")
    p_attempt.set_defaults(func=cmd_attempt)

    p_grade = sub.add_parser("grade", help="Grade an open attempt (runs the success command in its worktree).")
    p_grade.add_argument("attempt_id")
    p_grade.add_argument("--keep", action="store_true", help="Keep the worktree after grading (for debugging).")
    p_grade.set_defaults(func=cmd_grade)

    p_base = sub.add_parser(
        "import-baseline",
        help="Import already-graded CVDP verdicts (e.g. SiliconCrew's FINAL_MANIFEST.json) as an external reference line. Free - no agent runs.",
    )
    p_base.add_argument("--manifest", required=True, help="Path to a bench-orchestrator FINAL_MANIFEST.json.")
    p_base.add_argument("--label", default="SiliconCrew CVDP baseline", help="Name for this reference harness.")
    p_base.set_defaults(func=cmd_import_baseline)

    p_cvdp = sub.add_parser(
        "import-cvdp",
        help="Import NVIDIA CVDP problems as eval definitions from a dataset JSONL you supply (no data ships with this tool).",
    )
    p_cvdp.add_argument("--dataset", required=True, help="Path to the CVDP JSONL.")
    p_cvdp.add_argument("--limit", type=int, default=0, help="Max problems to import (0 = all matched).")
    p_cvdp.add_argument("--ids", help="Comma-separated problem ids to import.")
    p_cvdp.add_argument("--difficulty", choices=["easy", "medium", "hard"], help="Only import this difficulty.")
    p_cvdp.add_argument("--success-command", help="Grading command. Without it, problems import as needs_grading and don't affect the score.")
    p_cvdp.set_defaults(func=cmd_import_cvdp)

    p_harness = sub.add_parser("harness", help="Name the current harness state, e.g. `harness v3 \"added skill\"`.")
    p_harness.add_argument("version", nargs="?", help="Version label, e.g. v3. Omit to show current + history.")
    p_harness.add_argument("note", nargs="?", help="What changed in this version.")
    p_harness.set_defaults(func=cmd_harness)

    p_replay = sub.add_parser(
        "replay",
        help="Re-run all approved evals against the current tree and print the updated score.",
    )
    p_replay.set_defaults(func=cmd_replay)

    p_wrap = sub.add_parser("wrap", help="(advanced) Wrap a command, capturing before/after git state.")
    p_wrap.add_argument("cmd", help='Shell command, quoted as one argument, e.g. "make regression"')
    p_wrap.set_defaults(func=cmd_wrap)

    p_propose = sub.add_parser("propose", help="(advanced) Turn the last (or given) capture into a draft eval.")
    p_propose.add_argument("--capture-id")
    p_propose.set_defaults(func=cmd_propose)

    p_review = sub.add_parser("review", help="(advanced) Review pending evals: approve/edit/reject/ask.")
    p_review.set_defaults(func=cmd_review)

    p_run = sub.add_parser("run", help="(advanced) Replay one specific approved eval, optionally at another commit.")
    p_run.add_argument("eval_id")
    p_run.add_argument("--commit", help="Run against this commit in a temp worktree instead of the current tree.")
    p_run.set_defaults(func=cmd_run)

    p_score = sub.add_parser("score", help="Print pass rate and current harness id.")
    p_score.set_defaults(func=cmd_score)

    p_dashboard = sub.add_parser("dashboard", help="Build the local web app (.evalbench/dashboard.html) and open it.")
    p_dashboard.add_argument("--no-open", action="store_true", help="Write the file but don't open a browser.")
    p_dashboard.set_defaults(func=cmd_dashboard)

    p_ask = sub.add_parser("ask", help="Local Q&A over your own evals: score/failing/trend/tags/category/eval.")
    p_ask.add_argument("text", nargs=argparse.REMAINDER)
    p_ask.set_defaults(func=cmd_ask)

    p_feedback = sub.add_parser("feedback", help="Log free-text feedback about the tool.")
    p_feedback.add_argument("text", nargs=argparse.REMAINDER)
    p_feedback.set_defaults(func=cmd_feedback)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
