# 深度登记 药房近效期批次调拨与可用量

- 规模：中型
- 使用者与工作流：连锁药房库存核对员，汇集批号库存与调拨申请，按可用有效期安排门店补货。
- 核心规则：FEFO仅分配到店后剩余有效期满足需求的库存；冻结量先扣，拆包需整包装，召回批不得调拨。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 库存接收 | 运行库存接收并复核证据 | `POST /api/m1`；`/work/1` | `app/modules/m1_receiving/rules.py` | `tests/test_m1.py` |
| M2 批号裁决 | 运行批号裁决并复核证据 | `POST /api/m2`；`/work/2` | `app/modules/m2_lots/rules.py` | `tests/test_m2.py` |
| M3 效期分层 | 运行效期分层并复核证据 | `POST /api/m3`；`/work/3` | `app/modules/m3_expiry/rules.py` | `tests/test_m3.py` |
| M4 需求净额 | 运行需求净额并复核证据 | `POST /api/m4`；`/work/4` | `app/modules/m4_demand/rules.py` | `tests/test_m4.py` |
| M5 调拨分配 | 运行调拨分配并复核证据 | `POST /api/m5`；`/work/5` | `app/modules/m5_allocation/rules.py` | `tests/test_m5.py` |
| M6 收发对账 | 运行收发对账并复核证据 | `POST /api/m6`；`/work/6` | `app/modules/m6_reconcile/rules.py` | `tests/test_m6.py` |
| M7 方案复核 | 运行方案复核并复核证据 | `POST /api/m7`；`/work/7` | `app/modules/m7_approval/rules.py` | `tests/test_m7.py` |

## 退货回执锁定与替代分配

- 回执解析：`kind=receipt` 在 `app/modules/m2_lots/rules.py` 解析；召回、空批号、版本与同键冲突沿用既有裁决。
- 方案构建：`app/plan.py` 先跑基准 FEFO，再剥离回执锁定量，按“替代 → 原批补足 → 无法替代缺口”补足，输出四方对账。
- 执行账本：`app/ledger.py` 以 `(run_id, 方案版本, 步骤键)` 唯一，插入忽略重复，支持中断续跑；撤销回执释放旧版全部占用并切换新版本。
- 同版渲染：`app/report.py` 为页面与证据导出提供同一份方案快照；`/api/runs` 也用当前方案覆盖阶段 5–7。

测试：`tests/test_plan.py`（口径与守恒）、`tests/test_ledger.py`（幂等、续跑、撤销、同版）。范围限于当前调拨，不含供应商对接与实际物流。

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | BOM | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m1_receiving/rules.py` | `tests/test_m1.py::test_bom` |
| TRAP-05 | 列数不齐 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m1_receiving/rules.py` | `tests/test_m1.py::test_bad_columns` |
| TRAP-07 | 空值数量 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_lots/rules.py` | `tests/test_m2.py::test_missing_quantity` |
| TRAP-08 | 药品编码前导零 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_lots/rules.py` | `tests/test_m2.py::test_sku_leading_zero` |
| TRAP-14 | 同单冲突 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_lots/rules.py` | `tests/test_m2.py::test_conflict_not_override` |
| TRAP-19 | 召回优先 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m2_lots/rules.py` | `tests/test_m2.py::test_recall_dominates` |
| TRAP-23 | 效期端点 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m3_expiry/rules.py` | `tests/test_m3.py::test_expired_isolated` |
| TRAP-26 | 陈旧复核 | 静默覆盖或误计 | 显式裁决并保留证据 | `app/modules/m7_approval/rules.py` | `tests/test_m7.py::test_review_api_stale` |
| TRAP-27 | 回执锁定批被门店调拨 | 整批回执缺量或门店断供 | 专属锁定先于分配，替代补足并显式记缺口 | `app/plan.py` | `tests/test_plan.py::test_locked_stock_never_sent_twice` |
| TRAP-28 | 同一批库存两边各算一次 | 锁定量与调拨量重复占用 | 对账恒等式与执行前按批号校验 | `app/plan.py`、`app/ledger.py` | `tests/test_ledger.py::test_execute_never_exceeds_lot_availability` |
| TRAP-29 | 中断重跑追加占用 | 重复发出 | 步骤键幂等，只补未完成步骤 | `app/ledger.py` | `tests/test_ledger.py::test_execute_partial_then_resume_without_double_count` |

## 对账规则

输入记录 = 采信记录 + 隔离记录 + 相同行重复。重复表头与空行单独记数。业务合计按阶段中的守恒或差值列复核。运行清单有原文SHA256、规则版本与耗时；相同规则版本下同文重复提交复用批次。执行账本按方案版本隔离：撤销旧版后旧版占用释放，再执行只作用于当前版本。

## 已知不覆盖

部署为本地单用户工具，不连接外部上游系统。CSV单文件5MB及一万条业务记录上限；不推断未提供的未知数据。
