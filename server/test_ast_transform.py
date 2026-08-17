"""Semantic-equivalence probe for the ``da_async`` AST transform layer.

Each test case runs the script twice:
  1. directly (plain ``exec``)          -> reference namespace + output
  2. transformed (setup + time-budgeted stepping to completion)
and asserts the transformed run produced the same key variables and output.

Usage:
    python test_ast_transform.py            # pure-CPython semantic checks
    python test_ast_transform.py --ue       # also verify through the live UE
                                            # bridge (editor + plugin running,
                                            # e.g. on 127.0.0.1:8766)
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sys

sys.path.insert(0, ".")
import da_async  # noqa: E402

UE_HOST = "127.0.0.1"
UE_PORT = 8766


# --------------------------------------------------------------------------- #
# test cases
# --------------------------------------------------------------------------- #

CASES = [
    {
        "name": "REPL persistence (top-level assign)",
        "code": "acc = 0\nfor i in range(100):\n    acc += i\nprint('RESULT', acc)",
        "expect": {"acc": 4950},
    },
    {
        "name": "loop-body assignment visible",
        "code": "results = []\nfor j in range(5):\n    r = j * j\n    results.append(r)\nprint('RESULT', results)",
        "expect": {"results": [0, 1, 4, 9, 16], "r": 16},
    },
    {
        "name": "function def + call",
        "code": "def helper(x):\n    return x * 3\nvals = [helper(k) for k in range(4)]\nprint('RESULT', vals)",
        "expect": {"vals": [0, 3, 6, 9]},
    },
    {
        "name": "class def + instantiation",
        "code": "class Point:\n    def __init__(self, x):\n        self.x = x\np = Point(7)\nprint('RESULT', p.x)",
        "expect": {"p": "OBJ"},
    },
    {
        "name": "try/finally inside loop",
        "code": "log = []\nfor k in range(3):\n    try:\n        log.append(k)\n    finally:\n        log.append('f')\nprint('RESULT', log)",
        "expect": {"log": [0, "f", 1, "f", 2, "f"]},
    },
    {
        "name": "nested loops",
        "code": "pairs = []\nfor a in range(2):\n    for b in range(3):\n        pairs.append((a, b))\nprint('RESULT', pairs)",
        "expect": {"pairs": [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]},
    },
    {
        "name": "while loop",
        "code": "count = 0\nwhile count < 5:\n    count += 1\nprint('RESULT', count)",
        "expect": {"count": 5},
    },
    {
        "name": "import + module use",
        "code": "import math\nsq = []\nfor i in range(3):\n    sq.append(math.sqrt(i))\nprint('RESULT', sq)",
        "expect": {"sq": [0.0, 1.0, 2.0 ** 0.5]},
    },
    {
        "name": "multi-line string preserved",
        "code": "s = 'a\\nb\\nc'\nlines = 0\nfor ch in s:\n    if ch == '\\n':\n        lines += 1\nprint('RESULT', lines)",
        "expect": {"lines": 2},
    },
    {
        "name": "if/else inside loop",
        "code": "out = []\nfor i in range(4):\n    if i % 2 == 0:\n        out.append('e')\n    else:\n        out.append('o')\nprint('RESULT', out)",
        "expect": {"out": ["e", "o", "e", "o"]},
    },
    {
        "name": "exception -> ERROR state, partial state kept",
        "code": "total = 0\nfor i in range(5):\n    total += 10 // (i - 3)\nprint('RESULT', total)",
        "expect": {"total": "ANY"},  # only state/error checked below
        "expect_error": "ZeroDivisionError",
    },
    {
        "name": "walrus in top-level expression",
        "code": "vals = []\nfor i in range(3):\n    vals.append((n := i * 2))\nprint('RESULT', vals)",
        "expect": {"vals": [0, 2, 4]},
    },
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _exec_quiet(code: str, ns: dict) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, ns)  # noqa: S102 - executing probe scripts is the point
    return buf.getvalue()


def run_transformed(code: str, budget: float = 1e-9):
    """Transform + drive to completion. Returns (result, ns, steps, output)."""
    r = da_async.transform(code, time_budget=budget)
    ns = {}
    buf = io.StringIO()
    steps = 0
    with contextlib.redirect_stdout(buf):
        exec(r.setup_code, ns)  # noqa: S102
        while ns.get("_da_state") == "RUNNING" and steps < 100_000_000:
            exec(r.step_code, ns)  # noqa: S102
            steps += 1
    return r, ns, steps, buf.getvalue()


def check_case(case: dict) -> bool:
    name = case["name"]
    code = case["code"]

    orig_raised = None
    try:
        ns_orig = {}
        out_orig = _exec_quiet(code, ns_orig)
    except Exception as exc:  # noqa: BLE001
        orig_raised = exc
        out_orig = ""

    r, ns, steps, trans_out = run_transformed(code)
    ok = True

    if "expect_error" in case:
        if orig_raised is None:
            print(f"[{name}] expected original to raise, but it succeeded")
            ok = False
        elif case["expect_error"] not in type(orig_raised).__name__:
            print(f"[{name}] original raised {type(orig_raised).__name__}, expected {case['expect_error']}")
            ok = False
        if ns.get("_da_state") != "ERROR":
            print(f"[{name}] expected ERROR state, got {ns.get('_da_state')!r}")
            ok = False
        elif case["expect_error"] not in (ns.get("_da_error") or ""):
            print(f"[{name}] transformed error does not contain {case['expect_error']!r}")
            ok = False
    else:
        if orig_raised is not None:
            print(f"[{name}] original exec raised unexpectedly: {orig_raised!r}")
            ok = False
        if ns.get("_da_state") != "DONE":
            print(f"[{name}] expected DONE state, got {ns.get('_da_state')!r}")
            ok = False

    for key, expected in case.get("expect", {}).items():
        if expected == "ANY":
            continue
        got = ns.get(key)
        if expected == "OBJ":
            if got is None:
                print(f"[{name}] {key} is None")
                ok = False
            continue
        if got != expected:
            print(f"[{name}] {key}: expected {expected!r}, got {got!r}")
            ok = False

    if orig_raised is None:
        orig_results = [ln for ln in out_orig.splitlines() if ln.startswith("RESULT ")]
        trans_results = [ln for ln in trans_out.splitlines() if ln.startswith("RESULT ")]
        if orig_results != trans_results:
            print(f"[{name}] RESULT lines differ:\n  orig={orig_results}\n  trans={trans_results}")
            ok = False

    status = "PASS" if ok else "FAIL"
    print(f"  {status}: {name} (state={ns.get('_da_state')}, slices={ns.get('_da_slices_done')}, steps={steps})")
    return ok


def run_ue_bridge_case(case: dict) -> bool:
    """Execute the transformed (wrapped) script through the live UE bridge and
    compare its printed RESULT lines with the reference output."""
    code = case["code"]
    try:
        ns_orig = {}
        out_orig = _exec_quiet(code, ns_orig)
    except Exception:  # noqa: BLE001 - expect_error cases raise in reference
        out_orig = ""
    orig_results = [ln for ln in out_orig.splitlines() if ln.startswith("RESULT ")]

    r = da_async.transform(code)
    payload = (json.dumps({"id": 1, "code": r.wrapped}) + "\n").encode("utf-8")
    try:
        sock = socket.create_connection((UE_HOST, UE_PORT), timeout=10)
        sock.settimeout(120)
        try:
            sock.sendall(payload)
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
    except OSError as exc:
        print(f"[{case['name']}] UE bridge unreachable: {exc}")
        return False

    resp = json.loads(data.decode("utf-8"))
    log = resp.get("log") or ""
    trans_results = [ln for ln in log.splitlines() if ln.startswith("RESULT ")]

    if "expect_error" in case:
        ok = resp.get("ok") is True and case["expect_error"] in log
        detail = f"error-in-log={'Yes' if case['expect_error'] in log else 'No'}"
    else:
        ok = resp.get("ok") is True and orig_results == trans_results
        detail = f"orig={orig_results}, trans={trans_results}"

    status = "PASS" if ok else "FAIL"
    print(f"  {status}: UE-bridge {case['name']} ({detail})")
    return ok


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    failures = 0
    print("== pure-CPython semantic checks ==")
    for case in CASES:
        if not check_case(case):
            failures += 1

    if "--ue" in sys.argv:
        print("== UE bridge checks ==")
        for case in CASES:
            if not run_ue_bridge_case(case):
                failures += 1

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURES'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
