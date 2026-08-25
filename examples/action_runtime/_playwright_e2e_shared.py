from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

TODO_TITLE = "Review Agently E2E"
PLAYWRIGHT_MCP_PACKAGE = os.getenv("PLAYWRIGHT_MCP_PACKAGE", "@playwright/mcp@0.0.78")


APP_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Agently Todo</title>
    <style>
      body { font-family: sans-serif; max-width: 42rem; margin: 3rem auto; }
      form, li { display: flex; gap: .75rem; margin: 1rem 0; }
      .completed span { text-decoration: line-through; }
    </style>
  </head>
  <body>
    <main>
      <h1>Agently Todo</h1>
      <form id="todo-form">
        <label for="todo-input">New todo</label>
        <input id="todo-input" data-testid="todo-input" autocomplete="off" required>
        <button type="submit">Add todo</button>
      </form>
      <p id="status" role="status">Ready</p>
      <ul id="todo-list" aria-label="Todo list"></ul>
    </main>
    <script>
      const form = document.querySelector('#todo-form');
      const input = document.querySelector('#todo-input');
      const list = document.querySelector('#todo-list');
      const status = document.querySelector('#status');

      async function render() {
        const response = await fetch('/api/todos');
        const todos = await response.json();
        list.replaceChildren(...todos.map((todo, index) => {
          const item = document.createElement('li');
          item.className = todo.completed ? 'completed' : '';
          const checkbox = document.createElement('input');
          checkbox.type = 'checkbox';
          checkbox.checked = todo.completed;
          checkbox.setAttribute('aria-label', `Mark ${todo.title} complete`);
          checkbox.addEventListener('change', async () => {
            await fetch(`/api/todos/${index}`, {
              method: 'PUT',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({completed: checkbox.checked}),
            });
            status.textContent = checkbox.checked ? 'Todo completed' : 'Todo reopened';
            await render();
          });
          const title = document.createElement('span');
          title.textContent = todo.title;
          item.append(checkbox, title);
          return item;
        }));
      }

      form.addEventListener('submit', async event => {
        event.preventDefault();
        await fetch('/api/todos', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({title: input.value}),
        });
        input.value = '';
        status.textContent = 'Todo added';
        await render();
      });

      render();
    </script>
  </body>
</html>
"""


class TodoState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._todos: list[dict[str, Any]] = []

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(todo) for todo in self._todos]

    def add(self, title: str) -> None:
        with self._lock:
            self._todos.append({"title": title, "completed": False})

    def set_completed(self, index: int, completed: bool) -> None:
        with self._lock:
            self._todos[index]["completed"] = completed


def create_handler(state: TodoState) -> type[BaseHTTPRequestHandler]:
    class TodoHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return None

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            size = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(size) or b"{}")

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self._send(200, APP_HTML.encode(), "text/html; charset=utf-8")
                return
            if self.path == "/api/todos":
                body = json.dumps(state.snapshot()).encode()
                self._send(200, body, "application/json")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/todos":
                self._send(404, b"not found", "text/plain")
                return
            title = str(self._read_json().get("title", "")).strip()
            if not title:
                self._send(400, b"title is required", "text/plain")
                return
            state.add(title)
            self._send(201, b"{}", "application/json")

        def do_PUT(self) -> None:  # noqa: N802
            prefix = "/api/todos/"
            if not self.path.startswith(prefix):
                self._send(404, b"not found", "text/plain")
                return
            try:
                index = int(self.path.removeprefix(prefix))
                completed = self._read_json().get("completed")
                if not isinstance(completed, bool):
                    raise ValueError("completed must be a boolean")
                state.set_completed(index, completed)
            except (IndexError, ValueError):
                self._send(400, b"invalid todo update", "text/plain")
                return
            self._send(200, b"{}", "application/json")

    return TodoHandler


@contextmanager
def serve_todo_app() -> Iterator[tuple[str, TodoState]]:
    state = TodoState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def resolve_playwright_npx() -> tuple[str, dict[str, str]]:
    npx = os.getenv("PLAYWRIGHT_NPX_BIN") or shutil.which("npx")
    if not npx:
        raise RuntimeError("npx is required. Install Node.js 20+ and npm first.")

    # Keep the user-selected launcher path instead of resolving symlinks: Homebrew's
    # npx symlink and its matching node binary intentionally share /opt/homebrew/bin.
    npx_path = Path(npx).expanduser().absolute()
    node = npx_path.with_name("node")
    if not node.exists():
        resolved_node = shutil.which("node")
        if not resolved_node:
            raise RuntimeError("node is required. Install Node.js 20+ first.")
        node = Path(resolved_node).resolve()

    version = subprocess.run(
        [str(node), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    major = int(version.removeprefix("v").split(".", 1)[0])
    if major < 20:
        raise RuntimeError(
            f"Playwright MCP requires Node.js 20+, but {node} reports {version}. "
            "Set PLAYWRIGHT_NPX_BIN to an npx installation backed by Node.js 20+."
        )

    env_path = os.pathsep.join([str(npx_path.parent), os.environ.get("PATH", "")])
    return str(npx_path), {"PATH": env_path}


def create_playwright_mcp_config(base_url: str, output_dir: str) -> dict[str, Any]:
    npx, mcp_env = resolve_playwright_npx()
    return {
        "mcpServers": {
            "playwright": {
                "command": npx,
                "args": [
                    "--yes",
                    PLAYWRIGHT_MCP_PACKAGE,
                    "--headless",
                    "--isolated",
                    "--browser",
                    "chrome",
                    "--allowed-origins",
                    base_url,
                    "--output-dir",
                    output_dir,
                ],
                "env": mcp_env,
            }
        }
    }


def assert_e2e(records: list[dict[str, Any]], state: TodoState) -> None:
    successful_browser_actions = [
        str(record.get("action_id", ""))
        for record in records
        if record.get("status") in {"success", "partial_success"}
        and str(record.get("action_id", "")).startswith("browser_")
    ]
    assert "browser_navigate" in successful_browser_actions, successful_browser_actions
    assert len(successful_browser_actions) >= 3, successful_browser_actions
    assert state.snapshot() == [{"title": TODO_TITLE, "completed": True}], (
        state.snapshot()
    )


def print_evidence(
    records: list[dict[str, Any]], state: TodoState, report: Any
) -> None:
    print("[ACTION_RECORDS]")
    print(
        json.dumps(
            [
                {
                    "action_id": record.get("action_id"),
                    "status": record.get("status"),
                    "error": record.get("error"),
                }
                for record in records
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    print("[SERVER_STATE]", json.dumps(state.snapshot(), ensure_ascii=False))
    print("[E2E_REPORT]", json.dumps(report, ensure_ascii=False))
