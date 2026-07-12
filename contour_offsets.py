from __future__ import annotations

import math


def _to_ring(geom) -> list[tuple[float, float]]:
    if geom is None:
        return []

    if isinstance(geom, list) and geom and isinstance(geom[0], (list, tuple)):
        points = [(float(p[0]), float(p[1])) for p in geom]
    elif hasattr(geom, "exterior") and hasattr(geom.exterior, "coords"):
        points = [(float(p[0]), float(p[1])) for p in list(geom.exterior.coords)]
    else:
        return []

    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        return []
    return points


def _signed_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += (x1 * y2) - (x2 * y1)
    return 0.5 * area


def _ensure_ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) < 3:
        return []
    if abs(_signed_area(points)) <= 1e-9:
        return []
    return points


def _line_intersection(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> tuple[float, float] | None:
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-12:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / den
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / den
    return (float(px), float(py))


def _offset_ring(points: list[tuple[float, float]], distance: float) -> list[tuple[float, float]]:
    points = _ensure_ring(points)
    if not points:
        return []
    if abs(distance) <= 1e-12:
        return points

    n = len(points)
    ccw = _signed_area(points) > 0
    lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    normals: list[tuple[float, float]] = []

    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            return []
        ux, uy = dx / length, dy / length
        if ccw:
            nx, ny = uy, -ux
        else:
            nx, ny = -uy, ux
        normals.append((nx, ny))
        q1 = (x1 + (nx * distance), y1 + (ny * distance))
        q2 = (x2 + (nx * distance), y2 + (ny * distance))
        lines.append((q1, q2))

    out: list[tuple[float, float]] = []
    for i in range(n):
        prev_line = lines[(i - 1) % n]
        curr_line = lines[i]
        inter = _line_intersection(prev_line[0], prev_line[1], curr_line[0], curr_line[1])
        if inter is None:
            x, y = points[i]
            n1x, n1y = normals[(i - 1) % n]
            n2x, n2y = normals[i]
            ax = (n1x + n2x) * 0.5
            ay = (n1y + n2y) * 0.5
            norm = math.hypot(ax, ay)
            if norm <= 1e-12:
                ax, ay = normals[i]
            else:
                ax, ay = ax / norm, ay / norm
            inter = (x + (ax * distance), y + (ay * distance))
        out.append((float(inter[0]), float(inter[1])))

    return _ensure_ring(out)


def generate_contour_offset_segments(
    geom,
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_direction: bool = False,
) -> list[list[list[float]]]:
    loops = generate_contour_offset_loops(geom, start_offset, spacing, repetitions, invert_direction=invert_direction)
    segments: list[list[list[float]]] = []
    for loop in loops:
        if len(loop) < 2:
            continue
        for idx in range(len(loop)):
            p1 = [float(loop[idx][0]), float(loop[idx][1])]
            p2 = [float(loop[(idx + 1) % len(loop)][0]), float(loop[(idx + 1) % len(loop)][1])]
            segments.append([p1, p2])
    return segments


def generate_contour_offset_loops(
    geom,
    start_offset: float,
    spacing: float,
    repetitions: int,
    invert_direction: bool = False,
) -> list[list[tuple[float, float]]]:
    ring = _to_ring(geom)
    if not ring:
        return []
    if start_offset < 0 or spacing < 0 or repetitions <= 0:
        return []

    loops: list[list[tuple[float, float]]] = []
    for step in range(repetitions):
        offset_distance = start_offset + (step * spacing)
        signed_distance = -offset_distance if invert_direction else offset_distance
        offset_ring = _offset_ring(ring, signed_distance)
        if not offset_ring:
            break
        loops.append(offset_ring)

    return loops
