"""差分测试：FME 认证结论 vs 独立的朴素网格枚举。

网格枚举只作为**参照实现**交叉验证，不参与产品内的任何认证；
产品的证明路径始终是精确 FME 叶子枚举。这里用小维数、稠密网格，
检查最佳/最差名次端点与成对关系在两套独立实现下一致。
"""
import itertools
import random
import unittest
from fractions import Fraction

from src.technology_rank_input_adapter import loads
from src.technology_rank_regions import analyze


def grid_weights(lower, upper, steps):
    """在单纯形上枚举有理权重网格点（朴素参照，非证明手段）。"""
    n = len(lower)
    grids = []
    for j in range(n):
        lo, hi = Fraction(lower[j]), Fraction(upper[j])
        grids.append([lo + (hi - lo) * Fraction(k, steps)
                      for k in range(steps + 1)])
    for point in itertools.product(*grids):
        if sum(point) == 1 and all(
                Fraction(lower[j]) <= point[j] <= Fraction(upper[j])
                for j in range(n)):
            yield point


def naive_ranks(bundle, target):
    """朴素枚举全部网格点，记录每个竞争者在网格上的 beat/tie 观察。

    网格是有限采样，观察不到 beat 不能证明不可能；因此只返回原始观察
    集合，由调用方按“FME 强结论必须与所有网格点相容”的方向比对。
    """
    n = len(bundle["criteria"])
    margin = Fraction(bundle["margin"])
    matrix = {t["id"]: [Fraction(x) for x in t["scores"]]
              for t in bundle["technologies"]}
    target_vec = matrix[target]
    competitors = [tid for tid in matrix if tid != target]
    seen_ranks = set()
    observed = {tid: {"beat": False, "tie": False} for tid in competitors}
    grid = list(grid_weights(
        bundle["weight_policy"]["lower"],
        bundle["weight_policy"]["upper"], 12))
    if not grid:
        # 网格与有理边界不对齐时，退回 60 等分（仍只是参照采样）
        grid = list(grid_weights(
            bundle["weight_policy"]["lower"],
            bundle["weight_policy"]["upper"], 60))
    for w in grid:
        beat = []
        for tid in competitors:
            gap = sum((a - b) * x for a, b, x in
                      zip(matrix[tid], target_vec, w))
            if gap >= margin:
                beat.append(tid)
                observed[tid]["beat"] = True
            if gap < margin:
                observed[tid]["tie"] = True
        seen_ranks.add(len(beat) + 1)
    if not seen_ranks:
        return None, None, observed
    return min(seen_ranks), max(seen_ranks), observed


class DifferentialTests(unittest.TestCase):
    def test_fme_matches_naive_grid_across_cases(self):
        random.seed(20261114)
        rng = random.Random(7)
        for case in range(40):
            n = rng.randint(2, 3)
            m = rng.randint(2, 4)
            technologies = []
            for i in range(m):
                technologies.append({
                    "id": f"T{i}",
                    "scores": [str(rng.randint(0, 10)) for _ in range(n)]})
            margin = str(rng.choice(["0.1", "0.25", "0.5", "1"]))
            # 保证权重网格非空：下界之和 <= 1
            lows = [rng.choice(["0", "0.1", "0.2"]) for _ in range(n)]
            while sum(Fraction(x) for x in lows) > 1:
                lows = ["0" for _ in range(n)]
            bundle = {
                "schema_version": 1,
                "technologies": technologies,
                "criteria": [{"id": f"c{j}", "direction": "benefit"}
                             for j in range(n)],
                "weight_policy": {"sum": "1", "lower": lows,
                                  "upper": ["1"] * n, "constraints": []},
                "margin": margin, "scenario": "s", "scales_version": "s1"}
            bundle = loads(__import__("json").dumps(bundle))
            target = "T0"
            report = analyze(bundle, target)
            ranking = report["results"][0]["ranking"]
            g_best, g_worst, observed = naive_ranks(bundle, target)
            # 网格是多面体的有限采样：它见到的名次必须落在 FME 认证范围内，
            # 但 FME 可能认证出网格采不到的边界名次（方向只能单向约束）
            self.assertGreaterEqual(ranking["best_rank"], 1)
            self.assertLessEqual(ranking["worst_rank"], m)
            if g_best is not None and ranking["certified_complete"]:
                self.assertGreaterEqual(g_best, ranking["best_rank"])
                self.assertLessEqual(g_worst, ranking["worst_rank"])
            # FME 的强结论必须与每一个网格采样点相容：
            # - FME 判 impossible：任何网格点都不应观察到 beat
            # - FME 判 necessary：任何网格点都不应观察到 tie
            fme_pairs = {p["competitor"]: p["relation"]
                         for p in report["results"][0]["pair_relations"]}
            for tid, rel in fme_pairs.items():
                if rel == "impossible":
                    self.assertFalse(
                        observed[tid]["beat"],
                        f"case {case}: FME 判 {tid} 不可能超过，但网格出现 beat")
                elif rel == "necessary":
                    self.assertFalse(
                        observed[tid]["tie"],
                        f"case {case}: FME 判 {tid} 必然超过，但网格出现 tie")
                else:
                    # possible 是存在性结论，网格因稀疏可以漏检，不反向约束
                    self.assertEqual(rel, "possible")

    def test_exact_grid_agreement_on_dense_canonical(self):
        # 题面范例的稠密网格应与 FME 端点完全一致
        bundle = loads(__import__("json").dumps({
            "schema_version": 1,
            "technologies": [
                {"id": "T", "scores": ["0.5", "0.5"]},
                {"id": "A", "scores": ["1", "0"]},
                {"id": "B", "scores": ["0", "1"]}],
            "criteria": [{"id": "energy", "direction": "benefit"},
                         {"id": "land", "direction": "benefit"}],
            "weight_policy": {"sum": "1", "lower": ["0", "0"],
                              "upper": ["1", "1"], "constraints": []},
            "margin": "0.1", "scenario": "s", "scales_version": "s1"}))
        g_best, g_worst, _ = naive_ranks(bundle, "T")
        ranking = analyze(bundle, "T")["results"][0]["ranking"]
        self.assertEqual((g_best, g_worst),
                         (ranking["best_rank"], ranking["worst_rank"]))
        self.assertEqual((g_best, g_worst), (1, 2))


if __name__ == "__main__":
    unittest.main()
