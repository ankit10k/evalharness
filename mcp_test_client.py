#!/usr/bin/env python3
"""Interactive terminal client for testing mcp_server.py directly, without a
full agent in the loop. Launches the server as a subprocess, does the MCP
handshake, then lets you type plain commands instead of hand-writing
JSON-RPC.

Usage:
    python3 mcp_test_client.py /path/to/repo

Commands:
    check <command...>              run+propose (e.g. check python3 check.py)
    list                            list pending evals
    decide <eval_id> approve|reject [tag1,tag2]
    score                           current scoreboard
    get <eval_id>                   full detail for one eval
    feedback <text...>
    raw <tool_name> <json_args>     escape hatch for anything else
    help
    quit
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent / "mcp_server.py"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 mcp_test_client.py /path/to/repo")
        sys.exit(1)
    repo = sys.argv[1]

    proc = subprocess.Popen(
        [sys.executable, str(SERVER), repo],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    next_id = [1]

    def call(method: str, params: dict) -> dict:
        msg_id = next_id[0]
        next_id[0] += 1
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            err = proc.stderr.read()
            print(f"(server exited unexpectedly) {err}", file=sys.stderr)
            sys.exit(1)
        return json.loads(line)

    def call_tool(name: str, args: dict) -> None:
        resp = call("tools/call", {"name": name, "arguments": args})
        if "error" in resp:
            print(f"error: {resp['error']}")
            return
        text = resp["result"]["content"][0]["text"]
        try:
            print(json.dumps(json.loads(text), indent=2))
        except json.JSONDecodeError:
            print(text)

    init = call("initialize", {})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    print(f"Connected: {init['result']['serverInfo']} against {repo}")
    instructions = init["result"].get("instructions")
    if instructions:
        print("\n--- server instructions (what a real agent would load into context) ---")
        print(instructions)
        print("--- end server instructions ---\n")
    print("Type `help` for commands, `quit` to exit.\n")

    while True:
        try:
            line = input("evalharness> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit"):
            break
        elif cmd == "help":
            print(__doc__)
        elif cmd == "check":
            call_tool("check", {"command": rest} if rest else {})
        elif cmd == "list":
            call_tool("list_pending", {})
        elif cmd == "decide":
            bits = rest.split()
            if len(bits) < 2:
                print("Usage: decide <eval_id> approve|reject [tag1,tag2]")
                continue
            eval_id, decision = bits[0], bits[1]
            tags = bits[2].split(",") if len(bits) > 2 else []
            call_tool("decide", {"eval_id": eval_id, "decision": decision, "tags": tags})
        elif cmd == "score":
            call_tool("score", {})
        elif cmd == "replay":
            call_tool("replay", {})
        elif cmd == "get":
            call_tool("get_eval", {"eval_id": rest.strip()})
        elif cmd == "feedback":
            call_tool("feedback", {"text": rest})
        elif cmd == "raw":
            bits = rest.split(maxsplit=1)
            if len(bits) < 2:
                print("Usage: raw <tool_name> <json_args>")
                continue
            try:
                args = json.loads(bits[1])
            except json.JSONDecodeError as exc:
                print(f"bad json: {exc}")
                continue
            call_tool(bits[0], args)
        else:
            print(f"unknown command: {cmd} (try `help`)")

    proc.terminate()


if __name__ == "__main__":
    main()
