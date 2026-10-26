"""Temporary, recorded Hub transport for a clean native Codex subagent."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sys
import urllib.request
import uuid


def record(path, event):
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("name", help="list_tools or an unqualified tool name")
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if args.name == "list_tools":
        request = urllib.request.Request(args.hub + "/tools?server_name=AMSTask")
        with opener.open(request, timeout=40) as response:
            print(response.read().decode())
        return
    arguments = json.load(sys.stdin)
    call_id = uuid.uuid4().hex
    record(args.record, {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": call_id, "name": args.name, "input": arguments,
    }]}})
    try:
        request = urllib.request.Request(args.hub + "/tools/call_tool",
            data=json.dumps({**arguments, "name": "AMSTask." + args.name}).encode(),
            headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=40) as response:
            content = json.load(response)
        failed = False
    except Exception as error:
        content = [{"type": "text", "text": str(error)}]
        failed = True
    record(args.record, {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": failed,
    }]}})
    print(json.dumps(content, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
