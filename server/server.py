"""DAUnreal MCP server.

A Model Context Protocol server with a small set of *environment* tools (not a
business toolset) around a script pass-through:

- ``execute_python`` — run arbitrary ``unreal.*`` Python in the editor.
- ``python_help``   — introspection (``dir`` + docstring) for API discovery.
- ``reset_session`` — clear user variables from the shared REPL namespace.

Run with:  python server.py
"""

import json
import socket
import threading

from mcp.server import MCPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CONNECT_TIMEOUT = 5.0   # short: connection refused => editor not running
READ_TIMEOUT = 300.0    # long: scripts run synchronously on the game thread

# --- helpers auto-injected into the editor's shared Python namespace ---
# Defined once (guarded by `if "da" not in globals()`), and persists across
# requests because the plugin executes with EPythonFileExecutionScope::Public.
DA_PRELUDE = '''\
# === auto-injected da helpers (DAUnreal MCP) ===
if "da" not in globals():
    import json as _da_json
    _da_protected = set(globals())

    class _Da:
        @staticmethod
        def _dump(obj, depth):
            if depth <= 0:
                return str(obj)
            if obj is None or isinstance(obj, (bool, int, float, str)):
                return obj
            if isinstance(obj, dict):
                return {str(k): _Da._dump(v, depth - 1) for k, v in obj.items()}
            if isinstance(obj, (list, tuple, set)):
                return [_Da._dump(v, depth - 1) for v in obj]
            # Unreal UObject
            if hasattr(obj, "get_name") and hasattr(obj, "get_class"):
                try:
                    return {
                        "type": "Object",
                        "class": obj.get_class().get_name(),
                        "name": obj.get_name(),
                        "path": obj.get_path_name(),
                    }
                except Exception:
                    pass
            # Unreal struct with to_dict
            if hasattr(obj, "to_dict") and callable(obj.to_dict):
                try:
                    return _Da._dump(obj.to_dict(), depth - 1)
                except Exception:
                    pass
            # Generic iterable (unreal.Array / unreal.Map)
            try:
                if hasattr(obj, "__iter__"):
                    return [_Da._dump(v, depth - 1) for v in obj]
            except Exception:
                pass
            if hasattr(obj, "__dict__"):
                return {k: _Da._dump(v, depth - 1) for k, v in vars(obj).items() if not k.startswith("_")}
            return str(obj)

        @staticmethod
        def dump(obj, depth=3):
            return _Da._dump(obj, depth)

        @staticmethod
        def dumps(obj, depth=3):
            return _da_json.dumps(_Da._dump(obj, depth), ensure_ascii=False, default=str)

        @staticmethod
        def u(path):
            import unreal
            return unreal.load_asset(path)

        @staticmethod
        def cls(name):
            import unreal
            return unreal.load_class(None, name)

        @staticmethod
        def selected():
            import unreal
            return unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_selected_level_actors()

        @staticmethod
        def all_actors():
            import unreal
            return unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()

        @staticmethod
        def reset():
            for _k in list(globals()):
                if _k.startswith("__") or _k in _da_protected:
                    continue
                del globals()[_k]

    da = _Da()
    _da_protected.update(("_Da", "da", "_da_json", "_da_protected"))
# === end da helpers ===
'''

server = MCPServer(name="da-unreal-mcp")


class UEBridge:
    """Minimal newline-delimited JSON (NDJSON) TCP client to the DAUnrealMCP plugin.

    The plugin serves one request per connection (HTTP-style, matching the
    Mochi/FMochiHttpServer pattern), so each execute() call opens a fresh
    socket, sends one line, reads one response line, then closes.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._counter = 0

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    def _connect(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        sock.settimeout(READ_TIMEOUT)
        return sock

    @staticmethod
    def _read_line(sock: socket.socket) -> str:
        data = b""
        while True:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("connection closed by plugin before newline")
            if chunk == b"\n":
                break
            if chunk != b"\r":
                data += chunk
        return data.decode("utf-8")

    def execute(self, code: str) -> dict:
        with self._lock:
            request_id = self._next_id()
            sock: socket.socket | None = None
            try:
                sock = self._connect()
                payload = (json.dumps({"id": request_id, "code": code}) + "\n").encode("utf-8")
                sock.sendall(payload)
                return json.loads(self._read_line(sock))
            except ConnectionRefusedError:
                return {
                    "id": request_id,
                    "ok": False,
                    "error": (
                        f"cannot connect to the Unreal Editor bridge at {self.host}:{self.port}. "
                        "Make sure the editor is running with the DAUnrealMCP plugin loaded."
                    ),
                }
            except socket.timeout:
                return {
                    "id": request_id,
                    "ok": False,
                    "error": (
                        f"no response from the bridge within {READ_TIMEOUT:.0f}s. "
                        "The script is likely still running synchronously on the editor's game thread "
                        "(this blocks the editor). Avoid long-running scripts or split them into smaller steps."
                    ),
                }
            except (OSError, ConnectionError):
                return {
                    "id": request_id,
                    "ok": False,
                    "error": "bridge connection error (check the editor log for details).",
                }
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass


bridge = UEBridge()


def _run(code: str, *, prelude: bool = True) -> dict:
    payload = (DA_PRELUDE + "\n" + code) if prelude else code
    return bridge.execute(payload)


def _format(resp: dict) -> str:
    if resp.get("ok"):
        log = (resp.get("log") or "").rstrip()
        result = (resp.get("result") or "").strip()
        lines = ["OK"]
        if log:
            lines.append(log)
        if result:
            lines.append("--- result ---")
            lines.append(result)
        return "\n".join(lines)

    error = resp.get("error") or "unknown error"
    log = (resp.get("log") or "").rstrip()
    lines = ["ERROR", error]
    if log:
        lines += ["--- log ---", log]
    return "\n".join(lines)


@server.tool()
def execute_python(code: str) -> str:
    """Execute a Python script inside the running Unreal Editor.

    The script runs with full access to the ``unreal`` module and its namespace
    persists between calls, REPL-style. Use ``print(...)`` to return output.

    A ``da`` helper is pre-injected: ``da.dump(obj)`` / ``da.dumps(obj)`` turn
    UObjects/structs/arrays into readable dicts/JSON, ``da.u(path)`` loads an
    asset, ``da.selected()`` / ``da.all_actors()`` list level actors.

    Prefer non-deprecated editor APIs, e.g.:
        subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = subsys.get_all_level_actors()

    Example:  execute_python("print(da.dumps(da.all_actors()))")
    """
    if not code or not code.strip():
        return "Error: code is empty."
    return _format(_run(code))


@server.tool()
def python_help(target: str) -> str:
    """Introspect an object/class/function in the editor's Python namespace.

    Returns its type, public members (``dir``), and docstring. Use this to
    discover the ``unreal`` API before writing a script. Example:
    python_help("unreal.EditorActorSubsystem")
    """
    if not target or not target.strip():
        return "Error: target is empty."

    script = f'''\
try:
    _t = eval({target!r}, globals())
except Exception as _e:
    print("RESOLVE ERROR:", _e)
else:
    print("REPR:", repr(_t))
    print("TYPE:", type(_t).__name__)
    _m = [n for n in dir(_t) if not n.startswith("_")]
    print("MEMBERS (%d):" % len(_m))
    print(", ".join(_m))
    _doc = getattr(_t, "__doc__", None)
    print("DOC:", _doc if _doc else "(none)")
'''
    return _format(_run(script, prelude=False))


@server.tool()
def reset_session() -> str:
    """Clear user-defined variables from the shared REPL namespace.

    Keeps ``unreal`` and the injected ``da`` helpers. Use this when the session
    gets cluttered or a variable is interfering.
    """
    return _format(_run("da.reset(); print('session namespace reset')"))


if __name__ == "__main__":
    server.run(transport="stdio")
