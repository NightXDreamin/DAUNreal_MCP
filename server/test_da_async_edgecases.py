"""Edge-case regression suite for da_async.py (pure CPython, no editor needed).

Adversarial coverage for the AST transform, written to hunt defects rather than
confirm the happy path. It found two real bugs on 2026-08-17 (see PROGRESS.md
section 15):
  - `_collect_stmt` had no `ast.While` branch -> while-body assignments were
    silently dropped from the shared REPL namespace.
  - walrus targets were only collected inside `ast.Expr` statements, missing
    while/if tests, for-iters, with-items and match subjects.

Run:  .venv\\Scripts\\python.exe test_da_async_edgecases.py
"""
import ast
import os
import sys
import textwrap
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import da_async  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def run_original(code):
    ns = {"__name__": "__main__"}
    out = []
    ns["print"] = lambda *a, **k: out.append(" ".join(str(x) for x in a))
    exec(compile(ast.parse(code), "<orig>", "exec"), ns)
    return {k: v for k, v in ns.items()
            if not k.startswith("_") and k != "print" and not callable(v)}, out


def run_transformed(code):
    t = da_async.transform(code)
    ns = {"__name__": "__main__"}
    out = []
    ns["print"] = lambda *a, **k: out.append(" ".join(str(x) for x in a))
    exec(t.setup_code, ns)
    if t.steppable:
        # drive to completion
        exec(da_async._make_drive_code(budget=None), ns)
    # The driver prints its own status line; that is protocol, not user output.
    out = [l for l in out if not l.startswith("DA_MCP_STATE|")]
    return ({k: v for k, v in ns.items()
             if not k.startswith("_") and k != "print" and not callable(v)},
            out, t)


def _comparable(ns):
    """Drop values whose repr embeds an identity (memory address), since the two
    runs create distinct instances; keep everything structurally comparable."""
    out = {}
    for k, v in ns.items():
        r = repr(v)
        if " object at 0x" in r:
            out[k] = f"<instance of {type(v).__name__}>"
        else:
            out[k] = v
    return out


def equiv(name, code):
    """Original vs transformed must agree on final namespace and printed output."""
    try:
        ns1, out1 = run_original(code)
    except Exception as e:
        check(name, False, f"original itself raised {type(e).__name__}: {e}")
        return
    try:
        ns2, out2, _t = run_transformed(code)
        ns2 = {k: v for k, v in ns2.items()
               if k not in ("da_time", "da_tb")}
    except Exception as e:
        check(name, False, f"TRANSFORMED raised {type(e).__name__}: {e}\n"
                           f"{traceback.format_exc(limit=3)}")
        return
    ok = (_comparable(ns1) == _comparable(ns2)) and (out1 == out2)
    detail = "" if ok else f"\n      orig ns={ns1} out={out1}\n      new  ns={ns2} out={out2}"
    check(name, ok, detail)

print("=" * 70)
print("SECTION 1: lambda handling (regression guard)")
print("=" * 70)

# ast.Lambda is an *expression*: `f = lambda x: x` parses to Assign, a bare
# lambda to Expr. It must never be listed in the statement isinstance checks —
# it has no `.name`, so such a branch would be dead code with a latent
# AttributeError. Guard against reintroducing it.
src = ast.getsource if False else None
import inspect  # noqa: E402
import re as _re  # noqa: E402


def _isinstance_targets(func):
    """Node classes actually used in isinstance(...) checks, ignoring comments."""
    code = "".join(
        line.split("#")[0] for line in inspect.getsource(func).splitlines(keepends=True)
    )
    return set(_re.findall(r"ast\.(\w+)", code))


collect_targets = _isinstance_targets(da_async._collect_stmt)
inject_targets = _isinstance_targets(da_async._inject_stmt)
check("_collect_stmt does not isinstance-check ast.Lambda",
      "Lambda" not in collect_targets,
      "ast.Lambda has no .name; a statement branch for it is dead + crashy")
check("_inject_stmt does not isinstance-check ast.Lambda",
      "Lambda" not in inject_targets)
check("_collect_stmt handles ast.While (the 2026-08-17 bug)",
      "While" in collect_targets, f"targets={sorted(collect_targets)}")

check("`f = lambda x: x` parses to Assign (not Lambda)",
      type(ast.parse("f = lambda x: x").body[0]).__name__ == "Assign")

equiv("lambda assignment still works end-to-end",
      "f = lambda x: x * 2\nvals = []\nfor i in range(3):\n    vals.append(f(i))")

print("\n" + "=" * 70)
print("SECTION 2: semantic equivalence on adversarial control flow")
print("=" * 70)

equiv("for + break", textwrap.dedent("""
    hits = []
    for i in range(10):
        if i == 3:
            break
        hits.append(i)
"""))

equiv("for + continue", textwrap.dedent("""
    kept = []
    for i in range(6):
        if i % 2:
            continue
        kept.append(i)
"""))

equiv("for-else (else runs when no break)", textwrap.dedent("""
    log = []
    for i in range(3):
        log.append(i)
    else:
        log.append('else-ran')
"""))

equiv("for-else with break (else must NOT run)", textwrap.dedent("""
    log2 = []
    for i in range(3):
        log2.append(i)
        break
    else:
        log2.append('should-not-appear')
"""))

equiv("while-else", textwrap.dedent("""
    n = 0
    trace = []
    while n < 3:
        trace.append(n)
        n += 1
    else:
        trace.append('done')
"""))

equiv("nested loops (both instrumented)", textwrap.dedent("""
    pairs = []
    for i in range(3):
        for j in range(2):
            pairs.append((i, j))
"""))

equiv("try/finally around loop", textwrap.dedent("""
    seq = []
    try:
        for i in range(3):
            seq.append(i)
    finally:
        seq.append('fin')
"""))

equiv("except as e (name deleted at handler exit)", textwrap.dedent("""
    caught = None
    results = []
    for i in range(3):
        try:
            if i == 1:
                raise ValueError('boom')
            results.append(i)
        except ValueError as e:
            caught = str(e)
"""))

equiv("with block containing loop", textwrap.dedent("""
    class CM:
        def __init__(self):
            self.log = []
        def __enter__(self):
            self.log.append('enter')
            return self
        def __exit__(self, *a):
            self.log.append('exit')
            return False
    cm = CM()
    with cm as b:
        for i in range(3):
            b.log.append(i)
    trace_cm = list(cm.log)
"""))

equiv("walrus in loop condition", textwrap.dedent("""
    src = [1, 2, 3, 0]
    taken = []
    idx = 0
    while (v := src[idx]) != 0:
        taken.append(v)
        idx += 1
"""))

equiv("comprehension (own scope, must not be instrumented)", textwrap.dedent("""
    squares = [x * x for x in range(4)]
    gen_sum = sum(y for y in range(4))
    total = 0
    for i in range(2):
        total += 1
"""))

equiv("loop var leaks after loop (Python semantics)", textwrap.dedent("""
    for leaked in range(3):
        pass
    after = leaked
"""))

equiv("function containing loop, called from top level", textwrap.dedent("""
    def work(n):
        acc = 0
        for i in range(n):
            acc += i
        return acc
    out = []
    for k in range(3):
        out.append(work(k))
"""))

equiv("tuple unpack in for target", textwrap.dedent("""
    items = [(1, 'a'), (2, 'b')]
    keys = []
    vals = []
    for num, letter in items:
        keys.append(num)
        vals.append(letter)
"""))

equiv("augassign to dict/list element (no new name)", textwrap.dedent("""
    d = {'n': 0}
    lst = [0, 0]
    for i in range(3):
        d['n'] += i
        lst[0] += 1
"""))

equiv("match statement inside loop", textwrap.dedent("""
    kinds = []
    for x in [1, 'a', None]:
        match x:
            case int():
                kinds.append('int')
            case str():
                kinds.append('str')
            case _:
                kinds.append('other')
"""))

print("\n" + "=" * 70)
print("SECTION 3: steppable detection & no-loop fallback")
print("=" * 70)

t_noloop = da_async.transform("a = 1\nb = a + 1")
check("no-loop script -> steppable False", t_noloop.steppable is False,
      f"steppable={t_noloop.steppable}, yields={t_noloop.yield_points}")

t_comp_only = da_async.transform("vals = [i for i in range(100000)]")
check("comprehension-only -> steppable False (correct: own scope, cannot yield)",
      t_comp_only.steppable is False,
      f"steppable={t_comp_only.steppable} -- NOTE: a 100k comprehension still "
      "blocks the game thread; this is a known limitation, not a bug")

t_func_loop = da_async.transform(
    "def f():\n    for i in range(100000):\n        pass\nf()")
check("loop only inside function -> steppable False",
      t_func_loop.steppable is False,
      f"steppable={t_func_loop.steppable} -- long work inside a called function "
      "cannot be sliced; falls back to sync (documented limitation)")

t_loop = da_async.transform("for i in range(10):\n    pass")
check("top-level loop -> steppable True", t_loop.steppable is True)

print("\n" + "=" * 70)
print("SECTION 4: line_map / traceback mapping")
print("=" * 70)

src_lines = textwrap.dedent("""
    x = 1

    # a comment

    for i in range(3):
        y = i
    z = 2
""").strip("\n")
t_lm = da_async.transform(src_lines)
print(f"  line_map = {t_lm.line_map}")
orig_stmt_lines = [n.lineno for n in ast.parse(src_lines).body]
print(f"  original top-level stmt lines = {orig_stmt_lines}")
check("line_map covers every top-level statement",
      len(t_lm.line_map) == len(orig_stmt_lines),
      f"{len(t_lm.line_map)} mapped vs {len(orig_stmt_lines)} statements")
mapped_vals = sorted(t_lm.line_map.values())
check("line_map values match original statement lines",
      mapped_vals == sorted(orig_stmt_lines),
      f"{mapped_vals} vs {sorted(orig_stmt_lines)}")

# Real traceback mapping: raise inside the loop, see if reported line is sane
err_src = "a = 1\nfor i in range(3):\n    raise RuntimeError('x')\n"
t_err = da_async.transform(err_src)
ns = {"__name__": "__main__"}
exec(t_err.setup_code, ns)
exec(da_async._make_drive_code(budget=None), ns)
raw_err = ns.get("_da_error") or ""
print("  raw traceback tail:", [l for l in raw_err.strip().split("\n") if l.strip()][-2:])
import re
m = re.search(r'line (\d+)', raw_err)
if m:
    new_line = int(m.group(1))
    mapped = da_async.map_traceback_line(new_line, t_err.line_map)
    print(f"  transformed line {new_line} -> mapped {mapped} (raise is at original line 3)")
    check("traceback maps into the right statement", mapped in (2, 3),
          f"mapped={mapped}; statement granularity means the `for` line (2) is acceptable")
else:
    check("traceback contains a line number", False, raw_err[:200])

print("\n" + "=" * 70)
print("SECTION 5: name-collection edge cases")
print("=" * 70)

names = da_async.collect_bound_names(ast.parse(textwrap.dedent("""
    import os
    import os.path
    from json import dumps
    from json import loads as jl
    a, b = 1, 2
    (c, [d, e]) = (3, [4, 5])
    *f, g = [1, 2, 3]
    h: int = 7
    i = 0
    i += 1
    del i
    for k in range(1): pass
    with open('x') as fh: pass
    try: pass
    except ValueError as exc: pass
    class C: pass
    def fn(): pass
""")))
print(f"  collected: {names}")
expected = {"os", "dumps", "jl", "a", "b", "c", "d", "e", "f", "g", "h", "i",
            "k", "fh", "exc", "C", "fn"}
missing = expected - set(names)
extra = set(names) - expected
check("collects all binding forms", not missing, f"missing={missing}")
check("no spurious names", not extra, f"extra={extra}")
check("`import os.path` binds 'os' not 'os.path'",
      "os" in names and "os.path" not in names)

# --- REGRESSION GUARDS for the two bugs fixed on 2026-08-17 --------------- #
# Bug 1: no ast.While branch -> while-body assignments never reached `global`.
while_names = da_async.collect_bound_names(ast.parse(textwrap.dedent("""
    n = 0
    while n < 3:
        inside_while = n * 2
        n += 1
""")))
check("REGRESSION: while-body assignment is collected",
      "inside_while" in while_names,
      f"collected={while_names} -- missing means while-body vars silently vanish "
      "from the REPL namespace")

# Bug 2: walrus only scanned inside ast.Expr statements.
for label, snippet, want in [
    ("while test", "while (w1 := 0) != 0:\n    pass", "w1"),
    ("if test", "if (w2 := 1):\n    pass", "w2"),
    ("for iter", "for x in [(w3 := 5)]:\n    pass", "w3"),
    ("assign value", "y = (w4 := 9)", "w4"),
    ("match subject", "match (w5 := 1):\n    case _:\n        pass", "w5"),
]:
    got = da_async.collect_bound_names(ast.parse(snippet))
    check(f"REGRESSION: walrus in {label} collected", want in got, f"collected={got}")

print("\n" + "=" * 70)
print("SECTION 6: robustness")
print("=" * 70)

try:
    da_async.transform("for i in range(:\n    pass")
    check("SyntaxError propagates", False, "no exception raised")
except SyntaxError as e:
    check("SyntaxError propagates with original line", e.lineno is not None,
          f"lineno={e.lineno} msg={e.msg}")

t_empty = None
try:
    t_empty = da_async.transform("")
    check("empty script handled", t_empty.steppable is False,
          f"steppable={t_empty.steppable}")
except Exception as e:
    check("empty script handled", False, f"{type(e).__name__}: {e}")

# global/nonlocal already in user code
equiv("user code already has `global`", textwrap.dedent("""
    counter = 0
    def bump():
        global counter
        counter += 1
    for i in range(3):
        bump()
"""))

# very deep nesting
deep = "acc = []\n" + "".join(
    f"{'    ' * i}for i{i} in range(2):\n" for i in range(4)
) + "    " * 4 + "acc.append((i0, i1, i2, i3))"
equiv("4-level nested loops", deep)

print("\n" + "=" * 70)
print(f"SUMMARY: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
