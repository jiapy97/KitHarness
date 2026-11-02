# 第十一步：宿主预检与中转认证

状态：**第十一步已完成一轮受控真实实验**，详见[真实 DeepSeek 实验结果](live-study-results.md)。
本页保留此前宿主与 Micu 认证排查；后续实际生成被 Micu 拒绝，用户明确选择本机 DeepSeek 配置，已按 10 美元授权完成实验并封存预算。
用户已澄清 Claude 使用 API 中转；不能继续按订阅登录配置或沿用订阅用量假设。

## 本轮解决的问题

原执行器清空全部继承的 `ANTHROPIC_*` 变量，却没有加载用户实际的中转配置，导致隔离进程没有正确的中转地址和凭据。
现在明确只读加载 CC Switch 当前选中的 **Claude / Micu** 记录，保留独立 `CLAUDE_CONFIG_DIR`，
仅传入允许的地址、认证变量和模型映射；不导入个人 hooks、MCP、apiKeyHelper 或整份设置。

[中转认证报告](../evals/live/relay-auth-check.json)：

| 隔离组 | 不带密钥的目录 GET | 带密钥的目录 GET | CLI 认证状态 | 边界探针 |
| --- | --- | --- | --- | --- |
| 无学习 | 401 | 200 | 通过 | 通过 |
| 原版 | 401 | 200 | 通过 | 通过 |
| KitHarness | 401 | 200 | 通过 | 通过 |

这是对同一个 `/v1/models` 只读接口的正反对照，证明当前密钥在中转上被接受；公开可读的 200 不会被误判成认证成功。
[Models API](https://platform.claude.com/docs/en/api/models/list)用于列出可用模型，不执行消息生成。
中转返回 14 个目录项，其中 4 个 Sonnet ID；**目录结果不证明这些模型的实际推理身份、可用额度或任务效果**。
探针使用当前 CLI 版本的 User-Agent；此前默认 Python User-Agent 得到的 403 不能直接当作凭据失效证据。

该次认证修复新增模型生成请求为 **0**，旧会话账本没有改动。误开的订阅授权流程已经停止。
密钥只在进程内存、子进程环境和匿名 stdin 管道中传递，未写入命令参数、实验配置或报告。
对本次运行目录和已保存证据做了实际密钥匹配检查，匹配数为 0。

## 重现认证检查

```sh
PYTHONPATH=src python3.12 tools/run_live_pilot.py setup --output /tmp/kitharness-relay-new
PYTHONPATH=src python3.12 tools/run_live_pilot.py check --output /tmp/kitharness-relay-new
```

默认读取 `~/.cc-switch`，也可用 `--cc-switch-dir` 指定本机的配置目录。不要把密钥贴入命令或对话。
setup 只准备固定源码和隔离目录，不发模型请求；已有目录不会被覆盖。
check 运行真实沙箱内的 CLI `auth status`、目录接口正反对照、固定读写/回环探针和后台 CLI 阻断检查。
失败时报告 passed=false，命令返回非零退出码。

CC Switch 的设置选择与数据库当前项不一致、来源为链接或提供商配置发生变化时拒绝继续。
密钥轮换不改变公开配置指纹；当前选择的提供商、地址或模型映射变化则要求重新准备。
只复制无凭据的校验程序，记录其摘要，校验程序变化后不能复用旧计划。

原 `run_live_pilot.py execute` 入口仍拒绝复用订阅授权。本页命令只检查认证，不会重新消耗旧的 3 次额度。
正式受控实验改用 `tools/run_live_study.py` 和独立的 10 美元 API 授权，真实数据与重评分入口见[实验结果](live-study-results.md)。

## 保留的历史失败

此前用户授权：“沿用 Sonnet 订阅，先限 3 次 CLI 会话预检”。
当时按订阅方式准备，三次全部失败；[会话账本](../evals/live/session-ledger.json)为 **3/3**，失败不返还。
[原失败报告](../evals/live/pilot-report.json)保持历史内容，不改写成中转成功结果：

| 组别 | 历史结果 |
| --- | --- |
| 无学习 | 启动阶段退出，未输出初始化或模型答案；首次错误正文未完整保留 |
| 原版 | SessionStart 成功，随后返回本地 synthetic 错误；Token 回执为 0，完整错误正文缺失 |
| KitHarness | SessionStart 和 UserPromptSubmit 成功，随后返回 Not logged in · Please run /login；Token 回执为 0 |

宿主为 Claude Code 2.1.312。历史初始化选择了 `claude-sonnet-5-5`，但没有服务端模型答案，实际响应模型身份未验证。
`subtype=success` 同时带 `is_error=true` 的结果按失败处理；`<synthetic>` 也不能算实际模型。
后两份估算费用为 0，首次缺报，完整总费用仍为 null。
这三次之间还变更过策略，不能据此比较正确率或声称 KitHarness 提升了效果。
历史中的 OAuth/Keychain 排查不是用户实际中转的配置，当前路径已经移除这些个人凭据读写例外。

## 隔离与计量边界

真实沙箱使用默认拒绝策略。固定探针先执行未隔离正向对照，再确认允许工作区读写、
拒绝评分文件读取、越界写入和父进程监听的回环连接。
新策略不允许读取 CC Switch 数据库、个人 Keychain 或个人 OAuth 文件；数据源只由外层控制器读取。
仅放行配置的中转 TCP 端口和 DNS，属于端口限制，**不是提供商域名白名单或完整虚拟机隔离**。
新策略：[无学习](../evals/live/relay-policies/no_learning.sb)、[原版](../evals/live/relay-policies/original.sb)、
[KitHarness](../evals/live/relay-policies/kitharness.sb)。这些文件含本机路径，只是本轮证据，不是通用模板。

原模型执行器已有持锁、原子扣账、失败不退名额、拒绝同组重试、禁止覆盖回执和超时终止进程组的保护。
受信任插件中两种后台 CLI 调用路径均被拦截；这不是对所有 HTTP/API 请求的独立计量。
旧策略与最初无模型快照仍可在[历史报告](../evals/live/pilot-report.json)和[预检快照](../evals/live/preflight.json)复核。

当前报告区分 `relay_authentication_verified=true` 和 `live_authentication_verified=false`：
前者是受保护目录接口及 CLI 的认证证据，后者保留“尚未进行模型推理”的边界。
无模型探针、hook 成功和认证成功都不能替代真实学习效果数据。

## 后续真实实验的状态

- 已取得 Sonnet 4.6、10 美元预算，并按用户明确指示回退至官方 `deepseek-flash`；旧订阅账本未重置。
- 已实现真实会话账本、官方单价估算与余额差观测；43 次 DeepSeek 会话均有回执，另保留 3 次 Sonnet 被拒绝的记录。
- 已完成三组相同预热、批量反思/整理、冻结库与 18 次求解。实际模型统一，原生自动触发频率没有混入受控比较。
- 已生成真实结果、成本与局限报告。三组满分，未证明正确率提升；未把插件字节变化当作全会话 Token 变化。

第十一步按已执行范围验收完成。目录认证探针、第十步离线夹具和最终模型实验保持各自独立标记，不混用证据。
