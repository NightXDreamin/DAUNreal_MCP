"""Adversarial QA for phase 6: dry_run static analysis + token handling.

dry_run is a SAFETY feature: if it says "no dangerous calls" for a script that
deletes assets, it actively creates false confidence. So the probes here try to
get destructive scripts past it, and check the token path for crash-on-malformed
input.

Sections:
  A. dry_run must not execute anything (no bridge contact at all).
  B. True positives: canonical destructive scripts must be flagged HIGH.
  C. False negatives: destructive operations the DANGEROUS set may miss.
  D. Obfuscation / escape hatches (getattr, eval, exec, __import__).
  E. Risk-level logic sanity.
  F. Robustness: syntax error, empty, huge script, unicode, nested calls.
  G. Token loading: missing file, malformed JSON, no token key, mtime cache.
"""
import ast
import json
import os
import sys
import tempfile
import textwrap

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVER_DIR)

# Import without letting the module contact anything.
import server as srv  # noqa: E402

PASS, FAIL, ISSUE = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def issue(name, detail=""):
    """A finding that is not a crash but weakens the feature's promise."""
    ISSUE.append(name)
    print(f"[ISSUE] {name}" + (f"  -- {detail}" if detail else ""))


def report(code):
    return srv._dry_run_report(code)


def flagged(code):
    """Names listed under 'dangerous calls' in the report."""
    rep = report(code)
    out = []
    collecting = False
    for line in rep.split("\n"):
        if line.startswith("dangerous calls"):
            collecting = "(none)" not in line
            continue
        if collecting:
            if line.startswith("  - "):
                out.append(line[4:].strip())
            elif not line.strip():
                break
    return out, rep


def risk_of(code):
    for line in report(code).split("\n"):
        if line.startswith("risk:"):
            return line.split(":", 1)[1].strip()
    return "?"


print("=" * 72)
print("SECTION A: dry_run must NOT execute or even contact the bridge")
print("=" * 72)

calls_made = []
orig_request = srv.UEBridge._request


def spy(self, payload):
    calls_made.append(payload)
    raise AssertionError("dry_run contacted the bridge!")


srv.UEBridge._request = spy
try:
    rep = report("import unreal\nunreal.EditorAssetLibrary.delete_asset('/Game/X')")
    check("dry_run makes zero bridge requests", len(calls_made) == 0,
          f"{len(calls_made)} requests")
    check("dry_run says it did not execute", "was NOT executed" in rep, rep[:80])
finally:
    srv.UEBridge._request = orig_request

# side-effect check: a script with an obvious local side effect must not run
probe_file = os.path.join(tempfile.gettempdir(), "da_dryrun_sideeffect.txt")
if os.path.exists(probe_file):
    os.remove(probe_file)
report(f"open({probe_file!r}, 'w').write('executed')")
check("dry_run does not execute local side effects",
      not os.path.exists(probe_file),
      "file NOT created (correct)" if not os.path.exists(probe_file)
      else "file was created => it EXECUTED")

print("\n" + "=" * 72)
print("SECTION B: true positives — canonical destructive scripts")
print("=" * 72)

cases = [
    ("delete asset", "import unreal\nunreal.EditorAssetLibrary.delete_asset('/Game/A')",
     "delete_asset"),
    ("destroy actor",
     "sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
     "sub.destroy_actor(a)", "destroy_actor"),
    ("spawn actor",
     "sub.spawn_actor_from_class(unreal.StaticMeshActor, loc, rot)",
     "spawn_actor_from_class"),
    ("set property", "obj.set_editor_property('foo', 1)", "set_editor_property"),
    ("save asset", "unreal.EditorAssetLibrary.save_asset('/Game/A')", "save_asset"),
    ("rename asset", "unreal.EditorAssetLibrary.rename_asset('/Game/A', '/Game/B')",
     "rename_asset"),
]
for label, code, want in cases:
    got, rep = flagged(code)
    check(f"flags {label}", want in got, f"flagged={got}")

got, _ = flagged("import unreal\nfor a in da.all_actors():\n    sub.destroy_actor(a)")
check("flags destructive call inside a loop", "destroy_actor" in got, str(got))

got, rep = flagged("print(len(da.all_actors()))")
check("read-only script is not flagged", got == [], f"flagged={got}")

print("\n" + "=" * 72)
print("SECTION C: false negatives — destructive ops possibly NOT in the set")
print("=" * 72)

# These all mutate the project/scene. Check which ones dry_run stays silent on.
suspects = [
    ("set_actor_location", "sub.set_actor_location(a, loc, False, True)"),
    ("set_actor_transform", "a.set_actor_transform(t, False, True)"),
    ("set_actor_rotation", "a.set_actor_rotation(r, True)"),
    ("set_actor_scale3d", "a.set_actor_scale3d(s)"),
    ("new_level (discards unsaved)", "les.new_level('/Game/NewMap')"),
    ("load_level (discards unsaved)", "les.load_level('/Game/Other')"),
    ("save_current_level", "les.save_current_level()"),
    ("save_all_dirty_levels", "les.save_all_dirty_levels()"),
    ("make_directory", "unreal.EditorAssetLibrary.make_directory('/Game/New')"),
    ("rename_directory", "unreal.EditorAssetLibrary.rename_directory('/Game/A', '/Game/B')"),
    ("delete_actor (alias)", "a.delete_actor()"),
    ("destroy_component", "c.destroy_component()"),
    ("attach_to_actor", "a.attach_to_actor(b, '', 0, 0, 0, True)"),
    ("import_asset_tasks", "unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([t])"),
    ("create_asset", "unreal.AssetToolsHelpers.get_asset_tools().create_asset(n, p, c, f)"),
    ("set_editor_properties", "obj.set_editor_properties({'a': 1})"),
    ("editor_request_begin_play (starts PIE)", "les.editor_request_begin_play()"),
    ("build_light_maps", "les.build_light_maps(q, o)"),
    ("undo/redo", "unreal.SystemLibrary.transact_undo()"),
]
missed = []
for label, code in suspects:
    got, _ = flagged(code)
    if not got:
        missed.append(label)
print(f"   NOT flagged ({len(missed)}/{len(suspects)}):")
for m in missed:
    print(f"      - {m}")
check("all probed mutating unreal ops are flagged", not missed,
      f"missed: {missed}")

# Transform setters are the sneakiest: they move things with no visible trace.
for nm, code in [("set_actor_location", "sub.set_actor_location(a, loc, False, True)"),
                 ("set_actor_transform", "a.set_actor_transform(t, False, True)"),
                 ("new_level", "les.new_level('/Game/M')"),
                 ("load_level", "les.load_level('/Game/M')")]:
    got, _ = flagged(code)
    check(f"REGRESSION: {nm} is flagged", nm in got, f"flagged={got}")

print("\n" + "=" * 72)
print("SECTION D: obfuscation / escape hatches")
print("=" * 72)

escapes = [
    ("getattr indirection",
     "getattr(unreal.EditorAssetLibrary, 'delete_asset')('/Game/A')"),
    ("string in variable then getattr",
     "op = 'delete_asset'\ngetattr(lib, op)('/Game/A')"),
    ("eval", "eval(\"lib.delete_asset('/Game/A')\")"),
    ("exec", "exec(\"lib.delete_asset('/Game/A')\")"),
    ("__import__", "__import__('unreal').EditorAssetLibrary.delete_asset('/Game/A')"),
    ("os.remove (filesystem, not unreal)", "import os\nos.remove('C:/important.txt')"),
    ("shutil.rmtree", "import shutil\nshutil.rmtree('C:/Games')"),
    ("subprocess", "import subprocess\nsubprocess.run(['cmd', '/c', 'del *.*'])"),
]
esc_missed = []
for label, code in escapes:
    rep = report(code)
    # Escapes must be surfaced SOMEWHERE — either as dangerous, as an
    # outside-editor side effect, or as dynamic dispatch — and must not be
    # silently rated LOW/MEDIUM as if the script were harmless.
    surfaced = ("dynamic dispatch" in rep) or ("outside-editor side effects" in rep) \
        or ("dangerous calls (mutate" in rep)
    if not surfaced:
        esc_missed.append(f"{label} (risk={risk_of(code)})")
print(f"   NOT surfaced anywhere ({len(esc_missed)}/{len(escapes)}):")
for m in esc_missed:
    print(f"      - {m}")
check("every escape hatch is surfaced in the report", not esc_missed,
      f"unsurfaced: {esc_missed}")

# risk must not read as safe for these
for label, code in escapes:
    r = risk_of(code)
    check(f"escape '{label}' is not rated LOW", r != "LOW", f"risk={r}")

check("os.remove is reported as an outside-editor side effect",
      "outside-editor side effects" in report("import os\nos.remove('C:/x.txt')"))
check("eval is reported as dynamic dispatch",
      "dynamic dispatch" in report("eval('1+1')"))
check("dynamic dispatch alone yields UNKNOWN risk (not MEDIUM)",
      risk_of("getattr(lib, op)('/Game/A')") == "UNKNOWN",
      risk_of("getattr(lib, op)('/Game/A')"))
check("report carries an explicit not-a-sandbox caveat",
      "not a sandbox" in report("a = 1"), report("a = 1")[-160:])

# eval/exec/__import__ at least deserve a mention
got, rep = flagged("eval(\"lib.delete_asset('/Game/A')\")")
check("eval appears in 'all called functions' (visible even if not 'dangerous')",
      "eval" in rep, rep[:200])

print("\n" + "=" * 72)
print("SECTION E: risk-level logic")
print("=" * 72)

check("destructive script -> HIGH", risk_of("lib.delete_asset('/Game/A')") == "HIGH",
      risk_of("lib.delete_asset('/Game/A')"))
check("read-only with calls -> MEDIUM", risk_of("print(len(x))") == "MEDIUM",
      risk_of("print(len(x))"))
r_nocall = risk_of("a = 1 + 1")
check("no calls at all -> LOW", r_nocall == "LOW", r_nocall)
# a pure read like get_all_level_actors is MEDIUM — is that useful signal?
r_read = risk_of("sub.get_all_level_actors()")
if r_read == "MEDIUM":
    issue("harmless reads are rated MEDIUM (any call => MEDIUM)",
          "risk levels only really distinguish HIGH vs has-calls")

print("\n" + "=" * 72)
print("SECTION F: robustness of dry_run")
print("=" * 72)

rep = report("for i in range(:\n    pass")
check("syntax error -> clean message with line", rep.startswith("ERROR: SyntaxError")
      and "line" in rep, rep[:100])

rep = report("")
check("empty script does not crash", "was NOT executed" in rep, rep[:80])

rep = report("# only a comment\n")
check("comment-only script does not crash", "was NOT executed" in rep, rep[:80])

big = "x = 0\n" + "".join(f"x += lib.delete_asset('/Game/A{i}')\n" for i in range(2000))
rep = report(big)
check("2000-line script handled", "delete_asset" in rep, f"{len(rep)} chars report")
check("report stays compact for huge input", len(rep) < 6000, f"{len(rep)} chars")

rep = report("名字 = 1\nprint('中文 ok')")
check("unicode identifiers/strings handled", "was NOT executed" in rep, rep[:80])

got, _ = flagged("a.b.c.delete_asset('/Game/X')")
check("deeply chained attribute call is caught", "delete_asset" in got, str(got))

got, _ = flagged("lib.delete_asset(other.destroy_actor(a))")
check("nested calls both caught",
      "delete_asset" in got and "destroy_actor" in got, str(got))

print("\n" + "=" * 72)
print("SECTION G: token loading robustness (no editor needed)")
print("=" * 72)

tmpdir = tempfile.mkdtemp(prefix="da_token_qa_")


def bridge_with(path):
    b = srv.UEBridge.__new__(srv.UEBridge)
    b.host, b.port = "127.0.0.1", 9
    b._lock = srv.threading.Lock()
    b._counter = 0
    b.endpoint_path = path
    b._token = ""
    b._token_mtime = None
    return b


b = bridge_with("")
check("no DAUNREAL_MCP_ENDPOINT -> empty token (auth off)", b._load_token() == "")

b = bridge_with(os.path.join(tmpdir, "missing.json"))
check("missing endpoint file -> empty token, no crash", b._load_token() == "")

good = os.path.join(tmpdir, "endpoint.json")
with open(good, "w", encoding="utf-8") as fh:
    json.dump({"token": "TOK-123", "port": 8766}, fh)
b = bridge_with(good)
check("valid endpoint.json -> token read", b._load_token() == "TOK-123", b._load_token())

# cached by mtime
with open(good, "w", encoding="utf-8") as fh:
    json.dump({"token": "TOK-456", "port": 8766}, fh)
os.utime(good, (0, 0))  # force a different mtime
check("token refreshes when file mtime changes", b._load_token() == "TOK-456",
      b._load_token())

nokey = os.path.join(tmpdir, "nokey.json")
with open(nokey, "w", encoding="utf-8") as fh:
    json.dump({"port": 8766}, fh)
b = bridge_with(nokey)
check("endpoint.json without token key -> empty, no crash", b._load_token() == "")

bad = os.path.join(tmpdir, "bad.json")
with open(bad, "w", encoding="utf-8") as fh:
    fh.write('{"token": "TRUNCA')  # half-written file, as during editor startup
b = bridge_with(bad)
try:
    tok = b._load_token()
    check("malformed endpoint.json handled gracefully", tok == "", f"token={tok!r}")
except Exception as exc:
    check("malformed endpoint.json handled gracefully", False,
          f"RAISED {type(exc).__name__}: {exc}  <-- _load_token only catches OSError; "
          "json.JSONDecodeError (ValueError) escapes and breaks every request")

print("\n" + "=" * 72)
print(f"SUMMARY: {len(PASS)} passed, {len(FAIL)} failed, {len(ISSUE)} issues")
for f in FAIL:
    print(f"  FAILED: {f}")
for i in ISSUE:
    print(f"  ISSUE:  {i}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
