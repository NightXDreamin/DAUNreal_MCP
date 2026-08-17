"""Real-editor QA for phase 6: token auth + audit log (+ dry_run non-execution).

Auth is the security-relevant part, so the probes try to BYPASS it rather than
just confirm the happy path: no token, wrong token, empty token, token on the
wrong field, and auth coverage across every action (execute/poll/cancel).

The audit log is the accountability part: every execution must land in
history.jsonl with the ORIGINAL user code (not the AST-transformed generator),
including failures and async jobs.
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


def _find_saved_dir():
    """Locate the project's Saved/DAUnrealMCP for the editor we are testing.

    DAUNREAL_MCP_ENDPOINT wins; otherwise pick the project whose endpoint.json
    records the port under test (8765 = 5.4 project, 8766 = 5.5 project).
    """
    explicit = os.environ.get("DAUNREAL_MCP_ENDPOINT", "")
    if explicit and os.path.exists(explicit):
        return os.path.dirname(explicit)
    base = r"C:\Users\qingpulou\Documents\Unreal Projects"
    for proj in ("DAUNrealTest55", "DAUNrealTest"):
        d = os.path.join(base, proj, "Saved", "DAUnrealMCP")
        ep = os.path.join(d, "endpoint.json")
        if os.path.exists(ep):
            try:
                with open(ep, "r", encoding="utf-8") as fh:
                    if int(json.load(fh).get("port", -1)) == PORT:
                        return d
            except (OSError, ValueError):
                continue
    return os.path.join(base, "DAUNrealTest55", "Saved", "DAUnrealMCP")


PROJ_SAVED = _find_saved_dir()
ENDPOINT = os.path.join(PROJ_SAVED, "endpoint.json")
HISTORY = os.path.join(PROJ_SAVED, "history.jsonl")

sys.path.insert(0, SERVER_DIR)

PASS, FAIL, ISSUE = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def issue(name, detail=""):
    ISSUE.append(name)
    print(f"[ISSUE] {name}" + (f"  -- {detail}" if detail else ""))


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


print("=" * 72)
print("SECTION A: endpoint.json contract")
print("=" * 72)

check("endpoint.json exists", os.path.exists(ENDPOINT), ENDPOINT)
TOKEN = ""
if os.path.exists(ENDPOINT):
    with open(ENDPOINT, "r", encoding="utf-8") as fh:
        ep = json.load(fh)
    TOKEN = ep.get("token", "")
    print(f"   endpoint.json: port={ep.get('port')} token={TOKEN[:8]}...")
    check("endpoint.json has a token", bool(TOKEN))
    check("endpoint.json port matches the bridge we are testing",
          int(ep.get("port", -1)) == PORT, f"file={ep.get('port')} testing={PORT}")
    check("token looks like a GUID (36 chars with hyphens)",
          len(TOKEN) == 36 and TOKEN.count("-") == 4, f"len={len(TOKEN)}")

print("\n" + "=" * 72)
print("SECTION B: auth cannot be bypassed")
print("=" * 72)

r = raw({"id": 1, "code": "print('no token')"})
check("request WITHOUT token is rejected",
      r and r.get("ok") is False and "unauthorized" in (r.get("error") or ""),
      str(r)[:140])

r = raw({"id": 1, "code": "print('wrong')", "token": "not-the-real-token"})
check("request with WRONG token is rejected",
      r and r.get("ok") is False and "unauthorized" in (r.get("error") or ""),
      str(r)[:140])

r = raw({"id": 1, "code": "print('empty')", "token": ""})
check("request with EMPTY token is rejected",
      r and r.get("ok") is False and "unauthorized" in (r.get("error") or ""),
      str(r)[:140])

r = raw({"id": 1, "code": "print('case')", "token": TOKEN.upper()})
r2 = raw({"id": 1, "code": "print('case')", "token": TOKEN.lower()})
# FString::operator== is case-INsensitive; using it would make the GUID's hex
# digits interchangeable (A == a) and needlessly shrink the guessing space.
# IsAuthorized must use Equals(..., ESearchCase::CaseSensitive).
wrong_case_accepted = [
    lbl for lbl, resp in (("UPPER", r), ("lower", r2))
    if resp and resp.get("ok") and TOKEN != (TOKEN.upper() if lbl == "UPPER" else TOKEN.lower())
]
check("REGRESSION: token comparison is case-SENSITIVE", not wrong_case_accepted,
      f"wrong-case tokens accepted: {wrong_case_accepted}")

r = raw({"id": 1, "code": "print('prefix')", "token": TOKEN[:-1]})
check("truncated token is rejected", r and r.get("ok") is False, str(r)[:100])

r = raw({"id": 1, "code": "print('authed ok')", "token": TOKEN})
check("request WITH correct token succeeds",
      r and r.get("ok") and "authed ok" in (r.get("log") or ""), str(r)[:140])

# auth must guard EVERY action, not just execute
r = raw({"id": 1, "action": "poll", "job_id": 1})
check("poll without token is rejected",
      r and r.get("ok") is False and "unauthorized" in (r.get("error") or ""),
      str(r)[:120])
r = raw({"id": 1, "action": "cancel", "job_id": 1})
check("cancel without token is rejected",
      r and r.get("ok") is False and "unauthorized" in (r.get("error") or ""),
      str(r)[:120])
r = raw({"id": 1, "action": "execute", "mode": "async",
         "setup_code": "x=1", "step_code": "pass"})
check("async submit without token is rejected",
      r and r.get("ok") is False and "unauthorized" in (r.get("error") or ""),
      str(r)[:120])

# malformed JSON must not leak execution
r = raw({"id": 1, "token": TOKEN})   # no code field at all
check("authed request with no code does not crash the bridge", r is not None,
      str(r)[:120])
r = raw({"id": 1, "code": "print('alive after malformed')", "token": TOKEN})
check("bridge still alive after odd requests",
      r and r.get("ok") and "alive" in (r.get("log") or ""), str(r)[:120])


def call(code, tmo=120):
    """Authenticated bare call."""
    r = raw({"id": 1, "code": code, "token": TOKEN}, tmo)
    return (r.get("log") or "") if r else "", (r.get("error") or "") if r else "NO RESP"


print("\n" + "=" * 72)
print("SECTION C: audit log (history.jsonl)")
print("=" * 72)


def read_history():
    if not os.path.exists(HISTORY):
        return []
    out = []
    with open(HISTORY, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"_unparsable": line[:80]})
    return out


before = read_history()
print(f"   history entries before: {len(before)}")

MARK = f"qa_audit_marker_{int(time.time())}"
call(f"print('{MARK}')")
time.sleep(0.4)
after = read_history()
new = after[len(before):]
print(f"   new entries: {len(new)}")
check("successful sync execution is logged", any(MARK in (e.get("code") or "") for e in new),
      f"{len(new)} new entries")

entry = next((e for e in new if MARK in (e.get("code") or "")), None)
if entry:
    check("log entry has ts/mode/code/ok fields",
          all(k in entry for k in ("ts", "mode", "code", "ok")), str(entry)[:160])
    check("log entry mode is 'sync'", entry.get("mode") == "sync", str(entry.get("mode")))
    check("log entry ok is True", entry.get("ok") is True, str(entry.get("ok")))
    check("every history line is valid JSON",
          not any("_unparsable" in e for e in after), "found unparsable lines")

# failures must be logged too — an audit log that only records successes is useless
before = read_history()
FAILMARK = f"qa_fail_marker_{int(time.time())}"
call(f"print('{FAILMARK}'); raise ValueError('deliberate audit failure')")
time.sleep(0.4)
new = read_history()[len(before):]
fail_entry = next((e for e in new if FAILMARK in (e.get("code") or "")), None)
check("FAILED execution is also logged", fail_entry is not None, f"{len(new)} new entries")
if fail_entry:
    check("failed entry has ok=False", fail_entry.get("ok") is False,
          str(fail_entry.get("ok")))
    check("failed entry captures the error", bool(fail_entry.get("error")),
          str(fail_entry.get("error"))[:100])

# async must log the ORIGINAL code, not the transformed generator
import da_async  # noqa: E402

before = read_history()
ASYNCMARK = f"qa_async_marker_{int(time.time())}"
orig_code = f"vals = []\nfor i in range(5):\n    vals.append(i)  # {ASYNCMARK}"
t = da_async.transform(orig_code)
r = raw({"id": 1, "action": "execute", "mode": "async", "code": orig_code,
         "setup_code": t.setup_code, "step_code": t.step_code, "token": TOKEN})
job = r.get("job_id") if r else None
check("async submit with token succeeds", r and r.get("ok"), str(r)[:120])
for _ in range(20):
    time.sleep(0.3)
    p = raw({"id": 1, "action": "poll", "job_id": job, "token": TOKEN})
    if p and p.get("status") in ("done", "error", "cancelled"):
        break
new = read_history()[len(before):]
async_entry = next((e for e in new if ASYNCMARK in (e.get("code") or "")), None)
check("async execution is logged with the ORIGINAL code",
      async_entry is not None, f"{len(new)} new entries; "
      f"codes={[str(e.get('code'))[:40] for e in new]}")
if async_entry:
    check("async log entry mode is 'async'", async_entry.get("mode") == "async",
          str(async_entry.get("mode")))
    check("async log does NOT contain the transformed generator",
          "_da_gen" not in (async_entry.get("code") or ""),
          "transformed code leaked into the audit log")

# unicode + very large scripts in the log
before = read_history()
call("print('审计中文测试 ok')")
time.sleep(0.3)
new = read_history()[len(before):]
uni = next((e for e in new if "审计中文" in (e.get("code") or "")), None)
check("unicode code is logged without corruption", uni is not None,
      f"codes={[str(e.get('code'))[:30] for e in new]}")

before = read_history()
big = "x = 0\n" + "".join(f"x += {i}\n" for i in range(3000))
call(big)
time.sleep(0.5)
new = read_history()[len(before):]
big_entry = new[-1] if new else None
if big_entry:
    logged_len = len(big_entry.get("code") or "")
    print(f"   large script: sent {len(big)} chars, logged {logged_len} chars")
    check("large script is logged (single valid JSON line)",
          logged_len > 1000 and "_unparsable" not in big_entry, f"{logged_len} chars")
    # history.jsonl is append-only, so an unbounded body grows it without limit.
    check("REGRESSION: oversized script body is truncated in the audit log",
          logged_len < len(big) and "truncated" in (big_entry.get("code") or ""),
          f"sent {len(big)} chars, logged {logged_len} — no cap applied")

print("\n" + "=" * 72)
print("SECTION D: dry_run through the real MCP (must not touch the editor)")
print("=" * 72)

log, err = call("import unreal\n"
                "sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
                "print('BEFORE', len(sub.get_all_level_actors()))")
n_before = int(log.split("BEFORE")[1].strip().split()[0]) if "BEFORE" in log else -1
print(f"   actor count before: {n_before}")

env = dict(os.environ)
env["DAUNREAL_MCP_PORT"] = str(PORT)
env["DAUNREAL_MCP_ENDPOINT"] = ENDPOINT
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


def recv(want_id=None, tmo=120):
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
        if msg.get("method", "").startswith("notifications/"):
            continue
        if want_id is None or msg.get("id") == want_id:
            return msg
    raise TimeoutError("no response")


def text_of(msg):
    return (msg.get("result") or {}).get("content", [{}])[0].get("text", "")


try:
    send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "qa-p6", "version": "1"}})
    recv(1)
    send("notifications/initialized", {}, need_id=False)

    destructive = ("import unreal\n"
                   "sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
                   "for a in sub.get_all_level_actors():\n"
                   "    sub.destroy_actor(a)\n")
    rid = send("tools/call", {"name": "execute_python",
                              "arguments": {"code": destructive, "dry_run": True}})
    rep = text_of(recv(rid))
    print("   dry_run report:")
    for ln in rep.split("\n")[:9]:
        print(f"      {ln}")
    check("dry_run flags destroy_actor", "destroy_actor" in rep, rep[:120])
    check("dry_run reports HIGH risk", "risk: HIGH" in rep, rep[-120:])
    check("dry_run states it did not execute", "was NOT executed" in rep)
    check("dry_run includes the not-a-sandbox caveat", "not a sandbox" in rep)

    # the level must be untouched
    log, err = call("import unreal\n"
                    "sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
                    "print('AFTER', len(sub.get_all_level_actors()))")
    n_after = int(log.split("AFTER")[1].strip().split()[0]) if "AFTER" in log else -2
    print(f"   actor count after dry_run: {n_after}")
    check("dry_run did NOT destroy anything", n_after == n_before and n_after > 0,
          f"{n_before} -> {n_after}")

    # dry_run must not be logged as an execution (it never reached the editor)
    before = read_history()
    DRYMARK = f"qa_dry_marker_{int(time.time())}"
    rid = send("tools/call", {"name": "execute_python",
                              "arguments": {"code": f"print('{DRYMARK}')",
                                            "dry_run": True}})
    recv(rid)
    time.sleep(0.4)
    new = read_history()[len(before):]
    check("dry_run is not recorded in the audit log",
          not any(DRYMARK in (e.get("code") or "") for e in new),
          f"{len(new)} new entries")

    # server must pick up the token from endpoint.json automatically
    rid = send("tools/call", {"name": "execute_python",
                              "arguments": {"code": "print('server auth ok')"}})
    out = text_of(recv(rid))
    check("server authenticates automatically via DAUNREAL_MCP_ENDPOINT",
          out.startswith("OK") and "server auth ok" in out, out[:140])

    # and without the env var it should hit unauthorized (auth is really enforced)
    proc2 = subprocess.Popen(
        [VENV_PY, "server.py"], cwd=SERVER_DIR,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
        env={**os.environ, "DAUNREAL_MCP_PORT": str(PORT),
             "DAUNREAL_MCP_ENDPOINT": ""})
    try:
        def send2(method, params=None, nid=True, i=[0]):
            i[0] += 1
            m = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if nid:
                m["id"] = i[0]
            proc2.stdin.write(json.dumps(m) + "\n")
            proc2.stdin.flush()
            return i[0]

        def recv2(want, tmo=60):
            dl = time.time() + tmo
            while time.time() < dl:
                ln = proc2.stdout.readline()
                if not ln:
                    break
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    m = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if m.get("method", "").startswith("notifications/"):
                    continue
                if m.get("id") == want:
                    return m
            raise TimeoutError()

        send2("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "qa-noauth", "version": "1"}})
        recv2(1)
        send2("notifications/initialized", {}, nid=False)
        rid2 = send2("tools/call", {"name": "execute_python",
                                    "arguments": {"code": "print('should not run')"}})
        out2 = text_of(recv2(rid2))
        check("server WITHOUT endpoint env var gets unauthorized",
              "unauthorized" in out2.lower(), out2[:140])
    finally:
        try:
            proc2.stdin.close()
        except Exception:
            pass
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()
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

print("\n" + "=" * 72)
print("CLEANUP")
print("=" * 72)
log, err = call("""
for _k in [k for k in list(globals()) if k.startswith('_da') or k in
           ('sub','vals','i','x','a')]:
    globals().pop(_k, None)
print('probe symbols removed')
""")
print("   " + log.strip())

print("\n" + "=" * 72)
print(f"SUMMARY: {len(PASS)} passed, {len(FAIL)} failed, {len(ISSUE)} issues")
for f in FAIL:
    print(f"  FAILED: {f}")
for i in ISSUE:
    print(f"  ISSUE:  {i}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
