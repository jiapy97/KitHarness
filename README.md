<h1 align="center">KitHarness</h1>
<p align="center"><strong>让 Claude Code 把工作经验沉淀为可复用技能。</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11 及以上" />
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS-lightgrey.svg" alt="支持 Linux 和 macOS" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="MIT 许可证" /></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#使用方式">使用方式</a> ·
  <a href="docs/configuration.md">配置文档</a> ·
  <a href="docs/demo.md">本地演示</a> ·
  <a href="https://github.com/jiapy97/KitHarness/issues">反馈问题</a>
</p>

KitHarness 是一个 Claude Code 插件，从真实会话中提炼经验，保存为原生技能，并在后续任务中召回。相同场景的经验会合并，已有技能可以持续修订；使用率低的成熟技能会在容量不足时归档。

例如，一次调试确定了某类问题的排查顺序，你又补充了容易遗漏的步骤。KitHarness 可以把这段经验提炼为技能，在下次处理相关问题时提供给模型，并继续吸收后续修正。

## 特性

- **自动学习**：按会话中的工具调用量触发后台反思，也支持 `/learn` 手动提炼。
- **合并与更新**：对照已有技能处理新经验，优先修订同类内容，减少重复条目。
- **按任务召回**：支持中英文词项匹配，按相关性选择技能，并限制上下文注入量。
- **可追溯、可回滚**：修改带有原因、会话证据、操作 ID 和完整目录快照。
- **按使用情况管理**：区分实际加载、查看和改进，按请求量观察技能，再进行转正审查与容量归档。
- **轻量部署**：Python 实现，无第三方运行依赖，无需常驻守护进程；只管理自身生成的技能。

## 快速开始

需要 **Claude Code**、**Python 3.11+**，支持 Linux 和 macOS。

**1. 获取仓库**

在终端执行；已有本地仓库可跳过此步：

```sh
git clone https://github.com/jiapy97/KitHarness.git
cd KitHarness
```

**2. 安装插件**

在 **Claude Code 输入框**中执行，将路径替换为本地仓库的绝对路径：

```text
/plugin marketplace add /absolute/path/to/KitHarness
/plugin install kitharness@kitharness
/reload-plugins
```

也可以重启 Claude Code 来加载插件。插件 ID 和市场 ID 均为 `kitharness`。

**3. 正常工作，留下经验**

继续调试、开发或处理日常任务。默认每累计 50 次主会话工具调用，在达到阈值的轮次结束后请求后台反思；纯聊天不触发自动学习。

如果刚解决了一个值得复用的问题，可直接输入：

```text
/learn
```

通过校验的经验会写入项目的 `.claude/skills/` 或全局的 `~/.claude/skills/`。没有值得保留的经验时，不会强行创建技能。

> 运行环境需要可用的 `python3` 启动入口。启动模块会自动寻找 Python 3.11+，也可用 `KITHARNESS_PYTHON` 指定解释器。诊断方法见 [常见问题](#常见问题)。

## 使用方式

### 自动学习与手动提炼

自动学习在后台进行。`/learn` 则适合在完成排查、纠正做法或确认可复用流程后立即提炼，两者使用同一套提案、校验与落盘机制。

模型负责提出修改，确定性执行器负责验证和写入。已有技能覆盖同一场景时，优先更新或合并；每次修改都留下原因与证据。具体文件和变更记录见 [技能学习流程](docs/how-it-works.md)。

### 在后续任务中召回

默认 `task` 模式在每次提交提示时匹配相关技能，最多选择 5 条，附加上下文最多 4096 字节。没有匹配项时不注入。Claude Code 原生技能召回继续工作。

如果希望在会话启动时看到分组技能目录，可将 `KITHARNESS_RECALL_MODE` 设为 `catalog`。召回算法与解释方法见 [任务感知召回](docs/task-recall.md)。

### 查看与恢复

生成的技能使用原生 `SKILL.md` 格式，可直接查看：

```text
.claude/
├── skills/<name>/
│   ├── SKILL.md             # 技能内容
│   ├── .ledger.jsonl        # 创建、修改原因与证据引用
│   └── .sidecar.json        # 使用、查看与改进计数
└── kitharness/
    ├── runs/<run-id>.json   # 运行结果与拒绝原因
    └── snapshots/           # 整理前的技能树快照
```

全局层使用 `~/.claude/` 下的对应目录。生命周期归档会保留技能历史，移回目录即可恢复；错误修改可通过版本快照回滚。详见 [可靠技能演进](docs/reliable-skill-evolution.md)。

## 工作原理

```text
真实会话 → 捕获与脱敏 → 反思提案 → 校验与落盘 → 技能库
                                                   ↓
后续任务 ← 按任务召回 ←─────────────────────────────┘
```

反思者从单次工作中提炼经验，整理者定期审视整个技能库并合并重复内容。执行器是唯一写入方，负责校验、快照和故障恢复。

生命周期管理按技能创建后收到的请求数观察使用情况，电脑闲置不会让技能老化。试用期内正常召回、不参与容量淘汰；之后对从未使用也未查看的技能进行归档审查，成熟技能超出容量时优先归档低使用率条目。

组件说明、流水线图和逐步观察方法见 [技能学习流程](docs/how-it-works.md)。

## 配置

默认配置即可运行。需要调整时，在 `.claude/settings.json` 的 `env` 中设置，例如仅启用项目层学习：

```json
{
  "env": {
    "KITHARNESS_DISABLE_GLOBAL": "1"
  }
}
```

常用选项：

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `KITHARNESS_DISABLE_GLOBAL` | `0` | 设为 `1`，仅管理项目层 |
| `KITHARNESS_REFLECT_EVERY_N` | `50` | 每累计多少次主会话工具调用触发反思 |
| `KITHARNESS_CONSOLIDATE_EVERY_N` | `250` | 同项目累计多少次工具调用触发整库整理；`0` 禁用 |
| `KITHARNESS_RECALL_MODE` | `task` | 按任务召回，或使用启动目录模式 `catalog` |
| `KITHARNESS_RECALL_MAX_BYTES` | `4096` | 每次事件的附加上下文字节上限 |
| `KITHARNESS_NOTIFY` | 未设置 | 设为 `desktop`，开启运行结果桌面通知 |

设置后从下一次会话起生效。全部选项见 [配置参考](docs/configuration.md)；后台调用的超时、并发和预算限制见 [运行诊断与限额](docs/runtime-controls.md)。

## 评测与当前结果

仓库分别记录了合成召回测试和一轮真实模型三组对照实验：

| 测量 | 结果 | 说明 |
|---|---|---|
| 合成召回：20 个技能、18 条查询 | 任务排序召回 15/16，MRR 0.938，平均注入 170.7 字节；完整目录召回 16/16，平均 2092 字节 | 衡量相关技能的检索表现 |
| 真实三组对照：6 类任务、24 个用例 | 不学习、基线和 KitHarness 三组均通过 6/6 任务、24/24 用例 | 使用 `deepseek-flash`，仅一轮 |
| 同轮插件注入量 | 基线平均 582 字节，KitHarness 平均 506 字节 | 仅统计插件注入内容 |

当前实验**尚未证明任务正确率提升**，也不足以推断普遍的 Token 节省。真实实验固定比较基线 `f2de058` 与 KitHarness `b232f74`；完整条件和证据见 [真实实验结果](docs/live-study-results.md)。

想在本地复核，可按 [本地演示](docs/demo.md) 运行召回对照、评分夹具和已保存实验的离线重评分，均无需调用模型。评分夹具使用合成答案，不能作为模型效果证据。

## 更新与卸载

以下命令在**终端**执行。Claude Code 内也可通过 `/plugin` 菜单操作。

更新时先刷新市场，再更新插件：

```sh
claude plugin marketplace update kitharness
claude plugin update kitharness@kitharness
```

更新后重启 Claude Code。先刷新目录可避免旧缓存造成“已是最新版”的误报。第三方市场默认关闭自动更新，可在 `/plugin` → **Marketplaces** → **kitharness** → **Enable auto-update** 中开启；发布新版本需提升 `plugin.json` 的 `version`。

0.6.0 统一了 Python 包名与环境变量前缀；旧入口和配置保留兼容支持，已有技能与状态无需迁移。新配置使用 `kitharness` 和 `KITHARNESS_*`，详见 [配置参考](docs/configuration.md)。

卸载：

```sh
claude plugin uninstall kitharness@kitharness
claude plugin marketplace remove kitharness
```

卸载后，生成的技能和状态仍保留在磁盘上。需要彻底清理时，可删除项目与全局的 `kitharness/` 状态目录，以及带有 `self-authored` 账本标记的插件生成技能。请保留手写或从其他来源安装的技能。

## 常见问题

### 为什么还没生成技能？

默认自动反思需要累计 50 次主会话工具调用，纯聊天不推进计数。也可用 `/learn` 主动提炼。触发反思只是发起后台任务；是否成功以及提案被拒绝的原因，可查看 `.claude/kitharness/runs/`。

如果已经调用 `stage_skill` 却没有生成技能，先展开工具返回结果。`missing/invalid frontmatter` 表示正文缺少 YAML 元数据，应补齐 `name`、触发条件 `description` 后重试；`altitude` 表示正文超过非空行数上限（默认 25 行），应精简规则或将示例移到被正文引用的 `references/` 文件。前置校验失败的提案尚未入队，因此不会生成运行报告。项目级长期偏好属于支持范围。`ok: true`、`status: queued` 仅表示入队，轮次结束后才由执行器校验落盘。

### Python 环境有问题怎么办？

在仓库根目录执行只读诊断，不会登录或调用模型：

```sh
PYTHONPATH=src python3 -m kitharness.runtime doctor
```

启动模块兼容 Python 3.9，会选择可用的 Python 3.11+。可通过 `KITHARNESS_PYTHON=/absolute/path/to/python3.12` 显式指定。详见 [运行诊断](docs/runtime-controls.md)。

### 会修改我已有的技能吗？

只管理通过本插件生成、带有专属账本标记的技能。用户手写或从其他来源安装的技能不在管理范围内。

### 可以只注册 MCP 服务吗？

应按快速开始安装为插件。代理允许列表引用插件命名空间下的工具名，手动注册可能造成名称不匹配。服务命名和项目配置优先级见 [MCP 服务命名](docs/how-it-works.md#mcp-服务命名)。

## 文档与贡献

| 文档 | 内容 |
|---|---|
| [配置参考](docs/configuration.md) | 全部环境变量、默认值和行为说明 |
| [技能学习流程](docs/how-it-works.md) | 组件职责、文件结构与观察学习过程 |
| [可靠技能演进](docs/reliable-skill-evolution.md) | 事务恢复、版本快照与回滚 |
| [任务感知召回](docs/task-recall.md) | 排序、预算和可解释召回 |
| [运行诊断与限额](docs/runtime-controls.md) | 解释器、并发、超时与预算 |
| [任务评测](docs/task-evaluation.md) | 三组协议、评分工具和复现方法 |
| [真实实验结果](docs/live-study-results.md) | 实验条件、结果和证据归档 |
| [宿主预检记录](docs/live-evaluation.md) | 认证检查和试运行记录 |
| [迭代计划](docs/PLAN.md) | 开发进度与验收记录 |

欢迎提交 [Issue](https://github.com/jiapy97/KitHarness/issues) 或 Pull Request。开发时需另行安装 `pytest` 和 `ruff`，在仓库根目录执行与 CI 一致的检查：

```sh
ruff check .
python3 -m pytest -m "not live" -q
```

## 致谢

[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的自动技能创建与记忆整理设计，为本项目提供了参考。

## 许可证

[MIT](LICENSE)
