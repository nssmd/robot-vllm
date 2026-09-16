"""Bounded append-only visual history for provider-side prefix caching.

Preserves actual historical frames; it does not substitute old frames for current
ones, cache answers, or force a provider cache hit. One instance per episode.
"""
import base64
import copy
import math


class ObservationHistory:
    def __init__(self, episode_id, *, max_frames=8, max_bytes=16 * 1024 * 1024):
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError("episode_id_required")
        if type(max_frames) is not int or not 1 <= max_frames <= 64:
            raise ValueError("invalid_history_frame_limit")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid_history_byte_limit")
        self.episode_id, self.max_frames, self.max_bytes = episode_id, max_frames, max_bytes
        self._frames = []
        self._last_sequence = -1
        self._last_time = -math.inf
        self.evictions = 0

    def append(self, *, sequence, captured_at, png_base64):
        if type(sequence) is not int or sequence <= self._last_sequence:
            raise ValueError("history_sequence_must_increase")
        if type(captured_at) not in (int, float) or not math.isfinite(captured_at) or captured_at < self._last_time:
            raise ValueError("history_capture_time_must_not_decrease")
        if not isinstance(png_base64, str):
            raise ValueError("png_base64_required")
        if len(png_base64) > (self.max_bytes + 2) // 3 * 4:
            raise ValueError("history_frame_exceeds_byte_limit")
        raw = base64.b64decode(png_base64, validate=True)
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError("history_png_required")
        if len(raw) > self.max_bytes:
            raise ValueError("history_frame_exceeds_byte_limit")
        self._frames.append({"sequence": sequence, "captured_at": captured_at,
                             "png": png_base64, "bytes": len(raw)})
        self._last_sequence, self._last_time = sequence, captured_at
        while len(self._frames) > self.max_frames or sum(f["bytes"] for f in self._frames) > self.max_bytes:
            self._frames.pop(0)
            self.evictions += 1

    def messages(self, question):
        if not self._frames or not isinstance(question, str) or not question:
            raise ValueError("history_and_question_required")
        # Older labels never change from 'current' to 'history'. The final query
        # explicitly names the current sequence instead, preserving the prefix.
        messages = [{"role": "user", "content": [
            {"type": "input_text", "text": f"Episode {self.episode_id}; observation {f['sequence']}; captured_at={f['captured_at']}."},
            {"type": "input_image", "image_url": "data:image/png;base64," + f["png"], "detail": "high"}
        ]} for f in self._frames]
        messages.append({"role": "user", "content": [{"type": "input_text", "text":
            f"Current observation is {self._last_sequence}. Earlier observations are history, not current state. {question}"}]})
        return copy.deepcopy(messages)

    def snapshot(self):
        return {"episode_id": self.episode_id, "frames": len(self._frames),
                "decoded_image_bytes": sum(f["bytes"] for f in self._frames),
                "evictions": self.evictions, "last_sequence": self._last_sequence}
