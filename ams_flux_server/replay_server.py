"""Read-only, loopback-only trial replay with automatic record discovery."""

import argparse
import json
from pathlib import Path
import threading

from flask import Flask, Response, abort, request

from .trace_viewer import load_trial, render_html


class ReplayRecords:
    filenames = ("trial.json", "claude.jsonl", "prompt.txt", "assessment.json")

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.cache = {}
        self.signature = None
        self.revision = 0
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            directories = []
            if self.root.exists():
                directories = sorted(path for path in self.root.iterdir()
                                     if path.is_dir() and not path.is_symlink()
                                     and (path / "trial.json").is_file())
            signature, trials = [], []
            current = set()
            for directory in directories:
                stamp = []
                for name in self.filenames:
                    path = directory / name
                    if path.is_symlink():
                        raise ValueError("Replay records must not be symbolic links")
                    if path.exists():
                        stat = path.stat()
                        stamp.append((name, stat.st_mtime_ns, stat.st_size))
                stamp = tuple(stamp)
                current.add(directory.name)
                signature.append((directory.name, stamp))
                cached = self.cache.get(directory.name)
                if cached is None or cached[0] != stamp:
                    cached = (stamp, load_trial(directory))
                    self.cache[directory.name] = cached
                trials.append(cached[1])
            self.cache = {name: value for name, value in self.cache.items() if name in current}
            signature = tuple(signature)
            if signature != self.signature:
                self.revision += 1
                self.signature = signature
            trials.sort(key=lambda trial: (trial["meta"].get("started_at", 0), trial["name"]))
            return trials, f'W/"{self.revision}"'


def create_app(root):
    app = Flask(__name__, static_folder=None)
    records = ReplayRecords(root)

    @app.before_request
    def local_requests_only():
        if request.host.split(":", 1)[0] not in {"127.0.0.1", "localhost"}:
            abort(403)
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            abort(403)

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return response

    @app.get("/")
    def index():
        return Response(render_html([], live=True), content_type="text/html; charset=utf-8")

    @app.get("/api/replay")
    def replay():
        try:
            trials, etag = records.snapshot()
        except (OSError, ValueError, KeyError, TypeError):
            # A writer may be replacing metadata; keep the client's prior view and retry.
            return {"error": "Records are temporarily unreadable; retry on the next refresh."}, 503
        if request.headers.get("If-None-Match") == etag:
            return Response(status=304, headers={"ETag": etag})
        return Response(json.dumps({"trials": trials}, ensure_ascii=False),
                        content_type="application/json; charset=utf-8", headers={"ETag": etag})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=Path(__file__).resolve().parents[1] / "runs")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    print(f"Replay: http://127.0.0.1:{args.port}", flush=True)
    create_app(args.runs).run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
