from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import Provider
from .errors import ProviderDeadError, ProviderSpawnError, ProviderStallError

log = logging.getLogger(__name__)


def _drain_stderr(
    stderr_pipe,
    log_path: Path,
) -> None:
    """Daemon thread: read cc stderr line-by-line and append to log_path."""
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            for raw_line in stderr_pipe:
                line = raw_line.rstrip("\n")
                if not line.strip():
                    continue
                ts = datetime.now(UTC).isoformat(timespec="seconds")
                try:
                    fh.write(f"{ts} {line}\n")
                    fh.flush()
                except Exception as exc:
                    log.warning("cc-stderr-drain write failed: %s", exc)
    except Exception as exc:
        log.warning("cc-stderr-drain thread error: %s", exc)
    finally:
        try:
            stderr_pipe.close()
        except Exception:
            pass

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# Liveness defaults (seconds of CONTINUOUS silence between stream events).
# Overridable per bridge via config.toml; bridges pass explicit values.
_DEFAULT_IDLE_SOFT_S = 60.0
_DEFAULT_IDLE_HARD_S = 300.0

# Per-turn OUTPUT token brake: cancel a runaway turn (e.g. a huge thinking
# spiral) instead of burning quota for 15 minutes. Overridable per bridge via
# config.toml; 0 or negative disables. Counts ONLY newly produced output
# tokens for the current turn — never input/cache figures (those reflect
# window size and would false-trigger every turn).
_DEFAULT_TURN_OUTPUT_CAP = 20000

# E-polish outbound quote v3: teach cc the bridge-specific <quote> protocol.
# Injected once per session via --append-system-prompt so cc emits the tag
# at bubble-heads when it intends to quote-reply, and never as filler text.
QUOTE_SYSTEM_PROMPT = (
    "When you want to reply to a specific earlier message from the user, "
    "start that bubble with <quote>their exact words or a unique fragment"
    "</quote> followed by your reply. Example: if the user said "
    "'try it', a quote-reply bubble looks like "
    "<quote>try it</quote>ok trying now.\n\n"
    "Only use <quote> when you are actually quoting a prior user message. "
    "Never write the literal characters `<quote>` outside of an actual "
    "quote-reply."
)

# Outbound media: teach cc the bridge tag so split.py can dispatch real
# channel media messages. Only these four tags exist — no generic <media>.
MEDIA_SYSTEM_PROMPT = (
    "To send media (photo/gif/video/file), put a tag in your reply, one tag "
    'per file: <image path="/abs/p.jpg"> <gif path="/abs/a.gif"> '
    '<video path="/abs/v.mp4"> <file path="/abs/doc.pdf"> '
    "(file = pdf/txt/any other type). Tag position is bubble order: a "
    "reference/document the user asked for leads; a sticker or reaction "
    "image lands where it fits the conversation, not forced to the front. "
    "The bridge uploads it and delivers a real channel media message. "
    "The path must be a real existing local file - never fabricate.\n\n"
    "Stickers: The user loves it when you pair messages with stickers — "
    "they're your actions and inner voice. When you express affection, tease, "
    "react, or show a mood, call sticker(action='search', query=...) by vibe/emotion "
    "(e.g. '老婆别走' → search '爱你' '委屈' '哭') "
    "Don't wait for a special moment — weave them in naturally. "
    "Call sticker(action='pick', sticker_id=id), then send with the matching tag: "
    "<image path=\"...\"/> for .png/.jpg/.webp, <gif path=\"...\"/> for .gif. "
    "When user sends an image: caption '1' = save as sticker (code-routed), "
    "'0' = skip; no digit = if it looks sticker-worthy, just call "
    "sticker_admin(action='ingest') "
    "directly — dedup handles duplicates, so never ask, just try. "
    "Do not add daily photos. (never run /sticker-entry, CLI batch tool). "
    "Desc format for ingest: emotion/scene | image text | one-line visual (CN preferred)."
)

# HTML-comment silence protocol: the bridge strips all <!-- ... --> from replies
# before sending. A reply consisting solely of comments sends nothing at all.
SILENCE_SYSTEM_PROMPT = (
    "The bridge strips all HTML comments <!-- ... --> from your reply before "
    "delivering it to the user. If you judge that this turn needs no reply at "
    "all (nothing worth saying, or the user should not be disturbed), reply "
    "with ONLY a comment, e.g. <!-- silent -->, and the bridge will send "
    "nothing. You may also use comments for private asides that should not be "
    "delivered. Never rely on comments being visible to the user."
)

WX_ICLOUD_PROMPT = (
    "Large files are auto-routed to iCloud by the bridge — no action needed."
)

NIGHT_SYSTEM_PROMPT = (
    "Before 23:00, never proactively bring up sleep or push the user to go "
    "to bed. Bedtime coaxing starts only when a 23:00 nudge arrives."
)


class ClaudeCodeProvider(Provider):
    """Persistent `claude` CLI subprocess speaking stream-json over stdio."""

    def __init__(
        self,
        model: str | None = None,
        resume_sid: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        binary: str = "claude",
        effort_level: str | None = None,
        *,
        channel: str,
        stderr_log: Path | None = None,
        system_prompts: list[str] = (),
        marrow_bridge: bool = False,
        idle_soft_s: float = _DEFAULT_IDLE_SOFT_S,
        idle_hard_s: float = _DEFAULT_IDLE_HARD_S,
        turn_output_cap: int = _DEFAULT_TURN_OUTPUT_CAP,
    ) -> None:
        self.model = model
        self.resume_sid = resume_sid
        self.cwd = cwd
        self.extra_env = extra_env or {}
        self.binary = binary
        # /effort: when set, append `--effort <level>` so cc uses the matching
        # effort tier on this swap. None = omit; cc applies its own default.
        # Valid levels: low|medium|high|xhigh|max|ultracode|auto (cc 2.1.159+).
        self.effort_level = effort_level
        self.stderr_log = stderr_log
        self.system_prompts = list(system_prompts)
        self.marrow_bridge = marrow_bridge
        self.channel = channel
        self.idle_soft_s = idle_soft_s
        self.idle_hard_s = idle_hard_s
        self.turn_output_cap = turn_output_cap
        self.process: subprocess.Popen[str] | None = None
        self.alive: bool = False
        self.session_id: str | None = None
        self.usage_total: dict[str, int] = {}
        # Resident reader thread infrastructure (populated in spawn()).
        self._event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._complete_turns: int = 0
        self._turn_lock = threading.Lock()
        # Per-turn output cap state (reset at the start of every recv()).
        # turn_output_capped: sticky flag the loop layer reads after the turn
        # to send a "interrupted by the token cap" notice. NO retry on breach.
        # _turn_output_by_request: max output_tokens seen per request_id
        # within this turn; the current-turn total is the sum of these
        # values. Main-line only — subagent-attributed events are excluded.
        self.turn_output_capped: bool = False
        self._turn_output_by_request: dict[str, int] = {}

    def _build_cmd(self) -> list[str]:
        cmd = [
            self.binary,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
            # cc help mis-states this as "--print only" — in practice it works
            # with persistent stream-json subprocess too (claude-agent-sdk does
            # the same). Required to surface plaintext `thinking_delta` events;
            # without it, the final assistant `thinking` block is empty under
            # OAuth (redacted to signature only).
            "--include-partial-messages",
            # fable-5+ models default to thinking display "omitted": the
            # plaintext is empty and the full chain is encrypted into the
            # signature field (documented API behaviour, not a bug). Request
            # summarized display explicitly so `thinking_delta` events carry
            # readable text regardless of host settings. Needs cc >= 2.1.x.
            "--thinking-display", "summarized",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.resume_sid:
            cmd += ["--resume", self.resume_sid]
        if self.effort_level:
            cmd += ["--effort", self.effort_level]
        # Teach cc the bridge-specific <quote> + media-tag protocols once per
        # session. On --resume, cc replays prior turns so the appended prompt
        # persists; injecting per-turn would pollute context, hence here only.
        if self.system_prompts:
            cmd += [
                "--append-system-prompt",
                "\n\n".join(self.system_prompts),
            ]
        return cmd

    def spawn(self, env: dict[str, str] | None = None) -> None:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        merged.update(self.extra_env)
        if self.marrow_bridge:
            merged["MARROW_BRIDGE"] = "1"
        # Channel marker — the channel_marker hook reads this and prepends
        # the active channel to the user prompt.
        merged["MARROW_CHANNEL"] = self.channel
        try:
            self.process = subprocess.Popen(
                self._build_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=merged,
                start_new_session=True,
            )
        except OSError as e:
            raise ProviderSpawnError(f"claude spawn failed: {e}") from e
        self.alive = True
        # Reset buffered-turn state on each spawn so a re-spawned provider
        # starts with an empty queue and zero counter. The resident reader
        # (started below) is the sole stdout consumer; recv() times its
        # queue gets to measure continuous silence for the idle policy.
        self._event_queue = queue.Queue()
        self._complete_turns = 0
        if self.stderr_log is not None:
            t = threading.Thread(
                target=_drain_stderr,
                args=(self.process.stderr, self.stderr_log),
                name="cc-stderr-drain",
                daemon=True,
            )
            t.start()
        reader = threading.Thread(
            target=self._reader_thread,
            name="cc-stdout-reader",
            daemon=True,
        )
        reader.start()

    def _reader_thread(self) -> None:
        """Daemon thread: sole consumer of process.stdout.

        Pumps every line into _event_queue as a parsed dict. Bad JSON is
        skipped with a warning (same policy as the old inline recv loop).
        On EOF (or any error) puts a None sentinel so recv() can detect
        subprocess death. Increments _complete_turns for each result frame
        so has_complete_turn() works without touching the queue.
        """
        assert self.process is not None
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning("skip non-json line: %s (%s)", line[:120], e)
                    continue
                if ev.get("type") == "result":
                    with self._turn_lock:
                        self._complete_turns += 1
                self._event_queue.put(ev)
        except Exception as exc:
            log.warning("cc-reader-thread error: %s", exc)
        finally:
            self._event_queue.put(None)

    def has_complete_turn(self) -> bool:
        """True iff at least one complete (result-terminated) turn is buffered."""
        with self._turn_lock:
            return self._complete_turns > 0

    def send(self, msg: str) -> None:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise ProviderDeadError("subprocess not alive")
        if self.process.stdin.closed:
            raise ProviderDeadError("stdin closed")
        payload = json.dumps(
            {"type": "user", "message": {"role": "user", "content": msg}}
        )
        try:
            self.process.stdin.write(payload + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise ProviderDeadError(f"stdin write failed: {e}") from e

    def send_raw_user_text(self, text: str) -> None:
        """E-polish /compact: pipe a literal user message (e.g. '/compact') to cc.

        cc parses leading slashes as native slash commands on its own side, so
        sending '/compact' through the standard user frame triggers cc's
        built-in compaction. Same wire format as `send`, named separately so
        callers can express the intent and tests can target it.
        """
        self.send(text)

    def _next_event(self) -> dict[str, Any] | None:
        """Block for the next parsed event, enforcing the idle liveness policy.

        Measures CONTINUOUS silence: the deadline is reset by the caller each
        time an event arrives (a fresh _next_event call starts a new clock).
        - At idle_soft_s of silence: poll the subprocess. Dead -> ProviderDead;
          alive -> keep waiting (self-heal window for a slow tool call).
        - At idle_hard_s of silence: process-group kill, raise ProviderStall.
        Returns the parsed event dict, or the None EOF sentinel put by the
        resident reader on clean stream end.
        """
        start = time.monotonic()
        soft_checked = False
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= self.idle_hard_s:
                self._kill_process_group()
                raise ProviderStallError(
                    f"no stream event for {self.idle_hard_s:.0f}s (stall)"
                )
            # Wake at the next boundary (soft check, else hard deadline).
            if not soft_checked and elapsed < self.idle_soft_s:
                wait = self.idle_soft_s - elapsed
            else:
                wait = self.idle_hard_s - elapsed
            try:
                return self._event_queue.get(timeout=max(0.0, wait))
            except queue.Empty:
                if not soft_checked:
                    soft_checked = True
                    if self.process is not None and self.process.poll() is not None:
                        self.alive = False
                        raise ProviderDeadError(
                            "subprocess died during recv (soft check)"
                        )
                continue

    def recv(self) -> Iterator[dict[str, Any]]:
        if not self.alive or self.process is None:
            raise ProviderDeadError("subprocess not alive")
        # Reset per-turn output-cap state at the start of every send->result
        # cycle so the counter measures ONLY this turn's newly produced output.
        self.turn_output_capped = False
        self._turn_output_by_request = {}
        saw_result = False
        while True:
            ev = self._next_event()
            if ev is None:
                # EOF sentinel: re-enqueue so subsequent recv() callers also
                # get it immediately rather than blocking forever.
                self._event_queue.put(None)
                break
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                sid = ev.get("session_id")
                if isinstance(sid, str) and sid:
                    self.session_id = sid
            elif t == "assistant":
                usage = (ev.get("message") or {}).get("usage") or {}
                for k in _USAGE_KEYS:
                    v = usage.get(k)
                    if isinstance(v, int):
                        self.usage_total[k] = self.usage_total.get(k, 0) + v
            yield ev
            if t == "result":
                saw_result = True
                with self._turn_lock:
                    self._complete_turns = max(0, self._complete_turns - 1)
                break
            # Output-cap brake: after yielding the event, tally this turn's
            # newly produced output tokens and interrupt if it exceeds the cap.
            # Done post-yield so the loop still gets the breaching event.
            # Subagent (Task-dispatched) events carry parent_tool_use_id and
            # are excluded — see _turn_output_breached.
            if t == "assistant" and self._turn_output_breached(ev):
                self.turn_output_capped = True
                log.warning(
                    "turn output cap %d exceeded (%d) — interrupting turn",
                    self.turn_output_cap,
                    sum(self._turn_output_by_request.values()),
                )
                self.cancel()
                return
        # A cap-interrupted turn returns cleanly above; any other missing
        # result is a genuine mid-turn death.
        if not saw_result:
            raise ProviderDeadError("subprocess died during recv")

    def _turn_output_breached(self, ev: dict[str, Any]) -> bool:
        """Accumulate this turn's OUTPUT tokens and report a cap breach.

        Counts ONLY usage.output_tokens (never input/cache_* — those reflect
        window size, easily 100k+, and would false-trigger every turn).
        Subagent output is excluded: a Task-dispatched subagent's assistant
        events are interleaved into the SAME stream but carry a top-level
        `parent_tool_use_id` (the dispatching tool_use id) instead of None —
        verified empirically against a real stream-json transcript (2.1.197,
        `claude --output-format stream-json` dispatching a Task). Counting
        those would false-trigger the cap on ordinary agent-dispatch turns.

        A turn spans several API requests (one per tool round trip); the
        stream also repeats identical usage lines within a request. Dedup by
        top-level `request_id` (snake_case — also verified on the same
        transcript; NOT `requestId`): keep the MAX output_tokens seen per
        request_id, then sum across requests. Returns True once the sum
        exceeds turn_output_cap.
        """
        cap = self.turn_output_cap
        if cap is None or cap <= 0:
            return False
        if ev.get("parent_tool_use_id") is not None:
            return False
        usage = (ev.get("message") or {}).get("usage") or {}
        out = usage.get("output_tokens")
        if not isinstance(out, int):
            return False
        # Events without a request_id (synthetic/test) each count once under a
        # unique key so they still sum rather than clobber one another.
        req_id = ev.get("request_id")
        key = req_id if isinstance(req_id, str) and req_id else f"_noid_{id(ev)}"
        prev = self._turn_output_by_request.get(key, 0)
        if out > prev:
            self._turn_output_by_request[key] = out
        return sum(self._turn_output_by_request.values()) > cap

    def _kill_process_group(self) -> None:
        """SIGKILL the whole process group (spawned with start_new_session).

        Used on hard stall: the subprocess is alive but wedged, so a plain
        terminate() may not reap child tool processes. Mirrors the intent of
        cancel()/close() but guarantees the group dies immediately.
        """
        self.alive = False
        p = self.process
        if p is None:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.kill()
            except Exception:
                pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def send_control_response(
        self, request_id: str, behavior: str, payload: dict[str, Any]
    ) -> None:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise ProviderDeadError("subprocess not alive")
        body = {"behavior": behavior, **payload}
        frame = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": body,
            },
        }
        try:
            self.process.stdin.write(json.dumps(frame) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise ProviderDeadError(f"control_response write failed: {e}") from e

    def cancel(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is not None:
            self.alive = False
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        self.alive = False

    def is_alive(self) -> bool:
        return (
            self.alive
            and self.process is not None
            and self.process.poll() is None
        )

    def close(self) -> None:
        # Grace budget tuned so cc's SessionEnd hook can finish writing the
        # lifecycle:end audit row + bridge_owns marker before SIGKILL fires.
        # archive_events on a long session can take >2s; SIGKILL'ing before
        # the INSERT lands produces silent_death alerts in marrow catchup.
        if self.process is None:
            self.alive = False
            return
        p = self.process
        try:
            if p.stdin and not p.stdin.closed:
                try:
                    p.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            try:
                p.wait(timeout=25)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        p.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            self.alive = False
