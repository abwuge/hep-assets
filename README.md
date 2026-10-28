# HEP Assets

为高能物理智能体提供共享数据、参考资料和分析执行工具。

## 安装

安装 uv 和 Git LFS，在项目根目录运行：

```sh
git lfs install
git lfs pull
```

## 启动

准备好 Hub 后，启动分析服务：

```sh
uv run python -m analysis
```

默认注册本机 Hub；使用其他 Hub 时添加 `--hub <Hub地址>`。

如需使用仓库自带的 Hub，在另一个终端先运行：

```sh
uv run python -m hub
```

## 使用

客户端连接所用 Hub 的 MCP 地址，上传任务说明和验收要求，提出分析需求。自带 Hub 的地址为 `http://127.0.0.1:46381/mcp`。

智能体可读取 `assets/` 中的数据和参考资料，在独立工作区执行分析并返回结果。

[分析服务配置](analysis/README.md) · [资产目录](assets/README.md) · [可选 Hub](hub/README.md)
