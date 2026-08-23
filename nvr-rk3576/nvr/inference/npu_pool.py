"""Ownership of the RK3576 NPU cores.

Both cores are exposed as independent handles so the DetectionWorker can run
two inference threads in parallel (one per core) — measured ~1.6-1.8x
aggregate throughput over a single fused core at 640x640. A camera is never
tied to a fixed core: the shared work queue lets whichever core frees up
first grab the next frame.
"""

import numpy as np
from rknnlite.api import RKNNLite


class NpuCorePool:
    """Two RKNNLite handles, one per NPU core, over the same model."""

    def __init__(self, model_path: str):
        self._runtimes = {
            0: self._make_runtime(model_path, RKNNLite.NPU_CORE_0),
            1: self._make_runtime(model_path, RKNNLite.NPU_CORE_1),
        }

    @staticmethod
    def _make_runtime(model_path: str, core_mask) -> RKNNLite:
        rknn = RKNNLite()
        if rknn.load_rknn(model_path) != 0:
            raise RuntimeError(f"load_rknn failed: {model_path}")
        if rknn.init_runtime(core_mask=core_mask) != 0:
            raise RuntimeError(f"init_runtime({core_mask}) failed")
        return rknn

    def run_detection(self, frame: np.ndarray, core: int = 0) -> list[np.ndarray]:
        """Inference on the given core; returns the raw float32 head outputs."""
        outputs = self._runtimes[core].inference(inputs=[frame])
        if outputs is None:
            raise RuntimeError("NPU inference returned None")
        return list(outputs)

    def run_pose(self, crop: np.ndarray, core: int = 1) -> list[np.ndarray]:
        raise NotImplementedError("pose is not planned on this pool (M3 re-plans)")

    def release(self) -> None:
        for rknn in self._runtimes.values():
            rknn.release()
