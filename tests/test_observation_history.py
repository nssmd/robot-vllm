import base64

import pytest

from robot_vllm.observation_history import ObservationHistory
from robot_vllm.testing.pi05_devices import fixture_image


def test_new_frame_preserves_prior_prefix_and_names_current_sequence():
    history = ObservationHistory("episode-a")
    first = fixture_image((255, 0, 0))["data"]
    second = fixture_image((0, 255, 0))["data"]
    history.append(sequence=0, captured_at=1.0, png_base64=first)
    old = history.messages("Return JSON.")
    history.append(sequence=1, captured_at=2.0, png_base64=second)
    new = history.messages("Return JSON.")
    assert old[:-1] == new[:1]
    assert new[1]["content"][1]["image_url"].endswith(second)
    assert "Current observation is 1" in new[-1]["content"][0]["text"]
    new[0]["content"][0]["text"] = "corrupted"
    assert history.messages("question")[0] == old[0]


def test_bounds_evict_history_and_never_relabel_it_as_current():
    png = fixture_image((0, 0, 0))["data"]
    history = ObservationHistory("one", max_frames=2)
    for seq in range(3):
        history.append(sequence=seq, captured_at=seq, png_base64=png)
    assert history.snapshot()["evictions"] == 1
    assert "observation 1;" in history.messages("question")[0]["content"][0]["text"]
    limited = ObservationHistory("two", max_bytes=len(base64.b64decode(png)))
    for seq in range(2):
        limited.append(sequence=seq, captured_at=seq, png_base64=png)
    assert limited.snapshot()["frames"] == 1
    assert limited.snapshot()["evictions"] == 1


def test_bad_identity_time_or_image_does_not_mutate_history():
    history = ObservationHistory("one")
    png = fixture_image((0, 0, 0))["data"]
    history.append(sequence=2, captured_at=10, png_base64=png)
    for seq, timestamp, data in [(2, 11, png), (3, 9, png), (3, float('nan'), png), (3, 11, 'invalid')]:
        with pytest.raises(ValueError):
            history.append(sequence=seq, captured_at=timestamp, png_base64=data)
    assert history.snapshot()["frames"] == 1
    assert history.snapshot()["last_sequence"] == 2
