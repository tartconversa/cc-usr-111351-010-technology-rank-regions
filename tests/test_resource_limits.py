"""资源护栏测试：超限必须诚实降级，绝不在未知时宣称认证名次。"""
import json
import unittest

from src import technology_rank_polyhedron as poly
from src.technology_rank_input_adapter import loads
from src.technology_rank_regions import analyze
from src.technology_rank_verify import verify_report


def big_bundle(n=6, m=12, seed=42):
    import random
    rng = random.Random(seed)
    return {
        "schema_version": 1,
        "technologies": [
            {"id": f"T{i}",
             "scores": [str(rng.randint(0, 10)) for _ in range(n)]}
            for i in range(m)],
        "criteria": [{"id": f"c{j}", "direction": "benefit"}
                     for j in range(n)],
        "weight_policy": {"sum": "1", "lower": ["0.05"] * n,
                          "upper": ["0.5"] * n, "constraints": []},
        "margin": "0.25", "scenario": "s", "scales_version": "s1"}


class ResourceGuardTests(unittest.TestCase):
    def test_solve_unknown_is_distinct_from_yes_no(self):
        # 用负行上限强制任何非平凡生成即触顶；feasible 必须为 None 而非 True/False
        rows = [poly.make_row([1, 1], 1), poly.make_row([1, -1], 0),
                poly.make_row([-1, 1], 0)]
        result = poly.solve(rows, 2, row_limit=-1)
        self.assertTrue(result.limited)
        self.assertIsNone(result.feasible)
        # 同一系统给足行限时必须能判定，证明 limited 确实是资源问题而非矛盾
        ok = poly.solve(rows, 2, row_limit=2000)
        self.assertTrue(ok.feasible)

    def test_time_limit_always_terminates(self):
        bundle = loads(json.dumps(big_bundle()))
        report = analyze(bundle, "T0", time_limit=2.0)
        ranking = report["results"][0]["ranking"]
        # 必须终止且不宣称完整认证；找到的见证（若有）不构成区间证明
        self.assertFalse(ranking["certified_complete"])
        self.assertIsNotNone(ranking["budget_exhaustion"])

    def test_verifier_does_not_overclaim_under_limits(self):
        bundle = loads(json.dumps(big_bundle()))
        report = analyze(bundle, "T0", time_limit=2.0)
        result = verify_report(bundle, report, time_limit=5.0)
        # 独立复核同样可能受限，但绝不能产生“报告过度声称”类失配
        self.assertTrue(result["ok"], result["mismatches"])

    def test_normal_problem_still_certified_with_default_limits(self):
        # 护栏不能误伤正常规模：3 准则 5 技术必须拿到完整认证
        bundle = loads(json.dumps(big_bundle(n=3, m=5)))
        report = analyze(bundle, "T0")
        ranking = report["results"][0]["ranking"]
        self.assertTrue(ranking["certified_complete"])
        self.assertIsNone(ranking["budget_exhaustion"])
        self.assertTrue(ranking["attainable_ranks"])


if __name__ == "__main__":
    unittest.main()
