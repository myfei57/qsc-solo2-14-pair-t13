# 多批次并行：资源分类与隔离规则

本文档约定 breweryctl 支持多批次并行时的资源归属模型。
目标：**任何一次操作都不能影响其他批次的状态**；共享物理设备同一时刻只能服务一个批次。

## 资源分类

### 1. 按批次隔离（键 = batch_id，无需跨批互斥）

| 资源 | 集合 | 锁 |
|---|---|---|
| 批次主文档 | `batches` | `batch:{batch_id}`（服务层复合操作外层锁） |
| 糖化运行 | `mash_runs` | `mash:{batch_id}` |
| 麦汁记录 | `wort_runs` | `wort:{batch_id}` |
| 煮沸运行 | `boil_runs` | `boil:{batch_id}` |
| 温控设定点 | `temp_setpoints` | `setpoint:{batch_id}` |
| 酒花投加 | `hop_additions`（键 `批次:序次`） | `hop:{batch_id}:{position}` |
| 审计条目 | `audit_entries` | 只追加，天然安全 |
| 批次源告警 | `alarms`（source 含 batch_id） | `alarm:{alarm_id}` |

规则：一切读写必须带 `batch_id` 键；禁止任何形式的"当前批次"全局量。
`BrewingService` 的所有批次写操作经 `@_serialized` 装饰器串行化，
保证「读批次 → 校验 → 改多个文档 → 写批次」整条序列原子完成。

### 2. 设备互斥（同一时刻只能属于一个批次）

| 设备 | 占用表达 | 互斥机制 |
|---|---|---|
| 糖化锅 / 煮沸锅 | `equipment.batch_id` | `equipment:{brewery}:{kind}` 锁内查空+占用；开批占糖化锅、过滤时换煮沸锅、回旋沉淀后释放 |
| 发酵罐 | `ferment_tanks.batch_id` + 状态机 | `tank:{tank_id}` 锁；批次操作前 `require_batch` 校验归属 |
| CIP 回路 | `cip_cycles`（一条回路一个活跃 cycle） | `cip_circuit:{circuit_id}` 锁内检查+建单；罐须先切入 `cleaning` |
| 阀门 / 罐压 | 随罐 | `valve:{id}` / `pressure:{tank_id}`，归属跟随罐 |

规则：
- 占用与释放必须原子（检查+写在同一把锁内），释放必须幂等。
- 批次中止（abort）与启动恢复（recover）必须释放其占用的全部设备与罐体。
- 一个发酵罐只能挂一条 CIP 回路；罐被批次占用时禁止开洗。

### 3. 全局互斥（全厂唯一，已有锁保护）

- 在制配额与批次号序列：`breweries` 文档锁内原子完成（`reserve_slot` 同时分配序号）。
- 存储写锁 `_write_lock`：所有落盘串行，是正确性的最后防线（也是吞吐上限）。
- 告警去重：按 `source + code`，source 必须含批次或设备标识。
- 已发布配方版本：不可变，批次锚定 `recipe_version`。

### 4. 共享物理量（显式允许影响全部批次）

- 温度探头：物理共享仪表，`suspect` 标记是设备属性，一个批次的异常读数
  可能使探头被标记——这是物理现实，使用时需经 `require_probe` 显式检查。
- 工艺阈值配置（`Settings`）：只读。

## 锁顺序（避免死锁）

```
batch:{id} → equipment:{brewery}:{kind} → tank:{id} → cip_circuit:{id} → 集合文档锁
```

任何代码路径不得反向持锁。领域组件不得回调 `BrewingService`。

## 失败语义

- 开批：名额 → 批次号 → 糖化锅 → 建单，任一步失败完整回滚（含 mash/wort/hops 残留清理）。
- 过滤转煮沸：先拿煮沸锅再改状态；拿不到锅批次停在保温，可重试。
- 设备获取幂等：同批次重复 `acquire` 返回已占用容器，复合操作可安全重试。
