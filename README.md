# HEP Assets

HEP Assets provides an analysis service and shared knowledge, tools, models, data or data catalogs, and workflows for high-energy physics agents.

## Installation

Install uv and run the commands below from the repository root.

## Start the service

If you are using the bundled Hub, start it in a separate terminal:

```sh
uv run python -m hub
```

Once the Hub is running, start the analysis service:

```sh
uv run python -m analysis
```

The analysis service registers with the local Hub by default. To use a different Hub, pass `--hub <hub-url>`.

## Usage

Connect your client to the Hub's MCP endpoint, upload the task instructions and acceptance criteria, and request an analysis. The bundled Hub's MCP endpoint is `http://127.0.0.1:46381/mcp`.

Agents can read shared assets from `assets/`, run analyses in isolated workspaces, and return result files.

[Analysis service](analysis/README.md) · [Shared assets](assets/README.md) · [Optional Hub](hub/README.md)
