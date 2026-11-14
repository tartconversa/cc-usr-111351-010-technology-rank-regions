"""技术多准则排序的联合可达名次分析（精确有理数，零第三方依赖）。

设计要点（与评审约定逐条对应）：

1. 所有准则先按**冻结标尺**转成“越大越好”的分数；成本类方向转换记录完整
   仿射公式与来源，可追溯，不做隐式取负或隐式补零。
2. 权重多面体：非负、和为 1、逐分量上下界、线性比例约束，全部表示为
   有理线性不等式，交由 Fourier-Motzkin 引擎精确求解。
3. 证据变量也带线性联合约束；每个**已冻结证据情景**先独立校验可行性，
   再在该情景固定的分数矩阵上、仅在权重多面体内求名次。多情景逐情景求解、
   并列输出，绝不把不同情景的名次见证拼成同一方案。
4. 名次 = 1 + 分数超过目标至少 margin 的竞争者数；|分差| < margin 视为并列。
   最差名次由**联合计数**分支限界得到：多个竞争者必须在同一组权重下同时
   超过目标，不能由各自单独可能胜出推断。
5. 缺少某准则的技术标记为证据不全，排除出正式榜，绝不默认补零。
6. 随机抽样不构成证明：所有结论来自穷举式可行性判定（FME），预算不足时
   明确区分“已认证范围”和“仅找到的见证”。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import time
from typing import Iterable, Sequence

from . import technology_rank_polyhedron as poly

# 关系符号到内部表示
_RELATIONS = {"<=": (poly.NONSTRICT, 1), ">=": (poly.NONSTRICT, -1),
              "<": (poly.STRICT, 1), ">": (poly.STRICT, -1),
              "=": None}


def _frac(value, what: str) -> Fraction:
    if value is None:
        raise ValueError(what + "不能为 null")
    if isinstance(value, bool):
        raise ValueError(what + "不能是布尔值")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValueError(what + "不是合法有理数：" + repr(value)) from exc


def _positive(value: Fraction, what: str) -> Fraction:
    if value <= 0:
        raise ValueError(what + "必须为正有理数")
    return value


# ---------------------------------------------------------------------------
# 标尺与证据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scale:
    """冻结标尺：score = scale * raw + shift，方向由 sign 显式表达。"""

    criterion_id: str
    direction: str  # benefit / cost
    kind: str       # identity / affine
    scale: Fraction
    shift: Fraction
    formula: str
    source: str

    def apply(self, raw: Fraction) -> Fraction:
        return self.scale * raw + self.shift

    def as_dict(self) -> dict:
        return {"criterion": self.criterion_id, "direction": self.direction,
                "kind": self.kind, "scale": str(self.scale),
                "shift": str(self.shift), "formula": self.formula,
                "source": self.source}


@dataclass(frozen=True)
class EvidenceVariable:
    vid: str
    lower: Fraction
    upper: Fraction


@dataclass
class ScenarioModel:
    """一个已冻结证据情景解析后的完整模型。"""

    sid: str
    frozen: bool
    scales: tuple[Scale, ...]
    criterion_ids: tuple[str, ...]
    policy_version: str | None
    margin: Fraction
    # tech_id -> 分数向量（None 表示该准则证据缺失）
    score_matrix: dict[str, list[Fraction | None]]
    incomplete: dict[str, list[int]]
    weight_rows: list[poly.Row]
    weight_origins: list[str]
    evidence_conflict: dict | None = None
    versions: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 资料解析与校验
# ---------------------------------------------------------------------------

class BundleError(ValueError):
    """资料结构或业务约束错误。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BundleError(message)


def parse_scales(bundle: dict) -> list[Scale]:
    scales = []
    for criterion in bundle["criteria"]:
        cid = criterion["id"]
        direction = criterion.get("direction", "benefit")
        _require(direction in ("benefit", "cost"),
                 f"准则 {cid} 的 direction 只能是 benefit/cost")
        spec = criterion.get("scale", {"type": "identity"})
        kind = spec.get("type", "identity")
        if kind == "identity":
            scale, shift = Fraction(1), Fraction(0)
            formula = f"score({cid}) = raw"
        elif kind == "affine":
            scale = _frac(spec["scale"], f"准则 {cid} 标尺 scale")
            shift = _frac(spec.get("shift", "0"), f"准则 {cid} 标尺 shift")
            formula = spec.get("formula",
                               f"score({cid}) = {scale} * raw + {shift}")
        else:
            raise BundleError(f"准则 {cid} 的标尺类型未知：{kind}")
        if direction == "cost" and kind == "identity":
            raise BundleError(
                f"成本类准则 {cid} 必须给出显式 affine 标尺，"
                "方向转换必须可追溯，不允许隐式取负")
        if direction == "cost" and scale >= 0:
            raise BundleError(
                f"成本类准则 {cid} 的 affine scale 必须为负（越大越好）")
        if direction == "benefit" and kind == "affine" and scale <= 0:
            raise BundleError(f"效益类准则 {cid} 的 affine scale 必须为正")
        scales.append(Scale(cid, direction, kind, scale, shift, formula,
                            spec.get("source", "frozen scale")))
    return scales


def parse_evidence(bundle: dict) -> tuple[dict[str, EvidenceVariable], list[dict]]:
    block = bundle.get("evidence")
    if not block:
        return {}, []
    variables = {}
    for spec in block.get("variables", []):
        vid = spec["id"]
        _require(vid not in variables, f"证据变量重复：{vid}")
        lo, hi = _frac(spec["lower"], f"证据变量 {vid} 下界"), \
                 _frac(spec["upper"], f"证据变量 {vid} 上界")
        _require(lo <= hi, f"证据变量 {vid} 下界超过上界")
        variables[vid] = EvidenceVariable(vid, lo, hi)
    return variables, block.get("constraints", [])


def _eval_cell(cell, assignment: dict[str, Fraction], what: str) -> Fraction:
    """把一个原始单元格（十进制/null/变量引用/仿射表达式）折成有理数。"""
    if cell is None:
        return None
    if isinstance(cell, (str, int)):
        return _frac(cell, what)
    if isinstance(cell, dict):
        if "var" in cell:
            vid = cell["var"]
            _require(vid in assignment, f"{what} 引用了未赋值的证据变量 {vid}")
            return assignment[vid]
        if "affine" in cell:
            total = _frac(cell.get("const", "0"), what + " 的 const")
            for term in cell["affine"]:
                vid = term["var"]
                _require(vid in assignment,
                         f"{what} 引用了未赋值的证据变量 {vid}")
                total += _frac(term["coeff"], what + " 的仿射系数") * assignment[vid]
            return total
    raise BundleError(f"{what} 的单元格形式无法识别：{cell!r}")


def _linear_rows(specs: Sequence[dict], names: Sequence[str],
                 n: int, origin_prefix: str) -> tuple[list[poly.Row], list[str]]:
    """通用线性约束（权重政策或证据联合约束）转不等式行。"""
    rows, registry = [], []
    for idx, spec in enumerate(specs):
        stype = spec.get("type", "linear")
        origin = f"{origin_prefix}[{idx}]"
        if stype == "ratio":
            num, den = spec["numerator"], spec["denominator"]
            _require(isinstance(num, int) and isinstance(den, int)
                     and 0 <= num < n and 0 <= den < n,
                     f"{origin} 的 ratio 下标越界")
            coeffs = [Fraction(0)] * n
            if "min" in spec and spec["min"] is not None:
                lo = _frac(spec["min"], origin + " min")
                _require(lo >= 0, f"{origin} 的比例下界不能为负")
                coeffs_lo = coeffs.copy()
                coeffs_lo[num], coeffs_lo[den] = Fraction(-1), lo
                rows.append(poly.make_row(coeffs_lo, 0, poly.NONSTRICT))
                registry.append(origin + f": w{num} >= {lo}*w{den}")
            if "max" in spec and spec["max"] is not None:
                hi = _frac(spec["max"], origin + " max")
                coeffs_hi = coeffs.copy()
                coeffs_hi[num], coeffs_hi[den] = Fraction(1), -hi
                rows.append(poly.make_row(coeffs_hi, 0, poly.NONSTRICT))
                registry.append(origin + f": w{num} <= {hi}*w{den}")
            continue
        coeffs = [_frac(c, origin + " 系数") for c in spec["coeffs"]]
        _require(len(coeffs) == n, f"{origin} 系数维数不符")
        rhs = _frac(spec["rhs"], origin + " rhs")
        relation = spec.get("relation", "<=")
        if relation == "=":
            rows.extend(poly.make_equality_rows(coeffs, rhs))
            registry.append(origin + f": = {rhs}")
            registry.append(origin + "(等式反向)")
            continue
        _require(relation in _RELATIONS, f"{origin} 关系符号未知：{relation}")
        strict, sign = _RELATIONS[relation]
        rows.append(poly.make_row([sign * c for c in coeffs],
                                  sign * rhs, strict))
        registry.append(origin + f": {relation} {rhs}")
    return rows, registry


def build_weight_polytope(bundle: dict, n: int):
    policy = bundle["weight_policy"]
    lower = [_frac(v, "权重下界") for v in policy["lower"]]
    upper = [_frac(v, "权重上界") for v in policy["upper"]]
    _require(len(lower) == n and len(upper) == n,
             "权重上下界维数与准则数不一致")
    _require(all(lo >= 0 for lo in lower), "权重下界不能为负")
    rows, registry = [], []
    for j, lo in enumerate(lower):
        rows.append(poly.make_row([Fraction(-1) if k == j else Fraction(0)
                                   for k in range(n)], -lo))
        registry.append(f"lower[{j}]")
    for j, hi in enumerate(upper):
        rows.append(poly.make_row([Fraction(1) if k == j else Fraction(0)
                                   for k in range(n)], hi))
        registry.append(f"upper[{j}]")
    target_sum = _frac(policy.get("sum", "1"), "权重和")
    rows.extend(poly.make_equality_rows([Fraction(1)] * n, target_sum))
    registry.extend(["weight_policy.sum", "weight_policy.sum(等式反向)"])
    extra, extra_names = _linear_rows(policy.get("constraints", []),
                                      [f"w{j}" for j in range(n)], n,
                                      "policy.constraints")
    rows.extend(extra)
    registry.extend(extra_names)
    # 重新编号 origins，保证与 registry 一一对应
    numbered = [poly.Row(r.coeffs, r.rhs, r.strict, frozenset([i]))
                for i, r in enumerate(rows)]
    return numbered, registry


def _scenario_specs(bundle: dict) -> list[dict]:
    if "scenarios" in bundle:
        specs = bundle["scenarios"]
        _require(isinstance(specs, list) and specs, "scenarios 必须为非空列表")
        ids = [s["id"] for s in specs]
        _require(len(set(ids)) == len(ids), "情景 id 重复")
        return specs
    # 向后兼容：无 scenarios 块时，单一冻结情景沿用顶层 scenario 名
    return [{"id": bundle["scenario"], "frozen": True,
             "assignments": {}, "joint_constraints": []}]


def _check_evidence_scenario(spec: dict, variables: dict[str, EvidenceVariable],
                             global_constraints: list[dict]) -> dict | None:
    """对冻结情景的证据赋值做边界与联合约束可行性校验。"""
    assignment = {vid: _frac(value, f"情景 {spec['id']} 赋值 {vid}")
                  for vid, value in spec.get("assignments", {}).items()}
    unknown = set(assignment) - set(variables)
    if unknown:
        raise BundleError(f"情景 {spec['id']} 赋值了未知证据变量："
                          + ",".join(sorted(unknown)))
    n = len(variables)
    if n == 0:
        _require(not spec.get("joint_constraints"),
                 f"情景 {spec['id']} 含证据约束但未声明证据变量")
        return None
    order = list(variables)
    rows, registry = [], []
    for j, vid in enumerate(order):
        var = variables[vid]
        rows.append(poly.make_row([Fraction(-1) if k == j else Fraction(0)
                                   for k in range(n)], -var.lower))
        registry.append(f"evidence.lower[{vid}]")
        rows.append(poly.make_row([Fraction(1) if k == j else Fraction(0)
                                   for k in range(n)], var.upper))
        registry.append(f"evidence.upper[{vid}]")
        if vid in assignment:
            rows.extend(poly.make_equality_rows(
                [Fraction(1) if k == j else Fraction(0) for k in range(n)],
                assignment[vid]))
            registry.extend([f"assignment[{vid}]",
                             f"assignment[{vid}](反向)"])
    all_constraints = list(global_constraints) + list(
        spec.get("joint_constraints", []))
    extra, extra_names = _linear_rows(all_constraints, order, n,
                                      "evidence.constraints")
    rows.extend(extra)
    registry.extend(extra_names)
    numbered = [poly.Row(r.coeffs, r.rhs, r.strict, frozenset([i]))
                for i, r in enumerate(rows)]
    result = poly.solve(numbered, n)
    if result.feasible:
        return None
    minimal = poly.minimal_conflict(numbered, n, result.origins)
    return {"scenario": spec["id"],
            "conflict_origins": sorted(result.origins),
            "minimal_conflict": [registry[i] for i in minimal],
            "all_constraints": registry}


def build_scenario_model(bundle: dict, spec: dict,
                         scales: list[Scale],
                         evidence_block: tuple) -> ScenarioModel:
    variables, global_constraints = evidence_block
    conflict = _check_evidence_scenario(spec, variables, global_constraints)
    n = len(scales)
    assignment = {vid: _frac(value, f"情景 {spec['id']} 赋值 {vid}")
                  for vid, value in spec.get("assignments", {}).items()}
    matrix, incomplete = {}, {}
    for tech in bundle["technologies"]:
        tid = tech["id"]
        cells = tech.get("raw", tech.get("scores"))
        _require(cells is not None, f"技术 {tid} 缺少 raw/scores")
        _require(len(cells) == n, f"技术 {tid} 的准则维数不符")
        vector, missing = [], []
        for j, cell in enumerate(cells):
            raw = _eval_cell(cell, assignment, f"技术 {tid} 准则 {j}")
            if raw is None:
                vector.append(None)
                missing.append(j)
                continue
            if "raw" in tech:
                vector.append(scales[j].apply(raw))
            else:
                # 已冻结分数（向后兼容）：原样采纳，标尺记为 identity 来源
                vector.append(raw)
        matrix[tid] = vector
        if missing:
            incomplete[tid] = missing
    weight_rows, weight_origins = build_weight_polytope(bundle, n)
    return ScenarioModel(
        sid=spec["id"], frozen=bool(spec.get("frozen", True)),
        scales=tuple(scales), criterion_ids=tuple(c["id"] for c in bundle["criteria"]),
        policy_version=bundle["weight_policy"].get("version"),
        margin=_positive(_frac(bundle["margin"], "margin"), "margin"),
        score_matrix=matrix, incomplete=incomplete,
        weight_rows=weight_rows, weight_origins=weight_origins,
        evidence_conflict=conflict,
        versions={"schema_version": bundle["schema_version"],
                  "scales_version": bundle["scales_version"],
                  "scenario": spec["id"],
                  "policy_version": bundle["weight_policy"].get("version")})


def build_models(bundle: dict) -> list[ScenarioModel]:
    scales = parse_scales(bundle)
    evidence_block = parse_evidence(bundle)
    return [build_scenario_model(bundle, spec, scales, evidence_block)
            for spec in _scenario_specs(bundle)]


# ---------------------------------------------------------------------------
# 名次分析
# ---------------------------------------------------------------------------

class RankEngine:
    """单情景、单目标的名次区域分析器。"""

    def __init__(self, model: ScenarioModel, target: str, budget: int = 10000,
                 row_limit: int | None = None, time_limit: float | None = None):
        self.model = model
        self.target = target
        self.budget = budget
        self._calls = 0
        if target not in model.score_matrix:
            raise BundleError(f"目标技术 {target} 不在资料的技术清单中")
        if target in model.incomplete:
            raise BundleError(
                f"目标技术 {target} 在准则 "
                f"{[model.criterion_ids[j] for j in model.incomplete[target]]} "
                "证据不全，不能进入正式排序（不允许补零）")
        self._target_scores = model.score_matrix[target]
        self.competitors = [
            tid for tid in model.score_matrix
            if tid != target and tid not in model.incomplete]
        self.excluded = {tid: idx for tid, idx in model.incomplete.items()
                         if tid != target}
        deadline = (time.monotonic() + time_limit
                    if time_limit is not None else None)
        self._deadline = deadline
        self.oracle = poly.FeasibilityOracle(
            model.weight_rows, len(model.criterion_ids),
            row_limit=(poly.DEFAULT_ROW_LIMIT if row_limit is None
                       else row_limit),
            deadline=deadline)
        self._beat_key, self._tie_key, self._diff = {}, {}, {}
        for tid in self.competitors:
            diff = [a - b for a, b in zip(model.score_matrix[tid],
                                          self._target_scores)]
            self._diff[tid] = diff
            # 超过：d·w >= m  <=>  -d·w <= -m（非严格）
            self.oracle.add_extra_row(
                f"beat:{tid}",
                poly.make_row([-c for c in diff], -model.margin))
            # 未超过（含并列）：d·w < m（严格）
            self.oracle.add_extra_row(
                f"tie:{tid}",
                poly.make_row(diff, model.margin, poly.STRICT))
            self._beat_key[tid] = f"beat:{tid}"
            self._tie_key[tid] = f"tie:{tid}"

    # -- 基础多面体 --------------------------------------------------------

    def check_base(self) -> poly.Feasibility:
        return self.oracle.check_base()

    # -- 预算包装 ----------------------------------------------------------

    def _deadline_reached(self) -> bool:
        return (self._deadline is not None
                and time.monotonic() >= self._deadline)

    def _check(self, keys: Iterable[str]):
        if self._calls >= self.budget:
            raise poly.BudgetExhausted(self.budget, self.oracle.calls)
        result = self.oracle.check(keys)
        self._calls = self.oracle.calls
        return result

    def _classify(self):
        """返回 (forced, impossible, possible, certified)。

        任一求解触及 FME 运算量上限（feasible is None）时，不做强分类，
        把该竞争者保守归入 possible 并置 certified=False，绝不把未知当
        “不可能”从而低估最差名次。
        """
        forced, impossible, possible, certified = [], [], [], True
        for tid in self.competitors:
            if self._deadline_reached():
                # 剩余竞争者全部保守归入 possible，不做强分类
                possible.extend(
                    t for t in self.competitors[self.competitors.index(tid):])
                certified = False
                break
            try:
                beat = self._check([self._beat_key[tid]])
                tie = self._check([self._tie_key[tid]])
            except poly.BudgetExhausted:
                possible.append(tid)
                certified = False
                continue
            if beat.limited or tie.limited:
                possible.append(tid)
                certified = False
                continue
            if not tie.feasible:
                forced.append(tid)
            elif not beat.feasible:
                impossible.append(tid)
            else:
                possible.append(tid)
        if self.oracle.limited_calls or self._deadline_reached():
            certified = False
        return forced, impossible, possible, certified

    # -- 成对关系 ----------------------------------------------------------

    def pair_relations(self) -> list[dict]:
        relations = []
        for pos, tid in enumerate(self.competitors):
            if self._deadline_reached():
                for rest in self.competitors[pos:]:
                    relations.append({
                        "competitor": rest,
                        "relation": "unknown_due_to_resources",
                        "diff_vector": [str(x) for x in self._diff[rest]],
                        "beat_witness": None, "tie_witness": None,
                        "note": "墙钟期限到达，关系未认证"})
                break
            try:
                beat = self._check([self._beat_key[tid]])
                tie = self._check([self._tie_key[tid]])
            except poly.BudgetExhausted:
                relations.append({"competitor": tid,
                                  "relation": "unknown_due_to_budget",
                                  "diff_vector": [str(x) for x in self._diff[tid]],
                                  "beat_witness": None, "tie_witness": None})
                continue
            if beat.limited or tie.limited:
                relations.append({"competitor": tid,
                                  "relation": "unknown_due_to_resources",
                                  "diff_vector": [str(x) for x in self._diff[tid]],
                                  "beat_witness": None, "tie_witness": None,
                                  "note": "FME 运算量超限，关系未认证"})
                continue
            if not tie.feasible:
                relation = "necessary"   # 必然超过
            elif not beat.feasible:
                relation = "impossible"  # 不可能超过
            else:
                relation = "possible"
            entry = {"competitor": tid, "relation": relation,
                     "diff_vector": [str(x) for x in self._diff[tid]],
                     "beat_witness": self._witness_dict(beat),
                     "tie_witness": self._witness_dict(tie)}
            if beat.feasible:
                entry["gap_at_beat_witness"] = str(self._gap(tid, beat.witness))
            if tie.feasible:
                entry["gap_at_tie_witness"] = str(self._gap(tid, tie.witness))
            relations.append(entry)
        return relations

    # -- 联合计数分支限界 --------------------------------------------------

    def rank_region(self) -> dict:
        """枚举 possible 竞争者的 beat/tie 全部分支，求精确可达名次集合。

        每个叶子区域对每个 possible 竞争者显式选择“超过”行或“未超过”行，
        因而叶子见证对应的超过者名单是精确的——不能只用基础多面体的可行点
        充当低名次见证（该点上可能还有别人超过目标）。
        """
        forced, impossible, possible, classified = self._classify()
        forced_keys = [self._beat_key[t] for t in forced]
        try:
            base = self._check(forced_keys)
        except poly.BudgetExhausted as exc:
            return self._assemble_ranking(
                forced, impossible, possible, {}, None, None,
                False, {"budget": exc.budget, "calls": exc.calls})
        if base.limited:
            # 连强制集合的联合区域都无法认证：不产出任何名次结论
            return self._assemble_ranking(
                forced, impossible, possible, {}, None, None, False,
                {"resource_limit": True,
                 "generated_rows": base.generated_rows})
        if not base.feasible:  # 理论上不应发生（forced 在整个多面体成立）
            raise AssertionError("内部错误：必然超过集合联合不可行")

        certified = classified
        exhaustion = None
        witnesses: dict[int, tuple] = {}
        resource_limited = False

        def dfs(idx: int, included: list[str], region: list[str]):
            nonlocal certified, exhaustion_local, resource_limited
            # 期限到达即停止枚举：剩余叶子一律不认证，避免空转遍历
            if self._deadline_reached():
                certified = False
                resource_limited = True
                return
            if idx == len(possible):
                try:
                    result = self._check(region)
                except poly.BudgetExhausted as exc:
                    certified = False
                    exhaustion_local = exc
                    return
                if result.limited:
                    # 未知：不记录见证、不宣称该名次可达或不可达
                    certified = False
                    resource_limited = True
                    return
                if result.feasible:
                    count = len(forced) + len(included)
                    if count not in witnesses:
                        witnesses[count] = (result.witness, frozenset(included))
                return
            tid = possible[idx]
            # 先试“超过”分支，尽快取得更差名次；不可行则整支剪枝（单调性）
            beat_region = region + [self._beat_key[tid]]
            tie_region = region + [self._tie_key[tid]]
            try:
                beat_res = self._check(beat_region)
                tie_res = self._check(tie_region)
            except poly.BudgetExhausted as exc:
                certified = False
                exhaustion_local = exc
                return
            # 未知分支保守地仍向下探索另一侧；只有确定可行才进入该分支
            if beat_res.feasible is True:
                dfs(idx + 1, included + [tid], beat_region)
            elif beat_res.limited:
                certified = False
                resource_limited = True
            if tie_res.feasible is True:
                dfs(idx + 1, included, tie_region)
            elif tie_res.limited:
                certified = False
                resource_limited = True

        exhaustion_local = None
        dfs(0, [], forced_keys)
        if exhaustion_local is not None:
            exhaustion = {"budget": exhaustion_local.budget,
                          "calls": exhaustion_local.calls}
        elif resource_limited:
            exhaustion = {"resource_limit": True,
                          "limited_calls": self.oracle.limited_calls}

        if witnesses:
            min_count, max_count = min(witnesses), max(witnesses)
        else:
            min_count = max_count = None
        return self._assemble_ranking(forced, impossible, possible,
                                      witnesses, min_count, max_count,
                                      certified, exhaustion)

    def _assemble_ranking(self, forced, impossible, possible, witnesses,
                          min_count, max_count, certified, exhaustion):
        witness_blocks = {}
        for count in sorted(witnesses):
            point, included = witnesses[count]
            beaters = sorted(forced) + sorted(included)
            gaps = {tid: self._gap(tid, point) for tid in self.competitors}
            witness_blocks[count + 1] = {
                "rank": count + 1,
                "weights": self._witness_dict(point, raw=True),
                "beaters": beaters,
                "pairwise_gaps": {tid: str(g) for tid, g in gaps.items()},
                "beat_check": {
                    tid: ("beats" if gaps[tid] >= self.model.margin
                          else "tied_or_below")
                    for tid in self.competitors}}
        return {
            "best_rank": None if min_count is None else min_count + 1,
            "worst_rank": None if max_count is None else max_count + 1,
            "attainable_ranks": sorted(witness_blocks),
            "certified_complete": certified and bool(witnesses),
            "forced_beaters": forced,
            "impossible_beaters": impossible,
            "possible_beaters": possible,
            "witnesses": witness_blocks,
            "feasibility_calls": self.oracle.calls,
            "budget": self.budget,
            "budget_exhaustion": exhaustion}

    # -- 小工具 ------------------------------------------------------------

    def _gap(self, tech: str, point: Sequence[Fraction]) -> Fraction:
        return sum(d * w for d, w in zip(self._diff[tech], point))

    def _witness_dict(self, result, raw=False):
        if result is None:
            return None
        point = result.witness if isinstance(result, poly.Feasibility) else result
        if point is None:
            return None
        weights = {cid: str(x) for cid, x in
                   zip(self.model.criterion_ids, point)}
        if raw or not isinstance(result, poly.Feasibility):
            return weights
        return {"weights": weights}


# ---------------------------------------------------------------------------
# 顶层编排
# ---------------------------------------------------------------------------

def analyze(bundle: dict, target: str, scenario_id: str | None = None,
            budget: int = 10000, row_limit: int | None = None,
            time_limit: float | None = None) -> dict:
    """对一个目标在（指定或全部）冻结情景下做名次分析，逐情景并列结果。"""
    models = build_models(bundle)
    if scenario_id is not None:
        models = [m for m in models if m.sid == scenario_id]
        if not models:
            raise BundleError(f"资料中不存在情景：{scenario_id}")
    blocks = []
    for model in models:
        block = {"scenario": model.sid, "frozen": model.frozen,
                 "versions": dict(model.versions),
                 "scales": [s.as_dict() for s in model.scales],
                 "margin": str(model.margin)}
        if model.evidence_conflict is not None:
            block["status"] = "evidence_infeasible"
            block["evidence_conflict"] = model.evidence_conflict
            blocks.append(block)
            continue
        engine = RankEngine(model, target, budget=budget,
                            row_limit=row_limit, time_limit=time_limit)
        base = engine.check_base()
        if base.limited:
            # 基础权重多面体都无法在运算量内判定：不进入正式排序
            block["status"] = "resource_limited"
            block["resource_limit"] = {
                "generated_rows": base.generated_rows,
                "note": "基础权重多面体的 FME 判定触及行数上限，结论未知"}
            blocks.append(block)
            continue
        if not base.feasible:
            minimal = poly.minimal_conflict(model.weight_rows,
                                            len(model.criterion_ids),
                                            base.origins)
            block["status"] = "policy_infeasible"
            block["policy_conflict"] = {
                "conflict_origins": sorted(base.origins),
                "minimal_conflict": [model.weight_origins[i] for i in minimal]}
            blocks.append(block)
            continue
        block["status"] = "ranked"
        block["excluded_incomplete"] = {
            tid: [model.criterion_ids[j] for j in idx]
            for tid, idx in engine.excluded.items()}
        block["competitors"] = engine.competitors
        block["base_witness"] = engine._witness_dict(base)
        block["pair_relations"] = engine.pair_relations()
        block["ranking"] = engine.rank_region()
        blocks.append(block)
    return {"target": target, "scenario_count": len(blocks),
            "results": blocks}


def compare_bundles(base_bundle: dict, modified_bundle: dict, target: str,
                    budget: int = 10000, row_limit: int | None = None,
                    time_limit: float | None = None) -> dict:
    """两份资料（替换证据或调整权重政策）的名次区域对比，版本全留存。"""
    base_report = analyze(base_bundle, target, budget=budget,
                          row_limit=row_limit, time_limit=time_limit)
    modified_report = analyze(modified_bundle, target, budget=budget,
                              row_limit=row_limit, time_limit=time_limit)
    by_id = {b["scenario"]: b for b in base_report["results"]}
    diffs = []
    for block in modified_report["results"]:
        sid = block["scenario"]
        old = by_id.get(sid)
        entry = {"scenario": sid,
                 "base_versions": old["versions"] if old else None,
                 "modified_versions": block["versions"]}
        if old is None or old["status"] != "ranked" or block["status"] != "ranked":
            entry["status_change"] = (None if old is None else old["status"],
                                      block["status"])
            diffs.append(entry)
            continue
        old_r, new_r = old["ranking"], block["ranking"]
        entry["rank_region_change"] = {
            "base": {"best": old_r["best_rank"], "worst": old_r["worst_rank"],
                     "attainable": old_r["attainable_ranks"]},
            "modified": {"best": new_r["best_rank"], "worst": new_r["worst_rank"],
                         "attainable": new_r["attainable_ranks"]}}
        old_pairs = {p["competitor"]: p["relation"]
                     for p in old["pair_relations"]}
        entry["pair_relation_changes"] = [
            {"competitor": p["competitor"],
             "from": old_pairs.get(p["competitor"]),
             "to": p["relation"]}
            for p in block["pair_relations"]
            if old_pairs.get(p["competitor"]) != p["relation"]]
        diffs.append(entry)
    return {"target": target, "comparison": diffs,
            "base_report": base_report, "modified_report": modified_report}
