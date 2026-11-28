"""Safe mathematical answer extraction, parsing, and comparison."""

from __future__ import annotations

import ast
import contextlib
import json
import math as stdlib_math
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

import anyio
from anyio.abc import Process
from anyio.lowlevel import RunVar
from anyio.streams.buffered import BufferedByteReceiveStream

from inspect_ai._util.error import PrerequisiteError, pip_dependency_error
from inspect_ai.solver._task_state import TaskState

from ._metric import CORRECT, INCORRECT, Score
from ._metrics import accuracy, stderr
from ._scorer import Scorer, scorer
from ._target import Target

_PARSER_PACKAGE = "latex2sympy2-extended"
_PARSER_VERSION = "1.11.0"
_ANTLR_PACKAGE = "antlr4-python3-runtime"
_ANTLR_REQUIREMENT = (
    "antlr4-python3-runtime>=4.9.3,<=4.13.2,!=4.10.*,!=4.12.*,!=4.13.0,!=4.13.1"
)

_MAX_COMPLETION_CHARS = 1_000_000
_MAX_CANDIDATE_CHARS = 4_096
_MAX_NESTING = 64
_MAX_COMMANDS = 256
_MAX_OPERATORS = 1_024
_MAX_DIGIT_RUN = 256
_MAX_TOTAL_DIGITS = 2_048
_MAX_MATRIX_CELLS = 256
_MAX_EXPRESSION_NODES = 512
_MAX_EXPRESSION_DEPTH = 64
_MAX_EXPRESSION_ARGS = 128
_MAX_SYMBOLS = 64
_MAX_SYMBOL_CHARS = 128
_MAX_INTEGER_BITS = 4_096
_SYMBOLIC_POWER_EXPONENT_LIMIT = 10_000
_MAX_FACTORIAL_ARGUMENT = 10_000

_DEFAULT_TIMEOUT_SECONDS = 2.0
# The first call also starts the worker process and pays its one-time imports
# (sympy, latex2sympy2), so its budget is floored well above the steady-state
# timeout.
_MIN_COLD_TIMEOUT_SECONDS = 10.0
# One worker process, reused across scorings: each new process pays the
# one-time imports, and a single worker bounds the host CPU the scorer uses.
_MATH_WORKERS = 1
# Address space the worker may grow by beyond its size after imports (Linux).
_WORKER_MEMORY_HEADROOM_BYTES = 2 * 1024**3
_MAX_WORKER_RESPONSE_BYTES = 1_000_000
_WORKER_START_TIMEOUT_SECONDS = 60.0
_WORKER_IDLE_SECONDS = 60.0
# Run the worker with the parent's sys.path so it imports this same module,
# without re-executing the caller's __main__ (as anyio.to_process would).
_WORKER_SCRIPT = (
    "import json, sys; sys.path[:] = json.loads(sys.argv[1]); "
    "from inspect_ai.scorer._math import _worker_main; _worker_main()"
)

_BOX_START = re.compile(
    r"(?:\\(?:beginboxed|boxed|fbox)|(?<![A-Za-z\\])(?:boxed|fbox|oxed))\s*\{"
)
_ANSWER_MARKER = re.compile(r"(?i)(?:final\s+answer|answer|result)\s*(?:is\b|[:=])\s*")
_NUMBER = re.compile(
    r"[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?\s*%?"
)
_PERCENT_SUFFIX = re.compile(
    r"\s*(?:\\?%|\\text\s*\{\s*(?:percent(?:age)?|pct)\s*\}|"
    r"\s+(?:percent(?:age)?|pct))\s*$",
    re.IGNORECASE,
)
_LATEX_NUMBER = re.compile(r"[+-]?(?:\d+|\d*\.\d+)(?:E[+-]?\d+)?\Z")
_LATEX_COMMA_NUMBER = re.compile(r"\s*-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*\Z")
# LaTeX thin-space digit grouping, e.g. "1\,234" — a thousands separator.
_LATEX_THIN_SPACE_NUMBER = re.compile(r"\s*-?\d{1,3}(?:\\,\d{3})+(?:\.\d+)?\s*\Z")
_DIGIT_RUN = re.compile(r"\d+")
_COMMAND = re.compile(r"\\[A-Za-z]+")
_WORD = re.compile(r"[A-Za-z]{2,}")
_CODE_SHAPED = re.compile(
    r"__|"
    r"\b(?:breakpoint|compile|eval|exec|getattr|globals|import|locals|open|"
    r"setattr)\s*\(|"
    r"\b(?:builtins|os|pathlib|subprocess|sys)\s*\."
)
_EAGER_PARSER_OPERATION = re.compile(
    r"\\begin\{[vV]matrix\}|"
    r"\\(?:det|gcd|lcm)\b|"
    r"\\xrightarrow|"
    r"\\\||"
    r"\\operatorname\s*\{\s*(?:"
    r"cols|diag|diagonalize|eig|eigen|eigenvals|eigenvalues|eigenvects|"
    r"eigenvectors|eye|gcd|hstack|lcm|nullspace|norm|ones|orth|ortho|"
    r"orthogonal|orthogonalize|rank|ref|rows|rref|svd|trace|tr|vstack|"
    r"zeros"
    r")\s*\}",
    re.IGNORECASE,
)
_UNEVALUATED_LATEX_CONSTRUCTOR = re.compile(r"\\(?:binom|gamma)\b", re.IGNORECASE)
_SPACING_COMMANDS = (r"\qquad", r"\quad", r"\,", r"\;", r"\:", r"\!")
# Functions whose value has about n*log2(n) bits for an argument n.
_COMBINATORIAL_FUNCTIONS = {"binomial", "factorial", "gamma"}

_PLAIN_FUNCTIONS = {
    "abs",
    "acos",
    "acosh",
    "asin",
    "asinh",
    "atan",
    "atanh",
    "binomial",
    "ceil",
    "cos",
    "cosh",
    "exp",
    "factorial",
    "floor",
    "ln",
    "log",
    "max",
    "min",
    "sin",
    "sinh",
    "sqrt",
    "tan",
    "tanh",
}

_ParseStatus = Literal[
    "correct",
    "incorrect",
    "answer_parse_error",
    "answer_limit",
]


class _MathParseError(ValueError):
    pass


class _MathLimitError(_MathParseError):
    pass


class _MathUnsafeError(_MathParseError):
    pass


@dataclass(frozen=True)
class _ParsedValue:
    expression: Any | None
    text: str | None
    source: str
    expensive: bool = False
    # The symbol policy the winning parser used; None for text values. A
    # target's policy is reused to parse the answer compared against it.
    policy: _SymbolPolicy | None = None


@dataclass(frozen=True)
class _ExpressionInfo:
    expensive: bool


@dataclass(frozen=True)
class _WorkerScore:
    status: _ParseStatus
    answer: str | None
    reason: str | None = None


@dataclass
class _MathWorkerContext:
    # A single-slot limiter serializes scorings ahead of the per-item budget.
    # The `async with queue` sits outside `fail_after`, so time spent waiting
    # for the worker does not consume the budget. Parsing then runs in one
    # worker process, which keeps the event loop responsive even while SymPy
    # or a regex holds the GIL. When the budget expires (or the scoring is
    # cancelled) the process is killed, so no abandoned work keeps consuming
    # CPU or memory; the next scoring starts a fresh one.
    queue: anyio.CapacityLimiter
    started: bool = False
    process: Process | None = None
    output: BufferedByteReceiveStream | None = None
    last_used: float = 0.0


_MATH_WORKER_CONTEXT: RunVar[_MathWorkerContext] = RunVar("math_worker_context")


def _math_worker_context() -> _MathWorkerContext:
    try:
        return _MATH_WORKER_CONTEXT.get()
    except LookupError:
        context = _MathWorkerContext(queue=anyio.CapacityLimiter(_MATH_WORKERS))
        _MATH_WORKER_CONTEXT.set(context)
        return context


def _replace_unicode(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", text)
    replacements = {
        "\u23a7": r"\boxed{",
        "\u23ab": "}",
        "\n\u2502": r"\boxed{",
        "\u2502": "}",
        "\n\u2503": r"\boxed{",
        "\u2503": "}",
        "\n\uf8f0": r"\boxed{",
        "\uf8fb": "}",
        "\u221a": r"\sqrt",
        "\u00d7": r"\cdot",
        "\u00f7": "/",
        "\u202f": " ",
        "\u2212": "-",
        "\u2013": "-",
        "\u03c0": r"\pi",
        "\u00b0": r"^\circ",
        "\u221e": r"\infty",
        "\u2264": r"\le",
        "\u2265": r"\ge",
        "\u2260": r"\ne",
        "\u222a": r"\cup",
        "\u2229": r"\cap",
    }
    for source, replacement in replacements.items():
        text = text.replace(source, replacement)
    return text


def _balanced_content(text: str, opening: int) -> tuple[str, int] | None:
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
            if depth > _MAX_NESTING:
                return None
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = min(index, opening + _MAX_CANDIDATE_CHARS + 2)
                return text[opening + 1 : end], index + 1
    return None


def _boxed_candidates(text: str) -> list[str]:
    # Scan the whole (length-bounded) completion and keep only the most recent
    # \boxed{...}. Capping the scan at a fixed count would take that Nth box as
    # "last" and miss a genuine final answer placed after a flood of earlier
    # boxes — a correctness gap and a gaming vector.
    last: str | None = None
    position = 0
    while True:
        match = _BOX_START.search(text, position)
        if match is None:
            break
        parsed = _balanced_content(text, match.end() - 1)
        if parsed is None:
            position = match.end()
            continue
        last, position = parsed
    return [last] if last is not None else []


def _last_single_dollar_math(text: str) -> str | None:
    positions: list[int] = []
    for index, char in enumerate(text):
        if char == "$" and (index == 0 or text[index - 1] != "\\"):
            if index + 1 < len(text) and text[index + 1] == "$":
                continue
            if index > 0 and text[index - 1] == "$":
                continue
            positions.append(index)
    last_match: str | None = None
    for index in range(0, len(positions) - 1, 2):
        last_match = text[positions[index] + 1 : positions[index + 1]]
    return last_match


def _last_delimited_math(text: str) -> str | None:
    matches: list[tuple[int, str]] = []
    for opening, closing in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)")):
        end = text.rfind(closing)
        if end < 0:
            continue
        start = text.rfind(opening, 0, end)
        if start >= 0:
            matches.append((end, text[start + len(opening) : end]))

    single_dollar = _last_single_dollar_math(text)
    if single_dollar is not None:
        end = text.rfind("$")
        matches.append((end, single_dollar))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def _strip_delimiters(text: str) -> str:
    text = text.strip()
    if len(text) > _MAX_CANDIDATE_CHARS:
        # `_validate_candidate` rejects it; skip normalizing unbounded text.
        return text
    while len(text) >= 4 and text.startswith("**") and text.endswith("**"):
        text = text[2:-2].strip()
    for opening, closing in (
        ("$$", "$$"),
        (r"\[", r"\]"),
        (r"\(", r"\)"),
        ("$", "$"),
    ):
        if (
            text.startswith(opening)
            and text.endswith(closing)
            and (opening != "$" or text.count("$") == 2)
        ):
            text = text[len(opening) : -len(closing)].strip()
    text = re.sub(r"^(?:(?:\\[,;:!]|\\quad|\\qquad)\s*)+", "", text)
    # Trailing spacing commands are stripped by scanning back from the end: an
    # unanchored `(...)+$` regex rescans the remainder from every position.
    end = len(text)
    while True:
        while end and text[end - 1].isspace():
            end -= 1
        command = next(
            (
                command
                for command in _SPACING_COMMANDS
                if text.endswith(command, 0, end)
            ),
            None,
        )
        if command is None:
            break
        end -= len(command)
    return text[:end].rstrip(" \t\r\n.,;:")


def _append_candidate(candidates: list[str], candidate: str | None) -> None:
    if candidate is None:
        return
    candidate = _strip_delimiters(candidate)
    if candidate and candidate not in candidates:
        candidates.append(candidate)


def _answer_candidates(text: str) -> list[str]:
    text = _replace_unicode(text)
    if len(text) > _MAX_COMPLETION_CHARS:
        raise _MathLimitError("model output is too long")

    candidates: list[str] = []
    boxes = _boxed_candidates(text)
    if boxes:
        _append_candidate(candidates, boxes[-1])

    last_marker: re.Match[str] | None = None
    for match in _ANSWER_MARKER.finditer(text):
        last_marker = match
    if last_marker is not None:
        suffix = text[last_marker.end() :]
        _append_candidate(candidates, suffix.splitlines()[0] if suffix else None)

    if _is_short_composite_answer(text):
        _append_candidate(candidates, text)

    _append_candidate(candidates, _last_delimited_math(text))

    if len(text) <= _MAX_CANDIDATE_CHARS and not _is_short_composite_answer(text):
        _append_candidate(candidates, text)

    nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if nonempty_lines:
        _append_candidate(candidates, nonempty_lines[-1])

    last_number: str | None = None
    for match in _NUMBER.finditer(text):
        last_number = match.group(0)
    _append_candidate(candidates, last_number)

    return candidates


def _target_candidates(text: str) -> list[str]:
    text = _replace_unicode(text)
    candidates: list[str] = []
    boxes = _boxed_candidates(text)
    if boxes:
        _append_candidate(candidates, boxes[-1])
    _append_candidate(candidates, text)
    return candidates


def _contains_nested_latex_power(text: str) -> bool:
    index = 0
    while True:
        index = text.find("^", index)
        if index < 0:
            return False
        cursor = index + 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor < len(text) and text[cursor] == "{":
            parsed = _balanced_content(text, cursor)
            if (
                parsed is not None
                and "^" in parsed[0]
                and not re.search(r"[A-Za-z]", parsed[0])
            ):
                return True
        index += 1


def _validate_candidate(candidate: str) -> None:
    if not candidate:
        raise _MathParseError("answer is empty")
    if len(candidate) > _MAX_CANDIDATE_CHARS:
        raise _MathLimitError("expression is too long")
    if _CODE_SHAPED.search(candidate):
        raise _MathUnsafeError("expression contains non-mathematical code syntax")
    if _EAGER_PARSER_OPERATION.search(candidate):
        raise _MathLimitError("expression requests an eager mathematical operation")
    if _contains_nested_latex_power(candidate):
        raise _MathLimitError("expression contains a nested exponent")

    digit_runs = _DIGIT_RUN.findall(candidate)
    if any(len(run) > _MAX_DIGIT_RUN for run in digit_runs):
        raise _MathLimitError("expression contains an oversized numeric literal")
    if sum(len(run) for run in digit_runs) > _MAX_TOTAL_DIGITS:
        raise _MathLimitError("expression contains too many digits")
    if len(_COMMAND.findall(candidate)) > _MAX_COMMANDS:
        raise _MathLimitError("expression contains too many LaTeX commands")
    if sum(candidate.count(operator) for operator in "+-*/^=!<>") > _MAX_OPERATORS:
        raise _MathLimitError("expression contains too many operators")

    depth = 0
    max_depth = 0
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for char in candidate:
        if char in "([{":
            stack.append(char)
            depth += 1
            max_depth = max(max_depth, depth)
        elif char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
                depth -= 1
    if max_depth > _MAX_NESTING:
        raise _MathLimitError("expression is nested too deeply")

    matrix_rows = candidate.count(r"\\") + 1
    matrix_columns = candidate.count("&") + 1
    if matrix_rows * matrix_columns > _MAX_MATRIX_CELLS:
        raise _MathLimitError("expression contains an oversized matrix")


def _looks_like_prose(candidate: str) -> bool:
    if "\\" in candidate or any(char in candidate for char in "=+-*/^<>[]{}()"):
        return False
    return len(_WORD.findall(candidate)) >= 2


def _is_short_composite_answer(candidate: str) -> bool:
    if len(candidate) > 512 or candidate.count("\n") > 2:
        return False
    if "$" not in candidate and r"\(" not in candidate and r"\[" not in candidate:
        return False
    if "\n" in candidate and candidate.count("$") >= 4:
        return True
    outside_math = re.sub(
        r"\$\$.*?\$\$|\$.*?\$|\\\(.*?\\\)|\\\[.*?\\\]", " ", candidate, flags=re.DOTALL
    )
    return bool(_WORD.search(outside_math))


def _normalize_text(candidate: str) -> str:
    candidate = _strip_delimiters(candidate)
    candidate = re.sub(r"\\(?:text|mathrm|mbox)\s*\{([^{}]*)\}", r"\1", candidate)
    candidate = candidate.replace(r"\ ", " ")
    candidate = re.sub(r"\s+", " ", candidate)
    return candidate.strip(" \t\r\n.,;:").casefold()


def _split_plain_equation(text: str) -> tuple[str, str] | None:
    depth = 0
    split_at: int | None = None
    for index, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "=" and depth == 0:
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if previous in "<>!=" or following == "=":
                continue
            if split_at is not None:
                return None
            split_at = index
    if split_at is None:
        return None
    return text[:split_at], text[split_at + 1 :]


@dataclass(frozen=True)
class _SymbolPolicy:
    """How a candidate's symbols are built, for both notations.

    SymPy treats ``Symbol("x")``, ``Symbol("X", real=True)`` and
    ``Symbol("x", real=True)`` as three different variables, so the plain
    builder and the LaTeX converter must agree on case and on the real/complex
    assumption or the same symbol compares unequal across notations (#5259).
    `for_candidate` makes both decisions once per candidate and the two paths
    only consume the result, so neither can drift from the other.

    Detecting the imaginary unit stays with the caller because its spelling is
    notation-specific (`_looks_complex` for LaTeX, `_plain_looks_complex` for
    plain, which also accepts `I` and Python's `2j`).

    The policy is decided per answer-target pair, not per candidate: the
    target's policy is recorded on its `_ParsedValue` and the answer is parsed
    under it, so both sides build identical symbols. Otherwise `x + i - i`
    would build a non-real `x` that never equals the target's real `x` even
    though the imaginary terms cancel.
    """

    lowercase: bool
    is_real: bool

    @classmethod
    def for_candidate(
        cls, candidate: str, looks_complex: Callable[[str], bool]
    ) -> "_SymbolPolicy":
        """Decide the policy for one candidate, given its notation's detector."""
        # Symbols match case-insensitively: the LaTeX converter has always
        # lowercased them, and the plain builder follows it.
        return cls(lowercase=True, is_real=not looks_complex(candidate))

    def symbol(self, sympy: Any, name: str) -> Any:
        return sympy.Symbol(name.lower() if self.lowercase else name, real=self.is_real)


class _PlainExpressionBuilder(ast.NodeVisitor):
    def __init__(self, sympy: Any, policy: _SymbolPolicy) -> None:
        self.sympy = sympy
        self.policy = policy
        self.nodes = 0

    def visit(self, node: ast.AST) -> Any:
        self.nodes += 1
        if self.nodes > _MAX_EXPRESSION_NODES:
            raise _MathLimitError("plain expression contains too many nodes")
        return super().visit(node)

    def generic_visit(self, node: ast.AST) -> Any:
        raise _MathParseError(
            f"unsupported plain expression syntax: {type(node).__name__}"
        )

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        value = node.value
        if isinstance(value, bool) or value is None or isinstance(value, str):
            raise _MathParseError("unsupported literal")
        if isinstance(value, int):
            if value.bit_length() > _MAX_INTEGER_BITS:
                raise _MathLimitError("integer literal is too large")
            return self.sympy.Integer(value)
        if isinstance(value, float):
            if not stdlib_math.isfinite(value):
                raise _MathLimitError("non-finite numeric literal")
            return self.sympy.Float(repr(value))
        if isinstance(value, complex):
            if not (
                stdlib_math.isfinite(value.real) and stdlib_math.isfinite(value.imag)
            ):
                raise _MathLimitError("non-finite complex literal")
            return self.sympy.Add(
                self.sympy.Float(repr(value.real)),
                self.sympy.Mul(
                    self.sympy.Float(repr(value.imag)),
                    self.sympy.I,
                    evaluate=False,
                ),
                evaluate=False,
            )
        raise _MathParseError("unsupported literal")

    def visit_Name(self, node: ast.Name) -> Any:
        constants = {
            "E": self.sympy.E,
            "I": self.sympy.I,
            "e": self.sympy.E,
            "i": self.sympy.I,
            "inf": self.sympy.oo,
            "infinity": self.sympy.oo,
            "pi": self.sympy.pi,
        }
        constant = constants.get(node.id)
        if constant is not None:
            return constant
        return self.policy.symbol(self.sympy, node.id)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return self.sympy.Mul(-1, operand, evaluate=False)
        raise _MathParseError("unsupported unary operator")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return self.sympy.Add(left, right, evaluate=False)
        if isinstance(node.op, ast.Sub):
            return self.sympy.Add(
                left, self.sympy.Mul(-1, right, evaluate=False), evaluate=False
            )
        if isinstance(node.op, ast.Mult):
            return self.sympy.Mul(left, right, evaluate=False)
        if isinstance(node.op, ast.Div):
            return self.sympy.Mul(
                left, self.sympy.Pow(right, -1, evaluate=False), evaluate=False
            )
        if isinstance(node.op, ast.Pow):
            return self.sympy.Pow(left, right, evaluate=False)
        if isinstance(node.op, ast.Mod):
            return self.sympy.Mod(left, right, evaluate=False)
        raise _MathParseError("unsupported binary operator")

    def visit_Call(self, node: ast.Call) -> Any:
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id not in _PLAIN_FUNCTIONS
            or node.keywords
        ):
            raise _MathParseError("unsupported function call")
        args = [self.visit(arg) for arg in node.args]
        name = node.func.id
        if name == "sqrt" and len(args) == 1:
            return self.sympy.Pow(args[0], self.sympy.S.Half, evaluate=False)
        if name == "factorial" and len(args) == 1:
            return self.sympy.factorial(args[0], evaluate=False)
        if name == "binomial" and len(args) == 2:
            return self.sympy.binomial(*args, evaluate=False)
        if name == "abs" and len(args) == 1:
            return self.sympy.Abs(args[0], evaluate=False)
        if name in {"ceil", "floor"} and len(args) == 1:
            function = self.sympy.ceiling if name == "ceil" else self.sympy.floor
            return function(args[0], evaluate=False)
        if name in {"log", "ln"} and 1 <= len(args) <= 2:
            base = args[1] if len(args) == 2 else self.sympy.E
            return self.sympy.log(args[0], base, evaluate=False)
        if name in {"min", "max"} and args:
            function = self.sympy.Min if name == "min" else self.sympy.Max
            return function(*args, evaluate=False)
        functions = {
            "acos": self.sympy.acos,
            "acosh": self.sympy.acosh,
            "asin": self.sympy.asin,
            "asinh": self.sympy.asinh,
            "atan": self.sympy.atan,
            "atanh": self.sympy.atanh,
            "cos": self.sympy.cos,
            "cosh": self.sympy.cosh,
            "exp": self.sympy.exp,
            "sin": self.sympy.sin,
            "sinh": self.sympy.sinh,
            "tan": self.sympy.tan,
            "tanh": self.sympy.tanh,
        }
        function = functions.get(name)
        if function is None or len(args) != 1:
            raise _MathParseError("unsupported function arguments")
        return function(args[0], evaluate=False)

    def visit_Tuple(self, node: ast.Tuple) -> Any:
        return self.sympy.Tuple(*(self.visit(element) for element in node.elts))

    def visit_List(self, node: ast.List) -> Any:
        return self.sympy.Tuple(*(self.visit(element) for element in node.elts))

    def visit_Set(self, node: ast.Set) -> Any:
        return self.sympy.FiniteSet(
            *(self.visit(element) for element in node.elts), evaluate=False
        )

    def visit_Compare(self, node: ast.Compare) -> Any:
        values = [
            self.visit(node.left),
            *(self.visit(item) for item in node.comparators),
        ]
        relations: list[Any] = []
        for left, operator, right in zip(
            values[:-1], node.ops, values[1:], strict=True
        ):
            if isinstance(operator, ast.Eq):
                relation = self.sympy.Eq(left, right, evaluate=False)
            elif isinstance(operator, ast.NotEq):
                relation = self.sympy.Ne(left, right, evaluate=False)
            elif isinstance(operator, ast.Lt):
                relation = self.sympy.StrictLessThan(left, right, evaluate=False)
            elif isinstance(operator, ast.LtE):
                relation = self.sympy.LessThan(left, right, evaluate=False)
            elif isinstance(operator, ast.Gt):
                relation = self.sympy.StrictGreaterThan(left, right, evaluate=False)
            elif isinstance(operator, ast.GtE):
                relation = self.sympy.GreaterThan(left, right, evaluate=False)
            else:
                raise _MathParseError("unsupported comparison")
            relations.append(relation)
        if len(relations) == 1:
            return relations[0]
        return self.sympy.And(*relations, evaluate=False)


def _parse_plain_expression(
    candidate: str, sympy: Any, policy: _SymbolPolicy | None = None
) -> Any | None:
    if "\\" in candidate or re.search(r"\^\s*\{", candidate):
        return None
    # An equation's halves inherit the whole candidate's policy: "z = 1+i" is
    # complex on both sides even though "z" alone does not look complex.
    if policy is None:
        policy = _SymbolPolicy.for_candidate(candidate, _plain_looks_complex)
    text = candidate.strip()
    text = text.replace("\u00d7", "*").replace("\u00f7", "/").replace("^", "**")

    # A bare grouped-thousands number (e.g. "1,234", "12,345,678") would parse
    # as a tuple; strip the separators so it reads as a single number, matching
    # the LaTeX path. Anchored to the whole token so structured answers like
    # "(1,2)" or "{1,2,3}" keep their commas and stay tuples/sets.
    if _LATEX_COMMA_NUMBER.fullmatch(text):
        text = text.replace(",", "")

    equation = _split_plain_equation(text)
    if equation is not None:
        left_text, right_text = equation
        left = _parse_plain_expression(left_text, sympy, policy)
        right = _parse_plain_expression(right_text, sympy, policy)
        if left is None or right is None:
            raise _MathParseError("invalid equation")
        return sympy.Eq(left, right, evaluate=False)

    try:
        parsed = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError):
        return None
    return _PlainExpressionBuilder(sympy, policy).visit(parsed)


def _looks_complex(candidate: str) -> bool:
    return bool(
        re.search(
            r"\\mathbb\{C\}|\\(?:i|imath)\b|(?<![A-Za-z])i(?![A-Za-z])|"
            r"\\(?:arg|Re|Im)\b",
            candidate,
        )
    )


def _plain_looks_complex(candidate: str) -> bool:
    """Whether a plain-notation candidate involves the imaginary unit.

    The plain parser accepts both ``i`` and ``I`` as the imaginary unit and
    Python complex literals such as ``2j``; a candidate using any of these must
    build non-real symbols, exactly as the LaTeX path does for ``i``.
    """
    return _looks_complex(candidate) or bool(
        re.search(
            r"(?<![A-Za-z0-9_])I(?![A-Za-z0-9_])|\d[jJ](?![A-Za-z0-9_])", candidate
        )
    )


def _parse_latex_number(text: str, sympy: Any) -> Any:
    normalized = text.replace(",", "").strip()
    if not _LATEX_NUMBER.fullmatch(normalized):
        raise _MathParseError("invalid numeric token from LaTeX parser")
    if "." in normalized or "E" in normalized:
        return sympy.Float(normalized)
    return sympy.Integer(normalized)


def _parse_latex_expression(
    candidate: str, sympy: Any, policy: _SymbolPolicy | None = None
) -> Any:
    from latex2sympy2_extended import (  # type: ignore[import-untyped]
        NormalizationConfig,
        normalize_latex,
    )
    from latex2sympy2_extended.latex2sympy2 import (  # type: ignore[import-untyped]
        ConversionConfig,
        _Latex2Sympy,
    )

    normalized_candidate = re.sub(
        r"\\(?:left|right|Bigl|Bigr|bigl|bigr|Big|big|Large|large)\b",
        "",
        candidate,
    )
    normalized_candidate = normalize_latex(
        normalized_candidate,
        NormalizationConfig(
            basic_latex=True,
            units=True,
            malformed_operators=True,
            nits=True,
            boxed="last",
            equations=False,
        ),
    )

    # Fold thin-space grouping ("1\,234") into the comma form the grouped-number
    # path already handles, so it reads as 1234 rather than being mis-parsed.
    if _LATEX_THIN_SPACE_NUMBER.fullmatch(normalized_candidate):
        normalized_candidate = normalized_candidate.replace(r"\,", ",")

    if _LATEX_COMMA_NUMBER.fullmatch(normalized_candidate):
        return _parse_latex_number(normalized_candidate, sympy)

    class _InspectLatex2Sympy(_Latex2Sympy):  # type: ignore[misc]
        def parse_number(self, text: str) -> Any:
            return _parse_latex_number(text, sympy)

    if policy is None:
        policy = _SymbolPolicy.for_candidate(candidate, _looks_complex)
    converter = _InspectLatex2Sympy(
        variable_values=None,
        is_real=policy.is_real,
        convert_degrees=False,
        config=ConversionConfig(
            interpret_as_mixed_fractions=True,
            interpret_simple_eq_as_assignment=False,
            interpret_contains_as_eq=True,
            lowercase_symbols=policy.lowercase,
        ),
    )

    def parse() -> Any:
        return converter.parse(normalized_candidate)

    if _UNEVALUATED_LATEX_CONSTRUCTOR.search(normalized_candidate):
        with sympy.evaluate(False):
            return parse()
    return parse()


def _parse_expression(
    candidate: str, sympy: Any, policy: _SymbolPolicy | None = None
) -> tuple[Any | None, _SymbolPolicy | None]:
    """Parse a candidate, returning the expression and the policy that built it.

    With `policy` given (the target's, when parsing an answer) both parsers use
    it as-is; otherwise each detects its own from the candidate's notation.
    """
    plain_policy = policy or _SymbolPolicy.for_candidate(
        candidate, _plain_looks_complex
    )
    try:
        expression = _parse_plain_expression(candidate, sympy, plain_policy)
    except _MathLimitError:
        raise
    except Exception:
        expression = None

    if expression is not None:
        return expression, plain_policy

    latex_policy = policy or _SymbolPolicy.for_candidate(candidate, _looks_complex)
    try:
        return _parse_latex_expression(candidate, sympy, latex_policy), latex_policy
    except Exception:
        return None, None


def _percentage_base(candidate: str) -> str | None:
    match = _PERCENT_SUFFIX.search(candidate)
    if match is None:
        return None
    return candidate[: match.start()].rstrip() or None


def _integer_too_large(value: Any) -> bool:
    try:
        return abs(int(value)).bit_length() > _MAX_INTEGER_BITS
    except (TypeError, ValueError, OverflowError):
        return True


def _validate_expression(expression: Any, sympy: Any) -> _ExpressionInfo:
    if isinstance(expression, sympy.MatrixBase):
        rows, columns = expression.shape
        if rows * columns > _MAX_MATRIX_CELLS:
            raise _MathLimitError("parsed matrix is too large")
        roots = list(expression)
    elif isinstance(expression, sympy.Basic):
        roots = [expression]
    else:
        raise _MathParseError("parser returned an unsupported value")

    nodes = 0
    symbols: set[str] = set()
    expensive = False
    stack = [(root, 1) for root in roots]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_EXPRESSION_NODES:
            raise _MathLimitError("parsed expression contains too many nodes")
        if depth > _MAX_EXPRESSION_DEPTH:
            raise _MathLimitError("parsed expression is nested too deeply")

        if isinstance(node, sympy.Integer) and _integer_too_large(node):
            raise _MathLimitError("parsed integer is too large")
        if isinstance(node, sympy.Rational) and (
            _integer_too_large(node.p) or _integer_too_large(node.q)
        ):
            raise _MathLimitError("parsed rational is too large")
        if isinstance(node, sympy.Symbol):
            symbols.add(node.name)
            if len(node.name) > _MAX_SYMBOL_CHARS:
                raise _MathLimitError("parsed symbol is too long")
            if len(symbols) > _MAX_SYMBOLS:
                raise _MathLimitError("parsed expression contains too many symbols")

        if isinstance(node, sympy.Pow):
            exponent = node.exp
            if isinstance(exponent, sympy.Integer):
                try:
                    if abs(int(exponent)) > _SYMBOLIC_POWER_EXPONENT_LIMIT:
                        expensive = True
                except (TypeError, ValueError, OverflowError):
                    expensive = True
            elif isinstance(exponent, sympy.Pow):
                expensive = True

        function_name = getattr(getattr(node, "func", None), "__name__", "")
        if function_name == "factorial" and node.args:
            argument = node.args[0]
            if isinstance(argument, sympy.Integer):
                try:
                    value = abs(int(argument))
                except (TypeError, ValueError, OverflowError):
                    value = _MAX_FACTORIAL_ARGUMENT + 1
                if value > _MAX_FACTORIAL_ARGUMENT:
                    raise _MathLimitError("factorial argument is too large")
                if value > 100:
                    expensive = True
        if function_name in {
            "Derivative",
            "Integral",
            "Limit",
            "Product",
            "Sum",
        }:
            expensive = True

        args = getattr(node, "args", ())
        if len(args) > _MAX_EXPRESSION_ARGS:
            raise _MathLimitError("parsed expression has too many arguments")
        stack.extend((arg, depth + 1) for arg in args if isinstance(arg, sympy.Basic))

    if any(_evaluated_bits(root, sympy) > _MAX_INTEGER_BITS for root in roots):
        expensive = True
    return _ExpressionInfo(expensive=expensive)


def _evaluated_bits(node: Any, sympy: Any) -> float:
    """Upper bound on the bit size of the numbers SymPy may build evaluating `node`.

    The parsers build trees with `evaluate=False`, so the literals checked above
    stay small even when the value they denote is huge (`factorial(10**9)`,
    `(9^{99})^{99}`). Comparison evaluates the whole tree, so its cost depends
    on the evaluated size through nested bases, exponents and arguments.
    Symbols and constants count as one bit.
    """
    if isinstance(node, sympy.Rational):
        return max(stdlib_math.log2(max(abs(int(node.p)), int(node.q))), 1.0)
    if isinstance(node, sympy.Float):
        _, _, exponent, precision = node._mpf_
        return float(max(abs(exponent + precision), precision, 1))
    args = [arg for arg in node.args if isinstance(arg, sympy.Basic)]
    if not args:
        return 1.0

    def magnitude(bits: float) -> float:
        return stdlib_math.ldexp(1.0, stdlib_math.ceil(min(bits, 1_000.0)))

    if isinstance(node, sympy.Pow):
        base, exponent = node.args
        if isinstance(exponent, sympy.Rational):
            power = -(-abs(int(exponent.p)) // int(exponent.q))
            exponent_bound = (
                float(power) if power.bit_length() < 1_000 else stdlib_math.inf
            )
        else:
            exponent_bound = magnitude(_evaluated_bits(exponent, sympy))
        return _evaluated_bits(base, sympy) * max(exponent_bound, 1.0)

    bits = [_evaluated_bits(arg, sympy) for arg in args]
    if getattr(node.func, "__name__", "") in _COMBINATORIAL_FUNCTIONS:
        largest = max(bits)
        return magnitude(largest) * largest
    return sum(bits)


def _parse_candidate(
    candidate: str, policy: _SymbolPolicy | None = None
) -> _ParsedValue:
    import sympy  # type: ignore[import-untyped]

    candidate = _strip_delimiters(candidate)
    _validate_candidate(candidate)

    expression: Any | None = None
    expression_policy: _SymbolPolicy | None = None
    percentage_base = _percentage_base(candidate)
    if percentage_base is not None:
        expression, expression_policy = _parse_expression(
            percentage_base, sympy, policy
        )
        if expression is not None:
            expression = sympy.Mul(
                expression,
                sympy.UnevaluatedExpr(sympy.Rational(1, 100)),
                evaluate=False,
            )

    if expression is None and (
        _looks_like_prose(candidate) or _is_short_composite_answer(candidate)
    ):
        return _ParsedValue(None, _normalize_text(candidate), candidate)

    if expression is None:
        expression, expression_policy = _parse_expression(candidate, sympy, policy)

    if expression is not None:
        try:
            info = _validate_expression(expression, sympy)
        except _MathParseError:
            raise
        except Exception as ex:
            raise _MathParseError("could not validate mathematical answer") from ex
        return _ParsedValue(
            expression, None, candidate, info.expensive, expression_policy
        )

    equation = _split_plain_equation(candidate)
    if equation is not None:
        _, right = equation
        try:
            return _parse_candidate(right, policy)
        except _MathParseError:
            pass

    normalized_text = _normalize_text(candidate)
    if normalized_text and _WORD.search(normalized_text):
        return _ParsedValue(None, normalized_text, candidate)
    raise _MathParseError("could not parse mathematical answer")


def _parse_first(
    candidates: list[str], policy: _SymbolPolicy | None = None
) -> _ParsedValue:
    parse_error: _MathParseError | None = None
    for candidate in candidates:
        try:
            return _parse_candidate(candidate, policy)
        except (_MathLimitError, _MathUnsafeError):
            raise
        except _MathParseError as ex:
            parse_error = ex
    raise parse_error or _MathParseError("could not extract mathematical answer")


def _matching_expression_candidate(
    candidates: list[str],
    parsed_targets: list["_ParsedValue"],
    sympy: Any,
) -> "_ParsedValue | None":
    """Find an expression-valued candidate that matches a target.

    Used only as a fallback when the primary answer parsed as opaque text (a
    prose wrapper like "42 because ...") and did not match, so a bare number on
    the last line can still be recognized. Unsafe/over-limit candidates abort.
    """
    for candidate in candidates:
        for policy in _target_policies(parsed_targets):
            try:
                parsed = _parse_candidate(candidate, policy)
            except (_MathLimitError, _MathUnsafeError):
                raise
            except _MathParseError:
                break
            if parsed.expression is not None and any(
                _expression_equivalent(target_value, parsed, sympy)
                for target_value in parsed_targets
                if target_value.policy == policy
            ):
                return parsed
    return None


def _target_policies(
    parsed_targets: list[_ParsedValue],
) -> list[_SymbolPolicy | None]:
    """The distinct symbol policies the targets were parsed under, in order.

    The answer is parsed once per policy so each target is compared against an
    answer whose symbols carry the same assumptions. Policies have two axes
    with one fixed, so this is at most real and complex (plus None for text).
    """
    return list(dict.fromkeys(target_value.policy for target_value in parsed_targets))


def _is_numeric_expression(expression: Any, sympy: Any) -> bool:
    return isinstance(expression, sympy.Expr) and not expression.free_symbols


def _is_exact_number(expression: Any, sympy: Any) -> bool:
    # Values SymPy can compare exactly (rationals, and algebraic combinations
    # thereof such as sqrt(2) or 1/3). These must never fall through to the
    # floating-point tolerance below, which would accept nearby-but-wrong
    # answers (e.g. 99999999999 vs 100000000000).
    return bool(getattr(expression, "is_number", False)) and bool(
        getattr(expression, "is_algebraic", False)
    )


def _numeric_equivalent(left: Any, right: Any, sympy: Any) -> bool:
    if not (
        _is_numeric_expression(left, sympy) and _is_numeric_expression(right, sympy)
    ):
        return False
    # Exact operands are only equal if SymPy's exact comparison said so (checked
    # by the caller before reaching here); do not approximate them.
    if _is_exact_number(left, sympy) and _is_exact_number(right, sympy):
        return False
    try:
        left_value = complex(sympy.N(left, 30))
        right_value = complex(sympy.N(right, 30))
        if not all(
            stdlib_math.isfinite(value)
            for value in (
                left_value.real,
                left_value.imag,
                right_value.real,
                right_value.imag,
            )
        ):
            return False
        error = abs(left_value - right_value)
        scale = max(abs(left_value), abs(right_value), 1e-10)
        return error < 1e-10 or error / scale < 1e-10
    except (ArithmeticError, TypeError, ValueError):
        return False


def _expression_equivalent(left: _ParsedValue, right: _ParsedValue, sympy: Any) -> bool:
    if left.text is not None or right.text is not None:
        return (
            left.text is not None and right.text is not None and left.text == right.text
        )

    left_expression = left.expression
    right_expression = right.expression
    if left_expression is None or right_expression is None:
        return False

    # An assignment-form value like "x = 2" (or "2 = x") should match the bare
    # answer "2" regardless of which side carries the equality. Unwrap an
    # Equality to its value side (the side that is not a bare symbol) when the
    # other operand is a non-relational scalar.
    def _is_relational(expression: Any) -> bool:
        return bool(getattr(expression, "is_Relational", False))

    def _equality_value(equality: Any) -> Any:
        lhs_is_symbol = bool(getattr(equality.lhs, "is_Symbol", False))
        rhs_is_symbol = bool(getattr(equality.rhs, "is_Symbol", False))
        if rhs_is_symbol and not lhs_is_symbol:
            return equality.lhs
        return equality.rhs

    if isinstance(right_expression, sympy.Equality) and not _is_relational(
        left_expression
    ):
        right_expression = _equality_value(right_expression)
    elif isinstance(left_expression, sympy.Equality) and not _is_relational(
        right_expression
    ):
        left_expression = _equality_value(left_expression)

    try:
        if left_expression == right_expression:
            return True
    except Exception:
        pass

    if left.expensive or right.expensive:
        return False

    if isinstance(left_expression, sympy.MatrixBase) or isinstance(
        right_expression, sympy.MatrixBase
    ):
        if not (
            isinstance(left_expression, sympy.MatrixBase)
            and isinstance(right_expression, sympy.MatrixBase)
            and left_expression.shape == right_expression.shape
        ):
            return False
        return all(
            _expression_equivalent(
                _ParsedValue(left_item, None, left.source),
                _ParsedValue(right_item, None, right.source),
                sympy,
            )
            for left_item, right_item in zip(
                left_expression, right_expression, strict=True
            )
        )

    try:
        equals = left_expression.equals(right_expression)
        if equals is True:
            return True
    except Exception:
        pass

    return _numeric_equivalent(left_expression, right_expression, sympy)


def _parse_targets_worker(
    targets: tuple[str, ...],
) -> tuple[list[_ParsedValue], str | None]:
    # Return the parsed target values so the answer worker doesn't re-parse them.
    # SymPy expressions are immutable and safe to hand back to the calling
    # thread; the clear_cache() below only drops memoized assumption lookups and
    # does not invalidate the returned expression objects.
    from sympy.core.cache import clear_cache  # type: ignore[import-untyped]

    try:
        if not targets:
            return [], "target is empty"
        parsed: list[_ParsedValue] = []
        last_error: _MathParseError | None = None
        for target in targets:
            try:
                parsed.append(_parse_first(_target_candidates(target)))
            except _MathParseError as ex:
                last_error = ex
        if parsed:
            return parsed, None
        return [], str(last_error or "could not parse mathematical target")
    finally:
        clear_cache()


def _score_answer_worker(
    completion: str, parsed_targets: list[_ParsedValue]
) -> _WorkerScore:
    import sympy  # type: ignore[import-untyped]
    from sympy.core.cache import clear_cache  # type: ignore[import-untyped]

    try:
        candidates = _answer_candidates(completion)
        try:
            # Parse the answer under each target's own symbol policy (at most
            # one parse per distinct policy) so the pair shares assumptions.
            answers = {
                policy: _parse_first(candidates, policy)
                for policy in _target_policies(parsed_targets) or [None]
            }
        except _MathLimitError as ex:
            return _WorkerScore("answer_limit", None, str(ex))
        except _MathParseError as ex:
            return _WorkerScore("answer_parse_error", None, str(ex))

        answer = next(iter(answers.values()))
        correct = any(
            _expression_equivalent(target_value, answers[target_value.policy], sympy)
            for target_value in parsed_targets
        )
        # If the primary answer was opaque text that didn't match (a prose
        # wrapper such as "42 because ..."), fall back to an expression-valued
        # candidate — the bare last line / last number — that the text masked.
        if not correct and answer.expression is None:
            fallback = _matching_expression_candidate(candidates, parsed_targets, sympy)
            if fallback is not None:
                correct = True
                answer = fallback
        return _WorkerScore(
            "correct" if correct else "incorrect",
            answer.source[:_MAX_CANDIDATE_CHARS],
        )
    finally:
        clear_cache()


def _limit_worker_memory() -> None:
    """Cap the worker's address space so runaway allocation fails in the worker."""
    try:
        import resource

        with open("/proc/self/statm") as statm:
            used = int(statm.read().split()[0]) * resource.getpagesize()
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = used + _WORKER_MEMORY_HEADROOM_BYTES
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ImportError, OSError, ValueError):
        # Not Linux: the worker is still killed when the time budget expires.
        pass


def _worker_main() -> None:
    """Serve parse and score requests from `score()` as JSON lines on stdin/stdout.

    A target request parses and keeps the targets; the following answer request
    is scored against them. Only plain JSON crosses the process boundary:
    unpickling SymPy objects would evaluate them.
    """
    from importlib import import_module

    # Import before capping memory, so the cap is headroom over a loaded worker.
    import_module("latex2sympy2_extended")
    import_module("sympy")
    _limit_worker_memory()
    # Keep stray prints from the parsers out of the response stream.
    responses = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)

    # The parent cannot close a worker whose event loop has ended (the pipe
    # stays open), so the worker exits once idle; the parent replaces idle
    # workers before reusing them (`_start_worker`).
    idle_since: float | None = time.monotonic()

    def exit_when_idle() -> None:
        while True:
            time.sleep(1)
            since = idle_since
            if since is not None and time.monotonic() - since > _WORKER_IDLE_SECONDS:
                os._exit(0)

    threading.Thread(target=exit_when_idle, daemon=True).start()
    responses.write("ready\n")
    responses.flush()

    parsed_targets: list[_ParsedValue] = []
    while line := sys.stdin.readline():
        idle_since = None
        request = json.loads(line)
        response: dict[str, Any]
        try:
            if "targets" in request:
                parsed_targets, error = _parse_targets_worker(tuple(request["targets"]))
                response = {"error": error}
            else:
                try:
                    result = _score_answer_worker(request["completion"], parsed_targets)
                except MemoryError:
                    result = _WorkerScore(
                        "answer_limit", None, "expression exceeded the memory limit"
                    )
                response = asdict(result)
        except Exception as ex:
            response = {"exception": f"{type(ex).__name__}: {ex}"[:1_000]}
        responses.write(json.dumps(response) + "\n")
        responses.flush()
        idle_since = time.monotonic()


async def _start_worker(worker: _MathWorkerContext) -> None:
    """Start the worker process unless a recently used one is running.

    Start-up (spawn and imports) is not driven by model output, so it is bounded
    separately rather than by the scoring budget. A worker unused for half its
    idle limit may be about to exit (see `_worker_main`), so it is replaced.
    """
    if (
        worker.process is not None
        and anyio.current_time() - worker.last_used < _WORKER_IDLE_SECONDS / 2
    ):
        return
    await _stop_worker(worker)
    try:
        with anyio.fail_after(_WORKER_START_TIMEOUT_SECONDS):
            worker.process = await anyio.open_process(
                [sys.executable, "-c", _WORKER_SCRIPT, json.dumps(sys.path)],
                stderr=subprocess.DEVNULL,
            )
            assert worker.process.stdout is not None
            worker.output = BufferedByteReceiveStream(worker.process.stdout)
            await worker.output.receive_until(b"\n", _MAX_WORKER_RESPONSE_BYTES)
    except BaseException:
        await _stop_worker(worker)
        raise
    worker.last_used = anyio.current_time()


async def _worker_request(
    worker: _MathWorkerContext, request: dict[str, Any]
) -> dict[str, Any]:
    """Send one request to the worker process started by `_start_worker`.

    Any failure or cancellation (including the scoring timeout) kills the
    worker, so a timed-out parse does not keep running.
    """
    try:
        assert worker.process is not None and worker.process.stdin is not None
        assert worker.output is not None
        await worker.process.stdin.send(json.dumps(request).encode() + b"\n")
        response: dict[str, Any] = json.loads(
            await worker.output.receive_until(b"\n", _MAX_WORKER_RESPONSE_BYTES)
        )
    except BaseException:
        await _stop_worker(worker)
        raise
    worker.last_used = anyio.current_time()
    if "exception" in response:
        raise RuntimeError(f"math() scorer worker failed: {response['exception']}")
    return response


async def _stop_worker(worker: _MathWorkerContext) -> None:
    process, worker.process, worker.output = worker.process, None, None
    worker.started = False
    if process is None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with anyio.CancelScope(shield=True), contextlib.suppress(OSError):
        await process.aclose()


def _dependency_error() -> Exception:
    return pip_dependency_error(
        "math() scorer", [f"{_PARSER_PACKAGE}=={_PARSER_VERSION}"]
    )


def _supported_antlr_version(installed: str) -> bool:
    return (
        installed == "4.9.3" or installed.startswith("4.11.") or installed == "4.13.2"
    )


def _check_dependency() -> None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        parser_version = version(_PARSER_PACKAGE)
        antlr_version = version(_ANTLR_PACKAGE)
    except PackageNotFoundError:
        raise _dependency_error() from None

    if parser_version != _PARSER_VERSION:
        raise PrerequisiteError(
            f"ERROR: math() scorer requires {_PARSER_PACKAGE}=={_PARSER_VERSION} "
            f"(you have {parser_version}).\n\n"
            f"Install with: pip install {_PARSER_PACKAGE}=={_PARSER_VERSION}"
        )

    if not _supported_antlr_version(antlr_version):
        raise PrerequisiteError(
            f"ERROR: math() scorer requires {_ANTLR_PACKAGE} 4.9.3, 4.11.x, "
            f"or 4.13.2 (you have {antlr_version}).\n\n"
            f'Install with: pip install "{_ANTLR_REQUIREMENT}"'
        )

    try:
        import latex2sympy2_extended  # noqa: F401
        import sympy  # noqa: F401
    except ImportError:
        raise _dependency_error() from None


def _status_metadata(status: str) -> dict[str, str]:
    return {"math_scorer_status": status}


@scorer(metrics=[accuracy(), stderr()])
def math(*, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> Scorer:
    """Create a mathematical expression scorer.

    Extracts a bounded final answer from model output, parses it without
    evaluating Python, and compares it to each target under bounded symbolic
    work. Raises a scoring error if none of the reference answers can be parsed.

    Args:
        timeout: Active-work budget in seconds for each parsing phase (target
            and answer). This is wall-clock time in the host process and so is
            sensitive to concurrent load; parsing that exceeds it is treated as
            an incorrect answer (or an unscored target). The first call gets a
            larger cold-start allowance to absorb one-time imports.
    """
    _check_dependency()
    cold_timeout = max(_MIN_COLD_TIMEOUT_SECONDS, 5 * timeout)

    async def score(state: TaskState, target: Target) -> Score:
        worker = _math_worker_context()
        targets = list(target)
        # Both phases run under one queue slot: the worker keeps the parsed
        # targets for the answer request that follows.
        async with worker.queue:
            try:
                await _start_worker(worker)
                target_timeout = timeout if worker.started else cold_timeout
                with anyio.fail_after(target_timeout):
                    target_response = await _worker_request(
                        worker, {"targets": targets}
                    )
                worker.started = True
            except TimeoutError:
                worker.started = False
                # A timeout is the scorer failing to run, not the model failing
                # to answer: record it in `reason` so analysis can attribute the
                # dropped sample to a slow host.
                return Score.unscored(
                    explanation="Mathematical target exceeded the parsing time limit.",
                    reason="scoring_failed",
                    metadata=_status_metadata("target_timeout"),
                )

            if target_response["error"] is not None:
                raise ValueError(
                    f"Could not parse any mathematical target: {target_response['error']}."
                )

            try:
                with anyio.fail_after(timeout):
                    result = _WorkerScore(
                        **await _worker_request(
                            worker, {"completion": state.output.completion}
                        )
                    )
            except TimeoutError:
                worker.started = False
                # A timeout is the scorer not running to completion, not the
                # model failing to answer: record `timeout` in `reason` so
                # analysis can separate a slow host from a wrong answer.
                return Score(
                    value=INCORRECT,
                    explanation="Mathematical answer exceeded the scoring time limit.",
                    reason="timeout",
                    metadata=_status_metadata("answer_timeout"),
                )

        if result.status == "correct":
            return Score(
                value=CORRECT,
                answer=result.answer,
                explanation=state.output.completion,
                metadata=_status_metadata(result.status),
            )
        if result.status == "incorrect":
            return Score(
                value=INCORRECT,
                answer=result.answer,
                explanation=state.output.completion,
                metadata=_status_metadata(result.status),
            )
        # Extraction/parse failures are a format violation by the model under
        # test: keep INCORRECT (the sample stays in the metric denominator) but
        # record the failure mode in `reason` so analysis can separate "wrong
        # answer" from "couldn't parse an answer". The completion is surfaced
        # as the answer when no candidate was extracted, per the custom-scorers
        # guidance for extraction scorers.
        return Score(
            value=INCORRECT,
            answer=result.answer
            if result.answer is not None
            else state.output.completion,
            explanation=(
                f"Could not parse mathematical answer: {result.reason}."
                if result.status == "answer_parse_error"
                else f"Mathematical answer exceeded a complexity limit: {result.reason}."
            ),
            reason="invalid_response_format",
            metadata=_status_metadata(result.status),
        )

    return score
