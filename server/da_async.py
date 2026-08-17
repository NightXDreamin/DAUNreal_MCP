"""AST transform layer for async script execution (DAUnreal MCP).

Transforms a plain Python script into a *steppable generator*: a ``yield`` is
injected at the end of every loop body, and the whole script is wrapped in a
generator function with ``global`` declarations for every name bound at module
scope. The UE plugin then drives the generator one chunk at a time (time-budgeted
``next()`` calls); each fresh ``yield`` releases the game thread, so long batch
scripts no longer freeze the editor.

Design notes (validated experimentally on UE 5.5.4, see PROGRESS.md):

- Top-level-statement slicing cannot split a single ``for`` loop — the standard
  shape of a long task — so we inject yields into *loop bodies*.
- Wrapping the script in a function would break REPL persistence (top-level
  assigns become locals), so we emit ``global`` for every bound name. The name
  scan recurses into same-scope compound statement bodies (``if``/``for``/
  ``while``/``with``/``try``) but STOPS at ``FunctionDef``/``ClassDef``/lambda:
  those are new scopes — injecting a yield there would turn a plain function
  into a generator.
- Chunking uses a *time budget*, not a fixed batch count: per-iteration cost
  varies by orders of magnitude, so a fixed batch either stalls a frame or
  wastes yields. Default budget 4 ms (measured: ~4104 real actor ops in 4 ms).
- Cancel: ``g.close()`` raises ``GeneratorExit`` at the suspended yield and
  runs ``finally`` blocks, so a cancelled job ends its FScopedTransaction
  cleanly.

The module is pure Python and side-effect free: it only *builds* strings.
Validation (semantic equivalence of original vs transformed script) lives in
``test_ast_transform.py``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

DEFAULT_TIME_BUDGET = 0.004  # seconds of game-thread time per tick


@dataclass
class TransformResult:
    """Output of :func:`transform`.

    - ``setup_code`` — run once: defines+instantiates ``_da_gen`` and initialises
      job state variables (``_da_state``, ``_da_slices_done``, ``_da_error``).
    - ``step_code``  — run repeatedly (one game-thread tick each): drives the
      generator under the time budget and prints one status line.
    - ``wrapped``    — setup + drive-to-completion in a single script (used to
      verify injection correctness through the plain synchronous channel).
    - ``line_map``   — new-source top-level statement line -> original line
      (statement granularity; used to map tracebacks back to user code).
    - ``yield_points`` — static count of injected yields (informational only;
      NOT a progress total: iteration counts are unknown).
    - ``steppable``  — False when the script contains no loops (nothing to
      release on): the caller should fall back to sync execution; ``setup_code``
      then runs the script as-is and reports ``DONE``.
    """

    setup_code: str
    step_code: str
    wrapped: str
    line_map: dict[int, int] = field(default_factory=dict)
    yield_points: int = 0
    steppable: bool = False


# --------------------------------------------------------------------------- #
# name collection (for `global` injection)
# --------------------------------------------------------------------------- #

def _add_target(target: ast.expr, names: list[str]) -> None:
    if isinstance(target, ast.Name):
        names.append(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _add_target(elt, names)
    elif isinstance(target, ast.Starred):
        _add_target(target.value, names)
    # Attribute/Subscript targets do not bind a new name (a.b = x binds a.b).


def _collect_in_body(body: list[ast.stmt], names: list[str]) -> None:
    for stmt in body:
        _collect_stmt(stmt, names)


def _collect_walrus(node: ast.AST | None, names: list[str]) -> None:
    """Collect names bound by `:=` anywhere inside an expression.

    Walrus can appear in a loop/if test, a call argument, a f-string, etc., all
    of which bind in the enclosing scope. Comprehension bodies are their own
    scope EXCEPT for walrus, which also binds outward — so scanning the whole
    expression is correct here.
    """
    if node is None:
        return
    for sub in ast.walk(node):
        if isinstance(sub, ast.NamedExpr):
            _add_target(sub.target, names)


def _collect_stmt(stmt: ast.stmt, names: list[str]) -> None:
    # New scopes: bind the def/class name itself, do NOT recurse inside.
    # (ast.Lambda is an *expression*, never a statement, so it is not listed
    # here; a bare `lambda` shows up as ast.Expr and binds nothing.)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.append(stmt.name)
        return

    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            _add_target(target, names)
        _collect_walrus(stmt.value, names)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        _add_target(stmt.target, names)
        _collect_walrus(stmt.value, names)
    elif isinstance(stmt, ast.Delete):
        for target in stmt.targets:
            _add_target(target, names)
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        _add_target(stmt.target, names)
        _collect_walrus(stmt.iter, names)
        _collect_in_body(stmt.body, names)
        _collect_in_body(stmt.orelse, names)
    elif isinstance(stmt, ast.While):
        # Was missing entirely: while-body assignments never reached `global`,
        # silently dropping them from the shared REPL namespace.
        _collect_walrus(stmt.test, names)
        _collect_in_body(stmt.body, names)
        _collect_in_body(stmt.orelse, names)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars is not None:
                _add_target(item.optional_vars, names)
            _collect_walrus(item.context_expr, names)
        _collect_in_body(stmt.body, names)
    elif isinstance(stmt, ast.If):
        _collect_walrus(stmt.test, names)
        _collect_in_body(stmt.body, names)
        _collect_in_body(stmt.orelse, names)
    elif isinstance(stmt, ast.Try):
        _collect_in_body(stmt.body, names)
        _collect_in_body(stmt.orelse, names)
        _collect_in_body(stmt.finalbody, names)
        for handler in stmt.handlers:
            if handler.name:
                names.append(handler.name)  # `except X as e:` binds e
            _collect_in_body(handler.body, names)
    elif isinstance(stmt, ast.Match):
        _collect_walrus(stmt.subject, names)
        for case in stmt.cases:
            _collect_in_body(case.body, names)
    elif isinstance(stmt, ast.Import):
        for alias in stmt.names:
            names.append(alias.asname or alias.name.split(".")[0])
    elif isinstance(stmt, ast.ImportFrom):
        for alias in stmt.names:
            if alias.name != "*":
                names.append(alias.asname or alias.name)
    elif isinstance(stmt, ast.Expr):
        # walrus assignments bind a name in the enclosing scope
        for node in ast.walk(stmt):
            if isinstance(node, ast.NamedExpr):
                _add_target(node.target, names)


def collect_bound_names(tree: ast.Module) -> list[str]:
    """Every name bound at (transformed) module scope, deduplicated, in order."""
    names: list[str] = []
    _collect_in_body(tree.body, names)
    return list(dict.fromkeys(names))


# --------------------------------------------------------------------------- #
# yield injection
# --------------------------------------------------------------------------- #

def _make_yield_stmt(anchor: ast.stmt) -> ast.stmt:
    stmt = ast.Expr(value=ast.Yield(value=None))
    ast.copy_location(stmt, anchor)
    return stmt


def _inject_in_body(body: list[ast.stmt]) -> None:
    for stmt in body:
        _inject_stmt(stmt)


def _inject_stmt(stmt: ast.stmt) -> None:
    # New scopes: never inject inside functions/classes (would turn them into
    # generators). Lambdas are expressions and contain no statements, so they
    # need no guard here.
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return

    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        _inject_in_body(stmt.body)          # nested loops first (inner-to-outer)
        stmt.body.append(_make_yield_stmt(stmt))
        _inject_in_body(stmt.orelse)
        return

    if isinstance(stmt, ast.If):
        _inject_in_body(stmt.body)
        _inject_in_body(stmt.orelse)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        _inject_in_body(stmt.body)
    elif isinstance(stmt, ast.Try):
        _inject_in_body(stmt.body)
        _inject_in_body(stmt.orelse)
        _inject_in_body(stmt.finalbody)
        for handler in stmt.handlers:
            _inject_in_body(handler.body)
    elif isinstance(stmt, ast.Match):
        for case in stmt.cases:
            _inject_in_body(case.body)


def count_yields(tree: ast.Module) -> int:
    """Static count of injected yield nodes (informational)."""
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Yield):
            n += 1
    return n


# --------------------------------------------------------------------------- #
# code generation
# --------------------------------------------------------------------------- #

def _make_generator_func(tree: ast.Module) -> ast.FunctionDef:
    names = collect_bound_names(tree)
    args = ast.arguments(
        posonlyargs=[], args=[], vararg=None, kwonlyargs=[],
        kw_defaults=[], kwarg=None, defaults=[],
    )
    body: list[ast.stmt] = [ast.Global(names=names)]
    body.extend(tree.body)
    func = ast.FunctionDef(
        name="_da_gen",
        args=args,
        body=body,
        decorator_list=[],
        returns=None,
    )
    return func


def _make_setup_ast(tree: ast.Module) -> ast.Module:
    func = _make_generator_func(tree)

    def assign(name: str, value: ast.expr) -> ast.Assign:
        return ast.Assign(
            targets=[ast.Name(id=name, ctx=ast.Store())],
            value=value,
        )

    setup_body: list[ast.stmt] = [
        func,
        assign("_da_gen", ast.Call(func=ast.Name(id="_da_gen", ctx=ast.Load()), args=[], keywords=[])),
        assign("_da_state", ast.Constant(value="RUNNING")),
        assign("_da_slices_done", ast.Constant(value=0)),
        assign("_da_error", ast.Constant(value=None)),
    ]
    module = ast.Module(body=setup_body, type_ignores=[])
    # Manually-built nodes carry no source positions; unparse needs them
    # (e.g. FunctionDef.lineno for type comments). Seed the top level and let
    # fix_missing_locations propagate to children.
    for idx, stmt in enumerate(module.body):
        stmt.lineno = idx + 1
        stmt.col_offset = 0
    ast.fix_missing_locations(module)
    return module


def _make_drive_code(*, budget: float | None) -> str:
    """The generator driver. With a budget it yields the game thread once the
    budget is exhausted; with ``budget=None`` it runs to completion.

    Uses ``time.perf_counter_ns()`` (integer ns, QPC-backed on Windows) for the
    budget check: ``time.monotonic()``/``monotonic_ns()`` have only millisecond
    resolution on some Windows Python builds (GetTickCount64-backed), which
    would make a sub-millisecond budget never trigger.
    """
    budget_ns = "10**18" if budget is None else str(int(budget * 1e9))
    return f'''\
import time as _da_time
import traceback as _da_tb
_da_budget = _da_time.perf_counter_ns() + {budget_ns}
_da_keep_running = True
while _da_keep_running:
    try:
        next(_da_gen)
    except StopIteration:
        _da_state = 'DONE'
        _da_keep_running = False
        break
    except GeneratorExit:
        _da_state = 'CANCELLED'
        _da_keep_running = False
        break
    except BaseException:
        _da_state = 'ERROR'
        _da_error = _da_tb.format_exc()
        _da_keep_running = False
        break
    _da_slices_done += 1
    if _da_time.perf_counter_ns() >= _da_budget:
        break
print('DA_MCP_STATE|' + str(_da_state) + '|' + str(_da_slices_done))
if _da_state == 'ERROR':
    print(_da_error)
'''


def _make_plain_setup(tree: ast.Module) -> str:
    """No-loop script: run the body as-is, then report DONE. Not steppable."""
    def assign(name: str, value: ast.expr) -> ast.Assign:
        return ast.Assign(
            targets=[ast.Name(id=name, ctx=ast.Store())],
            value=value,
        )

    body: list[ast.stmt] = list(tree.body) + [
        assign("_da_state", ast.Constant(value="DONE")),
        assign("_da_slices_done", ast.Constant(value=0)),
        assign("_da_error", ast.Constant(value=None)),
    ]
    module = ast.Module(body=body, type_ignores=[])
    for idx, stmt in enumerate(module.body):
        stmt.lineno = idx + 1
        stmt.col_offset = 0
    ast.fix_missing_locations(module)
    return ast.unparse(module)


def build_line_map(tree: ast.Module, new_tree: ast.Module) -> dict[int, int]:
    """Map new-source top-level statement lines -> original lines.

    The transformed generator body is ``[Global] + original_top_level_stmts``
    in the same order, so statement *k* of the new body corresponds to original
    top-level statement *k-1*. Line numbers of the original nodes are preserved
    by the injection pass (new yield nodes carry copies, never overwrite).
    """
    new_func = new_tree.body[0]
    assert isinstance(new_func, ast.FunctionDef)
    new_body = new_func.body[1:]  # skip Global
    orig_body = tree.body
    line_map: dict[int, int] = {}
    for new_stmt, orig_stmt in zip(new_body, orig_body):
        line_map[new_stmt.lineno] = orig_stmt.lineno
    return line_map


def transform(code: str, *, time_budget: float = DEFAULT_TIME_BUDGET) -> TransformResult:
    """Transform a Python script into a steppable generator.

    Raises ``SyntaxError`` for invalid input (line numbers are the original
    script's, so the caller can report them directly).
    """
    tree = ast.parse(code)  # raises SyntaxError with original line info
    _inject_in_body(tree.body)
    yield_points = count_yields(tree)

    if yield_points == 0:
        # No loops -> nothing to release on; not steppable. Long tasks are
        # loops, so callers fall back to plain sync execution for these. Run
        # the body as-is and report DONE (line numbers stay original).
        setup_code = _make_plain_setup(tree)
        step_code = "print('DA_MCP_STATE|DONE|0|')"
        wrapped = setup_code
        return TransformResult(
            setup_code=setup_code,
            step_code=step_code,
            wrapped=wrapped,
            line_map={},
            yield_points=0,
            steppable=False,
        )

    setup_ast = _make_setup_ast(tree)
    setup_code = ast.unparse(setup_ast)

    step_code = _make_drive_code(budget=time_budget)
    wrapped = setup_code + "\n" + _make_drive_code(budget=None)

    line_map = build_line_map(tree, ast.parse(setup_code))

    return TransformResult(
        setup_code=setup_code,
        step_code=step_code,
        wrapped=wrapped,
        line_map=line_map,
        yield_points=yield_points,
        steppable=True,
    )


def map_traceback_line(line: int, line_map: dict[int, int]) -> int:
    """Map a new-source line back to the original line (statement granularity):
    the closest mapped statement line at or above ``line``."""
    best = None
    for new_lineno, orig_lineno in line_map.items():
        if new_lineno <= line and (best is None or new_lineno > best[0]):
            best = (new_lineno, orig_lineno)
    return best[1] if best else line
