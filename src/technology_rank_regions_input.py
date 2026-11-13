"""技术证据与权重政策读取器。分数和margin以十进制字符串保存，正式排序情景固定。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

COLLECTION = 'technologies'
REQUIRED = ('schema_version', 'technologies', 'criteria', 'weight_policy', 'margin', 'scenario', 'scales_version')

def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON键重复：" + key)
        result[key] = value
    return result

def _nonfinite(value):
    raise ValueError("JSON包含非有限数：" + value)

def load_bundle(path):
    """读取资料结构，不推断缺失的业务事实。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=_object, parse_constant=_nonfinite)
    if not isinstance(data, dict):
        raise ValueError("资料根节点必须为对象")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("不支持的资料结构版本")
    for name in REQUIRED:
        if name not in data:
            raise ValueError("缺少资料字段：" + name)
    if not isinstance(data[COLLECTION], list) or not data[COLLECTION]:
        raise ValueError("主资料集合必须为非空列表：" + COLLECTION)
    return data

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    data = load_bundle(args.path)
    print(json.dumps({"schema_version": data["schema_version"],
                      "collection": COLLECTION, "count": len(data[COLLECTION])},
                     ensure_ascii=False))

if __name__ == "__main__":
    main()
