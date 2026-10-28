# 分析服务

为智能体提供工作区、共享资产读取、代码执行和结果文件。

## 启动

Linux 需安装 Bubblewrap 并允许用户命名空间；macOS 使用系统自带的 `sandbox-exec`。

启动 [Hub](../hub/README.md) 后，在项目根目录运行：

```sh
uv run --locked --group analysis python -m analysis.server
```

客户端连接 Hub 的 MCP 地址 `http://127.0.0.1:46381/mcp`，上传任务说明和文件，即可开始分析。

## 配置

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8001` | 服务端口 |
| `--hub` | `http://127.0.0.1:46380` | Hub 地址 |
| `--storage` | `analysis/.local/` | 任务存储目录 |
| `--assets` | 项目根目录的 `assets/` | 只读共享资产目录 |
| `--threads` | `2` | 数值库线程数 |
| `--public-url` | 按监听地址生成 | 文件传输的外部访问地址 |

使用反向代理时，通过 `--public-url` 指定客户端可达的地址，并将该地址下的 `/files/` 转发至本服务。

## 工作区

- `inputs/`：用户上传的文件，只读。
- `work/`：分析代码和中间文件。
- `outputs/`：结果和报告。

[共享资产](../assets/README.md)通过 `ASSETS_DIR` 读取。需要修改的资产先复制到 `work/`。

断开连接或重启服务不会取消正在运行的命令。重新连接后可继续查询任务，也可取消执行；取消后保留已有文件。
