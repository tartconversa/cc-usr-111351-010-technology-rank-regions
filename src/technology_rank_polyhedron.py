"""精确有理数线性不等式系统（Fourier-Motzkin 消元）。

只用标准库 fractions.Fraction，不依赖任何第三方求解器。支持：

* 非严格 (``<=``) 与严格 (``<``) 不等式、等式（拆成一对不等式）；
* 可行性判定与可行见证（有理数权重点）的构造式回代；
* 不可行时返回参与矛盾的原始约束来源，并可做删除极小化得到 IIS；
* 以约束集合指纹为键的可行性缓存（区域复用）。

Fourier-Motzkin 在最坏情况下约束数双指数增长，本项目准则维数很小
（技术评审通常个位数），且每次消元都做去重与恒真行删除，足够使用。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import time
from typing import Iterable, Mapping, Sequence

# 关系标记：False = 非严格 <=，True = 严格 <
STRICT = True
NONSTRICT = False


@dataclass(frozen=True)
class Row:
    """一行 a·x 关系 rhs；origins 记录来源原始约束编号。"""

    coeffs: tuple[Fraction, ...]
    rhs: Fraction
    strict: bool = NONSTRICT
    origins: frozenset[int] = frozenset()

    def key(self) -> tuple:
        return (self.coeffs, self.rhs, self.strict)


@dataclass(frozen=True)
class Feasibility:
    feasible: bool | None  # None 表示受运算量限制，结论未知
    witness: tuple[Fraction, ...] | None
    origins: frozenset[int]
    region_id: str
    limited: bool = False
    generated_rows: int = 0

    @property
    def status(self) -> str:
        if self.limited:
            return "unknown"
        return "feasible" if self.feasible else "infeasible"


class InfeasibleSystem(ValueError):
    """系统不可行，携带矛盾来源编号。"""

    def __init__(self, origins: frozenset[int]):
        super().__init__("约束系统不可行，矛盾来源编号：" + repr(sorted(origins)))
        self.origins = origins


class ResourceLimit(RuntimeError):
    """单次求解产生的行数超过上限，可行性结论未知（不得当作可行或不可行）。"""

    def __init__(self, generated: int, limit: int):
        super().__init__(f"FME 运算量超限：已生成 {generated} 行，上限 {limit}")
        self.generated = generated
        self.limit = limit


# 单次 solve 的默认生成行数上限（确定性护栏，非随机抽样）。
# 真实权重多面体（个位数准则、几十条约束）的完整消元通常只需数百行；
# 取 2000 既为正常问题留足余量，又让病态叶子快速报 unknown 而非长时间膨胀。
DEFAULT_ROW_LIMIT = 2_000


def _f(value) -> Fraction:
    if isinstance(value, bool):
        raise ValueError("布尔值不能作为有理系数")
    if isinstance(value, Fraction):
        return value
    return Fraction(value)


def make_row(coeffs: Iterable, rhs, strict: bool = NONSTRICT,
             origins: Iterable[int] = ()) -> Row:
    """由十进制字符串/整数/分数构造一行。"""
    cs = tuple(_f(c) for c in coeffs)
    return Row(cs, _f(rhs), strict, frozenset(origins))


def make_equality_rows(coeffs: Iterable, rhs, origins: Iterable[int] = ()) -> list[Row]:
    """a·x = b 拆成 a·x <= b 与 -a·x <= -b。"""
    cs = tuple(_f(c) for c in coeffs)
    b = _f(rhs)
    os = frozenset(origins)
    return [
        Row(cs, b, NONSTRICT, os),
        Row(tuple(-c for c in cs), -b, NONSTRICT, os),
    ]


def _drop_tautology(row: Row) -> Row | None:
    """全零系数行：0 <= rhs（或 0 < rhs），恒真则删除，矛盾则保留。"""
    if any(row.coeffs):
        return row
    if row.strict:
        return row if row.rhs <= 0 else None
    return row if row.rhs < 0 else None


def _normalized_split(rows: Sequence[Row], k: int):
    """按第 k 个变量把行分成下界组、上界组、零系数组并归一化。

    上界行形状 x_k + Σc_j x_j <= b（即 x_k <= b - Σc_j x_j）；
    下界行形状 x_k + Σc_j x_j >= b，内部用 x_k 系数为 +1 加 kind 区分。
    """
    lowers, uppers, zero = [], [], []
    for row in rows:
        a = row.coeffs[k]
        if a == 0:
            zero.append(row)
            continue
        scale = abs(a)
        coeffs = tuple(c / scale for c in row.coeffs)
        rhs = row.rhs / scale
        norm = Row(coeffs, rhs, row.strict, row.origins)
        if a > 0:
            uppers.append(norm)
        else:
            # a<0 归一化后 x_k 系数为 -1，翻成 x_k >= ...
            lowers.append(Row(tuple(-c for c in coeffs), -rhs,
                              row.strict, row.origins))
    return lowers, uppers, zero


def _combine(lower: Row, upper: Row, k: int) -> Row:
    """L <= x_k <= U 合并为 L <= U（任一严格则结果严格）。"""
    coeffs = tuple(
        (upper.coeffs[j] - lower.coeffs[j]) if j != k else Fraction(0)
        for j in range(len(upper.coeffs))
    )
    return Row(coeffs, upper.rhs - lower.rhs,
               lower.strict or upper.strict,
               lower.origins | upper.origins)


def _violated_constant(row: Row) -> bool:
    if any(row.coeffs):
        return False
    return row.rhs <= 0 if row.strict else row.rhs < 0


def _pick_variable(rows: Sequence[Row], remaining: list[int]) -> int:
    """贪心选取下界×上界组合数最小的变量先消元，抑制行数膨胀。"""
    best_k, best_score = remaining[0], None
    for k in remaining:
        lowers = uppers = 0
        for row in rows:
            a = row.coeffs[k]
            if a > 0:
                uppers += 1
            elif a < 0:
                lowers += 1
        score = (lowers * uppers, lowers + uppers)
        if best_score is None or score < best_score:
            best_score, best_k = score, k
    return best_k


def solve(rows: Sequence[Row], n_vars: int,
          row_limit: int = DEFAULT_ROW_LIMIT,
          deadline: float | None = None) -> Feasibility:
    """判定可行性；可行时构造并独立校验一个有理见证。

    采用贪心消元顺序减少组合增长；累计生成行数超过 row_limit，或墙钟
    到达 deadline 时，返回 limited 的未知结论，绝不猜测可行性。
    """
    def _deadline_hit() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    current: list[Row] = []
    seen: set = set()
    generated = 0
    for row in rows:
        if len(row.coeffs) != n_vars:
            raise ValueError("行系数维数与变量数不一致")
        kept = _drop_tautology(row)
        if kept is not None and kept.key() not in seen:
            seen.add(kept.key())
            current.append(kept)
        if kept is not None and _violated_constant(kept):
            return Feasibility(False, None, kept.origins,
                               _region_id(kept.origins),
                               generated_rows=generated)

    stages = []  # 每阶段保存 (实际变量编号, lowers, uppers) 供回代
    remaining = list(range(n_vars))
    while remaining:
        k = _pick_variable(current, remaining)
        remaining.remove(k)
        if _deadline_hit():
            return Feasibility(None, None, frozenset(), "deadline",
                               limited=True, generated_rows=generated)
        lowers, uppers, zero = _normalized_split(current, k)
        stages.append((k, lowers, uppers))
        nxt: list[Row] = list(zero)
        seen = {r.key() for r in nxt}
        for lower in lowers:
            for upper in uppers:
                if (generated & 255) == 0 and _deadline_hit():
                    return Feasibility(None, None, frozenset(), "deadline",
                                       limited=True, generated_rows=generated)
                combined = _drop_tautology(_combine(lower, upper, k))
                if combined is None:
                    continue
                if _violated_constant(combined):
                    return Feasibility(False, None, combined.origins,
                                       _region_id(combined.origins),
                                       generated_rows=generated)
                generated += 1
                if generated > row_limit:
                    return Feasibility(None, None, frozenset(),
                                       f"limited:{generated}",
                                       limited=True, generated_rows=generated)
                if combined.key() not in seen:
                    seen.add(combined.key())
                    nxt.append(combined)
        current = nxt

    witness = _back_substitute(stages, n_vars)
    _verify_witness(rows, witness)
    return Feasibility(True, witness, frozenset(),
                       "witness:" + _fmt_point(witness),
                       generated_rows=generated)


def _back_substitute(stages, n_vars: int) -> tuple[Fraction, ...]:
    assignment: dict[int, Fraction] = {}
    for k, lowers, uppers in reversed(stages):
        low_values = [(_evaluate(r, assignment), r.strict) for r in lowers]
        up_values = [(_evaluate(r, assignment), r.strict) for r in uppers]
        if not low_values and not up_values:
            assignment[k] = Fraction(0)
            continue
        if not low_values:
            u_min = min(v for v, _ in up_values)
            assignment[k] = u_min - 1
            continue
        l_max, l_strict = max(low_values, key=lambda item: item[0])
        if not up_values:
            assignment[k] = l_max + 1
            continue
        u_min, u_strict = min(up_values, key=lambda item: item[0])
        if l_max < u_min:
            assignment[k] = (l_max + u_min) / 2
        elif l_max == u_min and not l_strict and not u_strict:
            assignment[k] = l_max
        else:
            # 理论上不可达：消元阶段已判定可行
            raise InfeasibleSystem(frozenset())
    return tuple(assignment.get(j, Fraction(0)) for j in range(n_vars))


def _evaluate(row: Row, assignment: Mapping[int, Fraction]) -> Fraction:
    # 归一化行：x_k 系数 ±1，其余项给出边界常数 b - Σc_j x_j
    total = row.rhs
    for j, value in assignment.items():
        total -= row.coeffs[j] * value
    return total


def _verify_witness(rows: Sequence[Row], point: Sequence[Fraction]) -> None:
    """对原始约束逐行代入复核，防止消元/回代缺陷被当成结论。"""
    for row in rows:
        lhs = sum(c * x for c, x in zip(row.coeffs, point))
        if row.strict:
            ok = lhs < row.rhs
        else:
            ok = lhs <= row.rhs
        if not ok:
            raise AssertionError("内部错误：构造的见证不满足原始约束 "
                                 + str(row.origins))


def minimal_conflict(rows: Sequence[Row], n_vars: int,
                     origins: Iterable[int]) -> list[int]:
    """删除极小化：得到每个来源都不可或缺的矛盾来源子集（IIS）。

    同一来源编号可能对应多行（等式拆成一对不等式），删除时整组删除；
    若删除某来源后系统仍然矛盾，说明该来源并非不可或缺。若某次删除
    后的求解触及运算量上限（结论未知），保守保留该来源（不缩小冲突集）。
    """
    necessary = set(origins)
    for origin in sorted(origins):
        trial = [r for r in rows if origin not in r.origins]
        if not trial:
            # 删空后“无约束即可行”，该来源显然不可或缺
            continue
        result = solve(trial, n_vars)
        # 仅在确定“删除后仍矛盾”时才移除；未知时保守保留
        if result.feasible is False:
            necessary.discard(origin)
    return sorted(necessary)


def _region_id(origins: frozenset[int]) -> str:
    return "conflict:" + ",".join(str(o) for o in sorted(origins))


def _fmt_point(point: Sequence[Fraction]) -> str:
    return "(" + ",".join(str(x) for x in point) + ")"


class FeasibilityOracle:
    """以基础多面体 + 附加约束编号集合为键的可行性缓存（区域复用）。

    附加行通过 provider 按编号取用；同一 (基础指纹, 附加编号集合) 只求解一次，
    B&B 联合计数与成对关系判定共享全部缓存结论。
    """

    def __init__(self, base_rows: Sequence[Row], n_vars: int,
                 extra_rows: Mapping[str, Row] | None = None,
                 row_limit: int = DEFAULT_ROW_LIMIT,
                 deadline: float | None = None):
        """deadline 为 time.monotonic() 的绝对时刻；到期后新求解一律报未知。

        这是确定性的资源护栏（按墙钟），与行数上限互补，保证任何输入都会
        在期限内终止并以 limited 诚实声明，而不是无限膨胀。
        """
        self._n = n_vars
        self._base = tuple(base_rows)
        self._base_key = tuple(r.key() for r in self._base)
        self._extra = dict(extra_rows or {})
        self._row_limit = row_limit
        self._deadline = deadline
        self._cache: dict[frozenset[str], Feasibility] = {}
        self.calls = 0
        self.limited_calls = 0
        self.base_result = solve(self._base, self._n, row_limit, deadline)
        if self.base_result.limited:
            self.limited_calls += 1

    def add_extra_row(self, region_key: str, row: Row) -> None:
        self._extra[region_key] = row

    def check(self, region_keys: Iterable[str] = ()) -> Feasibility:
        key = frozenset(region_keys)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if self._deadline is not None and time.monotonic() >= self._deadline:
            result = Feasibility(None, None, frozenset(), "deadline",
                                 limited=True)
            self.limited_calls += 1
            self._cache[key] = result
            return result
        rows = list(self._base)
        for region_key in sorted(key):
            rows.append(self._extra[region_key])
        self.calls += 1
        result = solve(rows, self._n, self._row_limit, self._deadline)
        if result.limited:
            self.limited_calls += 1
        self._cache[key] = result
        return result

    def check_base(self) -> Feasibility:
        return self.base_result


class BudgetExhausted(RuntimeError):
    """可行性调用预算耗尽，已有结论只构成界，不构成完整认证。"""

    def __init__(self, budget: int, calls: int):
        super().__init__(f"求解预算耗尽：预算 {budget}，已调用 {calls}")
        self.budget = budget
        self.calls = calls
