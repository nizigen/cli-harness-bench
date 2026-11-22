# 下一轮 CLI / GUI 测试任务清单

更新时间：2026-11-22。状态：**已选任务，尚未启动本轮测试**。

## 范围与选择方法

建议先测 **9 个任务，Calc / Writer / Impress 各 3 个**。每个任务各跑 CLI 与 GUI 一次，共 18 格；沿用同一个模型 `auto/deepseek-v4.1-flash`。

预算改为 **每格最多 200 次工具调用 / 3600 秒**，并在两臂上完全一致。这是对 proposal §4「50 次调用 / 15 分钟初始值」的**显式偏离**：proposal 允许第 1 周冻结前调整初始值，但冻结之后它就是实验设计的一部分。此前 12 格用的是 50 次，本清单的 9 格用 200 次，**两批记录不能合并成同一组统计**，报告里必须分开标注。

把预算放宽到 200 次的实测依据（来自旧 12 格 CLI 记录，非猜测）：每格都用掉 50 次，其中 `--help` 探索占 7–29 次；仅 3 格在最后一击仍在 `--help` 找动词，其余 9 格在预算耗尽时已经在真正改文档（例如 `calc set-cell C30 ...`、`export render orig.docx`、`impress list-slides`）。也就是说 50 次主要卡住的是「探索 + 逐步操作」的总量，而不是「根本没找到入口」。200 次是否足够仍未被证明，本轮就是去测这个。

代价要如实计入：旧 12 格 CLI 共用 2,368 秒、约 213 万 token；200 次预算下单格上限是原来的 4 倍，18 格的最坏情况显著更贵。因此本轮仍是探索性样本，用于观察「预算放宽后 CLI 能否完成」，不用于宣称总体成功率。

此前这 6 项的 CLI 记录已经存在，GUI 尚未执行。优先补这 6 项 GUI，再与旧 CLI 配对；只有冻结配置、代码与依赖身份一致且输入文件哈希相同，才能作为有效配对。若必须改执行配置，应另建批次并重跑对应两臂，保留原始记录。

## 主清单（9 项）

| 编号 | 应用 | 任务 ID | 具体操作与验收重点 | 选择理由 |
| --- | --- | --- | --- | --- |
| C1 | Calc | `01b269ae-2111-4a07-81fd-3fcd711993b0` | 将 B1:E30 内所有空白单元格填为其上方单元格的值；保留区域外内容 | 检验区域批量填充与数据保留 |
| C2 | Calc | `0bf05a7d-b28b-44d2-955a-50b41e24012a` | 将 Old ID 列复制到 New 7 Digit Id 列，前补零至 7 位；保留无关区域 | 检验文本格式转换与列处理；原任务并非单格赋值 |
| C3 | Calc | `21ab7b40-77c2-4ae6-8321-e00d3a086c73` | 新列计算 Period Rate (%)，转数值，最高值字体设绿色 | 检验多步组合：公式、类型、条件样式 |
| W1 | Writer | `0810415c-bde4-4443-9047-d5f70165a697` | 仅将前两个段落设为双倍行距 | 检验局部段落格式，区分选择范围与格式设置 |
| W2 | Writer | `0b17a146-2934-46c7-8727-73ff6b6483e8` | 将 H2O 中的 2 改为下标，同时保持文档其它内容 | 检验字符级格式与原文保留 |
| W3 | Writer | `936321ce-5236-426a-9a20-e0e3c5dc536f` | 将逗号分隔的文本转为表格 | 检验结构化转换，旧记录里最像「缺动词」的一条 |
| I1 | Impress | `4ed5abd0-8b5d-47bd-839f-cacfa15ca37a` | 将第 2、3、5 页标题设为黑色并加下划线 | 检验多页指定对象的文字格式 |
| I2 | Impress | `ac1b39ff-ee4d-4483-abce-c117e98942f0` | 将第 3 页表格移动至幻灯片底部 | 检验对象定位与版面编辑 |
| I3 | Impress | `ac9bb6cb-1888-43ab-81e4-a98a547918cd` | 将幻灯片页码改为红色 | 检验页脚占位符样式，旧记录里另一条「缺动词」 |

## 原始任务定义与判分

保持上游任务指令和 evaluator，不把任务改写成只适合 CLI 的简化版。中文描述只帮助人工审阅，执行以原始英文指令为准。

| 编号 | 原始任务文件 | 官方 evaluator |
| --- | --- | --- |
| C1 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_calc/01b269ae-2111-4a07-81fd-3fcd711993b0.json) | `compare_table` |
| C2 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_calc/0bf05a7d-b28b-44d2-955a-50b41e24012a.json) | `compare_table` |
| W1 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_writer/0810415c-bde4-4443-9047-d5f70165a697.json) | `compare_line_spacing` × 2 |
| W2 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_writer/0b17a146-2934-46c7-8727-73ff6b6483e8.json) | `compare_docx_files`、`compare_subscript_contains` |
| I1 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_impress/4ed5abd0-8b5d-47bd-839f-cacfa15ca37a.json) | `compare_pptx_files_robust` × 2 |
| I2 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_impress/ac1b39ff-ee4d-4483-abce-c117e98942f0.json) | `compare_pptx_files` |
| C3 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_calc/21ab7b40-77c2-4ae6-8321-e00d3a086c73.json) | `compare_table` |
| W3 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_writer/936321ce-5236-426a-9a20-e0e3c5dc536f.json) | `compare_docx_tables` × 2 |
| I3 | [任务定义](../third_party/osworld/evaluation_examples/examples/libreoffice_impress/ac9bb6cb-1888-43ab-81e4-a98a547918cd.json) | `check_page_number_colors` |

机器可读清单见 [test-task-list.json](test-task-list.json)，含完整任务 ID、原始指令与 evaluator 名称。

## 新增的 3 项及理由

第 3 项按同一抽样顺序继续取，补上两类此前未覆盖的形态：C3 需要「计算 + 类型转换 + 条件格式」三步组合；W3 与 I3 是旧记录里明确卡在「找不到动词」的两条，正好用来检验放宽预算后是否仍然表达不了。

| 编号 | 应用 | 任务 ID | 具体要求 | 为什么补它 |
| --- | --- | --- | --- | --- |
| C3 | Calc | `21ab7b40-77c2-4ae6-8321-e00d3a086c73` | 新列计算 Period Rate (%)，转数值，最高值字体设绿色 `#00ff00` | 多步组合：公式、类型、样式三种操作串联 |
| W3 | Writer | `936321ce-5236-426a-9a20-e0e3c5dc536f` | 将逗号分隔文本转为表格 | 旧记录第 52 次仍在 `writer set-table --help`；用来区分「动词缺失」与「预算不足」 |
| I3 | Impress | `ac9bb6cb-1888-43ab-81e4-a98a547918cd` | 将幻灯片页码改为红色 | 旧记录第 51 次仍在 `impress set-format --help`；同上 |

## 暂不进入本轮的 3 项

仍保留在旧 12 任务试点中，本轮因规模缩减暂缓；不因失败而删除，也不作为失败分类已确定的证据。

| 应用 | 任务 ID | 要求 |
| --- | --- | --- |
| Calc | `7a4e4bc8-922c-4c84-865c-25ba34136be1` | 重排为 Date、First Name、Last Name、Order ID、Sales |
| Writer | `6f81754e-285d-4ce0-b59e-af7edb02d108` | 按 train_id 去重列车记录 |
| Impress | `ce88f674-ab7a-43da-9201-468d38539e4a` | 将幻灯片从横向改为纵向 |

## 执行与汇报约束

- 本文件只选任务，不授权启动 GUI；GUI 执行时间由用户另行指定。
- 保存每格成绩、工具调用数、模型返回的 token 用量、耗时与完整轨迹。环境或模型故障单列，不混作任务失败。
- 旧 CLI 的自动 `failure_class` 仅为初步标注；缺命令、保真度丢失、harness 缺陷与 agent 操作错误须结合接口源码和实际产物复核。
- `cli-anything-libreoffice` 指 CLI-Anything 项目的 LibreOffice harness，不是 LibreOffice 官方团队维护的 CLI；汇报时应明确来源。
- 更正此前口头描述：W1 是“前两个段落双倍行距”，不是全文字体修改；C2 是整列 ID 转换，不是单格任务。不得沿用这些错误描述解释失败或成功。
- 旧 12 格（50 次预算）与新 9 格（200 次预算）是两批不同设计，报告分开列；不得把旧批次成绩当作 200 次预算下的结果，也不得混算成功率。
