# AMS 通量分析接入 SCP

`ams_flux_server/` 将已完成的 demo 封装为异步科研工具。SCP SDK 负责工具发现和调用，独立子进程使用现成的科学环境运行分析；原有 demo 计算代码保持不变。

## 启动服务

本机已安装科学环境。安装适配层时使用独立虚拟环境：

```sh
cd /Users/wuhanxun/project/ams-flux-scp
.runtime/ams-flux/bin/python -m venv .venv-scp
.venv-scp/bin/python -m pip install -e .
.venv-scp/bin/ams-flux-server
```

已安装后只需执行最后一条命令。服务默认监听 `http://127.0.0.1:8000/sse`。SDK 依赖固定到下文核对的提交；其依赖不进入科学环境。其他机器在项目根目录执行相同安装命令，先按运行环境说明安装科学环境。

配置集中在启动参数：`--project` 指定材料根目录，`--runtime` 指定科学环境前缀，`--runs` 指定作业存储目录，`--host` 和 `--port` 指定监听地址。默认使用本项目、`.runtime/ams-flux`、`runs/` 和本机 8000 端口。

作业保存在 `runs/<job_id>/`，默认一次运行一个分析或验收作业。关闭客户端或重启 Server 不停止后台计算；重新连接后使用原 `job_id` 查询。`cancel_job` 会停止该作业的整个进程组。重启后失去 worker 的未完成作业会报告失败。分析成功与验收通过分别记录。

## 现有材料分别承担什么角色

| 目录 | 用途 |
| --- | --- |
| `Agent_Task/` | 任务版本 0.6：六个处理后的 ROOT 输入、指定方法源码、科学目标和验收要求。用于从题目开始完成分析。 |
| `demo_agent_task/submission/` | 已完成的参考解，含实现、九个分析子目录、ROOT 结果、报告和验收程序。用于接入联调和参考检查。 |
| `Runtime_Environment/` | Python 3.13.5、NumPy 2.2.6、ROOT 6.34.10，含 PyROOT/RooFit。提供原生 Apple Silicon 离线环境与 Linux 两种架构的 Docker 镜像。 |
| `source_materials/` | 从微信目录复制的原始文件，保留两个 ZIP 和项目介绍 PDF。 |

Git 跟踪代码、题包数据、参考结果和安装说明；原始 ZIP、环境压缩包、已安装环境和作业输出不入库。迁移到其他机器时需另外提供环境包，或使用运行环境中的在线安装入口。

项目介绍 PDF 第 2–3 页明确区分了题包、Codex 完成的参考解与运行环境。科学工作从六个现成的 ROOT 文件开始，不需要重新生产原始事件或运行全探测器模拟。PDF 最后提出的五类场景是扩展方向；本次可接入的具体示例是正负电子联合通量重建。

## 推荐的连接方式

```text
科研智能体 / SCP Client
          │
          ▼
SCP Hub（本地或托管平台）
          │ 发现工具、转发调用
          ▼
AMSFlux SCP Server（薄适配层，SSE）
          │ 创建独立作业目录、启动进程、查询状态
          ▼
Runtime_Environment 中的 Python / ROOT
          │
          ▼
ROOT 结果、REPORT.md、运行日志、验收结果
```

客户端可直接连 Server 的 `/sse`，也可经 Hub 发现和调用工具。默认只监听本机；跨机器接入时需要部署受认证和访问控制保护的服务。

SCP 仓库已有 MCP 风格的服务封装：`from scp.server.fastmcp import FastMCP`、`@server.tool()`、`server.run(transport="sse")`。所核对版本的 `FastMCP.run()` 支持 `stdio` 和 `sse`；这里选择 Hub 已实现调用分支的 SSE，不直接假定 `streamable-http` 启动参数可用。

### 为什么保留两个环境

- SCP SDK 安装在单独虚拟环境，避免其依赖解析改变科学环境中的固定 NumPy 等版本。
- Server 使用子进程调用原生科学环境。ROOT 对象留在计算进程中，协议层返回 JSON 数值、图片和产物内容。
- 配套 Docker 镜像用于独立执行 demo；适配层的 `--runtime` 指向原生科学环境前缀。

SCP 官方的 `scp-code` 镜像用于智能体与 skills 工作流；这份材料没有表明它包含当前 demo 所需的精确 ROOT 环境，因此应保留提供的科学镜像。

## 先确认 demo 的执行入口

以下命令在本项目根目录执行。原生科学环境已安装到 `.runtime/ams-flux`，完整 demo 已运行并生成九个分析步骤的结果。2026-10-19 本机验收通过：14 个有效能量 bin、独立不确定度执行和两次干净重放，结果记录在 `demo_agent_task/submission/run_records/acceptance.json`。

### 本地 Apple Silicon Mac

本机环境已就绪，重新运行 demo：

```sh
source "$PWD/.runtime/ams-flux/bin/activate"
python -B demo_agent_task/submission/run.py
python -B demo_agent_task/submission/acceptance_check.py --replay
```

新机器首次安装时，先执行 `bash Runtime_Environment/macOS/install_macos_offline.sh "$PWD/.runtime/ams-flux"`。安装前需有 Apple Command Line Tools 和可用 SDK；安装目标必须不存在。`--replay` 会额外执行两次干净重放，并检查独立不确定度计算，适合最终验收，不适合每次查询状态时运行。

### Linux 或 Docker 部署

先启动 Docker，再由已有 loader 按 Docker 服务端架构导入镜像：

```sh
bash Runtime_Environment/Docker/load.sh

docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/demo_agent_task,target=/workspace" \
  --workdir /workspace ams-flux-env:6.34.10 \
  python -B submission/run.py

docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/demo_agent_task,target=/workspace" \
  --workdir /workspace ams-flux-env:6.34.10 \
  python -B submission/acceptance_check.py --replay
```

直接运行上述命令会重新生成参考解中的结果。适配层使用原生科学环境，每次将 demo 复制到 `runs/<job_id>/submission/`，排除已有生成 ROOT 文件和运行记录，保留 `Data/` 中的 ROOT 输入；分析在该副本内执行。上述 Docker 命令用于独立运行，不是适配层的执行后端。不要将整个项目根目录挂载给被评测的智能体。

适配层同时指定科学环境的 Python 和 PATH，使 runner 调用的 `root` 来自同一环境。

## 工具接口

这些工具由 `ams_flux_server/server.py` 注册，不是 SCP SDK 的内置科学工具。

| 工具 | 输入 | 返回与行为 |
| --- | --- | --- |
| `describe_analysis` | 无 | 科学目标、任务版本、固定输入与产物说明。 |
| `run_analysis` | 无 | 创建干净工作副本，启动完整 runner，快速返回 `job_id`。 |
| `get_status` | `job_id` | 返回状态、阶段、退出码、日志尾部和产物列表；状态为 `queued/running/succeeded/failed/cancelled`。 |
| `validate_job` | 分析的 `job_id`，`replay=false` | 对成功分析的工作副本启动验收，返回独立验收作业 ID。`replay=true` 额外执行两次干净重放。 |
| `get_result` | `job_id`、`artifact="spectrum"` | 读取成功作业的 JSON 数值、报告、PNG 图片或 base64 ROOT 文件；验收作业可读取 `acceptance`。 |
| `cancel_job` | `job_id` | 停止该作业的进程组，返回最终状态；已结束的作业保持原状态。 |

启动计算必须短返回。当前仓库 `src/scp/hub/client_server.py` 的 `SSEClient.call_tool()` 有 **30 秒超时**，不应让一次工具调用同步等待完整 RooFit、误差传播或重放。超时也不能当作计算已经停止。

`jobs.py` 创建作业、读取状态和取消进程组；独立的 `worker.py` 监督计算并原子保存最终状态。每个作业保存执行规格、worker PID、开始/结束时间、退出码和日志。worker 持有作业文件锁，Server 据此识别重启后仍在运行的作业。

Server 使用 `FastMCP("AMSFlux", host="127.0.0.1", port=8000)`、`@server.tool()` 和 `server.run(transport="sse")`。

服务只执行固定的分析与验收入口，不接受来自客户端的任意 shell 字符串。`job_id` 映射到服务保存的工作目录；结果读取限制在该作业目录内。正式远程服务需要认证、作业归属检查和下载权限控制。

## 把 Server 注册到 SCP Hub

保持适配服务运行，再向已有 Hub 注册。

对于运行在同一主机的**自托管 Hub**，以下是该版本源码的注册格式。`HUB_URL` 指向已启动的 Hub，例如 `http://127.0.0.1:46380`：

```sh
export HUB_URL=http://127.0.0.1:46380

curl -X POST "$HUB_URL/register_server" \
  -H 'Content-Type: application/json' \
  -d '{
    "server_name": "AMSFlux",
    "server_description": "Electron-plus-positron flux reconstruction with ROOT",
    "server_url": "http://127.0.0.1",
    "server_port": 8000,
    "type": "sse"
  }'

curl "$HUB_URL/tools?server_name=AMSFlux"

curl -X POST "$HUB_URL/tools/call_tool" \
  -H 'Content-Type: application/json' \
  -d '{"name":"AMSFlux.run_analysis"}'
```

Hub 会拼接 `server_url:server_port/sse`，所以 `server_url` 不含端口和 `/sse`。调用时名称使用 `AMSFlux.<tool_name>`；工具参数与 `name` 放在同一层，例如查询状态：

```json
{"name":"AMSFlux.get_status","job_id":"<returned-job-id>"}
```

这是自托管 Hub 的 HTTP 格式；直接使用 SCP ClientSession 时，`call_tool(name, arguments)` 是另一层接口。经当前 Hub 调用时结果是 content block 列表，客户端应读取其中的文本内容，再解析适配层返回的 JSON。

如果 Hub 在容器或另一台机器上，`127.0.0.1` 指的是 Hub 自己，必须换成它能访问到的 Server 地址，并相应设置监听地址。不要把未认证的执行服务直接暴露到公网。

若使用托管 SCP 广场，应按平台的服务提交、身份认证和网络接入流程操作。公开仓库没有充分说明该托管部署是否原样开放上述注册端点，不能据此直接向生产平台 POST。本地 Server 的短作业接口仍可复用。

## 结果如何交给智能体

参考解已有稳定的检查目标：

- `submission/01_Flux_Formula/results.root`：普通通量 `flux`、`covariance`、`spectrum_canvas`；能量三次方加权结果 `flux_e3`、`cov_e3`、`spectrum_e3_canvas`；代表能量 `representative_energy` 和定义 `weighted_definition`。
- 普通通量单位为 `m^-2 sr^-1 s^-1 GeV^-1`，加权通量单位为 `m^-2 sr^-1 s^-1 GeV^2`。返回数值时同时返回 bin 边界和单位。
- `submission/REPORT.md`：总报告，各子目录 README 解释中间量及其用途。
- `submission/run_records/`：计算日志、环境与运行记录；验收后含 `acceptance.json`。

`get_result` 不要求消费者访问本机路径。`artifact` 的可选值为：

- `spectrum`：JSON，包含能量边界、代表能量、有效 bin 标记、两种通量及标准差、协方差、单位和不确定度范围。
- `report`：Markdown 报告文本。
- `spectrum_plot`、`spectrum_e3_plot`：SCP 原生 PNG 图片内容块，由科学环境读取已保存的 canvas 导出。
- `root`：`results.root` 的 base64 内容、文件名和 MIME 类型，解码即可保存原始 ROOT 文件。
- `acceptance`：验收作业的检查记录，使用 `validate_job` 返回的作业 ID。

例如通过 Hub 读取结果：

```json
{"name":"AMSFlux.get_result","job_id":"<analysis-job-id>","artifact":"spectrum"}
```

## 从重跑 demo 进入真正的智能体评测

接入联调完成后，新建独立的评测工作区，只放 `Agent_Task/` 的内容，并为智能体提供读取文件、编写代码和在科学环境中执行命令的受限能力。让它读取 README、TASK、指定方法和公开验收要求，自行实现分析。

`Agent_Task` 不包含现成的顶层分析 runner，不能把参考解的 `run.py` 当作题包自带入口。也不要将 `demo_agent_task`、参考结果或本说明挂载到被评测智能体可读的范围；只在提示词里要求“不看”不能形成隔离。

可以沿用题包给出的启动语句：

```text
Read README.md and TASK.md, complete and verify the analysis as instructed, and report the final result file paths and ROOT canvas names.
```

评测应预先约定计算资源、时间/交互预算和网络策略。SCP 管理任务调用与产物；智能体需要另行具备代码执行和科研推理能力。`run_analysis` 执行的是参考解固定工作流；从题目独立完成分析需要使用隔离的 `Agent_Task/` 工作区。

科学验收重点包括：九个指定子项目、规定的模板拟合和选择效率方法、绝对触发效率中心修正只应用一次且按题意排除其不确定度、能量迁移处理、可独立执行的不确定度源、完整协方差，以及干净工作区可重放的最终谱。新生成的科学代码、文档和图注仍按题包要求使用英文。

`demo_agent_task` 的验收程序用于它自己的实现和对象约定。若对另一智能体的独立解评分，应依据题包的公开验收合同和可用的独立评测器，不能强行要求它采用参考解的内部文件/对象名，或把参考解自检通过当成完整独立物理评分。

## 接入完成的判据

1. 客户端能列出 `AMSFlux` 工具，任务描述与题包一致。
2. 启动作业快速返回 ID，断开请求不丢失计算；查询能看到真实运行状态、日志及失败退出码。
3. 独立工作区由既有入口重新生成参考解；最终谱、协方差、报告与 ROOT canvas 可读取，验收及干净重放通过。
4. 经 Hub 的同一工具调用路径可用，远程消费者能实际取得产物。
5. 如用于能力评测，题包与参考解隔离，记录预算、版本、命令及独立验收结论。

## 验证适配层

快速检查作业隔离、失败退出码、取消进程组和状态恢复：

```sh
.venv-scp/bin/python -B -m unittest discover -s tests -v
```

使用真实 SDK、SSE 服务、Hub 和科学环境执行完整协议测试：

```sh
AMS_FLUX_INTEGRATION=1 .venv-scp/bin/python -B -m unittest discover -s tests -v
```

协议测试启动临时本机 Server 和自托管 Hub，验证工具发现、后台分析、Server 重启后查询、JSON/ROOT/PNG 结果读取、经 Hub 注册和调用、验收与两次干净重放，以及取消操作；结束后关闭测试服务并清理临时工作区。托管平台注册不属于本机测试。

## 核对来源

- [项目 README 与 Hub/Server 架构](https://github.com/InternScience/scp)。
- [FastMCP 服务封装](https://github.com/InternScience/scp/blob/cea5398564032aea65a78e246d06c30ae945e03f/src/scp/server/fastmcp/server.py)。
- [Hub 注册、发现和调用实现](https://github.com/InternScience/scp/blob/cea5398564032aea65a78e246d06c30ae945e03f/src/scp/hub/hub_server.py)。
- [Hub 的 SSE 客户端与 30 秒超时](https://github.com/InternScience/scp/blob/cea5398564032aea65a78e246d06c30ae945e03f/src/scp/hub/client_server.py)。
- 本地项目介绍 PDF 第 2–3、13 页；题包 README、TASK、ACCEPTANCE；参考解 README、runner 和验收实现；Runtime_Environment 的原生与 Docker 使用说明。
