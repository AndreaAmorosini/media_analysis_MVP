# Helper geometrici condivisi lungo tutta la pipeline
from __future__ import annotations

from collections.abc import Sequence
from math import isfinite

Box = Sequence[float]  # [x0, y0, x1, y1]


def valid_box(bbox: list[float] | None) -> bool:
    return (
        bbox is not None
        and len(bbox) == 4
        and all(isfinite(x) for x in bbox)
        and bbox[2] > bbox[0]
        and bbox[3] > bbox[1]
    )


def horizontal_overlap(a: Box, b: Box) -> float:
    # Frazione dell'ampiezza del piu' stretto tra a e b che si sovrappone all'altro
    intersection = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    width = min(a[2] - a[0], b[2] - b[0])
    return intersection / width if width > 0 else 0.0


def vertical_gap(a: Box, b: Box) -> float:
    # Distanza verticale tra a e b, 0 se si sovrappongono
    return max(0.0, a[1] - b[3], b[1] - a[3])


def box_union(boxes: list[Box]) -> list[float]:
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def containment_region(candidate: Box, container: Box) -> float:
    # Frazione area candidate all'interno di container
    x0, y0 = max(candidate[0], container[0]), max(candidate[1], container[1])
    x1, y1 = min(candidate[2], container[2]), min(candidate[3], container[3])
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    candidate_area = max(0.0, candidate[2] - candidate[0]) * max(
        0.0, candidate[3] - candidate[1]
    )
    return area / candidate_area if candidate_area > 0 else 0.0
