# Contributing

Small tool, few rules. The rules that exist are load-bearing — please don't
break them casually.

## Hard constraints

**1. Standard library only. No dependencies, ever.**
No `pip install`, no `requirements.txt`, no `pyproject.toml`/`setup.py` that
implies an install step. `git clone` + `python3 evalctl.py` must remain the
primary way to run this. The reason is not minimalism for its own sake: this
tool is meant to run on locked-down EDA workstations with stock Python where
you often cannot install anything.

**2. Python 3.9 compatible.**
Same reason. Concretely:
- Keep `from __future__ import annotations` at the top of every module so
  `list[dict]` / `str | None` annotations stay legal on 3.9.
- No `match` statements, no `X | Y` at runtime (only in annotations), no
  `tomllib`, no `itertools.pairwise`, no PEP 646/695 syntax.
- This is also why `mcp_server.py` hand-rolls JSON-RPC over stdio instead of
  using the official `mcp` SDK — that SDK requires Python 3.10+. Do not
  "simplify" it by adding the SDK.

**3. Zero network calls in core functionality.**
No telemetry, no analytics, no update checks, no phoning home. Users point
this at proprietary RTL; the guarantee that nothing leaves the machine is a
feature, not an accident. The only outbound anything is the LLM/agent the
user was already running, which evalharness never invokes itself.

**4. Ship no third-party data.**
No NVIDIA CVDP dataset files. No SiliconCrew source. The tool reads paths the
user supplies. If you add an importer for another benchmark, follow the same
posture: read a user-supplied path, bundle nothing.

**5. Toolchain-agnostic.**
Never assume iverilog, Verilator, a specific vendor flow, or any particular
simulator. The success command is always the user's own string, run through
the shell. Don't add per-tool special cases to the core.

## Before you open a PR

```bash
python3 -c "import ast; ast.parse(open('evalctl.py').read())"
python3 -c "import ast; ast.parse(open('mcp_server.py').read())"
python3 evalctl.py --help
```

And ideally exercise the real path in a scratch git repo:

```bash
mkdir /tmp/eh-demo && cd /tmp/eh-demo && git init && git commit --allow-empty -m init
python3 /path/to/evalharness/evalctl.py init --success-command "true" \
  --context-globs "" --model test --tools ""
python3 /path/to/evalharness/evalctl.py score
```

For the MCP server, `mcp_test_client.py` drives it end to end without an agent
in the loop:

```bash
python3 mcp_test_client.py /path/to/some/repo
```

## Data compatibility

Records in `.evalharness/` are long-lived — people accumulate them over weeks.
If you change the eval schema, extend `migrate_eval()` in `evalctl.py` so old
records keep loading instead of writing a migration script.
