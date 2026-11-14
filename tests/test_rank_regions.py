"""名次区域分析的领域测试：覆盖任务书的全部判定约定。"""
import unittest
from fractions import Fraction

from src.technology_rank_input_adapter import loads
from src.technology_rank_regions import (
    analyze, build_models, compare_bundles, BundleError, RankEngine)
from src.technology_rank_verify import verify_report


def canonical_bundle(margin="0.1", lower=None, upper=None, policy=None):
    bundle = {
        "schema_version": 1,
        "technologies": [
            {"id": "T", "scores": ["0.5", "0.5"]},
            {"id": "A", "scores": ["1", "0"]},
            {"id": "B", "scores": ["0", "1"]},
        ],
        "criteria": [
            {"id": "energy", "direction": "benefit"},
            {"id": "land", "direction": "benefit"},
        ],
        "weight_policy": policy or {
            "sum": "1",
            "lower": lower or ["0", "0"],
            "upper": upper or ["1", "1"],
            "constraints": [],
        },
        "margin": margin,
        "scenario": "frozen-1",
        "scales_version": "s1",
    }
    return bundle


class CanonicalExampleTests(unittest.TestCase):
    def setUp(self):
        self.bundle = loads(__import__("json").dumps(canonical_bundle()))

    def test_best_and_worst_rank(self):
        report = analyze(self.bundle, "T")
        ranking = report["results"][0]["ranking"]
        # T 最好并列第一；A、B 不能同时超过 T，最差第二
        self.assertEqual(ranking["best_rank"], 1)
        self.assertEqual(ranking["worst_rank"], 2)
        self.assertEqual(ranking["attainable_ranks"], [1, 2])
        self.assertTrue(ranking["certified_complete"])

    def test_pairwise_possible_but_not_joint(self):
        report = analyze(self.bundle, "T")
        block = report["results"][0]
        relations = {p["competitor"]: p["relation"]
                     for p in block["pair_relations"]}
        self.assertEqual(relations, {"A": "possible", "B": "possible"})
        # 名次 3（A、B 同时超过）不可达：见证里没有 rank 3
        self.assertNotIn(3, block["ranking"]["witnesses"])
        # 名次 2 的两个见证分别只有一个超过者
        beaters = block["ranking"]["witnesses"][2]["beaters"]
        self.assertEqual(len(beaters), 1)

    def test_tie_at_half_weights(self):
        report = analyze(self.bundle, "T")
        w = report["results"][0]["ranking"]["witnesses"][1]["weights"]
        # 第 1 名见证上，A、B 与 T 的分差都小于 margin（含并列）
        block = report["results"][0]
        gaps = block["ranking"]["witnesses"][1]["pairwise_gaps"]
        for tid in ("A", "B"):
            self.assertLess(abs(Fraction(gaps[tid])),
                            Fraction(self.bundle["margin"]))
        self.assertEqual(sum(Fraction(v) for v in w.values()), 1)

    def test_independent_verification_passes(self):
        report = analyze(self.bundle, "T")
        result = verify_report(self.bundle, report)
        self.assertTrue(result["ok"], result["mismatches"])
        self.assertGreater(result["check_count"], 10)


class MarginSemanticsTests(unittest.TestCase):
    def test_large_margin_forces_tie(self):
        # margin=0.6 时 A 对 T 的最大可能分差仅 0.5，无人能超过 => 恒第 1
        bundle = loads(__import__("json").dumps(canonical_bundle(margin="0.6")))
        report = analyze(bundle, "T")
        ranking = report["results"][0]["ranking"]
        self.assertEqual(ranking["best_rank"], 1)
        self.assertEqual(ranking["worst_rank"], 1)
        self.assertEqual(ranking["forced_beaters"], [])
        relations = {p["competitor"]: p["relation"]
                     for p in report["results"][0]["pair_relations"]}
        self.assertEqual(set(relations.values()), {"impossible"})

    def test_zero_margin_rejected(self):
        with self.assertRaises(ValueError):
            loads(__import__("json").dumps(canonical_bundle(margin="0")))


class ScaleAndEvidenceTests(unittest.TestCase):
    def test_cost_requires_traceable_affine_scale(self):
        bundle = canonical_bundle()
        bundle["criteria"][0] = {"id": "cost", "direction": "cost"}
        with self.assertRaises(ValueError):
            build_models(loads(__import__("json").dumps(bundle)))
        # 正仿射的成本标尺也必须拒绝（适配器或领域层均属 ValueError 家族）
        bundle["criteria"][0] = {
            "id": "cost", "direction": "cost",
            "scale": {"type": "affine", "scale": "1", "shift": "0"}}
        with self.assertRaises(ValueError):
            build_models(loads(__import__("json").dumps(bundle)))

    def test_cost_affine_transformation_applied(self):
        bundle = {
            "schema_version": 1,
            "technologies": [
                {"id": "T", "raw": ["4"]},
                {"id": "A", "raw": ["2"]},
            ],
            "criteria": [{
                "id": "unit_cost", "direction": "cost",
                "scale": {"type": "affine", "scale": "-1", "shift": "10",
                          "formula": "score = 10 - raw",
                          "source": "frozen scale s1"}}],
            "weight_policy": {"sum": "1", "lower": ["1"], "upper": ["1"],
                              "constraints": []},
            "margin": "0.1", "scenario": "s", "scales_version": "s1",
        }
        models = build_models(loads(__import__("json").dumps(bundle)))
        matrix = models[0].score_matrix
        # raw=4 -> 6；raw=2 -> 8（成本越低，越大越好的分数越高）
        self.assertEqual(matrix["T"], [Fraction(6)])
        self.assertEqual(matrix["A"], [Fraction(8)])
        self.assertEqual(models[0].scales[0].formula, "score = 10 - raw")

    def test_incomplete_technology_excluded_not_zero_filled(self):
        bundle = canonical_bundle()
        bundle["technologies"].append({"id": "C", "scores": ["1", None]})
        bundle = loads(__import__("json").dumps(bundle))
        report = analyze(bundle, "T")
        block = report["results"][0]
        self.assertIn("C", block["excluded_incomplete"])
        self.assertNotIn("C", block["competitors"])
        # 目标自身证据不全必须报错
        with self.assertRaises(BundleError):
            RankEngine(build_models(bundle)[0], "C")

    def test_target_missing_technology_raises(self):
        bundle = loads(__import__("json").dumps(canonical_bundle()))
        with self.assertRaises(BundleError):
            analyze(bundle, "GHOST")


class MultiScenarioTests(unittest.TestCase):
    def test_scenarios_solved_separately(self):
        # 情景 s1 与 s2 给出互斥的证据赋值，结果并列输出，见证不串用
        bundle = {
            "schema_version": 1,
            "technologies": [
                {"id": "T", "raw": [{"var": "x"}]},
                {"id": "A", "raw": [{"var": "y"}]},
            ],
            "criteria": [{"id": "c1", "direction": "benefit"}],
            "evidence": {"variables": [
                {"id": "x", "lower": "0", "upper": "10"},
                {"id": "y", "lower": "0", "upper": "10"}]},
            "scenarios": [
                {"id": "s1", "frozen": True,
                 "assignments": {"x": "5", "y": "5"}},
                {"id": "s2", "frozen": True,
                 "assignments": {"x": "5", "y": "9"}},
            ],
            "weight_policy": {"sum": "1", "lower": ["1"], "upper": ["1"],
                              "constraints": []},
            "margin": "0.1", "scenario": "s1", "scales_version": "s1",
        }
        bundle = loads(__import__("json").dumps(bundle))
        report = analyze(bundle, "T")
        ids = [b["scenario"] for b in report["results"]]
        self.assertEqual(ids, ["s1", "s2"])
        s1, s2 = report["results"]
        # s1 分数相等 => 并列第 1；s2 中 A 明显更高 => T 最差第 2
        self.assertEqual(s1["ranking"]["worst_rank"], 1)
        self.assertEqual(s2["ranking"]["worst_rank"], 2)
        # 两个情景的版本分别留存
        self.assertEqual(s1["versions"]["scenario"], "s1")
        self.assertEqual(s2["versions"]["scenario"], "s2")

    def test_infeasible_scenario_reported_not_solved(self):
        bundle = {
            "schema_version": 1,
            "technologies": [{"id": "T", "raw": [{"var": "x"}]}],
            "criteria": [{"id": "c1", "direction": "benefit"}],
            "evidence": {"variables": [
                {"id": "x", "lower": "0", "upper": "10"}]},
            "scenarios": [{
                "id": "bad", "frozen": True,
                "assignments": {"x": "5"},
                "joint_constraints": [{
                    "type": "linear", "coeffs": ["1"],
                    "rhs": "9", "relation": ">="}]}],
            "weight_policy": {"sum": "1", "lower": ["1"], "upper": ["1"],
                              "constraints": []},
            "margin": "0.1", "scenario": "bad", "scales_version": "s1",
        }
        bundle = loads(__import__("json").dumps(bundle))
        report = analyze(bundle, "T")
        block = report["results"][0]
        self.assertEqual(block["status"], "evidence_infeasible")
        self.assertIn("minimal_conflict", block["evidence_conflict"])


class PolicyAndComparisonTests(unittest.TestCase):
    def test_infeasible_policy_conflict_report(self):
        bundle = canonical_bundle(lower=["0.8", "0.8"])
        bundle = loads(__import__("json").dumps(bundle))
        report = analyze(bundle, "T")
        block = report["results"][0]
        self.assertEqual(block["status"], "policy_infeasible")
        names = block["policy_conflict"]["minimal_conflict"]
        self.assertIn("weight_policy.sum", names)
        self.assertTrue(any(n.startswith("lower[") for n in names))

    def test_ratio_constraint_changes_region(self):
        base = canonical_bundle()
        tightened = canonical_bundle(policy={
            "sum": "1", "lower": ["0", "0"], "upper": ["1", "1"],
            "version": "p2",
            "constraints": [{"type": "ratio", "numerator": 0,
                             "denominator": 1, "min": "2", "max": None}]})
        base_b = loads(__import__("json").dumps(base))
        mod_b = loads(__import__("json").dumps(tightened))
        report = compare_bundles(base_b, mod_b, "T")
        entry = report["comparison"][0]
        self.assertEqual(entry["base_versions"]["policy_version"], None)
        self.assertEqual(entry["modified_versions"]["policy_version"], "p2")
        change = entry["rank_region_change"]
        self.assertNotEqual(change["base"], change["modified"])

    def test_budget_does_not_overclaim(self):
        bundle = loads(__import__("json").dumps(canonical_bundle()))
        report = analyze(bundle, "T", budget=1)
        ranking = report["results"][0]["ranking"]
        self.assertFalse(ranking["certified_complete"])
        self.assertIsNotNone(ranking["budget_exhaustion"])


class NecessaryBeaterTests(unittest.TestCase):
    def test_necessary_and_impossible_classification(self):
        # A 在两个准则上都严格强于 T 且分差超过 margin => 必然超过
        bundle = {
            "schema_version": 1,
            "technologies": [
                {"id": "T", "scores": ["0.2", "0.2"]},
                {"id": "A", "scores": ["0.9", "0.9"]},
                {"id": "B", "scores": ["0.0", "0.0"]},
            ],
            "criteria": [
                {"id": "c1", "direction": "benefit"},
                {"id": "c2", "direction": "benefit"}],
            "weight_policy": {"sum": "1", "lower": ["0", "0"],
                              "upper": ["1", "1"], "constraints": []},
            "margin": "0.1", "scenario": "s", "scales_version": "s1",
        }
        bundle = loads(__import__("json").dumps(bundle))
        report = analyze(bundle, "T")
        block = report["results"][0]
        relations = {p["competitor"]: p["relation"]
                     for p in block["pair_relations"]}
        self.assertEqual(relations["A"], "necessary")
        self.assertEqual(relations["B"], "impossible")
        ranking = block["ranking"]
        self.assertEqual(ranking["forced_beaters"], ["A"])
        # A 恒超过 => T 永远至少第 2，且 B 不可能超过 => 不会到第 3
        self.assertEqual(ranking["best_rank"], 2)
        self.assertEqual(ranking["worst_rank"], 2)
        self.assertTrue(verify_report(bundle, report)["ok"])


if __name__ == "__main__":
    unittest.main()
