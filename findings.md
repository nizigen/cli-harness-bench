# LibreOffice配对补测发现（2026-11-21）

- 采用任务从vendored JSON和Task.adopted现场复算：Calc45/Writer23/Impress45，113 unique IDs。采用判据是getter可重现，不能保证桌面setup可移植。
- 原 runner materialize仅download；GuiInterface从未启动LO；finalize不保存；postconfig跳过Ctrl+S；normalize仅处理磁盘文件。quick_compare手工开单条Writer不能证明批量路径正确。
- 决策：复用run_once/官方OSWorld grader，新增薄桌面生命周期管理，不重建另一套任务/业务模型；串行独立profile进程；保存按原postconfig语义单独记录。新依赖0。已有psutil/mcp/openai可用，未安装或升级任何包。
- MCP AsyncExitStack必须在同一个async task进出，单一loop仍不足；用owner coroutine保持整个会话生命周期。
- Agent原prune改写唯一transcript，旧截图在本地证据也消失。改为deepcopy请求后裁图，审计保持完整，并逐轮持久化outcome/transcript。
- 5条Linux前置execute包含pyautogui坐标/按键；先标unsupported_setup，不伪造agent失败。后续需明确UI等价处理，才能声称113有效配对全覆盖。
- 本批一轮配对226格，非proposal三轮CLI正式统计。manifest冻结config+任务/实现sha256；按CLI→GUI串行，worker超时1200s独立计环境错误；恢复不重放已完成格。
- Gateway模型元数据auto/deepseek-v4.1-flash存在，owned_by=workbuddy，input=[text,image]、tool_call=true；这仅证明声明，不替代实际图片理解验收。magpie quota对应workbuddy无余额接口，不能从deepseek独立直连key余额推断本路由额度。
- 原生真实打开/保存/退出与模型图像验收：待验证。
- 原生排查：私有profile首启弹“欢迎使用 LibreOffice！”，次启弹“每日贴士：1/224”。预写FirstRun/ShowTipOfTheDay与headless warm-up均无效；无任务文档的空白Writer完成真实关框/优雅退出后，复制profile三次都在6.4–6.7秒干净打开正确文档。用owned Job验证窗口归属，不能拿外部旧窗口冒充成功。
- 实现选型：复用Win32 Job和Windows-MCP，新增profile模板初始化与逐任务复制，不安装依赖。仅空白bootstrap允许关闭明确识别的首次运行提示，任务会话遇未知弹框仍报告错误。模板绑定LO二进制与文件哈希。
- 五条execute脚本已用exact source白名单转换为GUI操作（setup_actions.py）；不执行上游Python。保留40/8次Down、滚动与点击顺序，发生上游变化必须重新审阅；Windows布局是否等价仍待原生核查。
- CLI预检导出.docx实际为ODT包（缺[Content_Types].xml），判分0；这是固定上游harness缺陷，不修改被测接口掩盖结果。
