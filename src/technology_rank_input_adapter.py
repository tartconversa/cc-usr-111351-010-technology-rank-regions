"""资料读取适配层：复用既有保真读取器，叠加名次分析所需的语义校验。

不修改原始字典的数值表示：所有分数/参数保持十进制字符串，由分析模块
自行转为 Fraction；本层只做结构与字段一致性检查。
"""
from __future__ import annotations

import json
from pathlib import Path

from .technology_rank_regions_input import load_bundle


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate(bundle: dict) -> dict:
    n = len(bundle["criteria"])
    _check(n >= 1, "至少需要一个准则")
    ids = [c["id"] for c in bundle["criteria"]]
    _check(len(set(ids)) == n, "准则 id 重复")

    policy = bundle["weight_policy"]
    _check(len(policy["lower"]) == n and len(policy["upper"]) == n,
           "权重上下界维数必须等于准则数")
    for j, criterion in enumerate(bundle["criteria"]):
        direction = criterion.get("direction", "benefit")
        _check(direction in ("benefit", "cost"),
               f"准则 {criterion['id']} 方向非法")
        scale = criterion.get("scale")
        if direction == "cost":
            _check(scale is not None and scale.get("type") == "affine",
                   f"成本准则 {criterion['id']} 必须提供显式 affine 标尺")

    tech_ids = set()
    for tech in bundle["technologies"]:
        tid = tech["id"]
        _check(tid not in tech_ids, f"技术 id 重复：{tid}")
        tech_ids.add(tid)
        cells = tech.get("raw", tech.get("scores"))
        _check(cells is not None and len(cells) == n,
               f"技术 {tid} 的数据维数必须等于准则数")

    if "evidence" in bundle:
        var_ids = {v["id"] for v in bundle["evidence"].get("variables", [])}
        _check(len(var_ids) == len(bundle["evidence"].get("variables", [])),
               "证据变量 id 重复")
    if "scenarios" in bundle:
        for spec in bundle["scenarios"]:
            _check(spec.get("frozen", True),
                   f"情景 {spec['id']} 未冻结：本轮查询只接受冻结证据情景")
            unknown = set(spec.get("assignments", {})) - var_ids
            _check(not unknown,
                   f"情景 {spec['id']} 赋值了未知证据变量：{sorted(unknown)}")
    _check(isinstance(bundle["margin"], str), "margin 必须为十进制字符串")
    from fractions import Fraction as _Fraction
    _check(_Fraction(bundle["margin"]) > 0, "margin 必须为正有理数")
    return bundle


def load(path: str | Path) -> dict:
    """读取并校验一份资料，原始字段全部保留。"""
    bundle = load_bundle(path)
    return _validate(bundle)


def loads(text: str) -> dict:
    """从 JSON 文本加载（测试与对比命令复用同一套校验）。"""
    bundle = json.loads(text)
    _check(isinstance(bundle, dict), "资料根节点必须为对象")
    return _validate(bundle)
