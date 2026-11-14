"""独立复核：不信任分析器的结论，用独立路径重新核算。

复核原则：

* 自行从原始 bundle 重新解析标尺、分数、权重多面体与 margin，不读分析器中间量；
* 所有见证只做**直接有理代入**（Fraction，不用浮点），逐条验证非严格/严格关系；
* 最佳/最差名次区间用独立的 2^m 子集穷举（独立重建行、独立调用 FME）重算；
* 不可行结论不仅验证矛盾子集确实不可行，还验证其删除极小性；
* 任何不符都收集到 mismatches，不静默通过。
"""
from __future__ import annotations

from fractions import Fraction
import time

from . import technology_rank_polyhedron as poly


def _f(value):
    return Fraction(value)


def _strict_less(lhs: Fraction, rhs: Fraction, strict: bool) -> bool:
    return lhs < rhs if strict else lhs <= rhs


class IndependentVerifier:
    def __init__(self, bundle: dict, row_limit: int | None = None,
                 time_limit: float | None = None):
        self.bundle = bundle
        self.mismatches: list[str] = []
        self.checks: list[str] = []
        self._row_limit = (poly.DEFAULT_ROW_LIMIT if row_limit is None
                           else row_limit)
        self._deadline = (time.monotonic() + time_limit
                          if time_limit is not None else None)

    def _time_up(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline

    def _record(self, ok: bool, message: str) -> None:
        self.checks.append(("OK " if ok else "FAIL ") + message)
        if not ok:
            self.mismatches.append(message)

    # -- 独立解析 ----------------------------------------------------------

    def _scales(self):
        scales = []
        for c in self.bundle["criteria"]:
            spec = c.get("scale", {"type": "identity"})
            if spec["type"] == "identity":
                scales.append((Fraction(1), Fraction(0), c["direction"]))
            else:
                scales.append((_f(spec["scale"]),
                               _f(spec.get("shift", "0")), c["direction"]))
        return scales

    def _scenario_specs(self):
        if "scenarios" in self.bundle:
            return {s["id"]: s for s in self.bundle["scenarios"]}
        return {self.bundle["scenario"]: {"assignments": {}}}

    def _scores(self, spec):
        """独立折分：变量赋值 -> 原始值 -> 仿射标尺。"""
        assignment = {k: _f(v) for k, v in spec.get("assignments", {}).items()}
        scales = self._scales()
        matrix = {}
        for tech in self.bundle["technologies"]:
            cells = tech.get("raw", tech.get("scores"))
            vector = []
            for j, cell in enumerate(cells):
                if cell is None:
                    vector.append(None)
                    continue
                if isinstance(cell, dict):
                    if "var" in cell:
                        raw = assignment[cell["var"]]
                    else:
                        raw = _f(cell.get("const", "0"))
                        for term in cell["affine"]:
                            raw += _f(term["coeff"]) * assignment[term["var"]]
                else:
                    raw = _f(cell)
                if "raw" in tech:
                    a, b, _ = scales[j]
                    vector.append(a * raw + b)
                else:
                    vector.append(raw)
            matrix[tech["id"]] = vector
        return matrix

    def _weight_rows(self):
        """独立重建权重多面体，并按加入顺序给每行编号（origins=行号）。"""
        n = len(self.bundle["criteria"])
        policy = self.bundle["weight_policy"]
        rows = []

        def add(row):
            rows.append(poly.Row(row.coeffs, row.rhs, row.strict,
                                 frozenset([len(rows)])))

        for j in range(n):
            add(poly.make_row(
                [-1 if k == j else 0 for k in range(n)],
                -_f(policy["lower"][j])))
            add(poly.make_row(
                [1 if k == j else 0 for k in range(n)],
                _f(policy["upper"][j])))
        for r in poly.make_equality_rows([1] * n, _f(policy.get("sum", "1"))):
            add(r)
        for spec in policy.get("constraints", []):
            if spec.get("type") == "ratio":
                num, den = spec["numerator"], spec["denominator"]
                coeffs = [0] * n
                if "min" in spec and spec["min"] is not None:
                    c2 = coeffs.copy()
                    c2[num], c2[den] = -1, _f(spec["min"])
                    add(poly.make_row(c2, 0))
                if "max" in spec and spec["max"] is not None:
                    c2 = coeffs.copy()
                    c2[num], c2[den] = 1, -_f(spec["max"])
                    add(poly.make_row(c2, 0))
                continue
            coeffs = [_f(c) for c in spec["coeffs"]]
            rhs = _f(spec["rhs"])
            relation = spec.get("relation", "<=")
            if relation == "=":
                for r in poly.make_equality_rows(coeffs, rhs):
                    add(r)
            elif relation == "<=":
                add(poly.make_row(coeffs, rhs))
            elif relation == ">=":
                add(poly.make_row([-c for c in coeffs], -rhs))
            elif relation == "<":
                add(poly.make_row(coeffs, rhs, poly.STRICT))
            elif relation == ">":
                add(poly.make_row([-c for c in coeffs], -rhs, poly.STRICT))
        return rows

    # -- 直接代入 ----------------------------------------------------------

    def _named_weight_rows(self):
        """重建权重行并附带与主分析器一致的行名，便于按名称抽取冲突子集。"""
        n = len(self.bundle["criteria"])
        policy = self.bundle["weight_policy"]
        named = []

        def add(name, row):
            named.append((name, poly.Row(row.coeffs, row.rhs, row.strict,
                                         frozenset([len(named)]))))

        for j in range(n):
            add(f"lower[{j}]", poly.make_row(
                [-1 if k == j else 0 for k in range(n)],
                -_f(policy["lower"][j])))
        for j in range(n):
            add(f"upper[{j}]", poly.make_row(
                [1 if k == j else 0 for k in range(n)],
                _f(policy["upper"][j])))
        for r in poly.make_equality_rows([1] * n, _f(policy.get("sum", "1"))):
            add("weight_policy.sum" if r.rhs == _f(policy.get("sum", "1"))
                else "weight_policy.sum(等式反向)", r)
        for idx, spec in enumerate(policy.get("constraints", [])):
            origin = f"policy.constraints[{idx}]"
            if spec.get("type") == "ratio":
                num, den = spec["numerator"], spec["denominator"]
                coeffs = [0] * n
                if "min" in spec and spec["min"] is not None:
                    c2 = coeffs.copy()
                    c2[num], c2[den] = -1, _f(spec["min"])
                    add(origin + f": w{num} >= {spec['min']}*w{den}",
                        poly.make_row(c2, 0))
                if "max" in spec and spec["max"] is not None:
                    c2 = coeffs.copy()
                    c2[num], c2[den] = 1, -_f(spec["max"])
                    add(origin + f": w{num} <= {spec['max']}*w{den}",
                        poly.make_row(c2, 0))
                continue
            coeffs = [_f(c) for c in spec["coeffs"]]
            rhs = _f(spec["rhs"])
            relation = spec.get("relation", "<=")
            if relation == "=":
                rows = poly.make_equality_rows(coeffs, rhs)
                add(origin + f": = {rhs}", rows[0])
                add(origin + "(等式反向)", rows[1])
            elif relation == "<=":
                add(origin + f": {relation} {rhs}",
                    poly.make_row(coeffs, rhs))
            elif relation == ">=":
                add(origin + f": {relation} {rhs}",
                    poly.make_row([-c for c in coeffs], -rhs))
            elif relation == "<":
                add(origin + f": {relation} {rhs}",
                    poly.make_row(coeffs, rhs, poly.STRICT))
            elif relation == ">":
                add(origin + f": {relation} {rhs}",
                    poly.make_row([-c for c in coeffs], -rhs, poly.STRICT))
        return named

    def _rebuild_named_rows(self, names):
        by_name = {name: row for name, row in self._named_weight_rows()}
        return [by_name[name] for name in names if name in by_name]

    def _substitute(self, rows, point):
        for row in rows:
            lhs = sum(c * x for c, x in zip(row.coeffs, point))
            if not _strict_less(lhs, row.rhs, row.strict):
                return False, row
        return True, None

    def _weight_point(self, witness_block, n):
        if witness_block is None:
            return None
        weights = witness_block.get("weights", witness_block)
        values = list(weights.values())
        return tuple(_f(v) for v in values[:n])

    # -- 独立名次穷举 ------------------------------------------------------

    def _independent_rank_interval(self, rows, diffs, margin, n):
        """对所有竞争者做独立的 beat/tie 叶子穷举。

        每个叶子对每个竞争者显式选择 beat 行（-d·w <= -m）或
        tie 行（d·w < m），因此只有叶子见证对应精确超过者数；
        与主分析器完全独立地重建行、独立调用 FME。
        """
        competitors = list(diffs)
        counts = []
        limited = 0
        timed_out = False
        for mask in range(1 << len(competitors)):
            if (mask & 63) == 0 and self._time_up():
                timed_out = True
                break
            extra = []
            included = []
            for i, tid in enumerate(competitors):
                d = diffs[tid]
                if mask & (1 << i):
                    extra.append(poly.make_row([-c for c in d], -margin))
                    included.append(tid)
                else:
                    extra.append(poly.make_row(d, margin, poly.STRICT))
            result = poly.solve(list(rows) + extra, n,
                                self._row_limit, self._deadline)
            if result.limited:
                limited += 1
                continue
            if result.feasible:
                counts.append((mask.bit_count(), result.witness, included))
        if timed_out:
            return ("limited", limited + (1 << len(competitors)))
        if not counts:
            return None if not limited else ("limited", limited)
        return (min(c for c, _, _ in counts),
                max(c for c, _, _ in counts), counts, limited)

    # -- 入口 --------------------------------------------------------------

    def verify(self, report: dict) -> dict:
        n = len(self.bundle["criteria"])
        margin = _f(self.bundle["margin"])
        specs = self._scenario_specs()
        base_rows = self._weight_rows()

        for block in report["results"]:
            sid = block["scenario"]
            prefix = f"[{sid}]"
            matrix = self._scores(specs[sid])
            target = report["target"]

            # 标尺方向追溯：成本类必须负仿射，效益仿射必须正
            for j, criterion in enumerate(self.bundle["criteria"]):
                spec = criterion.get("scale", {"type": "identity"})
                if criterion.get("direction") == "cost":
                    self._record(
                        spec["type"] == "affine" and _f(spec["scale"]) < 0,
                        f"{prefix} 成本准则 {criterion['id']} 带负仿射标尺且可追溯")
                if spec["type"] == "affine" and criterion.get("direction") == "benefit":
                    self._record(_f(spec["scale"]) > 0,
                                 f"{prefix} 效益准则 {criterion['id']} 仿射方向为正")

            # 证据不全的技术不得出现在正式竞争者里
            incomplete = {tid for tid, vec in matrix.items()
                          if any(v is None for v in vec)}
            if block["status"] == "ranked":
                self._record(
                    not (set(block["competitors"]) & incomplete),
                    f"{prefix} 证据不全技术未进入正式榜：{sorted(incomplete)}")
                self._record(
                    target not in incomplete,
                    f"{prefix} 目标技术 {target} 证据齐全")

            # 基础多面体见证直接代入
            base_claim = poly.solve(base_rows, n, self._row_limit, self._deadline)
            if block["status"] == "ranked":
                point = self._weight_point(block["base_witness"], n)
                if point is not None:
                    ok, bad = self._substitute(base_rows, point)
                    self._record(ok, f"{prefix} 基础见证满足全部权重约束"
                                 + ("" if ok else f"，违例行 {bad}"))
            if block["status"] == "policy_infeasible":
                self._record(base_claim.feasible is False,
                             f"{prefix} 独立复核确认权重政策不可行")
                minimal_names = block["policy_conflict"]["minimal_conflict"]
                # 名称级追溯性检查：最小冲突的行名都来自原始政策声明
                allowed = {"weight_policy.sum", "weight_policy.sum(等式反向)"}
                allowed |= {f"lower[{j}]" for j in range(n)}
                allowed |= {f"upper[{j}]" for j in range(n)}
                self._record(
                    all(name in allowed or name.startswith("policy.constraints")
                        for name in minimal_names),
                    f"{prefix} 冲突说明全部可追溯到原始政策行：{minimal_names}")
                # 独立重建一个小而矛盾的子集：按冲突类型重新构造并验证极小性
                subset = self._rebuild_named_rows(minimal_names)
                subset_result = poly.solve(subset, n, self._row_limit, self._deadline) if subset else None
                self._record(
                    subset_result is not None
                    and subset_result.feasible is False,
                    f"{prefix} 声明的矛盾子集独立判定仍不可行")
                if subset and subset_result.feasible is False:
                    origins = {o for r in subset for o in r.origins}
                    for row_origin in sorted(origins):
                        smaller = [r for r in subset
                                   if row_origin not in r.origins]
                        smaller_res = poly.solve(smaller, n, self._row_limit, self._deadline)
                        self._record(
                            smaller_res.feasible is not False,
                            f"{prefix} 矛盾行 {row_origin} 不可或缺"
                            "（删除后恢复可行或结论未知）")
                continue
            if block["status"] == "evidence_infeasible":
                self._record(block["evidence_conflict"] is not None,
                             f"{prefix} 证据冲突说明存在")
                continue
            if block["status"] == "resource_limited":
                # 资源受限是诚实的“未知”声明：验证它没有同时给出认证名次
                ranking = block.get("ranking")
                self._record(
                    ranking is None or not ranking.get("certified_complete"),
                    f"{prefix} 资源受限时未宣称认证名次")
                continue

            # 分数矩阵一致性（独立折分 vs 报告内见证所引用的分差）
            t_scores = matrix[target]
            diffs = {tid: [a - b for a, b in zip(vec, t_scores)]
                     for tid, vec in matrix.items()
                     if tid != target and tid not in incomplete}

            # 成对关系独立复核
            for entry in block["pair_relations"]:
                tid = entry["competitor"]
                d = diffs[tid]
                beat_rows = base_rows + [poly.make_row([-c for c in d], -margin)]
                tie_rows = base_rows + [poly.make_row(d, margin, poly.STRICT)]
                beat_res = poly.solve(beat_rows, n, self._row_limit, self._deadline)
                tie_res = poly.solve(tie_rows, n, self._row_limit, self._deadline)
                beat_feasible, tie_feasible = beat_res.feasible, tie_res.feasible
                if beat_res.limited or tie_res.limited:
                    # 独立复核也触及上限：只能核对报告是否同样未做强断言
                    self._record(
                        entry["relation"] in (
                            "unknown_due_to_resources", "unknown_due_to_budget"),
                        f"{prefix} 对 {tid} 的关系在资源受限时未被强断言")
                else:
                    expected = ("necessary" if not tie_feasible else
                                "impossible" if not beat_feasible else "possible")
                    if entry["relation"] != "unknown_due_to_budget":
                        self._record(entry["relation"] == expected,
                                     f"{prefix} 成对关系 {tid}={entry['relation']}"
                                     f"（独立重算 {expected}）")
                for key in ("beat_witness", "tie_witness"):
                    point = self._weight_point(entry.get(key), n)
                    if point is None:
                        continue
                    ok, bad = self._substitute(base_rows, point)
                    if ok:
                        gap = sum(c * w for c, w in zip(d, point))
                        if key == "beat_witness":
                            ok = gap >= margin
                        else:
                            ok = gap < margin
                    self._record(ok, f"{prefix} {tid} {key} 直接代入成立"
                                 + ("" if ok else f"（违例行 {bad}）"))

            # 名次区间独立穷举比对（完整可达计数集合，而非仅端点）
            independent = self._independent_rank_interval(
                base_rows, diffs, margin, n)
            ranking = block["ranking"]
            if independent is None:
                self._record(False, f"{prefix} 独立穷举未找到任何可行区域")
            elif (isinstance(independent, tuple)
                  and independent and independent[0] == "limited"):
                # 独立复核同样资源受限：只能核对报告没有过度声称
                self._record(
                    not ranking["certified_complete"],
                    f"{prefix} 独立复核资源受限时，报告未宣称完整认证")
            else:
                imin, imax, counts, limited = independent
                independent_ranks = sorted({c + 1 for c, _, _ in counts})
                if ranking["certified_complete"] and limited == 0:
                    self._record(
                        ranking["best_rank"] == imin + 1,
                        f"{prefix} 最佳名次 {ranking['best_rank']}（独立 {imin + 1}）")
                    self._record(
                        ranking["worst_rank"] == imax + 1,
                        f"{prefix} 最差名次 {ranking['worst_rank']}（独立 {imax + 1}）")
                    self._record(
                        ranking["attainable_ranks"] == independent_ranks,
                        f"{prefix} 完整可达名次集合 {ranking['attainable_ranks']}"
                        f"（独立 {independent_ranks}）")
                else:
                    # 未认证完整时，找到的见证只能是独立可达集合的子集
                    self._record(
                        set(ranking["attainable_ranks"]) <= set(independent_ranks),
                        f"{prefix} 资源受限下找到的见证均落在独立可达集合内")

            # 每个见证：直接代入 + 实际计名 + 名次自洽
            for rank, witness in ranking["witnesses"].items():
                point = self._weight_point(witness["weights"], n)
                ok, bad = self._substitute(base_rows, point)
                self._record(ok, f"{prefix} 名次 {rank} 见证满足权重约束"
                             + ("" if ok else f"（违例行 {bad}）"))
                actual = 0
                for tid, d in diffs.items():
                    gap = sum(c * w for c, w in zip(d, point))
                    declared = witness["beat_check"][tid]
                    recomputed = "beats" if gap >= margin else "tied_or_below"
                    self._record(
                        declared == recomputed,
                        f"{prefix} 名次 {rank} 对 {tid} 的逐对分差判定自洽"
                        f"（gap={gap}, margin={margin}）")
                    if gap >= margin:
                        actual += 1
                self._record(
                    rank == actual + 1,
                    f"{prefix} 名次 {rank} 与独立计名 {actual + 1} 一致")
                self._record(
                    sorted(witness["beaters"]) ==
                    sorted(t for t in diffs
                           if sum(c * w for c, w in zip(diffs[t], point))
                           >= margin),
                    f"{prefix} 名次 {rank} 的超过者名单与独立计算一致")

        return {"ok": not self.mismatches,
                "check_count": len(self.checks),
                "checks": self.checks,
                "mismatches": self.mismatches}


def verify_report(bundle: dict, report: dict, row_limit: int | None = None,
                  time_limit: float | None = None) -> dict:
    """对外入口：独立复核一份分析报告。"""
    return IndependentVerifier(
        bundle, row_limit=row_limit, time_limit=time_limit).verify(report)
