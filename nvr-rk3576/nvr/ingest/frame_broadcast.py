"""Latest-frame broadcast: one shared-memory block per camera so browsers
can preview raw decoded frames without going through the detection path.

Blocks are created lazily by the first writer (the ingest worker, which
knows the camera's probed resolution — 640x360 for the local testbed, 720p
for public VOD sources) under a deterministic name, so the reader (the
panel process) can attach by name even though the block was created after
fork. Each camera's height/width are published through shared RawValues
before the frame bytes are copied. Resolution is fixed per camera after the
first write; a mid-run change is logged and ignored.

Write discipline (deadlock-free): bytes are copied FIRST, then the
generation counter bumps on a lock-free RawValue. A reader may catch a rare
torn frame mid-write (visually negligible for a preview); a writer dying
can never wedge readers, which a guarding Lock could (M1.1 lesson).
"""

import logging
import multiprocessing
import multiprocessing.shared_memory
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

_BLOCK_PREFIX = "nvr_fb_"


def _block_name(camera_name: str) -> str:
    return f"{_BLOCK_PREFIX}{camera_name}"


class LatestFrameStore:
    """Per-camera latest BGR frame + generation counter in shared memory."""

    def __init__(self, camera_names: list[str]):
        self.camera_names = list(camera_names)
        self._gens = {
            name: multiprocessing.RawValue("L", 0) for name in camera_names
        }
        self._hw = {
            name: (multiprocessing.RawValue("i", 0), multiprocessing.RawValue("i", 0))
            for name in camera_names
        }
        # Per-process handle cache: each process attaches its own handle.
        self._handles: dict[str, multiprocessing.shared_memory.SharedMemory] = {}

    def write(self, name: str, frame: np.ndarray) -> None:
        """Copy ``frame`` into the camera's block and bump generation."""
        handle = self._handles.get(name)
        if handle is None:
            try:
                # The panel (stable process) owns the blocks, so the name
                # survives worker restarts; attach to it first.
                handle = multiprocessing.shared_memory.SharedMemory(
                    name=_block_name(name)
                )
            except FileNotFoundError:
                # Startup race: panel hasn't created it yet.
                handle = multiprocessing.shared_memory.SharedMemory(
                    create=True, size=frame.nbytes, name=_block_name(name)
                )
            self._handles[name] = handle
        elif handle.size != frame.nbytes:
            # Resolution is fixed per camera after the first write.
            log.warning(
                "frame_store: %s resolution changed (%d -> %d bytes); ignoring",
                name, handle.size, frame.nbytes,
            )
            return
        hw = self._hw[name]
        hw[0].value, hw[1].value = frame.shape[0], frame.shape[1]
        np.copyto(
            np.frombuffer(handle.buf, dtype=np.uint8).reshape(frame.shape), frame
        )
        self._gens[name].value += 1

    def _ensure_block_by_shape(self, name: str) -> bool:
        """Reader-side creation: the panel owns the blocks so names survive
        worker restarts. Lazily creates the named block once the camera's
        shape has been published by its worker."""
        if name in self._handles:
            return True
        if name not in self._gens:
            return False
        hw = self._hw[name]
        h, w = hw[0].value, hw[1].value
        if h <= 0 or w <= 0:
            return False
        try:
            self._handles[name] = multiprocessing.shared_memory.SharedMemory(
                create=True, size=h * w * 3, name=_block_name(name)
            )
        except FileExistsError:
            try:
                self._handles[name] = multiprocessing.shared_memory.SharedMemory(
                    name=_block_name(name)
                )
            except FileNotFoundError:
                return False
        except Exception:
            return False
        return True

    def read(self, name: str) -> Optional[tuple[np.ndarray, int]]:
        """Return ``(copy, generation)`` for the camera's latest frame, or
        ``None`` if nothing has been written yet (or unknown camera)."""
        if name not in self._gens:
            return None
        if not self._ensure_block_by_shape(name):
            return None
        hw = self._hw[name]
        h, w = hw[0].value, hw[1].value
        if h <= 0 or w <= 0:
            return None
        handle = self._handles[name]
        try:
            frame = (
                np.frombuffer(handle.buf, dtype=np.uint8)[: h * w * 3]
                .reshape(h, w, 3)
                .copy()
            )
        except Exception:
            return None
        return frame, self._gens[name].value

    def read_view(self, name: str) -> Optional[tuple[np.ndarray, int]]:
        """Like :meth:`read` but returns a live VIEW of the shared buffer.

        Zero-copy — for the raw preview path, which only reads. Callers must
        not mutate the view and must copy before drawing. A rare torn frame
        mid-write is acceptable for a preview.
        """
        if name not in self._gens:
            return None
        if not self._ensure_block_by_shape(name):
            return None
        hw = self._hw[name]
        h, w = hw[0].value, hw[1].value
        if h <= 0 or w <= 0:
            return None
        handle = self._handles[name]
        try:
            view = (
                np.frombuffer(handle.buf, dtype=np.uint8)[: h * w * 3]
                .reshape(h, w, 3)
            )
        except Exception:
            return None
        return view, self._gens[name].value

    def unlink_all(self) -> None:
        for handle in self._handles.values():
            try:
                handle.close()
            except Exception:
                pass
        self._handles.clear()
        for name in self.camera_names:
            try:
                shm = multiprocessing.shared_memory.SharedMemory(
                    name=_block_name(name)
                )
                shm.close()
                shm.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
