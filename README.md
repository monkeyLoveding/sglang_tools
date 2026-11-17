sglang NPU Nightly 看板

监控 `sgl-project/sglang` 仓库 [nightly-test-npu.yml](https://github.com/sgl-project/sglang/blob/main/.github/workflows/nightly-test-npu.yml)
**每日定时触发（schedule）** 的各 job 与用例文件执行情况。数据由采集脚本写入本仓库的 JSON 文件，前端为纯静态页面，通过 GitHub Pages 发布。

## 指标

| 指标 | 口径 |
| --- | --- |
| 用例成功 / 失败数 | 每个测试文件一次执行记 1 条记录，按 `passed` 统计 |
| 用例时长 | 单机 suite job：日志 `TIMINGS` 块中的 `elapsed`；多机 job：`Run test` 步骤耗时 |
| Job 排队时长 | 依赖感知：`job.started_at − max(运行开始时间, 其 needs 依赖 job 的完成时间)`；同时保存简单口径 `queue_from_run_start_seconds` |
| Job 执行时长 | `job.completed_at − job.started_at` |

前端支持：按 **Job** 汇聚、按 **模型** 汇聚、按 **用例文件** 汇聚；选择时间段后可查看单个用例的成功 / 失败个数与逐日趋势。

## 目录结构

```
index.html                 看板入口（GitHub Pages 首页）
assets/app.js              前端逻辑（筛选、聚合、图表、明细）
assets/style.css           样式
collector/collect.py       数据采集脚本（仅用标准库）
.github/workflows/collect.yml  GitHub Actions 每日采集并提交数据
config/model_map.json      可选：模型名归一化覆盖表
data/meta.json             概览 / 覆盖范围 / 计数
data/runs.json             运行级记录
data/jobs/<YYYY-MM>.json   Job 级记录（按北京日期分月）
data/cases/<YYYY-MM>.json  用例文件级记录（按北京日期分月）
data/_cache.json           增量采集缓存（前端不读取）
```

数据按**类型**分为 runs / jobs / cases 三类文件，jobs、cases 再按月份分片，便于按时间范围按需加载。

## 数据来源

1. **运行列表**：`GET /repos/sgl-project/sglang/actions/workflows/nightly-test-npu.yml/runs?event=schedule`
2. **Job 列表**：`GET /repos/{repo}/actions/runs/{run_id}/jobs`（含 `started_at/completed_at/steps`）
3. **Job 日志**：`GET /repos/{repo}/actions/jobs/{job_id}/logs`
   - 单机 suite job 会在日志末尾输出机器可读块，采集脚本据此得到**每个测试文件**的结果与耗时：

     ```
     ========== TIMINGS BEGIN ==========
     {"file": "test/registered/npu/accuracy/qwen3_32b/test_npu_qwen3_32b.py", "passed": true, "elapsed": 321}
     ========== TIMINGS END ==========
     ```
   - 多机 job 一个 job 只跑一个 `test_case`，因此 job 结论即用例结论。

> **job 日志下载必须带 token**：该接口对未认证请求返回 `403 Must have admin rights to Repository`（公共仓也一样），且未认证时 API 限流仅 60 次/小时。

## 配置 Token（必须）

1. 创建一个 PAT：
   - classic token：勾选 `public_repo`（或 `repo`）
   - 或 fine-grained token：授予 **Actions: Read**、**Contents: Read**（对 `sgl-project/sglang` 只读）
2. 在本仓库 `Settings → Secrets and variables → Actions` 新增 Secret，名字为 **`GH_API_TOKEN`**。

工作流会优先使用 `GH_API_TOKEN`，缺失时回退到 `GITHUB_TOKEN`（此时只能采到 job 级数据，无用例明细）。

本地运行也可以把 token 放在仓库根的 `token.txt`（已在 `.gitignore` 中忽略，勿提交）。
支持三种写法，任选其一：裸 token、`GH_API_TOKEN=xxx`、`GH_API_TOKEN: xxx`。

token 解析优先级：`--token` > 环境变量 `GH_API_TOKEN` / `GITHUB_TOKEN` > `--token-file`（默认 `token.txt`）。

## 定时采集

`.github/workflows/collect.yml`：

- 每日 `02:30 UTC`（北京 10:30，夜间任务结束后）自动运行；
- 也支持手动 `workflow_dispatch` 并指定 `days` 进行历史回填；
- 采集完成后自动 `git commit && git push` 更新 `data/`。

本地运行：

```bash
export GH_API_TOKEN=xxx            # PowerShell: $env:GH_API_TOKEN="xxx"
python collector/collect.py --days 30           # 常规采集
python collector/collect.py --days 90           # 回填 90 天
python collector/collect.py --days 30 --skip-logs   # 仅 job 级（不下载日志）
```

## 启用 GitHub Pages

仓库 `Settings → Pages`：

- Source：`Deploy from a branch`
- Branch：`main`，目录 `/ (root)`

保存后访问 `https://<owner>.github.io/<repo>/`（本仓库为 https://monkeyloveding.github.io/sglang_tools/ ）。`data/` 一旦有数据，页面即可用。

## 维护说明

- **模型分组**：默认从用例路径推导——先去掉 `test/registered` 与可选的 `npu` 前缀，首段为**类型**；
  **模型**取文件所在的那一级具体目录（如 `accuracy/qwen3_32b/test_npu_qwen3_32b.py` → `qwen3_32b`），
  当该目录是通用目录名（`npu` / `models` / `llm_models` / `vlm_models` / `common` 等）时，
  退回从文件名去掉 `test_npu_` / `test_ascend_` / `test_` 前缀后的部分。
  如需归一化（例如把多种变体归到同一模型），在 `config/model_map.json` 中配置覆盖：

  ```json
  {
    "test_npu_qwen3_30b_fuseep.py": "qwen3_30b",
    "accuracy/qwen3_32b": "qwen3_32b"
  }
  ```

  键支持：完整路径、文件名、`类型/模型`。
- **多机用例映射**：多机 job 名被 GitHub 截断，测试文件路径在 `collector/collect.py` 的 `MULTINODE_CONFIGS` 中维护，
  新增多机配置时需同步补充。
- **依赖图**：`collector/collect.py` 的 `NEEDS` 维护 job 依赖，用于计算依赖感知排队时长；workflow 结构变化时需同步。
- 修改解析 / 存储逻辑后，请同时递增 `CACHE_VERSION`，以便缓存的运行被重新解析。

## 已知限制

- 若 suite job 在测试开始前就被取消 / runner 掉线，日志中没有 `TIMINGS` 块，该 job 只有在 job 级数据、无用例明细。
- 采集窗口默认 30 天，`data/` 中超出窗口的记录会被清理；历史回填请用 `--days` 指定更大范围。
- 前端图表依赖 CDN 上的 Chart.js；CDN 不可用时表格仍可正常展示。
