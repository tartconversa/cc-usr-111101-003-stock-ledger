# 图书流转事实层设计说明

## 1. 要解决的问题

旧做法用“当天余额”覆盖历史，且把在途册同时记在调出、调入两边，盘点因此重复计数、
选品员据此多订货。本设计把入库、售出、退回、跨点调拨还原为**只追加的不可变流水**，
再从流水重放出按书目（book_key）和地点（location）查询的库存账与每日快照，
历史任何一天都可重算，不再被后到的数据覆盖。

## 2. 数据模型与记账规则

每条流水是不可变事件（`src/events.py` 的 `Movement`），关键字段约定：

* `effective_day`：业务归属日，决定该事件进入哪一天的日结；
* `recorded_at`：系统登记时间，只用于审计“迟到流水”，**不参与任何排序或记账**；
* `quantity`：恒为正整数，冲回不靠负数表达，而由独立的 `REVERSAL` 事件表达；
* `transfer_id`：发出 `TRANSFER_OUT` 与接收 `TRANSFER_IN` 共用，支持分批部分签收，
  累计接收量不能超过发出量；
* `event_id`：不可变流水标识；`reversal_of` 精确指向被冲回的原事件。

库存分三类账户，全部只增删事件、不改事件本身：

| 账户 | 键 | 变动 |
| --- | --- | --- |
| 在库 onhand | (book, location) | RECEIPT +、SALE −、RETURN +、调出 −、调入 + |
| 在途 in_transit | transfer_id | 发出 +、接收 −（按 transfer 桶累计，非负） |
| 净售出 sold_net | (book, location) | SALE +、RETURN − |

调拨语义：发出立即把册从原地点转入 `in_transit`，接收才从在途转入目的地。
因此“在途”不会同时落在两个地点，盘点重复计数问题从账户结构上被消除。

### 冲回（REVERSAL）语义

冲回是一条新事件，对原事件施加**反向 delta**，归属日必须等于原事件的 `effective_day`
（迟到登记的冲回仍改旧日结），且书目、地点、数量必须与原事件镜像一致，否则隔离：

* 冲回 RECEIPT/SALE/RETURN：反向调整对应地点在库与净售出；
* 冲回 TRANSFER_OUT：整笔调拨作废，在途清零、册退回原地点；
* 冲回 TRANSFER_IN：把已签收的册退回在途桶。

同一原事件最多被冲回一次；`reversal_of` 形成环（如 c1↔c2）属非法，整环隔离。

## 3. 流水的三种归宿：接受、隔离、挂起

`src/ledger.py` 的 `build_ledger` 把每次输入的完整集合分为三类，结果**只由流水集合
决定，与读取顺序、分段次数、进程是否重启无关**：

1. **accepted（接受）**：通过全部校验、能形成非负一致账的事件，进入事实层与快照。
2. **quarantined（隔离）**：非法流水独立隔离到 `quarantine.csv` 并附稳定原因码，
   不影响其他合法流水成账。包括：
   * 解析失败 `unparseable_row`、同 event_id 内容冲突 `event_id_conflict`；
   * 超收 `over_receipt`（在途桶转负）、负库存 `insufficient_balance`；
   * 循环冲回 `reversal_cycle`、冲回镜像不符 `bad_reversal_*`、重复冲回；
   * transfer_id 重用、方向/日期不符等。
3. **pending（挂起）**：依赖的原单本次输入中根本不存在（接收缺发出、冲回缺原事件）。
   挂起不参与本版快照，原单在后续导入出现后自动续算，无需人工改账。

重送幂等：event_id 与完整内容都相同的重复行只记到 `duplicates.csv`，绝不双记；
同 event_id 内容不同则判定冲突并隔离后来者，先到者保持有效。

## 4. 回放方案选择：依赖图全量重放，而非增量余额

需求点出两条路线——“依赖图回放”与“增量余额”，本实现选择**确定性全量依赖图重放**。

### 为什么不用增量余额补丁

增量方案在新流水到达时只更新受影响账户，看似省算力，但迟到流水（如题目中
09-08 才登记、归属 09-04 的退回）会改变旧日结，于是必须：

* 找到该事件下游的所有贡献（之后的售出、调拨、冲回），逐笔**撤销下游贡献**；
* 在旧位置插入新 delta，再把下游贡献按新前提重放；
* 还要保证撤销过程中任何中间账户不出现负数。

“撤销下游贡献”需要为每个账户维护完整的贡献来源链与逆操作，事件一旦被冲回或隔离，
其下游的逆操作集合也要递归调整，状态机复杂且极易产生“负在途”这类隐蔽错误；
进程崩溃后增量状态难以校验，重启补齐无法保证与一次导入等价。

### 依赖图重放如何规避这些问题

* 事件之间只显式表达依赖：`REVERSAL → 原事件`，以及同一 transfer_id 内
  OUT→IN 的次序边、冲回发出对接收侧的次序边。
* 在依赖图上做 **Kahn 拓扑排序**，堆以 `(effective_day, event_id)` 打破并列，
  得到与输入顺序无关的唯一回放次序。
* 从头重放全部 accepted 事件，逐笔登记 delta 并在每步检查在库/在途非负；
  一旦某事件导致负数，只隔离该事件（超收/负库存）及其 reversal 下游，然后**整轮重放**。
  不需要“撤销下游贡献”——因为没有需要修补的旧余额，每次都是从空账重建，
  天然避免逆操作链与中间负账。
* 按**书目独立成账**：一本书的非法流水只隔离该书子图，其他书的合法流水照常成账，
  满足“合法部分仍可形成一致快照”。

成本是每次构建 O(E·logE) 重放（E 为接受事件数）加一次快照物化。对门店级流水规模
这完全可接受；换来的是正确性、可审计性和“任意输入形态产出同一账面”的确定性。
若未来事件量增长，增量可作为在全量基线之上的有缓存优化，但必须以全量重放为校验基准。

## 5. 快照物化与发布边界（`src/snapshot.py`）

每次构建产出一组**不可变、可落盘**的产物，写入以内容指纹命名的新目录：

```
<output_root>/<build_id>/
  accepted_facts.csv        # 本次采用的流水集合（事实层）
  daily_location.csv        # 书目×地点×日 的日结（前向填充到全局最后业务日）
  daily_transit.csv         # transfer_id×日 的在途余额
  daily_book.csv            # 书目×日 的在库+在途+净售出（守恒视图）
  quarantine.csv            # 隔离流水及原因码、原始内容
  pending.csv               # 挂起流水及等待的依赖
  duplicates.csv            # 幂等去重留痕
  affected_vs_previous.json # 相对上一版变化的旧日结单元（字段级新旧值）
  manifest.json             # 采用的 event_id 集合、两套 SHA-256、守恒数
  _SUCCESS                  # 完整标记
```

* **保留每次采用的流水集合**：manifest 记录 accepted/input 的 event_id 列表、
  事实集 `fact_set_sha256` 与输入集 `input_set_sha256`；build_id 由规范化输入行的
  哈希得到，与文件如何切分、传入顺序无关。一次导入与多段导入、进程重启后补齐，
  只要流水集合相同就得到相同 build_id 与逐字节相同的快照。
* **迟到流水影响面**：新构建与上一版按业务主键外连接、逐字段 diff，
  `affected_vs_previous.json` 列出受影响的地点/在途单/书目及具体日期和新旧值。
* **发布边界（读者绝看不到半套结果）**：
  1. 先写到同级隐藏暂存目录 `.staging-*`，逐文件 `fsync`；
  2. 写 `_SUCCESS` 标记；
  3. `os.rename` 原子切换构建目录，再用临时软链 + `rename` 原子替换 `latest` 指针。
  任何步骤抛异常都清理暂存目录，`latest` 始终指向上一个带 `_SUCCESS` 的完整版本；
  查询入口拒绝读取缺少 `_SUCCESS` 的目录。相同 build_id 的重复构建直接短路复用。

## 6. 守恒不变量

每本书在每个有效日满足：

```
在库合计 + 在途合计 + 净售出 == 净入库（RECEIPT − 被冲回的 RECEIPT）
```

`build_ledger` 在回放结束后对每本书断言该等式，破坏即抛错，防止带病快照发布。
题面主线：期初 A 10 册，向 B 发 4、B 收 3 并售 2、迟到退回 1，
最终 A=6、B=2、在途=1、净售出=1，合计仍为 10。

## 7. 运行方式

```bash
# 读取器契约保持不变
python -m src.movement_reader fixtures/movements.csv

# 构建事实层（-i 可重复传入，支持分段导入）
python -m src.cli build -i fixtures/segment_a.csv -i fixtures/segment_b.csv -o .workspace-output

# 按书目/地点/日期查询，或查在途账
python -m src.cli query .workspace-output --book b1 --location B --day 2026-09-04
python -m src.cli query .workspace-output --in-transit

# 查看本次构建改写了哪些旧日结
python -m src.cli affected .workspace-output

# 测试（标准库 unittest，无第三方依赖）
python -m unittest discover -s tests -v
```

注：在解释器隔离模式（`python -I`）下运行时，cwd 与 PYTHONPATH 不自动进入
`sys.path`，需用 `python -I -m unittest discover -s tests -t <项目根>` 或在代码中
显式加入项目根；常规 `python -m unittest discover -s tests` 不受影响。
