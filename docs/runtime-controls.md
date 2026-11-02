# 运行诊断、后台任务限额与版本保留

本页对应迭代计划第 6 步。反思者和整理者共用后台执行入口，promoter 与生命周期管理器继续保留。

## 检查解释器

在仓库根目录执行：

```sh
PYTHONPATH=src python3 -m autoharness.runtime doctor
```

hook 和 MCP 都先经过这个兼容 Python 3.9 的启动模块。默认 `python3` 已满足 3.11 时直接使用；否则在 PATH 上依次寻找 `python3.14`、`python3.13`、`python3.12`、`python3.11`、`python3`。
可通过 `AUTOHARNESS_PYTHON=/absolute/path/to/python3.12` 显式指定解释器；指定错误时报告问题，不悄悄换成另一个解释器。
环境里仍需要能运行启动模块的 `python3`。

doctor 输出启动解释器、实际选中的解释器和 Claude 可执行文件位置。命令只读，不登录、不调用模型，不检查账号额度、CLI 参数兼容性或目录写权限。
找不到 Python 3.11+ 时，hook 在 stderr 提示诊断方法并退出，MCP 则以非零状态停止，避免把普通文本混入 JSON-RPC。
`AUTOHARNESS_CLAUDE_BIN` 可指定 Claude 可执行文件；它是一个文件路径或命令名，不能填带参数的 shell 命令。

## 后台任务限额

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `AUTOHARNESS_JOB_TIMEOUT_S` | `180` | Claude 子进程调用的超时秒数 |
| `AUTOHARNESS_JOB_PROJECT_LIMIT` | `1` | 同一项目配置根目录的并发上限，反思和整理共用 |
| `AUTOHARNESS_JOB_GLOBAL_LIMIT` | `2` | 共享同一全局配置根目录的项目之间的并发上限 |
| `AUTOHARNESS_JOB_MAX_BUDGET_USD` | `1` | 每次 Claude 调用传入的美元预算 |
| `AUTOHARNESS_VERSION_KEEP` | `10` | 手动清理时默认保留的最近版本数 |

这些值必须为正数；秒数、并发数和版本数必须是整数。非法值会提示并使用默认值。
全局层被 `AUTOHARNESS_DISABLE_GLOBAL=1` 禁用时，不读取或创建全局并发锁，只保留项目限制。
全局上限依赖所有项目使用同一个全局配置根目录；它不是跨机器的并发服务。

并发名额用跨进程文件锁管理，不等待空位，也不会堆积模型任务。名额占满时记录 `concurrency_limit` 并结束本次运行。
第七步增加了[触发优先级](lifecycle-and-cadence.md)：同一 Stop 反思和整理都到期时，优先请求整理，保留反思计数供后续触发。
整理者拿到名额后才做整库备份。POSIX 下 Claude 直接子进程继承名额锁；后台管理进程被强杀后，只要直接子进程还持有描述符，名额仍被占用。
正常结束释放名额；故意关闭继承描述符的外部程序不在这项保证内。

POSIX 超时会先将运行标记为 `cancelled`，随后终止 Claude 进程组及登记过的 MCP 写入进程组。
Windows 终止直接子进程，提案隔离规则相同；本轮进程组测试只在 macOS 上执行。
超时仅约束 Claude 调用，不是文件备份、磁盘 I/O 和 promoter 落盘的总时限。

反思者、整理者和 fork 模式都使用 `--output-format json --max-budget-usd AMOUNT`，需要本机 Claude CLI 支持这两个选项。
预算由宿主 CLI 执行，宿主报告 `error_max_budget_usd` 时按预算耗尽处理；它不保证账单精确停在指定金额，也不是月度总预算。

超时、预算耗尽、启动失败或子进程报错后，保留队列和 `cancelled` 标记，禁止后续写入及自动 drain，防止半套合并提案落盘。
并发跳过及上述失败均不推进反思窗口的已处理偏移。同一会话后续触发可使用新 run ID 重试；没有自动排队补跑或跨会话重试调度器。
旧的隔离队列供人工检查，不应删除标记后直接执行；需要重新验证并提出完整操作。
这收紧了旧版“子进程崩溃也处理已入队提案”的行为：非零退出或 JSON 报告 `is_error: true` 时统一隔离。

## 查看运行用量

项目配置根目录下的 `kitharness/runs/<run_id>.json` 增加 `job` 字段，包含子进程状态、耗时、限额和宿主报告的用量。
白名单字段包括输入/输出 Token、缓存读写 Token、`total_cost_usd`、`num_turns` 和 `duration_ms`。
只保存有效数字，不保存模型回复、输入窗口或原始 JSON。未知或过大的输出格式会标记 `usage.available: false`，不把缺失数据当作零成本。
stdout/stderr 先进入临时文件，只限量读取；错误摘要仍经过脱敏。临时文件没有额外的磁盘配额。
落盘或记账失败后重试会保留已有用量记录；`job.status` 描述子进程结果，具体提案结果仍看 `verdicts`。

## 清理版本

以下 `--root` 指向含有 `skills/` 的项目 `.claude` 目录。

```sh
# 默认只预览候选版本及可释放的文件字节数
PYTHONPATH=src python3.12 -m autoharness.lib.versions prune dates --root /path/to/project/.claude

# 核对预览后，显式删除旧版本；保留最近 10 个
PYTHONPATH=src python3.12 -m autoharness.lib.versions prune dates --root /path/to/project/.claude --keep 10 --apply
```

清理和技能修改、恢复、回滚使用同一把技能锁。至少保留一个版本，额外保护所有 `prepared` 事务引用的快照，以及最近一个可恢复的完整目录。
因此实际保留数量可能大于 `--keep`。先校验历史目录、摘要和事务记录；损坏或符号链接会阻止删除。
只处理指定技能；不扫描其他技能、归档副本或整理者的整库备份。

候选目录先原子移动到 `kitharness/version-trash/` 再移除。清理中断可能留下隔离目录，需要人工检查后清除，但版本列表不会出现删到一半的快照。
不删除 `transactions/` 中的完成日志和 `operations/` 中的回执，旧队列重试仍能复用结果，避免再次修改技能。
本步提供手动保留策略，默认不会自动删历史；小体积审计记录仍会随使用增长。

版本恢复与回滚说明见[可靠技能演进](reliable-skill-evolution.md)。
