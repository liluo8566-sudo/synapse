"""Synapse-WX media subpackage — inbound (C0) + outbound (C1) orchestration."""

from .inbound import build_read_tool_instruction, materialize
from .outbound import dispatch_media_bubble, send_media

__all__ = [
    "build_read_tool_instruction",
    "dispatch_media_bubble",
    "materialize",
    "send_media",
]
