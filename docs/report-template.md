# 报告骨架

> 这份文件是报告的骨架，不是报告。每个表格都标了「数据来源」和「现在有没有数」。
> 数字空着的地方不要猜——跑完对应命令再填。
> 冻结项（任务集、划分、预算、指标定义）写在这里，正文直接引用，不重复解释。

---

## 0. 一句话结论

（跑完第 0 轮再写。格式：在 133 个 OSWorld 任务上，harness 接口成功率 X%，
裸脚本 Y%，GUI Z%；harness 相对裸脚本的差值是 …，McNemar 精确检验 p = …。）

---

## 1. 研究问题与边界

| 编号 | 问题 | 用哪些数据回答 |
| --- | --- | --- |
| RQ1 | 四种接口在成功率、步数、token、耗时上差多少 | 全部 133 任务，第 0 轮 |
| RQ2 | 多少任务 harness 表达不了，失败原因怎么分布 | 133 任务的 harness 臂失败分类 |
| RQ3 | 把失败喂给 `/cli-anything/refine`，未见任务上成功率涨多少 | 只用测试集 80 个，第 0/1/2 轮 |
| RQ4 | 关掉 JSON 输出或会话状态会掉多少（可选） | 消融臂，若时间允许 |

**边界（必须写进正文，不要只放附录）：**

- 143 个任务里只有 133 个能在没有 OSWorld 虚拟机的情况下判分，10 条排除原因见 §2。
- harness 与脚本臂每任务跑 3 次，GUI 与混合臂受桌面占用限制每任务只跑 1 次。
  **这个不对称必须在主结果表下面注明**，稳定性指标只对前两者成立。
- 80 个测试任务下，15 个百分点以内的差异很难显著。只对大差异下结论。

---

## 2. 任务集与划分

数据来源：`python -m harness_bench tasks` / `tasks --excluded` / `split`

| 软件 | 总数 | 采用 | 排除 |
| --- | --- | --- | --- |
| gimp | 26 | 20 | 6 |
| libreoffice_calc | 47 | 45 | 2 |
| libreoffice_writer | 23 | 23 | 0 |
| libreoffice_impress | 47 | 45 | 2 |
| 合计 | 143 | 133 | 10 |

排除规则（代码里现算，不硬编码）：`infeasible` 任务直接采用；其余要求所有 `result`
getter 是 `vm_file`、所有 `expected` getter ∈ {`cloud_file`, `rule`}。

划分：按软件分层，`seed = 20260601`，开发集 40%（53）、测试集 60%（80）。
`configs/split.json` 是冻结产物，看到任何结果之后不许改。

---

## 3. 判分实现与自检

数据来源：`python -m harness_bench run --dry-run`

自检的含义：把每个任务的标准答案原样放到 agent 的输出位置，用我们的判分桥打分。
**这不是走过场**——它同时验证下载、路径改写、postconfig 重放、表格重算和判分本身。

| 软件 | 自判任务 | 通过 | 失败 | 参考件下载不到 |
| --- | --- | --- | --- | --- |
| libreoffice_calc | 43 | 38 | 0 | 5 |
| libreoffice_writer | 17 | 16 | 1 | 3 |
| libreoffice_impress | 38 | 33 | 5 | 13 |
| gimp | 9 | 2 | 7 | 0 |
| 合计 | 107 | 89 | 13 | 21 |

**13 条失败要逐条解释，这是报告的诚实性所在。** 已全部定位，全部是「参考件不是不动点」：

| 类别 | 条数 | 任务 | 原因 | 处理 |
| --- | --- | --- | --- | --- |
| 参考件不是不动点 | 13 | writer `0a0faba3`；impress `15aece23` `2b94c692` `5d901039` `ac1b39ff` `ed43c15f`；gimp `06ca5602` `554785e9` `72f83cdc` `734d6579` `7a4deb26` `d16c99dc` `f723c744` | 见下面两小节 | 标 `not_decidable`，不计入 harness 损失 |

> 原先另有 5 条 calc 失败（`0bf05a7d` `21df9241` `4f07fbe9` `6e99a1ad` `a01fbce3`），
> 原因是 postconfig 要跑 `--convert-to csv` 而本机没有 soffice。把 LibreOffice
> 解包到 `tools/libreoffice` 之后这 5 条已经能正常自判，不再出现在失败里。
> **这一栏现在是 0，说明缺软件造成的失败已经被消除干净。**
>
> 「参考件下载不到」21 条（calc 5 / writer 3 / impress 13）是测量当时的网络问题
> （HuggingFace 不可达），不是软件问题。它们由 `grade()` 的 `grader_unavailable`
> 分出来，**绝不进成功率的分母**。

**13 条「参考件不是不动点」的两种成因，写报告时分开说：**

*（a）expected 是「修改前」的参考件，metric 要求文件必须变过（6 条）*

| 任务 | result | expected | 判分选项 |
| --- | --- | --- | --- |
| writer `0a0faba3` | 同一份 gold | 同一份 gold | `check_tabstops(word_number_split_by_tabstop=3)` |
| impress `15aece23` | `134_2.pptx` | `134_2_Original.pptx` | `examine_title_bottom_position` |
| impress `2b94c692` | `201_6.pptx` | `201_6_Original.pptx` | `examine_right_position` |
| impress `ac1b39ff` | `55_10.pptx` | `55_10_Original.pptx` | `examine_table_bottom_position` |
| impress `ed43c15f` | `43_1.pptx` | `43_1_Gold.pptx` | `examine_top_position` |
| impress `5d901039` | `CPD_…_Process.pptx` | 同名 URL，dest `…_Original.pptx` | `check_image_stretch_and_center` |

`compare_pptx_files` 的位置分支原文：标题文本相同、且 `shape1.top <= shape2.top` 时直接
`return 0`——即「标题必须移动过」。把参考件拷到结果位置，它当然没移动过。
`check_tabstops` 更直接：实测 `check_tabstops(gold, gold, word_number_split_by_tabstop=3) == 0.0`，
因为该文档第 2 段 `'Make payment '` 首个 tab 前只有 2 个词，规则要求 3。

*（b）expected 是「输入参考图」，metric 要求对图做过变换（7 条，全部 gimp）*

| 任务 | result | expected（即输入图） | metric |
| --- | --- | --- | --- |
| `06ca5602` | `palette_computer.png` | `computer.png` | `check_palette_and_structure_sim` |
| `554785e9` | `edited_colorful.png` | `woman_sitting_by_the_tree2.png` | `check_saturation_increase_and_structure_sim` |
| `72f83cdc` | `berry_mirror.png` | `berry.jpeg` | `check_image_mirror` |
| `734d6579` | `green_background_with_object.png` | `white_background_with_object.png` | `check_green_background` |
| `7a4deb26` | `edited_darker.png` | `woman_sitting_by_the_tree.png` | `check_brightness_decrease_and_structure_sim` |
| `d16c99dc` | `resized.png`（两条） | `rule{height:512}` + `dog_with_background.png` | `check_image_size` + `check_structure_sim_resized` |
| `f723c744` | `berries_contrast.png` | `file_1X42_…jpg` | `check_contrast_increase_and_structure_sim` |

镜像任务的 expected 是原图，metric 检查「结果是原图的镜像」；把原图原样拷过去自然不通过。

> 写完这段要回头确认两件事：**没有任何一条失败被悄悄改成通过**；
> 这 13 条与 `roundtrip` 的 `not_decidable` 计数能对上账
> （13 条全部落进 `not_decidable`；`needs_soffice` 与 `harness_not_installed`
> 都已经是 0，因为本机现在两样都有）。

---

## 4. 上限检查（harness 的天花板）

数据来源：`python -m harness_bench roundtrip`

两步语义：

1. **baseline**：把参考文件当成 agent 输出直接判分。`< 1.0` 的任务标
   `reference_is_not_a_fixed_point`，**不计入 harness 损失**。
2. **round trip**：只对 baseline = 1.0 的任务，把参考件经 harness 导入再导出，重判。
   分数低于 baseline 才算 harness 丢了信息。

| 项 | 数量 |
| --- | --- |
| 采用任务 | 133 |
| 不可行（infeasible，无需往返） | 11 |
| 无参考件 | 15 |
| 可判定（baseline = 1.0） | 94 |
| 不可判定（参考件不是不动点） | 13 |
| 缺 soffice | 0 |
| harness 未安装 | 0 |
| 实际测到 / 通过 / 丢失 / 上限 | 由 `roundtrip` 汇总（见 `runs/roundtrip.json`） |

按软件：

| 软件 | 采用任务 | 可判定 |
| --- | --- | --- |
| gimp | 20 | 2 |
| libreoffice_calc | 45 | 43 |
| libreoffice_writer | 23 | 7 |
| libreoffice_impress | 45 | 42 |
| 合计 | 133 | 94 |

**分母必须写在表下面**：133 个采用任务里，11 个是不可行任务（不存在正确答案，
只需 agent 声明放弃），15 个没有参考件，13 个的参考件不是不动点，**真正能测 harness
保真度的只有 94 个**。`needs_soffice` 与 `harness_not_installed` 都已经归零——LibreOffice
和两个 harness 都装在 `tools/` 与本机 PATH 上了，之前那 111 条测不了的任务全部解锁。
这个上限是**接口能力**的上界，不是 agent 能力：它回答的是「如果 agent 完全照着标准答案
做，这个接口能不能把事情做对」。

初步信号（截至提交时的进度，正式数字以 `roundtrip` 汇总为准）：

- gimp（20/20 跑完）：可判定 2 条，1 通过（`77b8ab4d`）、1 丢失（`2a729ded`），
  上限 50%；其余 9 条 infeasible、7 条参考件非不动点、2 条无参考件。
- calc（已跑 9/43）：可判定 8 条，2 通过（`01b269ae`、`1273e544`）、6 丢失
  （`0326d92d`、`035f41ba`、`04d9aeaf`、`0a2e43bf`、`0cecd4f3`、`12382c62`），
  另有 1 条参考件下载不到。
- writer（7 条可判定）与 impress（42 条可判定）尚未开始，排在队尾。

丢在哪已经逐条取证（三处都是 harness 导出侧的真实缺陷，不是判分器噪声）：

| 任务 | 规则 | gold vs 导出（openpyxl 实测） | 机制 |
| --- | --- | --- | --- |
| `0326d92d` SalesRep | `sheet_data` + `chart(type,title)` | 单元格值/sheet 名/维度完全相同，但 **图表数 2 → 0** | 导出丢图表 |
| `04d9aeaf` SmallBalanceSheet | `sheet_data` + `preserve_cells` | 24 个非空格一致，但 **15 个 `0.00%` 数字格式全部退化为 `General`** | 导出丢数字格式 |
| `035f41ba` IncomeStatement2 | 两个 `sheet_data` | 值一致，但导出公式被写成 **`=of:=B2-C2-...`**（OpenFormula 前缀泄漏进 OOXML），重算后全部 `#VALUE!` | 导出破坏公式 |

→ 这三条是 §6 里 `fidelity` 类的直接证据，写报告时直接点名机制，比「成功率 0」有说服力得多。

唯一丢失的 gimp 任务（`2a729ded-3296-423d-aec4-7dd55ed5fbb3`）：任务要求把狗的抠图叠到
背景上，harness 没有「把一张图片当作项目导入」的动词，用「建画布 + 加图层 + 渲染」
复刻后能出文件但内容不对，baseline 1.00 → 往返后 0.00。这是**表达力**问题，不是
保真度问题，写进 §6 时要归到 `expressiveness`。

---

## 5. 主结果表

数据来源：`python -m harness_bench report`

| 接口 | 运行数 | 任务数 | 成功率 | 虚假完成率 | 工具调用 | token | 耗时(s) | 稳定性 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gui | | | | | | | | n/a（每任务 1 次） |
| harness | | | | | | | | |
| script | | | | | | | | |
| hybrid | | | | | | | | n/a（每任务 1 次） |

指标定义（正文引用，不重复解释）：

- **成功率**：判分均值，`grader_unavailable` 的运行不进分子但进分母，并在表下注明条数。
- **虚假完成率**：声明 `done` 但判分 < 1 的比例。不可行任务上声明完成也算。
- **稳定性**：同一任务多次运行中，与多数派结果一致的比例。

配对比较（harness vs script）：

- 两者都跑过的任务数：
- 仅 harness 成功 / 仅 script 成功：
- McNemar 精确检验 p：
- 按任务自助抽样 95% 置信区间：harness [ , ]，script [ , ]

---

## 6. 失败分类（RQ2）

数据来源：`python -m harness_bench report`，失败分类由 `runner.classify_failure` 初标

| 类别 | 定义 | 条数 |
| --- | --- | --- |
| expressiveness | 接口无法表达这个操作（命令不存在） | |
| fidelity | 命令存在，但往返丢失信息 | |
| harness_defect | 命令存在，报错或结果不对 | |
| agent_error | 接口没问题，agent 用错了 | |
| declared_infeasible | agent 判定不可行 | |
| grader_unavailable | 判分器缺依赖，不计成败 | |

标注协议：两人独立标注，报 Cohen's κ；只有一人时改为模型初标 + 人工抽查 20%，
并在正文注明采用的是哪一种。

分类依据可以直接引用上限检查的三条实测机制（§4）：图表被导出丢掉、数字格式退化成
`General`、公式被写成 `=of:=...` 导致重算 `#VALUE!`——这三条都属 `fidelity`（命令存在、
往返丢信息），不是 `agent_error`，也不是判分器噪声。写正文时逐条点名任务与机制。

---

## 7. refine 曲线（RQ3）

数据来源：`python -m harness_bench report`（读 `extra.round` 与 `extra.split`）

| 轮次 | 开发集成功率 | 开发集任务 | 测试集成功率 | 测试集任务 | 差距 |
| --- | --- | --- | --- | --- | --- |
| round0（`34f5195` 原样） | | 53 | | 80 | |
| round1 | | 53 | | 80 | |
| round2 | | 53 | | 80 | |

防过拟合的三道锁，写进正文：

1. refine 只喂开发集失败，测试集失败分类放到最后一轮 refine 之后才做；
2. 每轮改动人工审查，不接受只为单个任务服务的命令；
3. harness 原有测试必须继续通过。

开发集与测试集的差距就是过拟合的度量，**不解释成"测试集更难"**。

---

## 8. 未验证项与偏差

| 项 | 状态 | 影响 |
| --- | --- | --- |
| Windows-MCP 未安装 | 未验证 | GUI 与混合臂尚无数据 |
| LibreOffice | **已解决**（`tools/libreoffice`，26.8.3） | 44 个表格任务的判分与重算已可用 |
| `cli-anything-libreoffice` | **已解决**（pip -e 安装 1.0.1） | 上限检查不再被二进制缺失卡住 |
| HuggingFace 可达性 | 不稳定 | 21 条参考件当天下载不到，标 `reference_unavailable`，不进分母 |
| 模型未定 | 未定 | 所有臂都还没跑 |
| 控件树子对照 | 未做 | 若做，只作补充，不进主表 |

---

## 9. 结论（跑完再写）

只写数据支持的句子。**不要**写"harness 更好"这类没有对照的说法；
要写"在这 133 个任务上，harness 相对裸脚本的成功率差值是 X 个百分点，
配对检验 p = Y，因此我们只能说 Z"。
