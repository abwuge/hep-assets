# 分析工作区

接收用户数据，为每个分析任务提供独立工作区、代码执行、日志和结果文件。

## 运行环境

服务依赖由 uv 安装。`--runtime` 指定已配置的科学环境前缀，需包含 `bin/`；分析所需的 Python、ROOT 等程序由该环境提供。

- Linux：安装 Bubblewrap，并允许服务用户创建用户命名空间。
- macOS：使用系统自带的 `sandbox-exec`。

服务启动时检查隔离能力，不可用时退出。执行进程禁止联网，不继承客户端凭据。

## 启动

在 `hep-assets/` 目录启动 [Hub](../hub/README.md) 后运行：

```sh
uv run --locked --group analysis python -m analysis.server \
  --runtime /path/to/scientific-runtime
```

服务自动注册为 `HEPAnalysis`。默认在 `127.0.0.1:8001` 提供 MCP SSE 和文件传输接口，客户端通过 Hub 的 MCP URL 访问工具。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--runtime` | 必填 | 科学运行环境前缀 |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8001` | MCP SSE 与文件传输端口 |
| `--hub` | `http://127.0.0.1:46380` | Hub 注册地址 |
| `--storage` | `analysis/.local/` | 任务、文件和日志目录 |
| `--threads` | `2` | 数值库线程数 |
| `--public-url` | 按监听地址生成 | 覆盖文件传输根地址 |

端口被占用时需用 `--port` 指定其他端口。

## 文件传输

用户向智能体提供附件或文件链接，并描述分析需求。客户端需具备读取来源文件、发送 HTTP 请求和保存结果的能力。

文件传输优先使用 HTTP：`create_upload` 返回 PUT 目标，接收原始字节；`result_link` 为 `outputs/` 中的文件返回 GET 目标。两者通过 `urls` 公布候选地址，由客户端选择可达地址；所有地址均不可达时，警告并回退到 MCP 的 `upload_input`、`download_file`。

绑定具体 IP 时公布该 IP；绑定 `0.0.0.0` 或 `::` 时，枚举启用网卡上对应地址族的 IP。反向代理的外部地址不同时，用 `--public-url` 覆盖，例如 `https://files.example.org/analysis`，并将 `/analysis/files/` 转发至本服务的 `/files/`。代理需允许所需文件大小和传输时长。

传输 URL 含访问凭据。上传目标有效期为 24 小时，成功后失效；下载目标在 24 小时内可重复使用。上传完成后才移入 `inputs/`，已有输入不覆盖。未完成的 HTTP 上传需重新传输；MCP 支持按偏移续传，每块原始字节最多 256 KiB，使用 Base64 编码。

## 任务管理

每个任务包含三个目录：`inputs/` 为只读输入，`work/` 保存代码和中间文件，`outputs/` 保存交付结果。

`execute` 启动命令并返回命令 ID，`poll_command` 查询状态和输出。每个任务同时运行一条命令；使用 `bash -c`，遇错停止及管道错误传播需在命令中设置。`timeout_seconds=0` 表示不限时。

`cancel_command` 停止指定命令，`cancel_task` 停止任务并保留文件。客户端断开或服务重启后，可通过 `list_tasks`、`get_task` 和原命令 ID 继续查询；退出服务不会取消后台计算。

工具返回 `ok=false` 时读取 `error`；命令另检查 `status` 和 `exit_code`。分析完成后，用 `complete_task` 保存总结和产物清单。
