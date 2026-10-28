# HEP Assets

为高能物理智能体提供共享数据、参考资料和分析执行工具。

## 安装

安装 uv 和 Git LFS，在项目根目录运行：

```sh
git lfs install
git lfs pull
```

## 启动

在两个终端分别运行：

```sh
uv run --locked --group hub hub/run.py
```

```sh
uv run --locked --group analysis python -m analysis.server
```

## 使用

在智能体客户端添加 MCP 地址 `http://127.0.0.1:46381/mcp`，上传任务说明和验收要求，提出分析需求。

智能体可读取 `assets/` 中的数据和参考资料，在独立工作区执行分析并返回结果。

[分析服务配置](analysis/README.md) · [资产目录](assets/README.md) · [Hub 配置](hub/README.md)
