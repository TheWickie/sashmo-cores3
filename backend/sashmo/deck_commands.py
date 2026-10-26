"""Short-lived, idempotent CoreS3 commands issued only by the local desktop."""

from __future__ import annotations

import time
import secrets


class DeckCommands:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.sequence = secrets.randbelow(1 << 30)
        self.pending: dict[str, int | str | float] | None = None
        self.last_result: dict[str, int | str] | None = None
        self.client_seen_at = float("-inf")

    def note_client_poll(self) -> None:
        self.client_seen_at = self.clock()

    def fast_poll_enabled(self) -> bool:
        return self.clock() - self.client_seen_at <= 20

    def issue_talk(self, device_ready: bool) -> dict[str, int | str]:
        if not device_ready:
            raise ValueError("CoreS3 is offline or busy")
        if self.pending and self.clock() < float(self.pending["expires_at"]):
            raise ValueError("A Talk request is already pending")
        self.sequence += 1
        self.pending = {"seq": self.sequence, "kind": "talk",
                        "expires_at": int(self.clock() + 90)}
        self.last_result = {"seq": self.sequence, "state": "pending"}
        return dict(self.last_result)

    def for_pulse(self, ack_seq: int | None = None, ack_result: str | None = None) -> dict[str, int | str] | None:
        pending = self.pending
        if pending is None:
            return None
        if ack_seq == pending["seq"] and ack_result in {"started", "busy", "ignored"}:
            self.last_result = {"seq": ack_seq, "state": ack_result}
            self.pending = None
            return None
        if self.clock() >= float(pending["expires_at"]):
            self.last_result = {"seq": int(pending["seq"]), "state": "expired"}
            self.pending = None
            return None
        return dict(pending)

    def status(self) -> dict[str, int | str] | None:
        self.for_pulse()
        return dict(self.last_result) if self.last_result else None
