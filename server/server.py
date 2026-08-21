"""DAUnreal MCP server.

A Model Context Protocol server with a small set of *environment* tools (not a
business toolset) around a script pass-through:

- ``execute_python`` — run arbitrary ``unreal.*`` Python in the editor.
- ``python_help``   — introspection (``dir`` + docstring) for API discovery.
- ``reset_session`` — clear user variables from the shared REPL namespace.

Run with:  python server.py
"""

import ast
import asyncio
import json
import os
import re
import socket
import threading

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.utilities.types import Image

import da_async

DEFAULT_HOST = "127.0.0.1"
# Overridable so multiple editors (e.g. a 5.4 project on 8765 and a 5.5
# project on 8766) can each run a server instance:
#   set DAUNREAL_MCP_PORT=8766
DEFAULT_PORT = int(os.environ.get("DAUNREAL_MCP_PORT", "8765"))
CONNECT_TIMEOUT = 5.0   # short: connection refused => editor not running
READ_TIMEOUT = 300.0    # long: scripts run synchronously on the game thread
POLL_INTERVAL = 0.5     # async: how often to poll a running job
ASYNC_TIMEOUT = 600.0   # async: hard cap before we cancel the job

# --- helpers auto-injected into the editor's shared Python namespace ---
# Defined once (guarded by `if "da" not in globals()`), and persists across
# requests because the plugin executes with EPythonFileExecutionScope::Public.
DA_PRELUDE = '''\
# === auto-injected da helpers (DAUnreal MCP) ===
# Re-inject when `da` is missing OR present but broken. A plain
# `if "da" not in globals()` guard cannot self-heal: if `da` survives while one
# of its dependencies is gone, every da.* call fails forever and the only way
# out is restarting the editor. So probe the real functionality, and probe every
# helper that has module-level dependencies (dumps, reset).
try:
    _da_ok = (
        callable(getattr(da, "dumps", None))
        and da.dumps({"_": 1}) is not None
        and isinstance(getattr(type(da), "_protected", None), set)
    )
except Exception:
    _da_ok = False
if not _da_ok:
    import json as _da_json
    import unreal  # ensure unreal is loaded and protected before snapshotting
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
            import json as _j  # local import: no dependency on a module-level name
            return _j.dumps(_Da._dump(obj, depth), ensure_ascii=False, default=str)

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
            # The protected set is stashed on the class, not in globals(): a
            # module-level name can be deleted (by a previous reset, a stray
            # script, or a cleanup routine) and then reset() would break forever.
            protected = getattr(_Da, "_protected", None) or set()
            for _k in list(globals()):
                if _k.startswith("__") or _k in protected:
                    continue
                del globals()[_k]

        @staticmethod
        def set_root(tree, root):
            import unreal
            if hasattr(unreal, "DAUMGHelper"):
                return unreal.DAUMGHelper.set_widget_tree_root(tree, root)
            raise RuntimeError("unreal.DAUMGHelper is not available. Ensure DAUnrealMCP plugin is loaded.")

        @staticmethod
        def set_variable(widget, is_variable=True):
            import unreal
            if hasattr(unreal, "DAUMGHelper"):
                return unreal.DAUMGHelper.set_widget_is_variable(widget, is_variable)
            raise RuntimeError("unreal.DAUMGHelper is not available. Ensure DAUnrealMCP plugin is loaded.")

    da = _Da()
    _da_protected.update(("_Da", "da", "_da_json", "_da_protected", "unreal"))
    _Da._protected = set(_da_protected)
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
        # Auth token is read from the plugin's endpoint.json (path via the
        # DAUNREAL_MCP_ENDPOINT env var); empty when unset => no token sent.
        self.endpoint_path = os.environ.get("DAUNREAL_MCP_ENDPOINT", "")
        self._token = ""
        self._token_mtime: float | None = None

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

    def _load_token(self) -> str:
        """Read the auth token from the plugin's endpoint.json, re-reading only
        when the file changed (the editor regenerates it on each start).

        Any failure degrades to "no token" rather than raising: the file can be
        missing, half-written (the editor writes it during startup), or contain
        unexpected JSON. A parse error here must not break every request.
        """
        if not self.endpoint_path:
            return ""
        try:
            mtime = os.path.getmtime(self.endpoint_path)
            if mtime == self._token_mtime:
                return self._token
            with open(self.endpoint_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._token = data.get("token", "") if isinstance(data, dict) else ""
            self._token_mtime = mtime
        except (OSError, ValueError, UnicodeDecodeError):
            # ValueError covers json.JSONDecodeError (truncated/invalid file).
            self._token = ""
        return self._token

    def _request(self, payload: dict) -> dict:
        with self._lock:
            request_id = self._next_id()
            payload = dict(payload)
            payload["id"] = request_id
            token = self._load_token()
            if token:
                payload["token"] = token
            sock: socket.socket | None = None
            try:
                sock = self._connect()
                sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
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

    def execute(self, code: str) -> dict:
        return self._request({"code": code})

    def submit_async(self, setup_code: str, step_code: str, code: str = "") -> dict:
        return self._request({
            "action": "execute",
            "mode": "async",
            "setup_code": setup_code,
            "step_code": step_code,
            "code": code,
        })

    def poll(self, job_id: int) -> dict:
        return self._request({"action": "poll", "job_id": job_id})

    def cancel(self, job_id: int) -> dict:
        return self._request({"action": "cancel", "job_id": job_id})

    def import_assets(self, tasks: list[dict]) -> dict:
        return self._request({"action": "import_assets", "tasks": tasks})

    def compile_assets(self, paths: list[str]) -> dict:
        return self._request({"action": "compile_assets", "paths": paths})


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


async def _run_async(code: str, ctx: Context) -> str:
    """Async path: transform -> submit -> poll with progress -> final result."""
    try:
        transformed = da_async.transform(code)
    except SyntaxError as exc:
        return f"ERROR: SyntaxError at line {exc.lineno}: {exc.msg}"

    if not transformed.steppable:
        # No loops -> nothing to chunk; fall back to plain sync execution.
        return _format(_run(code))

    # Ensure the `da` helpers exist in the shared namespace before the job runs.
    # The prelude cannot be prepended to setup_code: that string is the output of
    # the AST transform, and re-parsing it would wrap the helpers in the generator
    # (and inject yields into their loops). It is idempotent and cheap, so send it
    # as its own request instead.
    prelude_resp = bridge.execute(DA_PRELUDE)
    if not prelude_resp.get("ok"):
        return f"ERROR: {prelude_resp.get('error', 'failed to inject da helpers')}"

    resp = bridge.submit_async(transformed.setup_code, transformed.step_code, code)
    if not resp.get("ok"):
        return f"ERROR: {resp.get('error', 'async submit failed')}"

    job_id = resp.get("job_id")
    last_slices = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ASYNC_TIMEOUT

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)

            p = bridge.poll(job_id)
            if not p.get("ok"):
                return f"ERROR: {p.get('error', 'poll failed')}"

            slices = p.get("slices_done", 0)
            if slices != last_slices:
                await ctx.report_progress(float(slices), None, f"{slices} slices")
                last_slices = slices

            status = p.get("status")
            if status == "done":
                return _format_async_result(p)
            if status == "error":
                return f"ERROR: {_map_traceback(p.get('error') or 'unknown error', transformed.line_map)}"
            if status == "cancelled":
                output = (p.get("output") or "").strip()
                return f"CANCELLED\n{output}".rstrip() if output else "CANCELLED"

            if loop.time() > deadline:
                bridge.cancel(job_id)
                return "ERROR: async job timed out and was cancelled"
    except asyncio.CancelledError:
        # Client cancelled the request (notifications/cancelled, interrupt
        # mode). Cancel the editor job inside a shielded scope so the cleanup
        # (g.close() -> GeneratorExit -> finally) completes.
        with anyio.CancelScope(shield=True):
            bridge.cancel(job_id)
            cancel_deadline = loop.time() + 5.0
            while loop.time() < cancel_deadline:
                await asyncio.sleep(0.2)
                p = bridge.poll(job_id)
                if p.get("status") == "cancelled":
                    break
        return "CANCELLED"


def _format_async_result(poll_resp: dict) -> str:
    output = (poll_resp.get("output") or "").strip()
    return f"OK\n{output}".rstrip() if output else "OK"


def _map_traceback(error: str, line_map: dict) -> str:
    if not line_map:
        return error

    def _replace(match: re.Match) -> str:
        new_line = da_async.map_traceback_line(int(match.group(1)), line_map)
        return f'File "<string>", line {new_line}'

    return re.sub(r'File "<string>", line (\d+)', _replace, error)


# Operations that mutate the scene or assets — flagged as dangerous in dry-run.
# Name-based matching: we only see the attribute/function name, not the object it
# is called on, so this is a *heuristic preview*, not a sandbox (see the caveat
# printed in every report).
DRY_RUN_DANGEROUS = frozenset({
    # asset lifecycle
    "delete_asset", "delete_loaded_asset", "delete_loaded_assets", "delete_directory",
    "rename_asset", "rename_directory", "duplicate_asset", "duplicate_directory",
    "save_asset", "save_loaded_asset", "save_loaded_assets", "save_package",
    "save_directory", "make_directory", "create_asset", "create_unique_asset_name",
    "checkout_asset", "checkout_loaded_asset", "consolidate_assets",
    "import_asset_tasks", "export_assets", "set_metadata_tag", "remove_metadata_tag",
    # actor lifecycle
    "destroy_actor", "destroy_actors", "delete_actor", "destroy_component",
    "spawn_actor_from_class", "spawn_actor_from_object", "duplicate_actor",
    "duplicate_actors", "convert_actors", "attach_to_actor", "detach_from_actor",
    "add_component", "add_component_by_class", "destroy_actor_component",
    # transforms / properties (silently move things — easy to miss visually)
    "set_actor_location", "set_actor_rotation", "set_actor_scale3d",
    "set_actor_transform", "set_actor_location_and_rotation",
    "set_actor_label", "set_editor_property", "set_editor_properties",
    "set_world_location", "set_relative_location", "set_relative_transform",
    "modify",
    # level lifecycle — new_level/load_level discard unsaved work
    "new_level", "new_level_from_template", "load_level",
    "save_current_level", "save_all_dirty_levels",
    # build / PIE — long, state-changing
    "build", "build_light_maps", "build_lighting",
    "editor_request_begin_play", "editor_request_end_play", "editor_play_simulate",
    # undo stack
    "transact_undo", "transact_redo", "undo", "redo",
})

# Escape hatches: dynamic dispatch or non-`unreal` destruction that name-based
# analysis cannot see through. Reported separately so the caveat is explicit.
DRY_RUN_OPAQUE = frozenset({
    "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr",
    "globals", "locals", "vars",
})
DRY_RUN_EXTERNAL = frozenset({
    "remove", "unlink", "rmtree", "rmdir", "move", "copytree", "chmod",
    "run", "call", "check_call", "check_output", "Popen", "system", "popen",
    "write", "writelines", "truncate",
})


def _dry_run_report(code: str) -> str:
    """Statically analyse a script and report what it would call — WITHOUT
    executing it. Highlights dangerous (mutating) calls so the AI can preview
    side effects before delete/destroy/save/spawn.

    This is a *heuristic preview*, not a sandbox: matching is by call name, so
    dynamic dispatch (``getattr``/``eval``) and destruction outside the
    ``unreal`` API (``os.remove``, ``subprocess``) cannot be detected reliably.
    Those are surfaced under their own headings instead of being silently
    ignored.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"ERROR: SyntaxError at line {exc.lineno}: {exc.msg}"

    calls: set[str] = set()
    imports: list[str] = []
    loops = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                calls.add(node.func.id)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            loops += 1
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append("." * node.level + (node.module or ""))

    dangerous = sorted(calls & DRY_RUN_DANGEROUS)
    opaque = sorted(calls & DRY_RUN_OPAQUE)
    external = sorted(calls & DRY_RUN_EXTERNAL)

    if dangerous or external:
        risk = "HIGH"
    elif opaque:
        risk = "UNKNOWN"       # cannot be judged statically
    elif calls:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    lines = ["DRY RUN — script was NOT executed", ""]
    if dangerous:
        lines.append("dangerous calls (mutate scene/assets):")
        lines.extend(f"  - {c}" for c in dangerous)
    else:
        lines.append("dangerous calls: (none)")
    if external:
        lines.append("")
        lines.append("outside-editor side effects (filesystem/process):")
        lines.extend(f"  - {c}" for c in external)
    if opaque:
        lines.append("")
        lines.append("dynamic dispatch — static analysis cannot see the real target:")
        lines.extend(f"  - {c}" for c in opaque)
    lines.append("")
    lines.append(f"all called functions: {', '.join(sorted(calls)) if calls else '(none)'}")
    lines.append(f"loops: {loops}")
    lines.append(f"imports: {', '.join(imports) if imports else '(none)'}")
    lines.append(f"risk: {risk}")
    lines.append("")
    lines.append("note: name-based heuristic, not a sandbox — it cannot resolve "
                 "dynamic dispatch or judge intent. Review the script itself for "
                 "anything destructive.")
    return "\n".join(lines)


@server.tool()
async def execute_python(code: str, ctx: Context, mode: str = "sync", dry_run: bool = False) -> str:
    """Execute a Python script inside the running Unreal Editor.

    The script runs with full access to the ``unreal`` module and its namespace
    persists between calls, REPL-style. Use ``print(...)`` to return output.

    A ``da`` helper is pre-injected: ``da.dump(obj)`` / ``da.dumps(obj)`` turn
    UObjects/structs/arrays into readable dicts/JSON, ``da.u(path)`` loads an
    asset, ``da.selected()`` / ``da.all_actors()`` list level actors.

    ``mode``: ``"sync"`` (default) runs the whole script in one go; ``"async"``
    splits loops into time-budgeted steps so long batch scripts do not freeze
    the editor, reports progress slices, and honours client cancellation.

    Async chunking only applies to *top-level* loops. Put the loop at the top
    level of the script (not inside a ``def``/``class``). If a script has no
    splittable top-level loop — a loop inside a function, a single giant
    blocking call, or only comprehensions — it falls back to sync execution and
    will block the editor until it finishes.

    ``dry_run``: when True, analyse the script statically and report what it
    would call (dangerous operations highlighted) WITHOUT executing it — use
    before delete/destroy/save/spawn to preview side effects. It is a
    name-matching heuristic, **not** a sandbox: it cannot resolve dynamic
    dispatch (``getattr``/``eval``) or judge intent, and destruction outside the
    ``unreal`` API (``os.remove``, ``subprocess``) is reported separately rather
    than inferred. A clean dry-run report is not a guarantee — still read the
    script.

    Prefer non-deprecated editor APIs, e.g.:
        subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = subsys.get_all_level_actors()

    Example:  execute_python("print(da.dumps(da.all_actors()))")
    """
    if not code or not code.strip():
        return "Error: code is empty."
    if dry_run:
        return _dry_run_report(code)
    if mode != "async":
        return _format(_run(code))
    return await _run_async(code, ctx)


async def _run_native_job(submit_resp: dict, ctx: Context, timeout_s: float, what: str) -> str:
    """Shared submit->poll loop for native jobs (import/compile).

    The editor executes these from its FTSTicker tick (not the request callback
    stack), which is what makes Interchange imports and blueprint compiles safe
    to run via MCP. Returns a plain-text report.
    """
    if not submit_resp.get("ok"):
        return f"ERROR: {submit_resp.get('error', f'{what} submit failed')}"
    job_id = submit_resp.get("job_id")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            p = bridge.poll(job_id)
            if not p.get("ok"):
                return f"ERROR: {p.get('error', 'poll failed')}"
            status = p.get("status")
            if status == "done":
                output = (p.get("output") or "").strip()
                return f"OK\n{output}".rstrip() if output else "OK"
            if status == "error":
                return f"ERROR: {p.get('error') or 'unknown error'}"
            if status == "cancelled":
                return "CANCELLED"
            if loop.time() > deadline:
                bridge.cancel(job_id)
                return f"ERROR: {what} timed out and was cancelled"
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            bridge.cancel(job_id)
        return "CANCELLED"


@server.tool()
async def import_assets(ctx: Context, tasks: list[dict], timeout_s: float = 900.0) -> str:
    """Import external asset files (FBX / OBJ / textures / etc.) into the project.

    Each task: ``{"filename": "C:/path/file.fbx", "destination_path": "/Game/Folder"}``
    (``destination_path`` defaults to ``/Game`` when omitted). Import runs on the
    editor's tick so Interchange imports complete safely (a plain
    ``execute_python`` call would crash the editor — see PROGRESS.md §21/22).

    Returns one line per file: ``ok: /Game/...`` or ``error: ...``.

    Example:
      import_assets([{"filename": "C:/tmp/char.fbx", "destination_path": "/Game/Chars"}])
    """
    if not tasks:
        return "Error: tasks is empty (each: filename + optional destination_path)."
    resp = bridge.import_assets(tasks)
    return await _run_native_job(resp, ctx, timeout_s, "import")


@server.tool()
async def compile_assets(ctx: Context, paths: list[str], timeout_s: float = 900.0) -> str:
    """Compile blueprint assets by package path.

    Compilation runs on the editor's tick, which avoids the game-thread
    FlushRenderingCommands deadlock that a direct ``execute_python`` compile
    triggers. Returns one line per asset: ``ok: /Game/...`` or ``error: ...``.

    Example:
      compile_assets(["/Game/Characters/MyBP.MyBP"])
    """
    if not paths:
        return "Error: paths is empty (blueprint asset paths)."
    resp = bridge.compile_assets(paths)
    return await _run_native_job(resp, ctx, timeout_s, "compile")


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
def python_search(keyword: str) -> str:
    """Fuzzy-search the ``unreal`` module namespace for names containing the
    keyword (case-insensitive).

    Returns matching top-level classes and functions, grouped and capped. Use it
    when you don't know the exact API name; follow up with
    ``python_help("unreal.<ClassName>")`` to list a class's members.

    Note: this searches *top-level* names only — a method like
    ``spawn_actor_from_class`` lives on ``EditorActorSubsystem``, so search the
    class name (e.g. ``python_search("editoractor")``) then inspect it.

    Example:  python_search("asset")  ->  EditorAssetSubsystem, load_asset, ...
    """
    if not keyword or not keyword.strip():
        return "Error: keyword is empty."

    script = f'''\
import unreal
_kw = {keyword!r}.lower()
_classes = []
_others = []
for _n in dir(unreal):
    if _kw not in _n.lower():
        continue
    _obj = getattr(unreal, _n)
    (_classes if isinstance(_obj, type) else _others).append(_n)
_classes.sort()
_others.sort()
def _show(_title, _names, _limit):
    print(_title + " (%d):" % len(_names))
    if not _names:
        print("  (none)")
        return
    print("  " + ", ".join(_names[:_limit]))
    if len(_names) > _limit:
        print("  ... and %d more" % (len(_names) - _limit))
_show("CLASSES", _classes, 40)
_show("FUNCTIONS/OTHER", _others, 40)
'''
    return _format(_run(script, prelude=False))


@server.tool()
def reset_session() -> str:
    """Clear user-defined variables from the shared REPL namespace.

    Keeps ``unreal`` and the injected ``da`` helpers. Use this when the session
    gets cluttered or a variable is interfering.
    """
    return _format(_run("da.reset(); print('session namespace reset')"))


@server.tool()
def screenshot(width: int = 1280, height: int = 720) -> Image:
    """Capture the editor viewport and return it as an image.

    Triggers a high-res screenshot, waits for it to land in the editor's
    screenshot directory, and returns the PNG as an image (visible to the AI).
    Use it to "see" the current viewport after a script changes the scene.
    """
    import time as _time

    fn = f"daunreal_{int(_time.time() * 1000)}.png"
    shot_code = (
        "import unreal\n"
        f"unreal.AutomationLibrary.take_high_res_screenshot({int(width)}, {int(height)}, {fn!r})\n"
        "print('SHOT_DIR', unreal.Paths.screen_shot_dir())\n"
    )
    resp = _run(shot_code)
    if not resp.get("ok"):
        return f"ERROR: {resp.get('error', 'screenshot trigger failed')}"

    shot_dir = None
    for ln in (resp.get("log") or "").splitlines():
        if ln.startswith("SHOT_DIR "):
            shot_dir = ln[len("SHOT_DIR "):].strip()
            break
    if not shot_dir:
        return "ERROR: could not determine screenshot directory"

    full = os.path.join(shot_dir, fn)
    deadline = _time.time() + 10.0
    while _time.time() < deadline:
        if os.path.exists(full) and os.path.getsize(full) > 0:
            return Image(path=full)
        _time.sleep(0.2)

    return "ERROR: screenshot file not found after 10s"


# --------------------------------------------------------------------------- #
# knowledge-layer resources (readable by the client on demand, no tool slot)
# --------------------------------------------------------------------------- #

@server.resource(
    "daunreal://subsystems",
    name="subsystems",
    title="常用 Editor Subsystem 对照表",
    description="常用 unreal.Editor*Subsystem 的用途与关键方法（unreal.get_editor_subsystem() 获取）。",
)
def subsystems_resource() -> str:
    return """\
# 常用 Editor Subsystem 对照表

统一获取方式：`sub = unreal.get_editor_subsystem(unreal.<类名>)`

## 关卡 Actor 操作
- **EditorActorSubsystem** — spawn / 删除 / 复制 / 选择 / 移动 actor
  - `get_all_level_actors()` / `get_selected_level_actors()`
  - `spawn_actor_from_class(cls, loc, rot)` / `spawn_actor_from_object(obj, loc, rot)`
  - `destroy_actor(actor)` / `destroy_actors(list)`
  - `duplicate_actor(actor, offset, is_world_space)` / `clear_actor_selection_set()`

## 资产操作（替代 EditorAssetLibrary）
- **EditorAssetSubsystem** — 资产 CRUD
  - `load_asset(path)` / `does_asset_exist(path)` / `do_assets_exist(list)`
  - `save_asset(path)` / `save_loaded_asset(obj)`
  - `delete_asset(path)` / `delete_loaded_asset(obj)`
  - `duplicate_asset(src, dst)` / `rename_asset(src, dst)` / `find_asset_data(path)`

## 关卡 / 编辑器状态
- **LevelEditorSubsystem** — 关卡操作 + PIE
  - `get_current_level()` / `save_current_level()` / `save_all_dirty_levels()`
  - `new_level(path)` / `load_level(path)`
  - `editor_play_simulate()` / `editor_request_begin_play()` / `editor_request_end_play()`
  - `build_light_maps()` / `eject_pilot_level_actor()`
- **UnrealEditorSubsystem** — 获取 world / 视口
  - `get_editor_world()` / `get_game_world()` / `get_world()`
  - `get_level_viewport_camera_info()`

## 网格资产编辑
- **StaticMeshEditorSubsystem** — 静态网格（替代 EditorStaticMeshLibrary）
  - `get_lod_count(mesh)` / `get_lod_build_settings(mesh, lod)` / `set_lods(...)`
  - `add_simple_collisions(mesh)` / `add_uv_channel(mesh, lod)`
- **SkeletalMeshEditorSubsystem** — 骨骼网格（替代 EditorSkeletalMeshLibrary）
  - `get_lod_count(mesh)` / `create_physics_asset(mesh)` / `assign_physics_asset(...)`

## 其他
- **EditorUtilitySubsystem** — 运行 Editor Utility Widget/Blueprint：`spawn_and_register_tab(...)` / `close_tab_by_id(id)` / `does_tab_exist(id)`
- **AssetEditorSubsystem** — 打开/关闭资产编辑器：`open_editor_for_assets(list)` / `close_all_editors_for_asset(asset)`
- **EditorValidatorSubsystem** — 数据校验（资产 / actor 规则）
"""


@server.resource(
    "daunreal://deprecated-api",
    name="deprecated-api",
    title="废弃 API → 新 API 映射",
    description="EditorScriptingUtilities 的 Editor*Library 已废弃，对应改用 Editor*Subsystem。",
)
def deprecated_api_resource() -> str:
    return """\
# 废弃 API → 新 API 映射

`EditorScriptingUtilities` 插件的 `Editor*Library` 在 5.5 已废弃（仍可用但告警）。
优先用 `unreal.get_editor_subsystem(unreal.<Subsystem>)`。

## EditorLevelLibrary → EditorActorSubsystem / LevelEditorSubsystem / UnrealEditorSubsystem
- `get_all_level_actors()` → `EditorActorSubsystem.get_all_level_actors()`
- `get_selected_level_actors()` → `EditorActorSubsystem.get_selected_level_actors()`
- `spawn_actor_from_class()` → `EditorActorSubsystem.spawn_actor_from_class()`
- `destroy_actor()` → `EditorActorSubsystem.destroy_actor()`
- `get_editor_world()` → `UnrealEditorSubsystem.get_editor_world()`
- `load_level()` / `save_current_level()` → `LevelEditorSubsystem.*`

## EditorAssetLibrary → EditorAssetSubsystem
- `load_asset` / `delete_asset` / `save_asset` / `duplicate_asset` / `rename_asset` / `does_asset_exist` / `find_asset_data` → `EditorAssetSubsystem` 同名方法

## EditorStaticMeshLibrary → StaticMeshEditorSubsystem
- `get_lod_count` / `add_simple_collisions` / `set_lods` → 同名方法

## EditorSkeletalMeshLibrary → SkeletalMeshEditorSubsystem
- `get_lod_count` / `create_physics_asset` / `regenerate_lod` → 同名方法
"""


@server.resource(
    "daunreal://conventions",
    name="conventions",
    title="DAUnreal MCP 工程约定",
    description="本 MCP 的使用约定：脚本直通、da helper、REPL 持久化、async、Undo 事务。",
)
def conventions_resource() -> str:
    return """\
# DAUnreal MCP 工程约定

- **脚本直通**：`execute_python` 直接执行 `unreal.*` Python，能力上限 = 整个 `unreal` API（本 MCP 不堆预置 tool）。
- **da helper（自动注入）**：
  - `da.dump(obj, depth=3)` / `da.dumps(obj, depth=3)` — UObject/struct/数组 → dict/JSON
  - `da.u(path)` 加载资产；`da.cls(name)` 加载类
  - `da.selected()` / `da.all_actors()` — 当前选择 / 全部关卡 actor
  - `da.set_root(tree, root)` / `da.set_variable(widget, is_var)` — EUW / UMG 控件树根节点与变量标记
  - `da.reset()` — 清空用户变量（保留 `unreal` 和 `da`）
- **REPL 持久化**：变量与 `import` 跨 `execute_python` 调用保留。
- **输出**：用 `print(...)` 返回结果。
- **异步**：`mode="async"` 时循环放顶层（不在函数/类内、非纯 comprehension）才能分片；否则回退 sync 并阻塞编辑器。
- **Undo**：每次执行包 `FScopedTransaction`，改错可 Ctrl+Z 回滚。
- **API 选择**：优先 `Editor*Subsystem`（`unreal.get_editor_subsystem`），避免废弃的 `Editor*Library`。
"""


# --------------------------------------------------------------------------- #
# script templates (prompts the client can fetch to guide the AI)
# --------------------------------------------------------------------------- #

@server.prompt(
    name="batch-process-assets",
    title="批量处理资产模板",
    description="批量遍历并处理资产（async 分片写法 + AssetRegistry + EditorAssetSubsystem）。",
)
def batch_process_assets() -> str:
    return """\
Write a script to batch-process assets in the Unreal Editor, then run it with
`execute_python(code, mode="async")`.

Key points:
- Put the loop at the TOP LEVEL of the script so `mode="async"` can split it into
  time-budgeted steps (a loop inside a function or a bare comprehension falls back
  to sync and freezes the editor).
- List assets via the AssetRegistry (non-deprecated):
      reg = unreal.AssetRegistryHelpers.get_asset_registry()
      assets = reg.get_all_assets()          # list[AssetData]
- Each AssetData exposes package_name / asset_name / asset_class; load with:
      path = str(a.package_name) + "." + str(a.asset_name)
      asset = unreal.load_asset(path)        # None if not loadable
- Inspect UObjects with da.dumps(obj); report progress with print(...).

Template:
    import unreal
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    for a in reg.get_all_assets():
        path = str(a.package_name) + "." + str(a.asset_name)
        asset = unreal.load_asset(path)
        # ... process asset ...
        print("processed", path)
"""


@server.prompt(
    name="scene-inspection",
    title="场景巡检模板",
    description="遍历当前关卡 actor 并输出可读摘要（EditorActorSubsystem + da.dump）。",
)
def scene_inspection() -> str:
    return """\
Write a script to inspect the current level's actors, then run it with
`execute_python(code)`.

Key points:
- Use EditorActorSubsystem (non-deprecated):
      subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
      actors = subsys.get_all_level_actors()
- Turn each actor into readable JSON with da.dumps(actor, depth=2).
- Print a summary (count, classes, names).

Template:
    import unreal
    subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = subsys.get_all_level_actors()
    print("actor count:", len(actors))
    for a in actors:
        print(da.dumps(a, depth=2))
"""


if __name__ == "__main__":
    server.run(transport="stdio")
