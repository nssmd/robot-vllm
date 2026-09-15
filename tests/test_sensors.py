import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from robot_vllm.models import multimodal_content
from robot_vllm.sensors import ros_image_png


def message(data, **kw):
    fields = {"width": 2, "height": 1, "step": 8, "encoding": "rgb8", "data": data,
              "header": SimpleNamespace(frame_id="camera", stamp=SimpleNamespace(sec=1, nanosec=2))}
    return SimpleNamespace(**{**fields, **kw})


def test_actual_pixels_and_row_padding_preserved():
    result = ros_image_png(message(bytes([255, 0, 0, 0, 255, 0, 99, 99])))
    image = Image.open(io.BytesIO(base64.b64decode(result["data"])))
    assert list(image.getdata()) == [(255, 0, 0), (0, 255, 0)]
    text, images = multimodal_content({"observation": {"images": {"camera": result}}})
    assert len(images) == 1 and result["data"] not in text


def test_zero_pixels_are_valid_data_not_missing():
    result = ros_image_png(message(bytes(8)))
    image = Image.open(io.BytesIO(base64.b64decode(result["data"])))
    assert list(image.getdata()) == [(0, 0, 0), (0, 0, 0)]


@pytest.mark.parametrize("raw", [b"", bytes(5), bytes(9)])
def test_missing_or_wrong_length_pixels_never_zero_filled(raw):
    with pytest.raises(ValueError, match="invalid_or_unsupported_image"):
        ros_image_png(message(raw))
