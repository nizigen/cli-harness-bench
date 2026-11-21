# LibreOffice 全覆盖补测进度

2026-11-21（Asia/Shanghai）

- 用户指出单条Writer字体任务不足以代表LibreOffice，范围扩至113任务（Calc45/Writer23/Impress45），两接口各1次，共226格。
- 修执行器：MCP会话同async task进出；逐调用预算；完整审计与请求图片历史分离；每步持久化；终态异常记录；私有Win32 Job/profile；保存与清理错误不计有效失败；冻结依赖身份和可恢复配对。
- 离线测试阶段曾达187 passed，后续profile与setup集成正在更新，最终总数尚未复核。setup_actions新增3项聚焦测试通过。
- 原生验证暴露欢迎框与每日贴士两连弹，预写配置及headless预热均无效。空白Writer真实关框/优雅退出得到干净模板，复制三次原生打开成功（6.4–6.7秒），生产模板流程正在集成。
- 五条上游execute已审阅exact source并映射GUI操作，未执行上游Python；原生前置状态等价性仍待核验。
- CLI单任务预检真实运行23calls约109秒，score=0；发现固定上游harness将ODT内容写成.docx，判分报缺[Content_Types].xml。预检不计本次正式配对。
- 当前正式批次未启动；三应用真实修改/保存、模型图像理解与最终独立审查未完成。原始轨迹仅本地runs保留。

2026-11-21 18:52（Asia/Shanghai）
- 原生验收全通过：Writer/Calc/Impress 各自打开正确文档、无弹窗；SetForegroundWindow 被前台锁挡住，根因是 Windows 前台锁而非等待不足，focus 改为 owns 校验 + PeekMessage 建队列 + AttachThreadInput 同步 + ShowWindow/BringWindowToTop/SetForegroundWindow，finally 必 detach；三应用 focused=true、Ctrl+S 保存 verified=true（11.7KB/7.0KB/20.7MB）。
- 模型图像验收通过：auto/deepseek-v4.1-flash 对三张原生截图分别正确读出应用名与文件名（未被告知文件名），判定无弹窗；vision.json 落 runs/_desktop_native_preflight。
- 探测脚本因系统 NO_PROXY 含 [::1] 触发 httpx InvalidURL；生产 agent 本就 trust_env=False，探测脚本对齐后通过。
- 端到端配对冒烟（runs/_lo_pair_probe，0810415c Writer 行距）：两臂均 graded、valid pair=1；harness 50calls/343k tok/194s score0，GUI 50calls/498k tok/252s score0，均预算耗尽；管线完整。
- paired execute 改为臂外层（先全部 CLI 后 GUI），新增 --interface 只跑单臂；全仓 226 passed。正式批次未启动，待用户确认桌面占用时段。
