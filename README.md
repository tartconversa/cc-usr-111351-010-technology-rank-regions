# 绿色技术多准则排序的联合可达名次

技术证据与权重政策读取器。分数和margin以十进制字符串保存，正式排序情景固定。

当前代码提供JSON资料结构读取与字段存在性检查，保留原始数据。
fixtures/sample.json 使用合成记录；读取时不执行推断、匹配、规划或核算。
src/technology_rank_regions_input.py 是资料读取入口，tests/test_input.py 检查结构解析和错误处理。
schema_version 当前为整数1，主集合为 technologies；其余字段保留在返回的字典中。
此版本没有第三方运行依赖，使用 Python 3.14。

读取示例：

    python -m src.technology_rank_regions_input fixtures/sample.json

运行资料读取检查：

    python -m unittest discover -s tests -v

检查源码可编译性：

    python -m compileall -q src tests
