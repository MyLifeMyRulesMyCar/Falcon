"""Phase A validation: probe the RKNN model's I/O format with the installed
rknn-toolkit-lite2. Not part of the pipeline — its only job is confirming
whether inference() returns float32 (dequantized) or raw int8 output."""

import sys
from pathlib import Path

import numpy as np
from rknnlite.api import RKNNLite

MODEL = Path(__file__).resolve().parent / "model" / "yolov5s_relu_rk3576.rknn"


def main() -> None:
    print("model:", MODEL)
    rknn = RKNNLite()
    assert rknn.load_rknn(str(MODEL)) == 0, "load_rknn failed"
    assert rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_0) == 0, "init_runtime failed"

    dummy = np.zeros((1, 640, 640, 3), dtype=np.uint8)
    outputs = rknn.inference(inputs=[dummy])

    print(f"inference returned {len(outputs)} tensors:")
    for i, o in enumerate(outputs):
        print(
            f"  output[{i}]: shape={o.shape} dtype={o.dtype} "
            f"min={float(o.min()):.4f} max={float(o.max()):.4f}"
        )

    shapes = [tuple(o.shape) for o in outputs]
    expected = {(1, 255, 80, 80), (1, 255, 40, 40), (1, 255, 20, 20)}
    print("shapes match YOLOv5 640x640 3-head:", set(shapes) == expected)
    print("output dtype:", outputs[0].dtype)
    rknn.release()


if __name__ == "__main__":
    main()
