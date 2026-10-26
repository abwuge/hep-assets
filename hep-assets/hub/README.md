# SCP Hub

## 启动

在 `hep-assets/` 目录执行：

```sh
uv run --locked --group hub hub/run.py
```

Hub 地址：`http://127.0.0.1:46380`。

## 注册工具服务

先启动下游服务，再向 Hub 注册其地址，例如：

```sh
curl --fail http://127.0.0.1:46380/register_server \
  -H 'Content-Type: application/json' \
  -d '{"server_name":"AMSTask","server_description":"Scientific task workspace","server_url":"http://127.0.0.1","server_port":8001,"type":"sse"}'
```

## 接入 MCP 客户端

在支持 MCP stdio 的客户端中配置以下进程命令，将路径换成实际项目目录：

```sh
uv run --directory /path/to/hep-assets --locked --group hub hub/mcp_bridge.py
```

## 测试

在 `hep-assets/` 目录执行：

```sh
uv run --locked --group hub python -m unittest discover -s hub/tests -v
```
