"""CLI 端到端测试：rank / scenarios / compare / verify 子命令。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "fixtures" / "sample.json"
EVIDENCE = ROOT / "fixtures" / "evidence_scenarios.json"
INFEASIBLE = ROOT / "fixtures" / "infeasible_policy.json"


class CliTests(unittest.TestCase):
    def _run(self, *args):
        # 显式注入项目根到 PYTHONPATH；不从数据目录运行解释器，
        # 避免下载资料中的同名模块被 import
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.run(
            [sys.executable, "-P", "-m", "src.technology_rank_cli", *args],
            cwd=ROOT, capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_rank_canonical(self):
        out = self._run("rank", str(SAMPLE), "--target", "T")
        block = out["results"][0]
        self.assertEqual(block["ranking"]["best_rank"], 1)
        self.assertEqual(block["ranking"]["worst_rank"], 2)

    def test_rank_with_verify_flag(self):
        out = self._run("rank", str(SAMPLE), "--target", "T", "--verify")
        self.assertTrue(out["independent_verification"]["ok"])

    def test_scenarios_lists_status(self):
        out = self._run("scenarios", str(EVIDENCE))
        status = {s["id"]: s["evidence_infeasible"] is not None
                  for s in out["scenarios"]}
        self.assertFalse(status["frozen-baseline"])
        self.assertTrue(status["frozen-infeasible"])

    def test_verify_command(self):
        out = self._run("verify", str(SAMPLE), "--target", "T")
        self.assertTrue(out["independent_verification"]["ok"])

    def test_infeasible_policy_exit_zero_and_report(self):
        # 不可行是业务结论而非崩溃：退出码 0，报告带冲突说明
        out = self._run("rank", str(INFEASIBLE), "--target", "T")
        self.assertEqual(out["results"][0]["status"], "policy_infeasible")

    def test_compare_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            modified = Path(tmp) / "v2.json"
            data = json.loads(SAMPLE.read_text(encoding="utf-8"))
            data["weight_policy"]["version"] = "p2"
            data["weight_policy"]["lower"] = ["0.6", "0"]
            modified.write_text(json.dumps(data), encoding="utf-8")
            out = self._run("compare", str(SAMPLE), str(modified),
                            "--target", "T", "--verify")
            self.assertEqual(len(out["comparison"]), 1)
            self.assertTrue(out["independent_verification_modified"]["ok"])

    def test_unknown_target_fails(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.run(
            [sys.executable, "-P", "-m", "src.technology_rank_cli",
             "rank", str(SAMPLE), "--target", "GHOST"],
            cwd=ROOT, capture_output=True, text=True, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("GHOST", proc.stderr)


if __name__ == "__main__":
    unittest.main()
