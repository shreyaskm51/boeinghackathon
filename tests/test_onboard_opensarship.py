"""Tests for the OpenSARShip training path (no dataset download needed)."""
from __future__ import annotations

import io
import json
import struct
import tarfile

import numpy as np
import pytest

from onboard_ai.cfar_onnx_detector import (
    CLASS_NAMES,
    N_FEATURES,
    OPENSARSHIP_MODEL_PATH,
    get_classifier,
)
from onboard_ai.train_opensarship import (
    iter_shard,
    parse_rbb,
    process_sample,
    read_float_tiff,
    training_rows,
)


def _float_tiff(arr: np.ndarray, photometric: int = 34892) -> bytes:
    """Minimal little-endian uncompressed float32 TIFF, laid out like the OSSDD chips."""
    h, w = arr.shape
    pixels = arr.astype("<f4").tobytes()
    entries = [(256, 3, 1, w), (257, 3, 1, h), (258, 3, 1, 32), (259, 3, 1, 1), (262, 3, 1, photometric),
               (273, 4, 1, 8), (277, 3, 1, 1), (278, 3, 1, h), (279, 4, 1, len(pixels)), (339, 3, 1, 3)]
    ifd = struct.pack("<H", len(entries))
    for tag, typ, cnt, val in entries:
        ifd += struct.pack("<HHI", tag, typ, cnt) + (struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val))
    ifd += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", 8 + len(pixels)) + pixels + ifd


def _sample(key: str, amp: np.ndarray, rbb: str, land: float = 0.0) -> dict[str, bytes]:
    return {"__key__": key, "vv.tif": _float_tiff(amp), "rbb.txt": rbb.encode(),
            "meta.json": json.dumps({"land_fraction": land}).encode()}


def _sea_with_ship(seed: int = 0) -> tuple[np.ndarray, str]:
    rng = np.random.default_rng(seed)
    intensity = rng.gamma(4.0, 30000.0 / 4.0, size=(300, 300))
    intensity[140:160, 148:152] = 3.0e6           # 200 m x 40 m ship, very bright
    rbb = "1 \r\n1 148 140 152 140 152 160 148 160 150 150 \r\n"
    return np.sqrt(intensity).astype(np.float32), rbb


def test_read_float_tiff_roundtrip():
    arr = np.arange(12, dtype=np.float32).reshape(3, 4) * 1.5
    assert np.array_equal(read_float_tiff(_float_tiff(arr)), arr)


def test_read_float_tiff_rejects_non_tiff():
    with pytest.raises(ValueError):
        read_float_tiff(b"\x89PNG\r\n\x1a\n....")


def test_parse_rbb_length_width_and_edge_flag():
    ships = parse_rbb("2 \r\n1 300 676 312 676 312 700 300 700 306 688 \r\n2 10 10 30 10 30 14 10 14 20 12 \r\n",
                      (700, 700))
    assert len(ships) == 2
    assert ships[0].at_edge and not ships[1].at_edge          # first box touches the bottom edge
    assert ships[1].length_px == pytest.approx(20) and ships[1].width_px == pytest.approx(4)
    assert ships[1].size_class == "CARGO_VESSEL"              # 200 m


def test_iter_shard_groups_fields_by_key(tmp_path):
    path = tmp_path / "shard.tar"
    with tarfile.open(path, "w") as tar:
        for key in ("a", "b"):
            for fld, payload in (("vv.tif", b"x"), ("rbb.txt", b"0"), ("meta.json", b"{}")):
                info = tarfile.TarInfo(f"{key}.{fld}")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    samples = list(iter_shard(path))
    assert [s["__key__"] for s in samples] == ["a", "b"]
    assert set(samples[0]) == {"__key__", "vv.tif", "rbb.txt", "meta.json"}


def test_process_sample_labels_ship_and_clutter():
    amp, rbb = _sea_with_ship()
    res = process_sample(_sample("k", amp, rbb))
    assert res is not None and len(res.ships) == 1
    assert any(m == 0 for m in res.match), "the bright ship must be matched to its rotated box"
    X, y, stats = training_rows([res])
    assert X.shape[1] == N_FEATURES
    assert stats["real_rows_by_class"]["CARGO_VESSEL"] >= 1
    assert set(np.unique(y)) <= set(range(len(CLASS_NAMES)))


def test_edge_ships_are_not_used_for_training():
    amp, _ = _sea_with_ship()
    rbb = "1 \r\n1 148 0 152 0 152 160 148 160 150 80 \r\n"   # box touches the top edge
    res = process_sample(_sample("k", amp, rbb))
    _, _, stats = training_rows([res])
    assert stats["real_ship_rows"] == 0


@pytest.mark.skipif(not OPENSARSHIP_MODEL_PATH.exists(), reason="real-data model not trained yet")
def test_committed_opensarship_model_is_used_and_compatible():
    clf = get_classifier()
    assert clf.path == OPENSARSHIP_MODEL_PATH
    assert clf.session.get_inputs()[0].shape[-1] == N_FEATURES
