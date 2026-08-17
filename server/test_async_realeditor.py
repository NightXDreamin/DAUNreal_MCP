"""Real-editor QA for the two fixes + async end-to-end regression.

Fix A: while-body assignments now reach the shared REPL namespace
       (_collect_stmt had no ast.While branch).
Fix B: async path now injects DA_PRELUDE, so `da.*` works in async scripts.

Plus: async job lifecycle (submit/poll/cancel) sanity, and the documented
limitations are re-confirmed rather than assumed.
"""
import json
import os
import socket
import subprocess
import sys
import time

PORT = int(os.environ.get("DAUNREAL_MCP_PORT", "8765"))
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(SERVER_DIR, ".venv", "Scripts", "python.exe")

sys.path.insert(0, SERVER_DIR)
import da_async  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def raw(payload, tmo=120):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=tmo)
    s.settimeout(tmo)
    try:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        d = b""
        while True:
            c = s.recv(1)
            if not c or c == b"\n":
                break
            if c != b"\r":
                d += c
    finally:
        s.close()
    return json.loads(d.decode("utf-8")) if d else None


def call(code, tmo=120):
    return raw({"id": 1, "code": code}, tmo)


def log_of(r):
    return (r.get("log") or "") if r else ""


print("=" * 68)
print("PRE: bridge reachable + clean session")
print("=" * 68)
r = call("print('bridge ok')")
check("bridge responds", bool(r and r.get("ok")), log_of(r).strip())

# Wipe any leftover state from previous probes so `da` is genuinely absent.
call("""
for _k in [k for k in list(globals()) if k.startswith('_da') or k in ('da','_Da','_da_protected')]:
    globals().pop(_k, None)
print('session wiped; da present =', 'da' in globals())
""")
r = call("print('da present:', 'da' in globals())")
print("   ", log_of(r).strip())

print("\n" + "=" * 68)
print("FIX A: while-body assignments reach the REPL namespace")
print("=" * 68)

code_while = """counter = 0
collected = []
while counter < 4:
    doubled = counter * 2
    collected.append(doubled)
    counter += 1
"""
t = da_async.transform(code_while)
globals_line = [l for l in t.setup_code.split("\n") if "global" in l]
print("   injected:", globals_line)
check("`doubled` (while-body assign) is in the global decl",
      any("doubled" in l for l in globals_line),
      globals_line[0] if globals_line else "no global line")

# run it for real through the bridge, driving to completion
drive_all = da_async._make_drive_code(budget=None)
r = call(t.setup_code + "\n" + drive_all)
r2 = call("print('doubled =', globals().get('doubled'));"
          " print('collected =', globals().get('collected'));"
          " print('counter =', globals().get('counter'))")
out = log_of(r2)
print("   ", out.replace("\n", " | ").strip())
check("while-body var persists in editor namespace (real run)",
      "doubled = 6" in out and "counter = 4" in out, out.strip()[:120])

code_walrus = """src = [5, 7, 0]
idx = 0
taken = []
while (peek := src[idx]) != 0:
    taken.append(peek)
    idx += 1
"""
tw = da_async.transform(code_walrus)
gl = [l for l in tw.setup_code.split("\n") if "global" in l]
check("walrus in while-test is in the global decl",
      any("peek" in l for l in gl), gl[0] if gl else "none")
call(tw.setup_code + "\n" + drive_all)
r3 = call("print('peek =', globals().get('peek'), '| taken =', globals().get('taken'))")
print("   ", log_of(r3).strip())
check("walrus var persists (real run)", "peek = 0" in log_of(r3), log_of(r3).strip())

print("\n" + "=" * 68)
print("FIX B: async path injects da helpers (stdio end-to-end)")
print("=" * 68)

# Wipe da again so the async path must inject it itself.
call("""
for _k in ('da','_Da','_da_protected','_da_json'):
    globals().pop(_k, None)
print('da wiped; present =', 'da' in globals())
""")
r = call("print('da present before async:', 'da' in globals())")
print("   ", log_of(r).strip())
check("da is absent before the async run", "False" in log_of(r), log_of(r).strip())

env = dict(os.environ)
env["DAUNREAL_MCP_PORT"] = str(PORT)
proc = subprocess.Popen(
    [VENV_PY, "server.py"], cwd=SERVER_DIR,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1, env=env)

_id = [0]


def send(method, params=None, need_id=True):
    _id[0] += 1
    m = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if need_id:
        m["id"] = _id[0]
    proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    return _id[0]


def recv(want_id=None, tmo=180):
    """Read messages; collect progress notifications; return the matching result."""
    notes = []
    deadline = time.time() + tmo
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("method") == "notifications/progress":
            notes.append(msg["params"])
            continue
        if want_id is None or msg.get("id") == want_id:
            return msg, notes
    raise TimeoutError("no response")


def tool_text(msg):
    return (msg.get("result") or {}).get("content", [{}])[0].get("text", "")


try:
    send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "qa", "version": "1"}})
    recv(1)
    send("notifications/initialized", {}, need_id=False)

    # async script that USES da.* -> proves the prelude got injected
    rid = send("tools/call", {
        "name": "execute_python",
        "arguments": {
            "code": ("hits = []\n"
                     "for a in da.all_actors():\n"
                     "    hits.append(a.get_name())\n"
                     "print('collected', len(hits), 'actor names via da')\n"),
            "mode": "async",
        },
        "_meta": {"progressToken": "qa-tok-1"},
    })
    msg, notes = recv(rid)
    text = tool_text(msg)
    print("   result:", text.replace("\n", " | ")[:160])
    print(f"   progress notifications: {len(notes)}")
    check("async script can use da.* (prelude injected)",
          text.startswith("OK") and "via da" in text, text[:160])
    check("progress notifications delivered", len(notes) > 0,
          f"{len(notes)} notifications, first={notes[0] if notes else None}")

    # regression: sync mode still fine
    rid = send("tools/call", {"name": "execute_python",
                              "arguments": {"code": "print(1+1)"}})
    msg, _ = recv(rid)
    check("sync mode regression", tool_text(msg).startswith("OK"), tool_text(msg)[:80])

    # async with an error -> traceback line mapped back to user code
    rid = send("tools/call", {
        "name": "execute_python",
        "arguments": {"code": "acc = 0\nfor i in range(5):\n    acc += 1\n    raise ValueError('deliberate')\n",
                      "mode": "async"},
        "_meta": {"progressToken": "qa-tok-2"},
    })
    msg, _ = recv(rid)
    text = tool_text(msg)
    print("   error text:", text.replace("\n", " | ")[:200])
    check("async error surfaces traceback", "ValueError" in text and "deliberate" in text,
          text[:150])

    # no-loop script in async mode -> falls back to sync, must still work
    rid = send("tools/call", {
        "name": "execute_python",
        "arguments": {"code": "print('no loop here')", "mode": "async"},
    })
    msg, _ = recv(rid)
    check("async fallback for no-loop script", tool_text(msg).startswith("OK"),
          tool_text(msg)[:80])
finally:
    try:
        proc.stdin.close()
    except Exception:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

print("\n" + "=" * 68)
print("ASYNC JOB LIFECYCLE (direct protocol)")
print("=" * 68)

long_code = "spin = 0\nfor i in range(3000000):\n    spin += 1\n"
tl = da_async.transform(long_code)
sub = raw({"id": 1, "action": "execute", "mode": "async",
           "setup_code": tl.setup_code, "step_code": tl.step_code})
check("submit returns job_id", bool(sub and sub.get("ok") and sub.get("job_id") is not None),
      str(sub)[:120])
job = sub.get("job_id")

# editor must stay responsive while the job runs
lat = []
for _ in range(6):
    t0 = time.perf_counter()
    call("pass")
    lat.append((time.perf_counter() - t0) * 1000)
    time.sleep(0.15)
print(f"   sync ping latency during job: min={min(lat):.1f}ms max={max(lat):.1f}ms")
check("editor stays responsive during async job", max(lat) < 400,
      f"max {max(lat):.1f}ms")

p = raw({"id": 1, "action": "poll", "job_id": job})
print(f"   poll: status={p.get('status')} slices={p.get('slices_done')}")
check("poll reports progress", p.get("slices_done", 0) > 0, str(p)[:120])

c = raw({"id": 1, "action": "cancel", "job_id": job})
check("cancel accepted", bool(c and c.get("ok")), str(c)[:100])
time.sleep(0.6)
p2 = raw({"id": 1, "action": "poll", "job_id": job})
print(f"   after cancel: status={p2.get('status')}")
check("job reaches cancelled state", p2.get("status") == "cancelled", str(p2)[:120])

# unknown job id must not crash the bridge
bad = raw({"id": 1, "action": "poll", "job_id": 999999})
check("unknown job_id handled gracefully", bad is not None and bad.get("ok") is False,
      str(bad)[:100])
r = call("print('bridge still alive')")
check("bridge alive after bad request", "alive" in log_of(r), log_of(r).strip())

print("\n" + "=" * 68)
print("CLEANUP")
print("=" * 68)
r = call("""
for _k in [k for k in list(globals()) if k.startswith('_da')] + [
        'counter','collected','doubled','src','idx','taken','peek','hits','spin','i','a','acc']:
    globals().pop(_k, None)
print('probe symbols removed')
""")
print("   ", log_of(r).strip())

print("\n" + "=" * 68)
print(f"SUMMARY: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
