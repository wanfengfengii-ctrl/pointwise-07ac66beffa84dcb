# 投料纸单补录与盘点封定

盘点员把可辨出的袋号候选和末袋实盘录入同一张补录单。只有能唯一确定每笔实际投入的纸单才可成为正式操作账。已有称量实收、分装回混、实盘封定、净量发行和河西站复核见 README.md。

## 调用

- `inventory.close.close_from_paper(db, segment_id, inventory_id, paper_id, rows, observations)` 接收源库空闲连接、开放段号、新盘点号、原纸单号、操作行及实盘。`observations` 沿用 `close_inventory` 的格式和校验。
- `inventory.readback.read_paper_evidence(db, inventory_id)` 按盘点号读取固定依据。未知盘点或通过普通入口封定的盘点返回 `None`。

`rows` 是列表或元组，行以字典给出以下字段。

```python
{
    "line_no": 4,
    "operation_id": "mix-M",
    "kind": "mix",
    "input_candidates": [["q", "s"], ["u"]],
    "outputs": [("M", 6)],
}
```

`line_no` 是正整数原行号。行号与操作号各自在本单内唯一。每个投入位有一个非空的候选序列，位内按候选节点集合解释。一个假设为每个位取一个名字，同笔不得重复耗用同一节点。分装有一个投入位及至少两个产出，回混有至少两个投入位及一个产出。产出序列沿用 V1 `(节点号, 整克数)`。标识、整数克和单行产出形状沿用 V1 校验。空纸单可核对既有段前缀。

## 裁决与正式登记

所有纸单行都须纳入解释。纸面行号用于追溯，生产行可写在消费行之后。每份解释须能在当前段的既有登记之后按某个合法次序执行。遵循原批全库限用一次、段内产出先生产后消费、产出名称占用及逐笔整数克守恒。V1 优先解析本段同名产出，已耗用的本段产出仍占用该名称。末袋含既有段前缀留下且未消费的产出，袋号和克数须与整份实盘一致，具名零克袋也必须齐备。

比较解释时逐操作比对实际投入集合。相同归属的独立操作换序、同笔投入换位或重复候选只算一次。允许采用任一可执行的登记顺序。裁决与正式登记须使用同一权威源账状态。盘点负责裁决并把唯一的具名操作交给登记职责。登记负责按 V1 规则在本次事务中固定耗用、产出、操作序位及纸单出处。盘点以实际登记结果核对实盘并封定。正式操作、耗用、出处和盘点同成同败。

返回对象提供 `status`、`snapshot`、`evidence` 属性。`status` 为 `no_solution`、`ambiguous` 或 `unique`。前两种状态的 `snapshot` 和 `evidence` 均为 `None`，操作段保持开放。`unique` 返回正常的 `InventorySnapshot` 和固定依据。依据包含原纸单号及按原行号升序排列的行记录。依据对象提供 `paper_id` 和 `rows` 属性，每条行记录提供 `line_no` 和实际登记的 V1 `OperationRecord` 属性 `operation`。重开读取的依据与成功时返回的依据相等。新盘点通过 `read_inventory` 和 `issue_totals` 按原方式读取和发行。

未知段抛 `LookupError`。已封段、已用盘点号、非法纸单结构或非法实盘抛 `ValueError`。候选节点暂不可用、解释中的操作无法按 V1 登记或末袋与实盘不符时，排除该解释。写入异常原样传播。无解、多解、非法输入及写入失败均保留调用前的正式状态，写入失败后可用原参数重试。保留旧库、旧接口和所有原测试。

## 样例与核验

`paper_examples.prepare_case(db, name)` 用原入口为一个空源库建好起始状态，返回可作为 `close_from_paper(db, **args)` 参数的字典。`python3 -B paper_examples.py` 可核对已有起始账和封段失败夹具。

| 样例 | 应有结果 |
| --- | --- |
| `ambiguous` | M 可用 q+u 或 s+u，N 使用另一袋和 v，两种归属都得到 M、N 各 6 g。报 `ambiguous`，不新增耗用或盘点。 |
| `unique` | N 的投料固定使用 q，M 只能使用 s。两笔可独立换序，仍报 `unique`。按纸面第 4、9 行复读实际操作。 |
| `cross-segment` | 旁段已耗用 q，日段只能用 s 和 u。实际盘点只有 M=6 g。 |
| `reverse` | 日段已有 carried=3 g 和 old-zero=0 g。纸单第 8 行产出的 mid 先登记，第 2 行才能把它与 carried 回混为 M=7 g。末袋另有 tail=2 g、old-zero=0 g、zero=0 g，漏掉任一零克袋即无解。 |
| `cycle` | X、Y 互为必需的前置产出。即使逐笔重量相合且两袋都将耗尽，也无合法起步，报 `no_solution`。 |

用 `with paper_examples.fail_seal_write(db):` 包住 `cross-segment` 的补录封定可模拟封段写入失败。核对异常原样传播以及本次操作、耗用、依据和盘点全部回滚，退出后用原参数重试。重开源库按号读盘点和依据，从该盘点发行 V1 净量件。关闭源库后接纳，核定只从 M 取 6 g 的请求，再重开外站按原复核号复领。

沿用 `.pointwise-runtime.json` 的版本 1 控制台运行清单。`setup` 是准备命令的 argv 列表，`verify` 和 `demo` 分别是一条 argv 命令。沿用原回归核验普通业务，并让 `verify` 核对纸单裁决与回滚。`demo` 展示唯一补录、重开读回以及断源 V1 复领的实际结果。核验失败须退出非零。
