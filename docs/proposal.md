# CLI-Anything harness 在第三方桌面任务上值多少

接口对比、覆盖率与失败驱动的改进 · LLM 课程期末项目开题设计

- 状态：草稿，待团队审阅
- 工期：3 周
- 运行环境：Windows 原生，不使用虚拟机
- 尚未验证的前提集中列在第 9 节，开工第 1 天先处理

## 1. 背景

CLI-Anything 把桌面软件包装成命令行工具（下文称 harness），让 agent 用命令而不是点击来操作软件。它的技术报告（arXiv 2606.03854）主张这种接口比 GUI 操作更适合 agent。

这个主张目前没有任务成功率的数据支撑：

- 报告第 5 节的评估内容是目录规模（65 个 harness 加 18 个第三方 CLI）、CLI-Hub 调用量（129,916 次，87.8% 来自 agent）和三段录屏演示。全文没有成功率、基线或消融实验。
- 报告第 7 节把基准列为下一步："The next research step is benchmarkable artifact construction."
- 仓库 README 的 Roadmap 里，"Benchmark suite for agent task completion rates" 一项未完成。
- 仓库约 79 个 harness 中只有 `audacity` 和 `rekordbox` 带评估目录，且都不涉及 agent。

本项目用第三方任务集给这个主张补上数字，并检验 harness 能否靠失败反馈自动改进。

## 2. 研究问题

1. **接口对比**：同一个模型、同一批任务，走不同接口时的成功率、步数、token 和耗时各是多少？
2. **覆盖率**：有多少任务是 harness 表达不了的？失败的原因如何分布？
3. **失败驱动的改进**：把失败案例喂给 CLI-Anything 的 `/cli-anything:refine`，在没见过的任务上成功率能涨多少？
4. **设计消融（可选）**：关掉 harness 的 JSON 输出或会话状态，成功率掉多少？

最关键的对照是 harness 对裸脚本。如果 agent 自己写脚本就能做到一样好，harness 的价值就要落在成本和稳定性上，而不是能力上。

## 3. 任务集

任务取自 OSWorld（`xlang-ai/OSWorld`，Apache-2.0，查看的提交为 `5123bfdbbc`）中 GIMP 和 LibreOffice 的部分。用第三方任务是为了避免自己出题时只出 harness 做得到的题。

只取任务描述、输入文件和判分函数，不使用 OSWorld 的虚拟机。143 个任务按判分方式统计如下：

| 软件 | 总数 | 只对输出文件判分 | 不可行任务 | 其他 | 采用 |
|---|---|---|---|---|---|
| GIMP | 26 | 11 | 9 | 6 | 20 |
| LibreOffice Calc | 47 | 44 | 1 | 2 | 45 |
| LibreOffice Writer | 23 | 22 | 1 | 0 | 23 |
| LibreOffice Impress | 47 | 45 | 0 | 2 | 45 |
| 合计 | 143 | 122 | 11 | 10 | 133 |

- **只对输出文件判分**：判分函数比较 agent 产出的 xlsx、pptx、docx 或图片与标准答案。
- **不可行任务**：软件做不到的要求（例如用 GIMP 剪视频），正确做法是声明做不了。用来测虚假完成。
- **其他**：多数判的是软件设置或界面状态，命令行接口天然做不了，排除。

判分很集中：`compare_table` 用于 44 个任务，`compare_pptx_files` 被引用 48 次。

**开发集与测试集**：按软件分层、固定随机种子，40% 做开发集（53 个），60% 做测试集（80 个）。划分在看到任何结果之前冻结。

## 4. 接口

四种接口使用同一个模型、同一份任务描述、同样的预算上限。

| 接口 | agent 能用的工具 | 说明 |
|---|---|---|
| GUI | Windows-MCP 的截图、点击、输入类工具 | 对应报告批评的做法 |
| harness | 只能调用 `cli-anything-libreoffice` 和 `cli-anything-gimp` | 可读 harness 自带的 SKILL.md 和 `--help` |
| 裸脚本 | shell 加 Python（openpyxl、python-pptx、python-docx、Pillow、`soffice --headless`、GIMP 批处理） | 没有 harness |
| 混合 | harness 优先，做不了再用 GUI | 对应报告结论主张的形态 |

**GUI 接口的约束**：

- 使用 `CursorTouch/Windows-MCP`（MIT，v0.8.9）。
- 它还带 PowerShell、FileSystem、Registry、Process 等工具，必须用 `--tools` 白名单关掉，只保留 Screenshot、Snapshot、Click、Type、Scroll、Move、Shortcut、Wait、WaitFor。否则 agent 会直接写脚本，这一路就不是 GUI 了。
- 它默认带桌面占用门控（用户空闲 10 秒后才接管），挂机运行前要按需配置。
- 可选子对照：只给截图，对比只给控件树（Snapshot）。报告批评的是像素级点击，读控件树的 GUI agent 是更强的对手。是否做取决于第 1 周的 5 任务试跑。

**预算上限（初始值，第 1 周冻结）**：每任务最多 50 次工具调用、15 分钟。

## 5. 运行流程

每个任务独立运行：

1. 新建临时工作目录，放入输入文件。OSWorld 的任务描述里有 `/home/user/...` 这样的 Linux 路径，runner 要统一改写为工作目录下的路径。
2. 结束残留的 LibreOffice 和 GIMP 进程。GUI 和混合接口还要用全新的用户配置目录打开输入文件。
3. agent 在预算内操作，最后必须给出结构化结论：完成，或声明任务不可行。
4. runner 统一归一化输出：GUI 接口由 runner 保存文件；表格类输出用无界面 LibreOffice 重算一遍，因为 openpyxl 写入的公式不带计算结果。
5. 调用 OSWorld 的判分函数，记录得分、工具调用次数、token、耗时和完整轨迹。

**往返上限检查**：把每个任务的标准答案文件原样经过 harness 导入再导出，然后判分。通不过的任务，无论 agent 怎么做，harness 接口都不可能得分。这给出 harness 的客观上限，也用来验证第 4 步的归一化不会破坏判分。

## 6. 指标与分析

| 指标 | 定义 |
|---|---|
| 成功率 | 判分函数得分的均值 |
| 虚假完成率 | agent 声明完成但判分不通过的比例；不可行任务上声明完成也算 |
| 成本 | 每任务的工具调用次数、输入加输出 token、耗时 |
| 稳定性 | 同一任务多次运行结果的一致程度 |

**运行次数**：harness 和裸脚本每任务 3 次。GUI 和混合接口只能串行并占用桌面，先各跑 1 次。这个不对称会在报告里注明。

**失败原因分类**（针对 harness 接口，每个失败归入一类）：

- **表达不了**：harness 没有对应命令。
- **保真度丢失**：命令存在，但导入导出过程改坏了文件，即往返上限检查不通过。
- **harness 缺陷**：命令存在但报错或结果不对。
- **agent 出错**：命令可用，agent 用错或计划错。

分类由两人独立标注并报告一致性。只有一人时改为模型初标加人工抽查。

**统计**：按任务配对比较（McNemar 精确检验），成功率给出按任务自助抽样的置信区间。测试集只有 80 个任务，粗略估计 15 个百分点以内的差异很难达到显著，报告里只对大差异下结论。

## 7. 失败驱动的改进协议

- **第 0 轮**：固定提交的 harness 原样评测（CLI-Anything 提交 `34f5195`）。
- **第 1、2 轮**：把开发集上的失败（任务描述、轨迹、失败分类）交给 `/cli-anything:refine`，得到新版 harness 后重新评测。

防止过拟合的规则：

- refine 只能看开发集。测试集的失败分类放到最后一轮 refine 完成之后再做。
- 人工审查每轮改动，不接受只为单个任务服务的命令。
- harness 原有测试必须继续通过。
- 同时报告开发集和测试集的成功率，两者的差距就是过拟合程度。

第 1 个研究问题用全部 133 个任务的第 0 轮结果回答，第 3 个研究问题只用测试集回答。

## 8. 三周计划

| 周 | 工作 | 退出标准 |
|---|---|---|
| 1 | 第 1 天：查相关工作；验证判分函数能在 Windows 上单独运行；检查输入文件链接。第 2–3 天：装软件和 harness；写 harness 与裸脚本的 runner；路径改写、输出归一化、往返上限检查。第 4–5 天：GUI runner 与重置流程；控件树 5 任务试跑；冻结划分、指标、预算和提示词 | 10 个任务在四种接口下端到端跑通并自动判分 |
| 2 | 第 0 轮全量运行（非 GUI 白天跑，GUI 夜里挂机）；开发集失败分类；第 1 轮 refine 并重测 | 第 0 轮主结果表和开发集失败分类表 |
| 3 | 第 2 轮 refine；三轮在测试集上的对比；测试集失败分类；可选消融；写报告；向上游提 issue 或 PR | 报告、可复现的代码与结果、演示 |

GUI 全量运行的时间按每任务 3–10 分钟粗估为 7–22 小时。建议用专用的 Windows 本地账户运行，避免 agent 点到其他窗口。

## 9. 风险与未验证项

**已观察到的迹象**（来自阅读 harness 源码，未运行）：

- GIMP harness 的 README 写明 "built on Pillow"，未安装 GIMP 时导出会回退到 Pillow。这与 HARNESS.md 中"必须调用真实软件、不得回退"的原则不一致。
- LibreOffice harness 的 Calc 命令组只有 6 个命令（增删改表、读写单元格）。任务集里有画图表的任务，大概率表达不了。

所以第 0 轮 harness 的成功率可能很低。这本身是结论，也是第 3 个研究问题的起点。

**未验证，第 1 周必须确认**：

| 事项 | 不成立时的应对 |
|---|---|
| 相关工作未查。印象中已有工作在 OSWorld 上比较过代码操作与 GUI 操作 | 新意落在 harness 对裸脚本、覆盖率和带测试集的 refine，而不是 CLI 对 GUI |
| 判分函数能否脱离 OSWorld 环境在 Windows 上运行 | 只抽出用到的几个函数单独维护 |
| 任务输入文件的下载链接是否有效 | 剔除失效任务并重新划分 |
| 判分函数是否都返回 0 或 1 | 若有小数得分，成功率的定义相应调整 |
| LibreOffice 和 GIMP 在 Windows 上的控件树是否够用 | 放弃控件树子对照，GUI 只用截图 |
| Windows-MCP 未安装也未试跑，且要求 Python 3.14 以上 | 换用其他 Windows 桌面自动化 MCP |
| 技术报告第 2–4 节只做了关键词检索 | 第 1 天通读 |

**环境现状**：本机未安装 LibreOffice 和 GIMP；harness 只装了 `cli-anything-gimp`；本机 Python 为 3.13，已有 `uv`。

## 10. 待定决策

- 用哪个模型。GUI 的截图方式要求模型支持图像输入。
- 用什么驱动 agent 循环。要求能无人值守批量运行，并能按接口限制工具。
- 控件树子对照和设计消融做不做。
- 报告用中文还是英文。

## 11. 交付物

- runner 代码、任务划分、每次运行的轨迹与得分。
- 报告：主结果表、失败分类、三轮 refine 曲线、实验边界说明。
- 向 CLI-Anything 上游提交的 issue 或 PR（覆盖率缺口、GIMP harness 的回退问题、refine 产生的改动）。

## 参考

- Yang, Fan, Huang. *CLI-Anything: Towards Agent-Native Computer Use.* arXiv:2606.03854.
- CLI-Anything：https://github.com/HKUDS/CLI-Anything （提交 `34f5195`）
- OSWorld：https://github.com/xlang-ai/OSWorld （提交 `5123bfdbbc`）
- Windows-MCP：https://github.com/CursorTouch/Windows-MCP （v0.8.9）
