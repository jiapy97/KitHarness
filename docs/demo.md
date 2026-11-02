# 本地演示

这三步都在仓库根目录运行，使用 Python 3.12，不调用模型，不读个人技能库，也不改已保存的实验证据。它们对应项目说明里的三块实测，不是录屏成片。上游那份录屏稿仍是 [demo-video-script.md](demo-video-script.md)，命令还指向上游市场，不能拿来安装这个分支。

## 1. 召回对照

```sh
PYTHONPATH=src python3.12 tools/evaluate_recall.py --output /tmp/kitharness-recall-report.json
```

对照 [evals/recall/report.json](../evals/recall/report.json)。任务排序应仍是相关召回 15/16、MRR 0.9375、平均约 171.6 字节。完整目录仍是 16/16、2093 字节。不要把这次召回写成任务做对了。

## 2. 评分夹具

```sh
PYTHONPATH=src python3.12 tools/evaluate_tasks.py smoke --output /tmp/kitharness-task-smoke.json
```

输出标记为 `fixture_smoke`。它用人工构造的对、错和超时答案检查评分流程。这里的通过率不是模型效果。真实一轮的结果只看 [live-study-results.md](live-study-results.md)。

## 3. 已保存实验的离线重评分

```sh
PYTHONPATH=src python3.12 tools/check_live_evidence.py evals/live/real-study
```

应报告 197 份文件摘要一致，三组都是 6/6 任务、24/24 用例，并且 `model_requests` 为 0。这条命令复现已保存答案的评分，不会再次向模型要一份新答案。
