"""Small recursive-descent parser for `--filter` expressions.

Grammar:

    expr     := orExpr
    orExpr   := andExpr (('OR' | 'or') andExpr)*
    andExpr  := atom (('AND' | 'and') atom)*
    atom     := '(' expr ')'  |  comparison
    comparison := METRIC OP VALUE
    OP       := '>' | '>=' | '<' | '<=' | '==' | '!='
    METRIC   := one of the allowed ScreenRow attributes
    VALUE    := signed number (scientific notation allowed)

Example:
    (range_pct > 3 AND volume_60m > 5e5) OR atr_pct_60m >= 5

`parse_filter(expr)` returns a callable predicate `(ScreenRow) -> bool`.
None-valued attributes (e.g. `gap_pct` with no `--with-gap`) evaluate
their comparison as **False** rather than crashing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .metrics import ScreenRow

# Public allow-list of comparable attribute names.
_ALLOWED_METRICS: frozenset[str] = frozenset(
    {
        "range_pct",
        "abs_return_pct",
        "signed_return_pct",
        "realized_vol_60m",
        "atr_pct_60m",
        "vwap_60m",
        "vwap_dev_pct",
        "gap_pct",
        "volume_60m",
        "dollar_volume_60m",
        "first_open",
        "last_close",
        "high_60m",
        "low_60m",
        "n_bars",
    }
)


class FilterParseError(ValueError):
    """Raised on invalid `--filter` syntax. Includes position hint."""


# ---- tokenizer ----

_TOKEN_RE = re.compile(
    r"""
    \s*(
        \(                                # opening paren
        | \)                              # closing paren
        | (?:>=|<=|==|!=|>|<)             # comparison op (>=,<= before >,<)
        | [A-Za-z_][A-Za-z_0-9]*          # identifier (metric / and / or)
        | [-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?  # number
        | .                               # fallback so we can error nicely
    )
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Token:
    kind: str   # "lp" | "rp" | "op" | "id" | "num" | "eof"
    value: str
    pos: int


def _tokenize(expr: str) -> list[_Token]:
    out: list[_Token] = []
    for m in _TOKEN_RE.finditer(expr):
        s = m.group(1)
        if s is None or s.strip() == "":
            continue
        start = m.start(1)
        if s == "(":
            out.append(_Token("lp", s, start))
        elif s == ")":
            out.append(_Token("rp", s, start))
        elif s in (">", ">=", "<", "<=", "==", "!="):
            out.append(_Token("op", s, start))
        elif re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", s):
            out.append(_Token("id", s, start))
        elif re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", s):
            out.append(_Token("num", s, start))
        else:
            raise FilterParseError(f"Unexpected character {s!r} at position {start}")
    out.append(_Token("eof", "", len(expr)))
    return out


# ---- parser ----


class _Parser:
    def __init__(self, expr: str):
        self.expr = expr
        self.tokens = _tokenize(expr)
        self.i = 0

    def _peek(self) -> _Token:
        return self.tokens[self.i]

    def _eat(self, kind: str | None = None, value: str | None = None) -> _Token:
        tok = self.tokens[self.i]
        if kind is not None and tok.kind != kind:
            raise FilterParseError(
                f"Expected {kind} at pos {tok.pos}, got {tok.kind} ({tok.value!r})"
            )
        if value is not None and tok.value.lower() != value.lower():
            raise FilterParseError(
                f"Expected {value!r} at pos {tok.pos}, got {tok.value!r}"
            )
        self.i += 1
        return tok

    def parse(self) -> Callable[[ScreenRow], bool]:
        pred = self._or_expr()
        if self._peek().kind != "eof":
            tok = self._peek()
            raise FilterParseError(
                f"Unexpected token {tok.value!r} at pos {tok.pos}"
            )
        return pred

    def _or_expr(self) -> Callable[[ScreenRow], bool]:
        left = self._and_expr()
        while self._peek().kind == "id" and self._peek().value.lower() == "or":
            self._eat("id")
            right = self._and_expr()
            l_, r_ = left, right

            def _or(row, l_=l_, r_=r_):
                return l_(row) or r_(row)

            left = _or
        return left

    def _and_expr(self) -> Callable[[ScreenRow], bool]:
        left = self._atom()
        while self._peek().kind == "id" and self._peek().value.lower() == "and":
            self._eat("id")
            right = self._atom()
            l_, r_ = left, right

            def _and(row, l_=l_, r_=r_):
                return l_(row) and r_(row)

            left = _and
        return left

    def _atom(self) -> Callable[[ScreenRow], bool]:
        tok = self._peek()
        if tok.kind == "lp":
            self._eat("lp")
            inner = self._or_expr()
            if self._peek().kind != "rp":
                raise FilterParseError(
                    f"Missing ')' (opened at pos {tok.pos})"
                )
            self._eat("rp")
            return inner
        # comparison
        if tok.kind != "id":
            raise FilterParseError(
                f"Expected metric name or '(' at pos {tok.pos}, got {tok.value!r}"
            )
        metric_tok = self._eat("id")
        metric = metric_tok.value
        if metric.lower() in {"and", "or"}:
            raise FilterParseError(
                f"Unexpected boolean {metric!r} at pos {metric_tok.pos}"
            )
        if metric not in _ALLOWED_METRICS:
            raise FilterParseError(
                f"Unknown metric {metric!r} at pos {metric_tok.pos}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_METRICS))}"
            )
        op_tok = self._eat("op")
        val_tok = self._eat("num")
        try:
            threshold = float(val_tok.value)
        except ValueError as exc:
            raise FilterParseError(
                f"Invalid number {val_tok.value!r} at pos {val_tok.pos}"
            ) from exc

        op = op_tok.value
        return _make_comparison(metric, op, threshold)


_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _make_comparison(
    metric: str, op: str, threshold: float
) -> Callable[[ScreenRow], bool]:
    cmp = _OPS[op]

    def _pred(row: ScreenRow) -> bool:
        v = getattr(row, metric, None)
        if v is None:
            return False  # None-valued attribute -> filtered out, never crash.
        try:
            return cmp(float(v), threshold)
        except (TypeError, ValueError):
            return False

    _pred.__name__ = f"filter_{metric}_{op}_{threshold:g}"
    return _pred


def parse_filter(expr: str) -> Callable[[ScreenRow], bool]:
    """Compile `expr` into a row predicate. Raises FilterParseError on bad
    input. Empty/whitespace input is treated as the always-True predicate."""
    if not expr or not expr.strip():
        return lambda row: True
    return _Parser(expr).parse()


def compile_filters(exprs: list[str]) -> Callable[[ScreenRow], bool]:
    """Compose multiple expressions with implicit AND. Returns always-True
    when the list is empty.
    """
    preds = [parse_filter(e) for e in exprs if e and e.strip()]
    if not preds:
        return lambda row: True
    if len(preds) == 1:
        return preds[0]

    def _all(row: ScreenRow) -> bool:
        return all(p(row) for p in preds)

    return _all
