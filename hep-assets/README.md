# HEP Assets

面向高能物理科学智能体，组织知识、工具、模型、数据目录和科研工作流资产。

## 启动

安装 uv，准备[科学运行环境](analysis/README.md#运行环境)。在本目录的两个终端分别运行：

```sh
# Hub 与服务端 MCP 入口
uv run --locked --group hub hub/run.py

# 分析服务
uv run --locked --group analysis python -m analysis.server \
  --runtime /path/to/scientific-runtime
```

分析服务自动注册到 Hub。客户端通过 Streamable HTTP 连接 `http://127.0.0.1:46381/mcp`，文件传输默认使用 `http://127.0.0.1:8001`。

## 模块

- [SCP Hub](hub/README.md)：工具注册、MCP 接入与部署配置。
- [分析工作区](analysis/README.md)：运行环境、文件传输与任务管理。
- [AMS 通量分析](flux/README.md)：原始 demo、输入数据与科学运行环境。
