"""Find the streamer's facecam region automatically.

The configured ``stack_top`` region is a placeholder for most streamers, and
facecams sit anywhere (a corner, mid-right, full screen). This runs OpenCV's
bundled Haar frontal-face cascade (no extra weights) on a few frames, keeps the
face position seen most often, and grows it into a head-and-shoulders box.

In the app this would run on the 2 s snapshots, once every few minutes.
"""

from __future__ import annotations

import numpy as np


def detect_faces(gray: np.ndarray):
    import cv2
    casc = detect_faces._c = getattr(detect_faces, "_c", None) or cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    h = gray.shape[0]
    return casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                 minSize=(max(16, h // 20),) * 2)


def find_region(frames: list[np.ndarray]):
    """frames: grey images. Returns (x, y, w, h) fractions, or None if no stable face."""
    if not frames:
        return None
    H, W = frames[0].shape
    boxes = []
    for f in frames:
        for (x, y, w, h) in detect_faces(f):
            boxes.append((x + w / 2, y + h / 2, w, h))
    if len(boxes) < max(2, len(frames) // 5):
        return None
    b = np.array(boxes, float)
    # the most supported position: count neighbours within one face width
    support = [(np.hypot(b[:, 0] - cx, b[:, 1] - cy) < w).sum() for cx, cy, w, _ in b]
    cx, cy, w, h = b[int(np.argmax(support))]
    near = b[np.hypot(b[:, 0] - cx, b[:, 1] - cy) < w]
    cx, cy, w, h = np.median(near, axis=0)
    # head and shoulders: 3 face widths wide, from 1 face above to 2.5 below the centre
    x0, x1 = max(0, cx - 1.5 * w), min(W, cx + 1.5 * w)
    y0, y1 = max(0, cy - 1.2 * h), min(H, cy + 2.5 * h)
    return (x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H)
