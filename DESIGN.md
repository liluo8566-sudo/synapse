# Synapse — Design

> Channel-agnostic LLM bridge. Multi-provider, multimodal, marrow-companion. MIT.
> Goals + constraints only. Mechanism → MAP. Commands → COMMANDS.

## Goals

1. Channel parity — all commands, bidirectional media, sticker, session lifecycle, resume. No channel misses a feature.
2. Portable + standalone — clone + channel token + cc OAuth → working bridge. marrow = one config line (empty = opt-out). Provider swaps cc / API / Codex / local via config.
3. Native marrow companion — when wired, memory ingest + recall identical to cc terminal.
4. Native channel experience — [tg] markdown, inline keyboard, webp sticker, 4096-char, voice reply. [wx] WeChat bubble style, 200-char split, AES media, sticker catalog.
5. Cross-channel SID continuity — same sid across cli + wx + tg. Resume on any channel picks up same conversation.
6. Config-first customisable — persona, ack style, paths, cwd presets, provider all in config.toml. No hardcoded identity or paths in code.

## Architecture

- Single Python process per channel, launchd-supervised.
- Provider interface from synapse_core; channel code isolated.
- cc stream-json OAuth subprocess (no paid API).
- marrow integration via env gate + one config command string.

## Key rules

- Standalone repo; marrow = one config line.
- Bridge owns slash routing (cc can't parse / in stream-json).
- Atomic write on all state files.
- Ack strings via messages.py t() only — no inline f-string acks.
- Code uses generic terms (user/assistant). Persona from config.toml.

## Safety nets

- Alert, retry, launchd auto-restart, atomic write, provider death gate, session lifecycle, health gate.
