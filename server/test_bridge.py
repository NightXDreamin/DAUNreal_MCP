"""Quick connectivity test for the DAUnrealMCP bridge.

Connects directly to the plugin's TCP port (bypassing MCP) to verify the
Unreal Editor bridge is running. The editor must be open with the plugin loaded.

Usage:
    python test_bridge.py                          # runs unreal.log('hello from MCP')
    python test_bridge.py "print(1+1)"             # runs the given Python code
"""

import json
import socket
import sys

HOST = "127.0.0.1"
PORT = 8765


def main() -> int:
    code = sys.argv[1] if len(sys.argv) > 1 else "unreal.log('hello from MCP')"

    sock = socket.create_connection((HOST, PORT), timeout=10)
    sock.settimeout(60)
    try:
        sock.sendall((json.dumps({"id": 1, "code": code}) + "\n").encode("utf-8"))

        data = b""
        while True:
            ch = sock.recv(1)
            if not ch:
                break
            if ch == b"\n":
                break
            if ch != b"\r":
                data += ch
    finally:
        sock.close()

    if not data:
        print("No response from bridge.")
        return 1

    resp = json.loads(data.decode("utf-8"))
    print(json.dumps(resp, indent=2, ensure_ascii=False))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
