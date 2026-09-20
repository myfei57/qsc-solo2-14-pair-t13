# 多批次并行：资源划分与并发模型

本文档约定 BreweryCtl 在多批次并行在制时的资源归属、互斥范围与加锁规则。
目标只有一条：**任何一次操作都不能影响别的批次**；共享物理设备在同一时刻
只能服务一个占用者，且占用关系必须落盘、可恢复。

## 1. 资源分类

### 1.1 按批次私有（batch-scoped，批次间天然隔离）

以 `batch_id` 为键，只允许所属批次的操作读写；不同批次互不共享实例。

| 资源 | 集合 / 键 | 保护锁 |
| --- | --- | --- |
| 批次主文档 | `batches:{batch_id}` | `batch:{batch_id}`（编排层） |
| 糖化运行 | `mash_runs:{batch_id}` | `mash:{batch_id}` |
| 麦汁记录 | `wort_runs:{batch_id}` | `wort:{batch_id}` |
| 煮沸运行 | `boil_runs:{batch_id}` | `boil:{batch_id}` |
| 温控目标 | `temp_setpoints:{batch_id}` | `setpoint:{batch_id}` |
| 酒花计划 | `hop_additions:{batch_id}:{position}` | `hop:{batch_id}:{position}` |
| 审计/告警上下文 | 追加写，按 `batch_id` 过滤 | 单文档锁 |

规则：批次 A 的任何操作只允许读写键中含 A 的文档；跨批次不存在共享可写状态。

### 1.2 按设备私有（equipment-scoped，单设备互斥）

物理设备各自独立，键控设备 id；操作只影响该设备，不波及其他设备或批次。

| 资源 | 集合 / 键 | 保护锁 |
| --- | --- | --- |
| 发酵罐状态机 | `ferment_tanks:{tank_id}` | `tank:{tank_id}` |
| 压力闩锁 | `pressure_states:{tank_id}` | `pressure:{tank_id}` |
| 阀门 | `valves:{valve_id}` | `valve:{valve_id}` |
| 温度探头 | `temp_probes:{probe_id}` | `probe:{probe_id}` |
| 单个 CIP 过程 | `cip_cycles:{cycle_id}` | `cip:{cycle_id}` |

探头是共享仪表：采样按探头键控落盘，`batch_id` 只是标签；探头基线/异常
标记只影响该探头自身的数据质量判定，不改写任何批次文档。

### 1.3 全局互斥（shared，跨批次串行）

同一时刻只允许一个占用者；获取/释放必须成对出现并落盘。

| 资源 | 互斥粒度 | 占用 → 释放 | 锁 |
| --- | --- | --- | --- |
| 工厂在制配额 | 每厂 `max_active_batches` | `create_batch` → `complete_batch` / `abort_batch` / `recover` | `breweries:{brewery_id}` |
| 热端容器位（糖化锅/煮沸锅） | 每产线 `vessel_count` 个槽位 | `create_batch` → `transfer_to_tank` / `abort_batch` / `recover` | `lines:{line_id}` |
| CIP 回路 | 每回路 1 个清洗过程 | `start_cycle` → 清洗完成（`finished_at` 落盘） | `cip_circuit:{circuit_id}` |
| 批次号序列 | 每厂单调递增 | `create_batch` 内分配 | `batchseq:{brewery_id}` |
| 发酵罐占用 | 每罐 1 个批次 | `transfer_to_tank` → `empty_tank` | `tank:{tank_id}` |

要点：

- **热端容器位**是"批次进入热端"的许可证。批次创建即占用产线一个槽位，
  转罐进发酵罐（离开热端）时释放；中止与崩溃恢复同样释放。槽位占满时
  `create_batch` 抛 `quota_exceeded`，而不是让两个批次共用一口锅。
- **CIP 回路**只有一套泵与管路：同一回路存在未完成清洗时，对同回路任意
  罐再发起清洗会被拒绝（`conflict`）；罐级检查（同一罐不得重复清洗）仍然
  保留，覆盖一只罐挂在多条回路的登记方式。
- **批次号**在 `batchseq:{brewery_id}` 锁内按 `count + 1` 分配，并发创建
  不会重号。

## 2. 编排层加锁规则

`BrewingService` 的每个变更方法在 `batch:{batch_id}` 锁内完成"读批次 →
校验 → 写各领域文档 → 写审计"的完整序列：

- 同一批次的操作严格串行，复合操作（如过滤转煮沸）不会与自身交错；
- 不同批次持不同的锁，真正并行，互不阻塞；
- 创建批次没有批次锁对象，改用工厂级 `batchseq:{brewery_id}` 锁，把
  编号分配、配额、容器位三件事合成一个原子区。

**加锁顺序（禁止反向，防死锁）**：

```
batchseq:{brewery}  →  batch:{batch}  →  共享资源锁（breweries / lines / cip_circuit）
                    →  设备锁（tank / pressure / valve / probe）
                    →  集合文档锁（{collection}:{key}，由 Collection.update 自持）
```

所有路径都只沿这个方向嵌套；`KeyedLocks` 为可重入锁，同线程重入安全。

## 3. 故障与恢复语义

- **创建回滚**：`create_batch` 任一子步骤失败，依次清理酒花计划、麦汁
  记录、糖化运行、批次文档，并释放容器位与配额——不留下会被其他批次
  看到的半成品状态。
- **先校验后落状态**：`filter_mash` 等复合操作先完成全部参数与工艺校验
  （如麦汁浓度窗口 `check_gravity_window`），全部通过后才写第一个文档；
  校验失败零写入，批次可修正参数后重试。
- **罐批绑定**：`transfer_to_tank` 后批次文档记录 `tank_id`；
  `pitch_yeast` / `mature_batch` 校验传入罐与绑定罐一致，不一致即拒绝
  （`conflict`），从机制上杜绝跨罐串批。重复转罐同样被拒绝。
- **崩溃恢复**：`recover()` 将无法续跑的糖化运行判失败、中止对应批次，
  并幂等释放其配额与容器位；`release_vessel` / `release_slot` 均为幂等，
  重复调用安全。

## 4. 不变量（评审与测试依据）

1. 任意时刻，产线 `active_batches` 长度 ≤ `vessel_count`。
2. 任意时刻，同一 CIP 回路未完成清洗数 ≤ 1。
3. 任意时刻，一只发酵罐的 `batch_id` 至多对应一个在制批次，且该批次的
   `tank_id` 回指同一只罐。
4. 批次号在同一工厂内唯一。
5. 批次私有集合（糖化/麦汁/煮沸/酒花/温控）中不存在不属于任何在制
   批次的孤儿文档。
6. 批次结束（完成/中止/恢复中止）后，其配额与容器位占用必然清零。

对应测试见 `tests/test_parallel_batches.py`。
