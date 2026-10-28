# SCP Hub

仓库自带的官方 SCP Hub，通过 Streamable HTTP 向客户端提供工具发现和调用。已有 Hub 时可直接使用，无需启动本组件。

## 启动与连接

在项目根目录运行：

```sh
uv run python -m hub
```

- 客户端 MCP URL：`http://127.0.0.1:46381/mcp`。
- 内部 Hub API：`http://127.0.0.1:46380`。

停止启动器时，Hub 和 MCP 入口一起停止。

## 配置

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | MCP 监听地址 |
| `--port` | `46381` | MCP 端口 |
| `--hub-port` | `46380` | 内部 Hub 端口 |
| `--storage` | `hub/.local/` | 服务注册记录目录 |

远程接入可设置 `--host 0.0.0.0`，客户端使用服务器 IP。公网部署需由反向代理提供 HTTPS 和认证，并限制内部 Hub 及其全接口监听的 PermissionServer 的访问。端口被占用时启动失败，需指定其他端口。

## 注册下游服务

分析服务默认注册到本机 Hub：

```sh
uv run python -m analysis
```

注册到其他 Hub 时使用 `--hub <Hub地址>`。

其他 SSE 服务启动后，可调用注册接口：

```sh
curl --fail http://127.0.0.1:46380/register_server \
  -H 'Content-Type: application/json' \
  -d '{"server_name":"AMSTask","server_description":"Scientific task workspace","server_url":"http://127.0.0.1","server_port":8001,"type":"sse"}'
```
