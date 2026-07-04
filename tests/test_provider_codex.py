from __future__ import annotations

import json
from unittest.mock import patch

from synapse_core.providers.codex import CodexProvider, codex_model_arg, is_codex_model


class _FakePopen:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 1234

    def communicate(self, prompt: str) -> tuple[str, str]:
        self.prompt = prompt
        return self._stdout, self._stderr

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_codex_model_detection() -> None:
    assert is_codex_model("codex")
    assert is_codex_model("Codex:gpt-5.5")
    assert not is_codex_model("claude-opus-4-8[1m]")
    assert codex_model_arg("codex") is None
    assert codex_model_arg("codex:gpt-5.5") == "gpt-5.5"


def test_initial_turn_parses_codex_json_events() -> None:
    stdout = _jsonl(
        {"type": "thread.started", "thread_id": "019f190f-58dc"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 7,
                "output_tokens": 2,
                "reasoning_output_tokens": 1,
            },
        },
    )
    fake = _FakePopen(stdout)
    with patch("synapse_core.providers.codex.subprocess.Popen", return_value=fake) as popen:
        p = CodexProvider(model="codex:gpt-5.5", cwd="/tmp/demo", channel="test")
        p.spawn(env={"X": "1"})
        p.send("hi")
        events = list(p.recv())

    cmd = popen.call_args[0][0]
    assert cmd[:3] == [p.binary, "-a", "never"]
    assert "-C" in cmd
    assert cmd[cmd.index("-C") + 1] == "/tmp/demo"
    assert "resume" not in cmd
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-5.5"
    assert cmd[-1] == "-"
    assert fake.prompt == "hi"
    assert p.session_id == "019f190f-58dc"
    assert events[0]["type"] == "system"
    assert events[1]["message"]["content"][0]["text"] == "OK"
    assert events[2]["usage"]["cache_read_input_tokens"] == 7


def test_resume_turn_uses_existing_thread_id() -> None:
    stdout = _jsonl(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "again"}},
        {"type": "turn.completed", "usage": {}},
    )
    fake = _FakePopen(stdout)
    with patch("synapse_core.providers.codex.subprocess.Popen", return_value=fake) as popen:
        p = CodexProvider(model="codex", resume_sid="thread-1", cwd="/tmp/demo", channel="test")
        p.spawn()
        p.send("continue")
        events = list(p.recv())

    cmd = popen.call_args[0][0]
    assert "resume" in cmd
    assert cmd[cmd.index("resume") + 1] == "--json"
    assert "thread-1" in cmd
    assert "-m" not in cmd
    assert events[0]["message"]["content"][0]["text"] == "again"
