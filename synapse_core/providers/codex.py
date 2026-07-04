from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import Provider
from .errors import ProviderDeadError, ProviderSpawnError

log = logging.getLogger(__name__)

_APP_CODEX = Path("/Applications/Codex.app/Contents/Resources/codex")
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_output_tokens",
)


def is_codex_model(model: str | None) -> bool:
    if not model:
        return False
    lowered = model.strip().lower()
    return lowered == "codex" or lowered.startswith("codex:")


def codex_model_arg(model: str | None) -> str | None:
    if not model:
        return None
    stripped = model.strip()
    lowered = stripped.lower()
    if lowered == "codex":
        return None
    if lowered.startswith("codex:"):
        arg = stripped.split(":", 1)[1].strip()
        return arg or None
    return stripped


class CodexProvider(Provider):
    """One-turn Codex CLI provider with resumable Codex thread ids.

    Codex CLI is not a persistent stream-json subprocess like Claude Code.
    This wrapper keeps the bridge Provider contract by starting one `codex exec`
    process per turn and resuming the prior Codex thread id after the first
    turn.
    """

    def __init__(
        self,
        model: str | None = None,
        resume_sid: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        binary: str | None = None,
        effort_level: str | None = None,
        *,
        channel: str,
        stderr_log: Path | None = None,
        system_prompts: list[str] = (),
        sandbox: str = "danger-full-access",
    ) -> None:
        self.model = model or "codex"
        self._codex_model = codex_model_arg(model)
        self.session_id = resume_sid
        self.cwd = cwd
        self.extra_env = extra_env or {}
        self.binary = binary or (str(_APP_CODEX) if _APP_CODEX.is_file() else "codex")
        self.effort_level = effort_level
        self.channel = channel
        self.stderr_log = stderr_log
        self.system_prompts = list(system_prompts)
        self.sandbox = sandbox
        self.process: subprocess.Popen[str] | None = None
        self.alive = False
        self.usage_total: dict[str, int] = {}
        self._pending_msg: str | None = None
        self._bootstrapped = bool(resume_sid)

    def spawn(self, env: dict[str, str] | None = None) -> None:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        merged.update(self.extra_env)
        self.extra_env = merged
        self.alive = True

    def send(self, msg: str) -> None:
        if not self.alive:
            raise ProviderDeadError("provider not alive")
        if self._pending_msg is not None:
            raise ProviderDeadError("turn already pending")
        if not self._bootstrapped and self.system_prompts:
            parts = ["Bridge instructions:", *self.system_prompts, msg]
            self._pending_msg = "\n\n".join(p for p in parts if p)
            self._bootstrapped = True
        else:
            self._pending_msg = msg

    def _build_cmd(self) -> list[str]:
        cmd = [self.binary, "-a", "never"]
        if self.cwd:
            cmd += ["-C", self.cwd]
        if self.sandbox:
            cmd += ["-s", self.sandbox]
        cmd += ["exec"]
        if self.session_id:
            cmd += ["resume", "--json", "--skip-git-repo-check"]
            if self._codex_model:
                cmd += ["-m", self._codex_model]
            cmd += [self.session_id, "-"]
        else:
            cmd += ["--json", "--skip-git-repo-check"]
            if self._codex_model:
                cmd += ["-m", self._codex_model]
            cmd += ["-"]
        return cmd

    def recv(self) -> Iterator[dict[str, Any]]:
        if not self.alive:
            raise ProviderDeadError("provider not alive")
        if self._pending_msg is None:
            raise ProviderDeadError("no pending message")

        prompt = self._pending_msg
        self._pending_msg = None
        try:
            self.process = subprocess.Popen(
                self._build_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.cwd,
                env=self.extra_env or None,
            )
        except OSError as e:
            self.alive = False
            raise ProviderSpawnError(f"codex spawn failed: {e}") from e

        try:
            stdout, stderr = self.process.communicate(prompt)
        except OSError as e:
            self.alive = False
            self.process = None
            raise ProviderDeadError(f"codex communicate failed: {e}") from e
        if stderr:
            self._write_stderr(stderr)
        rc = self.process.returncode
        self.process = None
        if rc != 0:
            self.alive = False
            raise ProviderDeadError(f"codex exited with status {rc}")

        saw_result = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log.debug("skip non-json codex line: %s", line[:120])
                continue

            t = ev.get("type")
            if t == "thread.started":
                sid = ev.get("thread_id")
                if isinstance(sid, str) and sid:
                    self.session_id = sid
                    yield {
                        "type": "system",
                        "subtype": "init",
                        "session_id": sid,
                        "model": self.model,
                    }
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        yield {
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": text}],
                                "usage": {},
                            },
                            "session_id": self.session_id,
                        }
            elif t == "turn.completed":
                usage = self._normalise_usage(ev.get("usage"))
                for k in _USAGE_KEYS:
                    v = usage.get(k)
                    if isinstance(v, int):
                        self.usage_total[k] = self.usage_total.get(k, 0) + v
                yield {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "usage": usage,
                    "session_id": self.session_id,
                }
                saw_result = True

        if not saw_result:
            self.alive = False
            raise ProviderDeadError("codex completed without result event")

    @staticmethod
    def _normalise_usage(raw: Any) -> dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        usage = {k: v for k, v in raw.items() if isinstance(v, int)}
        cached = usage.pop("cached_input_tokens", None)
        if isinstance(cached, int):
            usage["cache_read_input_tokens"] = cached
        return usage

    def _write_stderr(self, body: str) -> None:
        if self.stderr_log is None:
            return
        try:
            self.stderr_log.parent.mkdir(parents=True, exist_ok=True)
            with self.stderr_log.open("a", encoding="utf-8") as fh:
                fh.write(body)
                if not body.endswith("\n"):
                    fh.write("\n")
        except OSError as e:
            log.warning("codex stderr log write failed: %s", e)

    def cancel(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._pending_msg = None

    def close(self) -> None:
        self.cancel()
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive
