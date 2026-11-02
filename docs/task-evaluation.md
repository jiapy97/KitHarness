# 三组任务评估工具与任务集

本页对应第 10 步：先固定任务、输入、评分和追踪方式，再做真实宿主实验。
工具只准备实验、读取 JSON 答案、评分和汇总，**没有模型调用入口**。真实宿主运行、预热学习、预算与隔离验证属于第 11 步。

## 任务与评分

[任务集](../evals/tasks/suite.json)包含 6 类结构化工作流任务、24 个用例：

| 任务 | 重点 |
| --- | --- |
| 重试计划 | 指数退避、Retry-After 单位、不可重试错误、耗尽后不再等待 |
| 分页汇总 | 稳定去重、空页、重复游标、提前结束 |
| UTC 日期边界 | 跨日、跨年、闰日和半小时时区偏移 |
| 嵌套脱敏 | 数组/对象递归、大小写、整个值替换、业务字段保留 |
| 合并归档保护 | 缺少目标、失败依赖、归档链、正常归档 |
| 事务恢复 | prepared、committed 与独立回执缺失、rolled_back |

所有任务的规则和输入都在公共任务文件中，单靠这些信息应能求解；不把只在学习样本里出现的隐藏政策当作评分前提。
每类还有一条相同的预热经验，供三组采用同一预热协议，工具不会自动执行它们。
任务成功要求该任务的全部用例正确，另记录逐用例对错和任务内正确比例。
汇总时每个任务等权；不是按正文关键词、描述相似度或 LLM 主观评语评分。

答案必须是 `answer.json` 中的 JSON 对象：

```json
{"answers":{"case-id":{"字段":"按具体任务要求输出"}}}
```

仅允许已知 case-id；漏答的用例计错，未知 case-id 或错误包装结构整份计错。
整数和布尔值不能互换，字典键集合及数组顺序按协议检查。Markdown 代码块、重复 JSON 键、非有限数和超限文件不是有效答案。
评分只解析和比较 JSON 数据，**不执行候选代码**。这是一组垂直工作流决策题，不是仓库补丁、单元测试修复或通用编码能力基准。

## 三组如何对齐

| 组 | 源码与处理 |
| --- | --- |
| `no_learning` | 无插件源码、无保留技能；准备独立的空技能根目录 |
| `original` | 原始 AutoHarness 提交 `f2de058720317025c4c18369b7227235141a218b` |
| `kitharness` | 完成第九步的提交 `b232f74c5c7e6f0a02dcda688584cfb6dbf19c52` |

`prepare` 从本地 Git 导出源码副本，保留原项目许可证；不联网拉取、不切换当前分支。
每组每次重复使用相同的任务输入和提示字节；模型 ID 写入统一清单。`--kit-ref` 可显式指定其他提交，落盘时仍解析为完整提交号。
每个任务有独立工作区和项目/全局技能根目录。任务顺序按调度种子打乱，三组首发顺序轮换；这个种子**不是模型采样种子**。

准备结果大致如下：

```text
run/
  manifest.json + manifest.sha256
  grading/suite.json          # 冻结的评分依据，不复制进候选工作区
  warmup.json                 # 三组共用的预热经验
  sources/original/ + sources/kitharness/
  attempts/<组-r次数-任务>/workspace/{PROMPT.txt,task.json,answer.json}
  attempts/<组-r次数-任务>/workspace/.claude/skills/
  attempts/<组-r次数-任务>/global-config/skills/
  results/<attempt-id>.json
```

**目录分离不是操作系统沙箱，也不能证明模型无法读取评分文件。** 第十一步必须用宿主/沙箱限制可见目录，隔离个人配置、网络和凭据，验证实际加载的插件、模型和技能快照，并保存运行证据。
当前工具不会启动候选进程或宿主，因此本步没有执行未受隔离的模型代码。

预热阶段应让三组看到同一批经验；无学习组不保留技能，原版与 KitHarness 各自学习。测评前冻结各组学习结果，每个待测任务使用其独立副本和新会话，不把前一个待测任务的反馈带进下一个任务。
工具提供记录和一致性检查，不会替操作者完成这套宿主协议；缺少预热完成、实际模型或库快照证据时，不能把评分表当作可信的学习效果对照。

## 准备、记录、汇总

在仓库根目录，使用 Python 3.11+。所有命令都不调用模型。

```sh
# 生成新实验目录；MODEL_ID 必须由操作者明确指定
PYTHONPATH=src python3.12 tools/evaluate_tasks.py prepare --output /tmp/kitharness-task-run --model MODEL_ID --repeats 1 --seed 7

# 真实回答由第十一步的宿主运行写入对应 workspace/answer.json
PYTHONPATH=src python3.12 tools/evaluate_tasks.py record /tmp/kitharness-task-run original-r00-retry-plan --metadata /tmp/actual-metadata.json

# 超时或调用失败也必须记录
PYTHONPATH=src python3.12 tools/evaluate_tasks.py record /tmp/kitharness-task-run kitharness-r00-retry-plan --status timeout

PYTHONPATH=src python3.12 tools/evaluate_tasks.py report /tmp/kitharness-task-run --output /tmp/kitharness-task-report.json
```

现有实验、已记录的 attempt 和已有 `--output` 报告不会被覆盖；重新试验应创建新实验目录，保留原来的失败。
默认 1 次重复会准备 18 个待测回答位置，不是已经发生 18 次调用。完整预热另有 6×3 组经验；真实调用规模、子进程维护开销和预算必须在第十一步明确，当前没有总成本上限或自动限额承诺。

外部采集的 metadata 是平面 JSON，支持：

```json
{
  "model":"MODEL_ID",
  "library_sha256":null,
  "duration_ms":null,
  "input_tokens":null,
  "output_tokens":null,
  "cache_read_input_tokens":null,
  "cache_creation_input_tokens":null,
  "total_cost_usd":null
}
```

实际提供的模型 ID 必须与清单一致；未知值用 null 或省略，不能填 0 冒充没有消耗。
Token 必须是非负整数，耗时/费用必须是有限非负数；无关字段不进入报告。
这些字段标记为外部提供、尚未独立核验。这里的用量只覆盖待测求解调用，**不含预热、反思、整理等维护开销**，不能当作完整实验账单。

## 报告如何处理失败和缺失

- 分母使用全部计划任务；漏记录、超时、调用错误、漏交或无效答案均不获得成功分。
- 报告包含逐用例结果、任务成功率、任务内正确比例、状态计数，以及已知用量总和/均值和缺报数量。全部费用未知时总额为 null。
- 配对统计按相同任务、相同重复次数对齐；未收到一组结果时不把它当成一场有效的配对失败。`comparable_pairs` 只说明评分数据可配对，不证明真实宿主协议已被验证。
- 提示/公共任务被修改、源码副本被改动、答案在记录后被替换会显示 drift，不作为有效成功。
- 无学习组的技能目录出现内容会标记 baseline_contamination；同组提供的冻结库指纹不一致时标记 library_drift。没有库指纹时标记覆盖缺失，不假装已验证冻结。
- 报告不复制答案原文，只给数据哈希、身份、状态、数值与对错。原答案保留在操作者指定的工作区，便于审计；这不是对所有外部文件的自动脱敏工具。

清单、评分集、源码和答案哈希用于发现误改与混用，不是签名或对恶意操作者的证明。输入/源码漂移属于实验数据问题，不应被解释成模型能力下降。

## 离线演练

```sh
PYTHONPATH=src python3.12 tools/evaluate_tasks.py smoke --output /tmp/kitharness-task-smoke.json
```

[参考演练报告](../evals/tasks/smoke-report.json)标记为 `fixture_smoke`，使用人工构造的完整正确、部分错误和超时结果，三组采用相同构造。
smoke 会读取预期答案来生成夹具，**不能把它的任何成功率写成项目效果**；耗时和模型费用保持未知，不伪造模型数据。
该报告只验证准备、记录、评分、配对和缺报处理的流程；真实模型实验尚未运行。
