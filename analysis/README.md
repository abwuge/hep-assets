# Analysis Service

The analysis service gives agents isolated workspaces where they can read shared assets, run code, and produce result files.

## Start the service

On Linux, install Bubblewrap and enable user namespaces. On macOS, the service uses the built-in `sandbox-exec` utility.

Once the Hub is running, run this command from the repository root:

```sh
uv run python -m analysis
```

The service registers with the local Hub by default. Pass `--hub <hub-url>` to register with a different Hub, then connect the client to that Hub's MCP endpoint.

## Configuration

| Option | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8001` | Service port |
| `--hub` | `http://127.0.0.1:46380` | Hub registration URL |
| `--storage` | `analysis/.local/` | Task storage directory |
| `--assets` | `assets/` at the repository root | Read-only shared asset directory |
| `--threads` | `2` | Number of threads for numerical libraries |
| `--public-url` | Derived from the bind address | External URL for file transfers |

When using a reverse proxy, set `--public-url` to a URL clients can reach and forward requests under `/files/` at that URL to the analysis service.

## Workspace

- `inputs/` holds read-only files uploaded by the user.
- `work/` holds analysis code and intermediate files.
- `outputs/` holds results and reports.

Use `ASSETS_DIR` to read [shared assets](../assets/README.md). Copy any asset you need to modify into `work/` first.

Running commands survive client disconnections and service restarts. After reconnecting, you can check task status or cancel execution. Files already written remain available after cancellation.
