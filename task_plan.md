# LibreOffice CLI / GUI 全覆盖配对

用户要求：此前仅一条字体配对不足，补齐 LibreOffice 采用集113条（Calc45/Writer23/Impress45）。沿用 proposal §4–6，模型 auto/deepseek-v4.1-flash；本批每任务每接口一次、50调用/900秒，CLI正式三次重复不与本批混称。

1. 审查 runner 的GUI初始化、保存、隔离与预算；修缺口并离线测试。
2. 冻结113任务与config快照，按任务CLI/GUI交替，桌面始终串行；每格独立子进程，基础设施失败独立计数、保留日志。
3. 每格写record，批次manifest可恢复。上游setup不能安全等价重放的任务明确标unsupported_setup，不默默跳过。
4. 跑完汇总226格覆盖、API累计token、模型步骤/工具调用、耗时与分数；环境不可判分不当成agent失败；不复用旧预算越界数据。

验收：测试通过、原生应用真实打开/保存/独立profile验证、独立审查。没有GUI证据不启动无效全量。工作中持续写progress.md。
