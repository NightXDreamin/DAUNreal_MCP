"""Knowledge-layer QA: verify the *content* is factually correct, not just that
the endpoints respond.

A knowledge layer that lists non-existent APIs is worse than no knowledge layer:
it actively misleads the AI. So every class and method named in the resources and
prompt templates is checked against the live `unreal` module.

Sections:
  A. MCP surface: resources/list, resources/read, prompts/list, prompts/get,
     tools/list all work over stdio.
  B. Every Subsystem class named in daunreal://subsystems really exists.
  C. Every method named under each Subsystem really exists on that class.
  D. Every deprecated->new mapping in daunreal://deprecated-api is accurate
     (old name exists AND new target exists).
  E. Prompt templates are executable as-written (the AssetRegistry snippet and
     the scene-inspection snippet actually run).
  F. python_search behaviour: hit, miss, case-insensitivity, empty input,
     cap/grouping, and the documented top-level-only limitation.
  G. Claims made in daunreal://conventions hold (da.* helpers exist).
"""
import json
import os
import re
import socket
import subprocess
import sys
import time

PORT = int(os.environ.get("DAUNREAL_MCP_PORT", "8765"))
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(SERVER_DIR, ".venv", "Scripts", "python.exe")

PASS, FAIL, WARN = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def warn(name, detail=""):
    WARN.append(name)
    print(f"[WARN] {name}" + (f"  -- {detail}" if detail else ""))


def _auth_token():
    """Read the bridge auth token the same way server.py does.

    Phase 6 added token auth, so bare bridge calls must carry it. Resolution
    order: DAUNREAL_MCP_ENDPOINT, then the endpoint.json the plugin writes under
    the project's Saved/DAUnrealMCP/. Returns "" when auth is disabled.
    """
    candidates = [os.environ.get("DAUNREAL_MCP_ENDPOINT", "")]
    for proj in (r"C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest55",
                 r"C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest"):
        candidates.append(os.path.join(proj, "Saved", "DAUnrealMCP", "endpoint.json"))
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if int(data.get("port", -1)) == PORT and data.get("token"):
                return data["token"]
        except (OSError, ValueError):
            continue
    return ""


AUTH_TOKEN = _auth_token()


def raw(payload, tmo=120):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=tmo)
    s.settimeout(tmo)
    try:
        if AUTH_TOKEN and "token" not in payload:
            payload = {**payload, "token": AUTH_TOKEN}
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
    r = raw({"id": 1, "code": code}, tmo)
    return (r.get("log") or "") if r else "", (r.get("error") or "") if r else "NO RESP"


def call_with_prelude(code, tmo=120):
    """Mirror execute_python: inject the da prelude, as the real tool does.

    A bare bridge call has no `da` — only execute_python (prelude=True) provides
    it. Probes that exercise `da.*` must go through this path or they test a
    situation no real client is ever in.
    """
    sys.path.insert(0, SERVER_DIR)
    import server as _srv
    return call(_srv.DA_PRELUDE + chr(10) + code, tmo)


# --------------------------------------------------------------------------- #
print("=" * 72)
print("SECTION A: MCP surface over stdio (resources / prompts / tools)")
print("=" * 72)

env = dict(os.environ)
env["DAUNREAL_MCP_PORT"] = str(PORT)
if AUTH_TOKEN and not env.get("DAUNREAL_MCP_ENDPOINT"):
    # the spawned server needs the endpoint file to authenticate too
    for _proj in (r"C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest55",
                  r"C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest"):
        _ep = os.path.join(_proj, "Saved", "DAUnrealMCP", "endpoint.json")
        if os.path.exists(_ep):
            env["DAUNREAL_MCP_ENDPOINT"] = _ep
            break
proc = subprocess.Popen(
    [VENV_PY, "server.py"], cwd=SERVER_DIR,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1, env=env)
_id = [0]
RESOURCES = {}
PROMPTS = {}


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


try:
    send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "qa-knowledge", "version": "1"}})
    init = recv(1)
    caps = (init.get("result") or {}).get("capabilities", {})
    check("server advertises resources capability", "resources" in caps, str(list(caps)))
    check("server advertises prompts capability", "prompts" in caps)
    send("notifications/initialized", {}, need_id=False)

    rid = send("tools/list", {})
    tools = [t["name"] for t in recv(rid)["result"]["tools"]]
    print("   tools:", tools)
    check("python_search registered", "python_search" in tools, str(tools))
    check("tool count is 4 (still a lean surface)", len(tools) == 4, f"{len(tools)}: {tools}")

    rid = send("resources/list", {})
    res = recv(rid)["result"]["resources"]
    uris = [r["uri"] for r in res]
    print("   resources:", uris)
    for want in ("daunreal://subsystems", "daunreal://deprecated-api",
                 "daunreal://conventions"):
        check(f"resource listed: {want}", want in uris)

    for uri in uris:
        rid = send("resources/read", {"uri": uri})
        contents = recv(rid)["result"]["contents"]
        text = contents[0].get("text", "")
        RESOURCES[uri] = text
        check(f"resources/read returns content: {uri}", len(text) > 200,
              f"{len(text)} chars")

    rid = send("prompts/list", {})
    prompts = recv(rid)["result"]["prompts"]
    pnames = [p["name"] for p in prompts]
    print("   prompts:", pnames)
    for want in ("batch-process-assets", "scene-inspection"):
        check(f"prompt listed: {want}", want in pnames)

    for pname in pnames:
        rid = send("prompts/get", {"name": pname})
        msgs = recv(rid)["result"]["messages"]
        c = msgs[0].get("content")
        text = c.get("text", "") if isinstance(c, dict) else str(c)
        PROMPTS[pname] = text
        check(f"prompts/get returns text: {pname}", len(text) > 100, f"{len(text)} chars")
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

# --------------------------------------------------------------------------- #
print("\n" + "=" * 72)
print("SECTION B/C: does every class+method named in the resources exist?")
print("=" * 72)

log, err = call("print('bridge ok')")
check("bridge reachable for fact-checking", "bridge ok" in log, err or log)

subsys_doc = RESOURCES.get("daunreal://subsystems", "")
# classes appear as **Name** in the markdown
claimed_classes = sorted(set(re.findall(r"\*\*(\w*Subsystem)\*\*", subsys_doc)))
print(f"   classes claimed in daunreal://subsystems: {claimed_classes}")
check("resource actually names some subsystems", len(claimed_classes) >= 8,
      f"{len(claimed_classes)} found")

code = f"""
import unreal
_claimed = {claimed_classes!r}
_missing = [c for c in _claimed if not hasattr(unreal, c)]
print('MISSING_CLASSES:' + (','.join(_missing) if _missing else 'NONE'))
"""
log, err = call(code)
missing_line = [l for l in log.split("\n") if l.startswith("MISSING_CLASSES:")]
missing = missing_line[0].split(":", 1)[1].strip() if missing_line else "?"
check("every claimed Subsystem class exists in unreal", missing == "NONE",
      f"missing={missing}")

# methods: lines like "  - `method_name(...)` / `other(...)`" under a **Class**
method_map = {}
current = None
for line in subsys_doc.split("\n"):
    m = re.match(r"\s*-\s+\*\*(\w+)\*\*", line)
    if m:
        current = m.group(1)
        method_map.setdefault(current, [])
        continue
    if current:
        # collect `name(` occurrences on indented sub-bullets
        if re.match(r"\s+-\s+`", line) or re.match(r"\s*-\s+\*\*\w+\*\*.*`", line):
            for name in re.findall(r"`(\w+)\s*\(", line):
                method_map[current].append(name)
        # inline methods on the class line itself
        for name in re.findall(r"`(\w+)\s*\(", line):
            if name not in method_map[current]:
                method_map[current].append(name)
total_methods = sum(len(v) for v in method_map.values())
print(f"   methods claimed: {total_methods} across {len(method_map)} classes")

code = f"""
import unreal
_mm = {json.dumps(method_map)}
_bad = []
for _cls, _methods in _mm.items():
    _c = getattr(unreal, _cls, None)
    if _c is None:
        _bad.append(_cls + ':<CLASS MISSING>')
        continue
    _have = set(dir(_c))
    for _m in _methods:
        if _m not in _have:
            _bad.append(_cls + '.' + _m)
print('BAD_METHODS_COUNT:' + str(len(_bad)))
for _b in _bad:
    print('BAD:' + _b)
"""
log, err = call(code)
bad = [l[4:] for l in log.split("\n") if l.startswith("BAD:")]
cnt_line = [l for l in log.split("\n") if l.startswith("BAD_METHODS_COUNT:")]
print(f"   verified against live unreal module -> {len(bad)} bad entries")
for b in bad:
    print(f"      MISMATCH: {b}")
check("every method named in daunreal://subsystems exists on its class",
      len(bad) == 0, f"{len(bad)} bad: {bad[:8]}")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 72)
print("SECTION D: deprecated-api mappings are accurate")
print("=" * 72)

dep_doc = RESOURCES.get("daunreal://deprecated-api", "")
old_classes = sorted(set(re.findall(r"(Editor\w*Library)", dep_doc)))
new_classes = sorted({c for c in re.findall(r"(\w*Subsystem)", dep_doc) if c != "Subsystem"})
print(f"   old classes referenced: {old_classes}")
print(f"   new classes referenced: {new_classes}")

code = f"""
import unreal
_old = {old_classes!r}
_new = {new_classes!r}
_bad_old = [c for c in _old if not hasattr(unreal, c)]
_bad_new = [c for c in _new if not hasattr(unreal, c)]
print('OLD_MISSING:' + (','.join(_bad_old) if _bad_old else 'NONE'))
print('NEW_MISSING:' + (','.join(_bad_new) if _bad_new else 'NONE'))
"""
log, err = call(code)
old_missing = next((l.split(":", 1)[1].strip() for l in log.split("\n") if l.startswith("OLD_MISSING:")), "?")
new_missing = next((l.split(":", 1)[1].strip() for l in log.split("\n") if l.startswith("NEW_MISSING:")), "?")
check("deprecated (old) classes referenced really exist", old_missing == "NONE",
      f"missing={old_missing}")
check("replacement (new) Subsystem classes really exist", new_missing == "NONE",
      f"missing={new_missing}")

# spot-check a few concrete mappings actually resolve on both sides
code = """
import unreal
pairs = [
    ('EditorLevelLibrary', 'get_all_level_actors', 'EditorActorSubsystem', 'get_all_level_actors'),
    ('EditorLevelLibrary', 'spawn_actor_from_class', 'EditorActorSubsystem', 'spawn_actor_from_class'),
    ('EditorLevelLibrary', 'destroy_actor', 'EditorActorSubsystem', 'destroy_actor'),
    ('EditorLevelLibrary', 'get_editor_world', 'UnrealEditorSubsystem', 'get_editor_world'),
    ('EditorAssetLibrary', 'load_asset', 'EditorAssetSubsystem', 'load_asset'),
    ('EditorAssetLibrary', 'duplicate_asset', 'EditorAssetSubsystem', 'duplicate_asset'),
    ('EditorAssetLibrary', 'find_asset_data', 'EditorAssetSubsystem', 'find_asset_data'),
]
bad = []
for oc, om, nc, nm in pairs:
    o = getattr(unreal, oc, None)
    n = getattr(unreal, nc, None)
    if o is None or om not in dir(o):
        bad.append('OLD ' + oc + '.' + om)
    if n is None or nm not in dir(n):
        bad.append('NEW ' + nc + '.' + nm)
print('PAIR_BAD:' + str(len(bad)))
for b in bad:
    print('PB:' + b)
"""
log, err = call(code)
pb = [l[3:] for l in log.split("\n") if l.startswith("PB:")]
check("spot-checked old->new method pairs all resolve", len(pb) == 0, str(pb))

# --------------------------------------------------------------------------- #
print("\n" + "=" * 72)
print("SECTION E: prompt templates are executable as written")
print("=" * 72)

# batch-process-assets: the AssetRegistry + package_name/asset_name pattern
code = """
import unreal
reg = unreal.AssetRegistryHelpers.get_asset_registry()
assets = reg.get_all_assets()
print('asset count:', len(assets))
a = assets[0]
path = str(a.package_name) + "." + str(a.asset_name)
obj = unreal.load_asset(path)
print('sample path:', path)
print('loaded:', obj is not None)
print('has package_name/asset_name/asset_class:',
      hasattr(a, 'package_name'), hasattr(a, 'asset_name'), hasattr(a, 'asset_class'))
"""
log, err = call(code, tmo=180)
print("   " + log.replace("\n", " | ").strip()[:220])
check("prompt template: AssetRegistry snippet runs", "asset count:" in log, err[:150])
check("prompt template: load_asset(package.asset) works", "loaded: True" in log,
      log.replace("\n", " | ")[:200])
check("prompt template: AssetData attrs exist as documented",
      "True True True" in log.replace("\n", " "),
      [l for l in log.split("\n") if "has package" in l])

# the doc claims AssetData has NO object_path -> confirm that claim is still true
code = """
import unreal
reg = unreal.AssetRegistryHelpers.get_asset_registry()
a = reg.get_all_assets()[0]
print('object_path present:', hasattr(a, 'object_path'))
"""
log, err = call(code)
has_op = "object_path present: True" in log
if has_op:
    warn("PROGRESS.md claims AssetData has no object_path, but it DOES here",
         log.strip())
else:
    check("documented pitfall holds: AssetData has no object_path", True, log.strip())

# scene-inspection template
code = """
import unreal
subsys = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actors = subsys.get_all_level_actors()
print("actor count:", len(actors))
print(da.dumps(actors[0], depth=2)[:120])
"""
log, err = call_with_prelude(code)
print("   " + log.replace("\n", " | ").strip()[:200])
check("prompt template: scene-inspection snippet runs",
      "actor count:" in log and '"type"' in log, err[:150])

# --------------------------------------------------------------------------- #
print("\n" + "=" * 72)
print("SECTION F: python_search behaviour")
print("=" * 72)


def search(keyword):
    """Reproduce the tool's script exactly (same code path as server.py)."""
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
    return call(script)


log, err = search("editoractor")
print("   editoractor ->", log.replace("\n", " | ")[:170])
check("python_search finds EditorActorSubsystem", "EditorActorSubsystem" in log, err[:120])

log, err = search("EDITORACTOR")
check("python_search is case-insensitive", "EditorActorSubsystem" in log, log[:120])

log, err = search("zzzzz_no_such_api")
check("python_search miss -> reports (none) not an error",
      "(none)" in log and "CLASSES (0)" in log, log.replace("\n", " | ")[:150])

log, err = search("a")
m = re.search(r"CLASSES \((\d+)\)", log)
n_classes = int(m.group(1)) if m else -1
truncated = "and" in log and "more" in log
print(f"   broad search 'a' -> {n_classes} classes, truncated={truncated}")
check("broad search is capped (does not flood context)", truncated and n_classes > 40,
      f"classes={n_classes}, truncated={truncated}")
check("capped output stays small", len(log) < 4000, f"{len(log)} chars")

# documented limitation: method names are NOT findable
log, err = search("spawn_actor_from_class")
check("documented limit: method names not found by python_search (top-level only)",
      "CLASSES (0)" in log and "(none)" in log,
      log.replace("\n", " | ")[:150])

# --------------------------------------------------------------------------- #
print("\n" + "=" * 72)
print("SECTION G: conventions resource claims hold")
print("=" * 72)

conv = RESOURCES.get("daunreal://conventions", "")
claimed_da = sorted(set(re.findall(r"`da\.(\w+)\(", conv)))
print(f"   da helpers claimed: {claimed_da}")
code = f"""
_claimed = {claimed_da!r}
_missing = [m for m in _claimed if not hasattr(da, m)]
print('DA_MISSING:' + (','.join(_missing) if _missing else 'NONE'))
"""
log, err = call_with_prelude(code)
da_missing = next((l.split(":", 1)[1].strip() for l in log.split("\n") if l.startswith("DA_MISSING:")), "?")
check("every da.* helper named in conventions exists", da_missing == "NONE",
      f"missing={da_missing}")

# reset_session must survive being called twice: its implementation calls
# da.reset(), so `da` must not delete itself (otherwise the 2nd call NameErrors).
log, err = call_with_prelude("""
leftover_var = 'should vanish'
da.reset()
print('da survived reset:', 'da' in globals())
print('unreal survived reset:', 'unreal' in globals())
print('user var cleared:', 'leftover_var' not in globals())
""")
print("   " + log.replace(chr(10), " | ").strip()[:180])
check("reset_session is re-callable: da survives da.reset()",
      "da survived reset: True" in log, err[:150] or log[:150])
check("reset_session keeps unreal", "unreal survived reset: True" in log, log[:150])
check("reset_session actually clears user vars", "user var cleared: True" in log,
      log[:150])

# REPL persistence claim
call("qa_conv_probe = 'persisted'")
log, err = call("print(globals().get('qa_conv_probe'))")
check("conventions claim: REPL persistence works", "persisted" in log, log.strip())

# Undo claim: transaction wraps execution
log, err = call("""
import unreal
sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
_a = sub.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0,0,700), unreal.Rotator())
print('spawned:', _a.get_name())
sub.destroy_actor(_a)
print('cleaned up')
""")
check("conventions claim: spawn/destroy via Subsystem works (undo-wrapped path)",
      "spawned:" in log and "cleaned up" in log, err[:150])

print("\n" + "=" * 72)
print("CLEANUP")
print("=" * 72)
log, err = call("""
for _k in ('qa_conv_probe','_a','_claimed','_missing','_mm','_bad','_old','_new',
           'reg','assets','a','path','obj','subsys','actors','sub','pairs','bad'):
    globals().pop(_k, None)
print('probe symbols removed')
""")
print("   " + log.strip())

print("\n" + "=" * 72)
print(f"SUMMARY: {len(PASS)} passed, {len(FAIL)} failed, {len(WARN)} warnings")
for f in FAIL:
    print(f"  FAILED: {f}")
for w in WARN:
    print(f"  WARN:   {w}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
