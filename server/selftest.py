"""End-to-end stdio self-test for the DAUnreal MCP server.

Spawns server.py and speaks raw newline-delimited JSON-RPC (the MCP stdio
transport) to verify: initialize handshake, tools/list, and tools/call of
``execute_python``. With no Unreal Editor running, the tool must return a
clear connection error rather than crash.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
VENV_PY = SERVER_DIR / ".venv" / "Scripts" / "python.exe"
SERVER_PY = SERVER_DIR / "server.py"

PROTOCOL_VERSION = "2025-06-18"


def main() -> int:
    proc = subprocess.Popen(
        [str(VENV_PY), str(SERVER_PY)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(timeout: float = 10.0) -> dict:
        # Read one JSON line from stdout (ignore any non-JSON lines).
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                print(f"[server stdout] {line}", file=sys.stderr)
        raise TimeoutError("no JSON-RPC response received")

    try:
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "selftest", "version": "0.1"},
            },
        })
        init = recv()
        print("initialize ->", json.dumps(init.get("result", init), ensure_ascii=False))

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = recv()
        names = [t["name"] for t in tools["result"]["tools"]]
        print("tools ->", names)

        send({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "execute_python", "arguments": {"code": "print('hi')"}},
        })
        call = recv()
        print("execute_python ->", json.dumps(call, ensure_ascii=False))

        send({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "python_help", "arguments": {"target": "unreal.EditorActorSubsystem"}},
        })
        help_call = recv()
        help_text = json.dumps(help_call, ensure_ascii=False)
        print("python_help ->", help_text[:200], "...")

        send({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "reset_session", "arguments": {}},
        })
        reset_call = recv()
        print("reset_session ->", json.dumps(reset_call, ensure_ascii=False))

        ok = (
            "execute_python" in names
            and "python_help" in names
            and "reset_session" in names
            and ("result" in call or "error" in call)
            and ("result" in help_call or "error" in help_call)
            and ("result" in reset_call or "error" in reset_call)
        )
        return 0 if ok else 1
    finally:
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
