# 馆藏调拨在途库存的事件事实重建

本项目把公园阅读空间的经营流水重建为只追加的图书流转事实层。`src/movement_reader.py`
仅负责读取原始资料、输出输入类型和条数，不加工业务事实、不修改数据；在此之上，
`src/events.py`、`src/ledger.py`、`src/snapshot.py` 把不可变流水重放为按书目和地点
查询的库存账与每日快照。资料中的标识、数量和金额均为合成值。

## 格式与目录

fixtures 保存输入，src/movement_reader.py 为读取入口，tests 核对读取与记账行为。
RECEIPT、SALE、RETURN 为入库、售出、退回；TRANSFER_OUT/IN 为调拨发出与接收，
REVERSAL 通过 reversal_of 精确引用。quantity 恒为正数。

CSV 为追加式流水，区分 effective_day（业务归属日，决定进入哪天日结）与
recorded_at（登记时间，仅用于审计迟到流水）。发出与接收共用 transfer_id，
支持分批部分签收，累计接收不得超过发出；event_id 标识不可变流水，更正通过
REVERSAL 精确冲回原事件再补新事件。重送同内容不双记，同 event_id 内容冲突报错隔离。

## 模块

- `src/events.py`：流水数据类与单行解析、字段级校验。
- `src/ledger.py`：依赖图构建、接受/隔离/挂起分类、按书目确定性 Kahn 回放与守恒断言。
- `src/snapshot.py`：日快照物化、内容指纹、受影响旧日结 diff、原子发布边界。
- `src/cli.py`：build / query / affected 命令行入口。
- `DESIGN.md`：回放方案取舍（依赖图重放 vs 增量余额）、撤销下游贡献与发布边界说明。

## 运行命令

Python 3.11 及以上，只使用标准库，无第三方安装步骤。进入项目根目录运行：

    python -m src.movement_reader fixtures/movements.csv

构建事实层快照（-i 可重复传入以支持分段导入，输出写入独立路径）：

    python -m src.cli build -i fixtures/movements.csv -o .workspace-output
    python -m src.cli build -i fixtures/segment_a.csv -i fixtures/segment_b.csv -o .workspace-output

查询库存账与在途：

    python -m src.cli query .workspace-output --book b1 --location B --day 2026-09-04
    python -m src.cli query .workspace-output --in-transit
    python -m src.cli affected .workspace-output

编译检查：

    python -m compileall -q src tests

读取与记账测试：

    python -m unittest discover -s tests -v

隔离模式（python -I）下需显式指定项目根：

    python -I -m unittest discover -s tests -t .

## 容器方式

    docker build -t stock-ledger:local .
    docker run --rm stock-ledger:local

容器默认执行读取测试。数据加工输出请写入独立路径，避免覆盖原始资料。
