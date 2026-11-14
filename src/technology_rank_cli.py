"""命令行入口：名次区域查询、情景清单、证据/政策变更对比。

用法：

    python -m src.technology_rank_cli rank <bundle.json> --target T
    python -m src.technology_rank_cli scenarios <bundle.json>
    python -m src.technology_rank_cli compare <base.json> <modified.json> --target T
    python -m src.technology_rank_cli verify <bundle.json> --target T

全部输出为 UTF-8 JSON；分数、margin、权重见证均为十进制/有理字符串，
不使用浮点。verify 额外运行独立复核并在输出中给出逐条核对记录。
"""
from __future__ import annotations

import argparse
import json

from .technology_rank_input_adapter import load
from .technology_rank_regions import analyze, compare_bundles, build_models
from .technology_rank_verify import verify_report


def _dump(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _limits(args) -> dict:
    return {"budget": args.budget,
            "row_limit": args.row_limit,
            "time_limit": args.time_limit}


def _verify_limits(args) -> dict:
    return {"row_limit": args.row_limit, "time_limit": args.time_limit}


def _cmd_rank(args) -> None:
    bundle = load(args.bundle)
    report = analyze(bundle, args.target, args.scenario, **_limits(args))
    if args.verify:
        report["independent_verification"] = verify_report(
            bundle, report, **_verify_limits(args))
    _dump(report)


def _cmd_scenarios(args) -> None:
    bundle = load(args.bundle)
    models = build_models(bundle)
    payload = {"scenarios": [
        {"id": m.sid, "frozen": m.frozen,
         "evidence_infeasible": m.evidence_conflict,
         "versions": m.versions,
         "incomplete": {tid: [m.criterion_ids[j] for j in idx]
                        for tid, idx in m.incomplete.items()}}
        for m in models]}
    _dump(payload)


def _cmd_compare(args) -> None:
    base = load(args.base)
    modified = load(args.modified)
    report = compare_bundles(base, modified, args.target, **_limits(args))
    if args.verify:
        report["independent_verification_base"] = verify_report(
            base, report["base_report"], **_verify_limits(args))
        report["independent_verification_modified"] = verify_report(
            modified, report["modified_report"], **_verify_limits(args))
    _dump(report)


def _cmd_verify(args) -> None:
    bundle = load(args.bundle)
    report = analyze(bundle, args.target, args.scenario, **_limits(args))
    result = verify_report(bundle, report, **_verify_limits(args))
    _dump({"report": report, "independent_verification": result})


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_limits(p):
        p.add_argument("--budget", type=int, default=10000,
                       help="可行性判定调用次数上限")
        p.add_argument("--row-limit", type=int, default=None,
                       help="单次 FME 生成行数上限（默认引擎内置 20000）")
        p.add_argument("--time-limit", type=float, default=None,
                       help="本情景墙钟秒数上限；到期后未决叶子诚实标记为未知")

    p_rank = sub.add_parser("rank", help="求目标技术的可达名次区域")
    p_rank.add_argument("bundle")
    p_rank.add_argument("--target", required=True)
    p_rank.add_argument("--scenario", default=None)
    add_limits(p_rank)
    p_rank.add_argument("--verify", action="store_true")
    p_rank.set_defaults(func=_cmd_rank)

    p_sc = sub.add_parser("scenarios", help="列出冻结情景与证据状态")
    p_sc.add_argument("bundle")
    p_sc.set_defaults(func=_cmd_scenarios)

    p_cmp = sub.add_parser("compare", help="对比两份资料的名次区域变化")
    p_cmp.add_argument("base")
    p_cmp.add_argument("modified")
    p_cmp.add_argument("--target", required=True)
    add_limits(p_cmp)
    p_cmp.add_argument("--verify", action="store_true")
    p_cmp.set_defaults(func=_cmd_compare)

    p_v = sub.add_parser("verify", help="分析并强制独立复核")
    p_v.add_argument("bundle")
    p_v.add_argument("--target", required=True)
    p_v.add_argument("--scenario", default=None)
    add_limits(p_v)
    p_v.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
