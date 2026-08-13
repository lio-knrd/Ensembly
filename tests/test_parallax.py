from pathlib import Path

import numpy as np
from PIL import Image

from backend.services import parallax


def test_depth_is_only_used_for_dolly_style_moves():
    assert parallax.uses_depth("push_in")
    assert parallax.uses_depth("pull_out")

    for move in (
        "static",
        "pan_left",
        "pan_right",
        "tilt_up",
        "tilt_down",
        "punch_in",
    ):
        assert not parallax.uses_depth(move)


def test_depth_preprocessing_preserves_portrait_aspect(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "portrait.png"
    Image.new("RGB", (768, 1365), "white").save(image_path)
    seen = {}

    class Input:
        name = "pixel_values"

    class Session:
        def get_inputs(self):
            return [Input()]

        def run(self, _outputs, inputs):
            tensor = inputs["pixel_values"]
            seen["shape"] = tensor.shape
            values = np.linspace(
                0, 1, tensor.shape[2] * tensor.shape[3], dtype=np.float32
            )
            return [values.reshape(1, tensor.shape[2], tensor.shape[3])]

    monkeypatch.setattr(parallax, "_get_session", lambda: Session())
    depth = parallax.depth_map(image_path, cache=False)

    assert seen["shape"] == (1, 3, 924, 518)
    assert depth.shape == (924, 518)


def test_depth_uses_same_cover_crop_as_artwork():
    # Portrait output should retain only the flat centre of this landscape map.
    depth = np.zeros((100, 200), dtype=np.float32)
    depth[:, 50:150] = 0.5
    depth[:, 150:] = 1.0
    rendered = parallax._render_depth(depth, (100, 200))
    assert np.count_nonzero(rendered) == 0


def test_guided_depth_does_not_invent_structure_for_a_flat_map():
    panel = Image.new("RGB", (100, 200), "navy")
    depth = np.full((100, 50), 0.4, dtype=np.float32)
    mesh = parallax._guided_mesh_depth(panel, depth, panel.size)
    assert np.count_nonzero(mesh) == 0


def test_neutral_camera_pose_reconstructs_source():
    width, height = 100, 200
    panel = Image.new("RGB", (width, height), "navy")
    depth = np.tile(np.linspace(0, 1, width, dtype=np.float32), (height, 1))
    mesh = parallax._guided_mesh_depth(panel, depth, panel.size)
    rendered = parallax._warp(panel, mesh, 1.0, 0.0, 0.0, panel.size)
    delta = np.abs(np.asarray(rendered).astype(int) - np.asarray(panel).astype(int))
    assert delta.max() <= 1
