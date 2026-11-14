"""Fourier-Motzkin 精确引擎的单元测试。"""
import unittest
from fractions import Fraction

from src import technology_rank_polyhedron as poly


def rows_from(specs, n):
    """specs: (coeffs, rhs, strict) 列表。"""
    out = []
    for i, spec in enumerate(specs):
        coeffs, rhs = spec[0], spec[1]
        strict = spec[2] if len(spec) > 2 else poly.NONSTRICT
        out.append(poly.Row(tuple(Fraction(c) for c in coeffs),
                            Fraction(rhs), strict, frozenset([i])))
    return out


class FmeTests(unittest.TestCase):
    def test_simple_feasible_and_witness(self):
        # x1 + x2 = 1, x1 >= 0.2, x2 >= 0.2
        rows = rows_from([
            ([-1, 0], "-0.2"),
            ([0, -1], "-0.2"),
            ([1, 1], "1"),
            ([-1, -1], "-1"),
        ], 2)
        result = poly.solve(rows, 2)
        self.assertTrue(result.feasible)
        x1, x2 = result.witness
        self.assertGreaterEqual(x1, Fraction("0.2"))
        self.assertGreaterEqual(x2, Fraction("0.2"))
        self.assertEqual(x1 + x2, 1)

    def test_strict_inequality_witness(self):
        # x1 < 0.5, x1 > 0.4
        rows = rows_from([
            ([1, 0], "0.5", poly.STRICT),
            ([-1, 0], "-0.4", poly.STRICT),
        ], 2)
        result = poly.solve(rows, 2)
        self.assertTrue(result.feasible)
        self.assertLess(result.witness[0], Fraction("0.5"))
        self.assertGreater(result.witness[0], Fraction("0.4"))

    def test_strict_boundary_infeasible(self):
        # x1 >= 1 且 x1 < 1 => 不可行；若把严格改成 <= 则可行
        infeasible = rows_from([
            ([-1, 0], "-1"),
            ([1, 0], "1", poly.STRICT),
        ], 2)
        self.assertFalse(poly.solve(infeasible, 2).feasible)
        feasible = rows_from([
            ([-1, 0], "-1"),
            ([1, 0], "1"),
        ], 2)
        self.assertTrue(poly.solve(feasible, 2).feasible)

    def test_equality_infeasible_conflict_origins(self):
        # x1 >= 0.8, x2 >= 0.8, x1+x2 = 1
        rows = rows_from([
            ([-1, 0], "-0.8"),
            ([0, -1], "-0.8"),
            ([1, 1], "1"),
            ([-1, -1], "-1"),
        ], 2)
        result = poly.solve(rows, 2)
        self.assertFalse(result.feasible)
        self.assertTrue(result.origins)

    def test_minimal_conflict_irredundant(self):
        rows = rows_from([
            ([-1, 0], "-0.8"),
            ([0, -1], "-0.8"),
            ([1, 1], "1"),
            ([-1, -1], "-1"),
        ], 2)
        result = poly.solve(rows, 2)
        minimal = poly.minimal_conflict(rows, 2, result.origins)
        # 最小冲突里每个来源删除后都应恢复可行
        for origin in minimal:
            smaller = [r for r in rows if origin not in r.origins]
            self.assertTrue(poly.solve(smaller, 2).feasible,
                            f"来源 {origin} 不应出现在最小冲突中")
        # 最小冲突整体仍不可行
        subset = [r for r in rows
                  if any(o in minimal for o in r.origins)]
        self.assertFalse(poly.solve(subset, 2).feasible)

    def test_oracle_cache_reuse(self):
        base = rows_from([([1, 1], "1"), ([-1, -1], "-1"),
                          ([-1, 0], "0"), ([0, -1], "0")], 2)
        oracle = poly.FeasibilityOracle(base, 2)
        extra = poly.make_row([-1, 0], "-0.6")
        oracle.add_extra_row("r", extra)
        first = oracle.check(["r"])
        calls_after_first = oracle.calls
        second = oracle.check(["r"])  # 同指纹命中缓存
        self.assertEqual(first.feasible, second.feasible)
        self.assertEqual(oracle.calls, calls_after_first)

    def test_no_float_anywhere(self):
        rows = rows_from([(["0.1", "0.2"], "0.3")], 2)
        result = poly.solve(rows, 2)
        self.assertTrue(result.feasible)
        for value in result.witness:
            self.assertIsInstance(value, Fraction)

    def test_three_dimensional_polytope(self):
        # 单纯形 w1+w2+w3=1, wi>=0.1
        rows = rows_from([
            ([-1, 0, 0], "-0.1"),
            ([0, -1, 0], "-0.1"),
            ([0, 0, -1], "-0.1"),
            ([1, 1, 1], "1"),
            ([-1, -1, -1], "-1"),
        ], 3)
        result = poly.solve(rows, 3)
        self.assertTrue(result.feasible)
        self.assertEqual(sum(result.witness), 1)
        self.assertTrue(all(w >= Fraction("0.1") for w in result.witness))


if __name__ == "__main__":
    unittest.main()
