# SCP Hub

The repository bundles the official SCP Hub, which lets clients discover and call tools over Streamable HTTP. If you already have a Hub, use it directly.

## Start and connect

From the repository root, run:

```sh
uv run python -m hub
```

- The client MCP endpoint is `http://127.0.0.1:46381/mcp`.
- The internal Hub API is at `http://127.0.0.1:46380`.

Stopping the launcher shuts down both the Hub and the MCP endpoint.

## Configuration

| Option | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | MCP bind address |
| `--port` | `46381` | MCP port |
| `--hub-port` | `46380` | Internal Hub port |
| `--storage` | `hub/.local/` | Directory for service registration records |

For remote access, set `--host 0.0.0.0` and have clients connect using the server's IP address. For public deployments, use a reverse proxy for HTTPS and authentication. Restrict access to the internal Hub and to PermissionServer, which listens on all interfaces. If a port is already in use, choose a different one before starting the Hub.

## Register downstream services

The analysis service registers with the local Hub by default:

```sh
uv run python -m analysis
```

Pass `--hub <hub-url>` to register with a different Hub.

To register another SSE service, start the service and call the registration endpoint:

```sh
curl --fail http://127.0.0.1:46380/register_server \
  -H 'Content-Type: application/json' \
  -d '{"server_name":"AMSTask","server_description":"Scientific task workspace","server_url":"http://127.0.0.1","server_port":8001,"type":"sse"}'
```
