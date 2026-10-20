# AMS 通量分析接入 SCP

建议先把已完成的 demo 封装成一个可异步调用的科研工具，打通 SCP 的发现、执行和结果读取；随后使用独立题包评测智能体。ROOT 计算继续使用现成的 Runtime_Environment，SCP 负责连接和调用，两者无需合并成一个运行环境。

## 现有材料分别承担什么角色

| 目录 | 用途 |
| --- | --- |
| `Agent_Task/` | 任务版本 0.6：六个处理后的 ROOT 输入、指定方法源码、科学目标和验收要求。用于从题目开始完成分析。 |
| `demo_agent_task/submission/` | 已完成的参考解，含实现、九个分析子目录、ROOT 结果、报告和验收程序。用于接入联调和参考检查。 |
| `Runtime_Environment/` | Python 3.13.5、NumPy 2.2.6、ROOT 6.34.10，含 PyROOT/RooFit。提供原生 Apple Silicon 离线环境与 Linux 两种架构的 Docker 镜像。 |
| `source_materials/` | 从微信目录复制的原始文件，保留两个 ZIP 和项目介绍 PDF。 |

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

开发时先让客户端直接连 Server 的 `/sse`，验证工具发现和调用，再注册到 Hub，便于区分分析执行问题与 Hub 配置问题。默认只监听本机；跨机器接入时再部署受认证和访问控制保护的服务。

SCP 仓库已有 MCP 风格的服务封装：`from scp.server.fastmcp import FastMCP`、`@server.tool()`、`server.run(transport="sse")`。所核对版本的 `FastMCP.run()` 支持 `stdio` 和 `sse`；这里选择 Hub 已实现调用分支的 SSE，不直接假定 `streamable-http` 启动参数可用。

### 为什么保留两个环境

- SCP SDK 安装在单独虚拟环境，避免其依赖解析改变科学环境中的固定 NumPy 等版本。
- Server 使用子进程调用原生科学环境，或通过 Docker 调用科学镜像。ROOT 对象留在计算进程中，协议层返回 JSON 摘要和产物标识。
- 本地 Mac 联调可用原生离线环境；部署到 Linux 服务端推荐现有 Docker 镜像，并固定镜像 CPU 架构。跨架构浮点结果可能不同。

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

直接运行上述命令会重新生成参考解中的结果。接入服务应在每次作业开始时将参考解复制到 `runs/<job_id>/demo_agent_task/`，再把该副本挂载为 `/workspace`，以保留分发的参考结果并隔离并发写入。不要将整个项目根目录挂载给被评测的智能体。

原生环境也要通过 `bash -lc` 激活后再启动 `python -B submission/run.py`，使 runner 调用的 `root` 命令与 Python 来自同一科学环境；仅指定 Python 路径还不够。

## 薄适配层需要提供哪些工具

以下是建议新增的 AMS 接口，不是 SCP SDK 已内置的工具。

| 工具 | 输入 | 返回与行为 |
| --- | --- | --- |
| `describe_task` | 无 | 科学目标、任务版本、允许输入、约束与产物说明。 |
| `start_demo` | 无 | 为参考解创建独立作业目录，启动完整 runner，立即返回 `job_id`。 |
| `get_job` | `job_id` | `running/succeeded/failed/cancelled`、退出码、日志尾部和可读产物列表。成功由进程退出码和产物检查确定。 |
| `start_validation` | 已完成的 `job_id`，是否重放 | 在同一作业目录启动验收子进程，返回独立的验收作业 ID。 |
| `read_result` | `job_id`、产物标识 | 报告文本，或约定 ROOT 对象的类型、维度、数值和单位；大型 ROOT 文件通过受控下载或共享存储交付。 |
| `cancel_job` | `job_id` | 停止该作业的进程组或容器，记录取消状态。 |

启动计算必须短返回。当前仓库 `src/scp/hub/client_server.py` 的 `SSEClient.call_tool()` 有 **30 秒超时**，不应让一次工具调用同步等待完整 RooFit、误差传播或重放。超时也不能当作计算已经停止。

首版使用后台子进程、作业记录和日志文件即可，无需另建复杂调度平台。作业记录应持久化 PID 或容器标识、工作目录、开始/结束时间与退出码；Server 重启后应重新核对进程状态，不能把内存中丢失的作业视为成功。

实现时创建 `FastMCP("AMSFlux", host="127.0.0.1", port=8000)`，使用 `@server.tool()` 注册上表的作业管理与结果读取函数，再用 `server.run(transport="sse")` 启动服务。

服务只执行固定的分析与验收入口，不接受来自客户端的任意 shell 字符串。`job_id` 映射到服务保存的工作目录；结果读取限制在该作业目录内。正式远程服务需要认证、作业归属检查和下载权限控制。

## 把 Server 注册到 SCP Hub

先在独立环境安装所核对的 SCP 源码版本，再运行适配服务：

```sh
git clone https://github.com/InternScience/scp.git
git -C scp checkout cea5398564032aea65a78e246d06c30ae945e03f
python3 -m venv .venv-scp
. .venv-scp/bin/activate
python -m pip install -e ./scp
```

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
  -d '{"name":"AMSFlux.start_demo"}'
```

Hub 会拼接 `server_url:server_port/sse`，所以 `server_url` 不含端口和 `/sse`。调用时名称使用 `AMSFlux.<tool_name>`；工具参数与 `name` 放在同一层，例如查询状态：

```json
{"name":"AMSFlux.get_job","job_id":"<returned-job-id>"}
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

本机文件路径只有本机消费者能用。远程调用应返回受控产物 URL 或可继续通过 `read_result` 获取的标识，不能把 Mac 绝对路径当成远程下载地址。ROOT 文件保留为数值与可编辑图的权威产物；需要网页预览时另行渲染图像。

## 从重跑 demo 进入真正的智能体评测

接入联调完成后，新建独立的评测工作区，只放 `Agent_Task/` 的内容，并为智能体提供读取文件、编写代码和在科学环境中执行命令的受限能力。让它读取 README、TASK、指定方法和公开验收要求，自行实现分析。

`Agent_Task` 不包含现成的顶层分析 runner，不能把参考解的 `run.py` 当作题包自带入口。也不要将 `demo_agent_task`、参考结果或本说明挂载到被评测智能体可读的范围；只在提示词里要求“不看”不能形成隔离。

可以沿用题包给出的启动语句：

```text
Read README.md and TASK.md, complete and verify the analysis as instructed, and report the final result file paths and ROOT canvas names.
```

评测应预先约定计算资源、时间/交互预算和网络策略。SCP 管理任务调用与产物；智能体需要另行具备代码执行和科研推理能力。只暴露 `start_demo` 能验证固定工作流的重现，不能验证智能体是否从题目完成分析。

科学验收重点包括：九个指定子项目、规定的模板拟合和选择效率方法、绝对触发效率中心修正只应用一次且按题意排除其不确定度、能量迁移处理、可独立执行的不确定度源、完整协方差，以及干净工作区可重放的最终谱。新生成的科学代码、文档和图注仍按题包要求使用英文。

`demo_agent_task` 的验收程序用于它自己的实现和对象约定。若对另一智能体的独立解评分，应依据题包的公开验收合同和可用的独立评测器，不能强行要求它采用参考解的内部文件/对象名，或把参考解自检通过当成完整独立物理评分。

## 接入完成的判据

1. 客户端能列出 `AMSFlux` 工具，任务描述与题包一致。
2. 启动作业快速返回 ID，断开请求不丢失计算；查询能看到真实运行状态、日志及失败退出码。
3. 独立工作区由既有入口重新生成参考解；最终谱、协方差、报告与 ROOT canvas 可读取，验收及干净重放通过。
4. 经 Hub 的同一工具调用路径可用，远程消费者能实际取得产物。
5. 如用于能力评测，题包与参考解隔离，记录预算、版本、命令及独立验收结论。

当前已完成材料整理、接入方案核对、原生科学环境安装和完整 demo 执行。SCP SDK、薄适配层和平台注册尚未实施；接入阶段从上表中的薄适配层开始。

## 核对来源

- [项目 README 与 Hub/Server 架构](https://github.com/InternScience/scp)。
- [FastMCP 服务封装](https://github.com/InternScience/scp/blob/cea5398564032aea65a78e246d06c30ae945e03f/src/scp/server/fastmcp/server.py)。
- [Hub 注册、发现和调用实现](https://github.com/InternScience/scp/blob/cea5398564032aea65a78e246d06c30ae945e03f/src/scp/hub/hub_server.py)。
- [Hub 的 SSE 客户端与 30 秒超时](https://github.com/InternScience/scp/blob/cea5398564032aea65a78e246d06c30ae945e03f/src/scp/hub/client_server.py)。
- 本地项目介绍 PDF 第 2–3、13 页；题包 README、TASK、ACCEPTANCE；参考解 README、runner 和验收实现；Runtime_Environment 的原生与 Docker 使用说明。
