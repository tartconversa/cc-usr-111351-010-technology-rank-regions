# 绿色技术多准则排序的联合可达名次

不再产出“貌似客观的唯一榜单”。本模块把**允许的权重范围**和**技术证据的
不确定性**公开成约束，回答某项技术**最好/最差的可达名次及其成立条件**，
并对每个结论给出可独立复核的有理见证。

## 方法概述

- 所有准则先按**冻结标尺**转成“越大越好”的分数；成本类准则必须给出显式
  负仿射标尺（如 `score = -raw + 10`），转换公式与来源随结果留存，可追溯，
  不做隐式取负，也不把缺失证据默认补零。
- 权重多面体：非负、和为 1、逐分量上下界、线性比例约束，全部表示为精确
  有理线性不等式。
- 证据变量带边界与线性联合约束（如两项技术共用同一基准试验、不能各自独立
  选极端）。每个**已冻结证据情景**先校验可行性，再在该情景固定的分数矩阵上、
  **仅在权重多面体内**求名次。多情景逐情景求解、并列输出，绝不把不同情景
  的见证拼成同一方案。
- 名次 = `1 + 分数超过该技术至少 margin 的竞争者数`；`|分差| < margin`
  视为并列，margin 必须为正有理数。
- 最差名次由**联合计数**得到：多个竞争者必须在**同一组权重**下同时超过
  目标，不能从每个竞争者分别可能胜出推断。
- 求解基于标准库 `fractions.Fraction` 的精确 **Fourier–Motzkin 消元**，
  零第三方运行依赖；不使用随机抽样，采样永远不构成“不可达”的证明。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `src/technology_rank_polyhedron.py` | 精确有理 FME 引擎：非严格/严格不等式、等式、可行见证构造、IIS 冲突极小化、贪心消元排序、行数与墙钟护栏、区域可行性缓存 |
| `src/technology_rank_regions.py` | 领域模型：标尺与成本方向转换、证据情景校验、权重多面体、成对关系、联合计数分支限界、多情景并列、版本留存与区域对比 |
| `src/technology_rank_verify.py` | 独立复核：重新解析资料、重建约束、见证纯有理代入、独立子集穷举重算名次区间 |
| `src/technology_rank_input_adapter.py` | 在保真读取器上叠加语义校验 |
| `src/technology_rank_regions_input.py` | 既有资料保真读取器（保持不变） |
| `src/technology_rank_cli.py` | `rank / scenarios / compare / verify` 命令 |

## 名次区域的表示与证明

- **多面体表示**：每个“竞争者 i 超过目标”是半空间 `-d_i·w <= -margin`；
  “未超过（含并列）”是严格半空间 `d_i·w < margin`，其中 `d_i` 是逐准则分差。
- **联合计数搜索**：对每个可能的竞争者在 beat/tie 两个半空间间二选一，形成
  叶子区域；只有对所有竞争者都显式选边的叶子，其可行见证才对应一个**精确
  超过者集合**，从而得到精确名次。分支用可行性做剪枝，结论按区域指纹缓存复用。
- **证明格式**：每个可达名次输出对应权重（有理数字符串）、超过者名单、对每个
  竞争者的逐对分差与 beat/tie 判定。独立复核模块用独立代码路径重算并逐条核对。
- 成对关系三分类：`necessary`（必然超过）、`possible`（可能）、`impossible`
  （不可能），分别由“未超过区域不可行 / 两侧均可行 / 超过区域不可行”认证。

## 资源不足时的诚实降级

FME 在最坏情况下行数指数增长。引擎有两道**确定性**护栏（均非随机）：

- 单次消元生成行数上限（默认 2000，`--row-limit` 可调）；
- 墙钟期限（`--time-limit`，检查下沉到消元循环内部）。

触限时该次可行性判定返回 `feasible = null` 的**未知**结论，绝不把未知当作
可行或不可行；报告置 `certified_complete = false`，只列出已找到的见证，并在
`budget_exhaustion` 中说明受限原因。区分“已认证范围”与“仅找到的见证”。

## 命令

```bash
# 求目标技术在全部冻结情景下的可达名次区域，并附独立复核
python -m src.technology_rank_cli rank fixtures/sample.json --target T --verify

# 列出情景、证据可行性与证据不全的技术
python -m src.technology_rank_cli scenarios fixtures/evidence_scenarios.json

# 替换一项证据或调整权重政策后，对比名次区域变化（版本全留存）
python -m src.technology_rank_cli compare base.json modified.json --target T --verify

# 强制独立复核（独立折分、重建约束、代入验证、独立穷举比对）
python -m src.technology_rank_cli verify fixtures/sample.json --target T
```

资源护栏参数：`--budget`（可行性调用次数）、`--row-limit`（单次 FME 行数）、
`--time-limit`（墙钟秒数）。

## 范例（fixtures/sample.json）

目标 T=(0.5,0.5)，竞争者 A=(1,0)、B=(0,1)，权重和为 1，margin=0.1：
A、B **各自都可能**超过 T，却**不可能同时**超过，因此 T 的最差名次为 2；
w=(0.5,0.5) 时三者并列第一。模块输出最佳名次 1、最差名次 2，可达名次
{1,2}，并为每个名次给出权重见证与逐对分差，独立复核全部通过。

## 资料字段要点

- `criteria[].direction`：`benefit` 或 `cost`；`cost` 必须带负仿射 `scale`。
- `technologies[].raw`：原始值，可为十进制字符串、`null`（证据不全）、
  证据变量引用 `{"var": "e_t"}` 或仿射表达式 `{"affine": [...], "const": ...}`；
  旧格式 `scores`（已冻结分数）仍兼容。
- `evidence.variables` 与全局/情景级 `joint_constraints`：证据不确定性的
  线性联合约束；情景必须 `frozen: true`。
- `weight_policy`：`sum/lower/upper/constraints`，约束支持 `linear`
  （`<=,<,>=,>,=`）与 `ratio`（线性比例，自动转线性不等式）。
- 版本留存：`scales_version`、情景 `id`、`weight_policy.version` 随每个结果输出。

## 测试

```bash
python -m unittest discover -s tests -v   # 含 FME 性质、领域语义、差分、CLI、资源护栏
python -m compileall -q src tests
```
