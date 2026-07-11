from __future__ import annotations

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.polygon import orient


def _iter_polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [p for p in geom.geoms if not p.is_empty and p.area > 1e-9]
    if isinstance(geom, GeometryCollection):
        polys: list[Polygon] = []
        for inner in geom.geoms:
            polys.extend(_iter_polygons(inner))
        return polys
    return []


def _normalize_polygonal(geom):
    polys = _iter_polygons(geom)
    if not polys:
        return None
    if len(polys) == 1:
        return polys[0]
    merged = polys[0]
    for poly in polys[1:]:
        merged = merged.union(poly)
    if isinstance(merged, (Polygon, MultiPolygon)) and not merged.is_empty:
        return merged
    merged_polys = _iter_polygons(merged)
    if not merged_polys:
        return None
    if len(merged_polys) == 1:
        return merged_polys[0]
    rebuilt = merged_polys[0]
    for poly in merged_polys[1:]:
        rebuilt = rebuilt.union(poly)
    if isinstance(rebuilt, (Polygon, MultiPolygon)) and not rebuilt.is_empty:
        return rebuilt
    return None


def _ring_segments(coords, reverse: bool = False) -> list[list[list[float]]]:
    points = list(coords)
    if len(points) < 2:
        return []
    if reverse:
        points = list(reversed(points))
    segments: list[list[list[float]]] = []
    for idx in range(len(points) - 1):
        p1 = [float(points[idx][0]), float(points[idx][1])]
        p2 = [float(points[idx + 1][0]), float(points[idx + 1][1])]
        segments.append([p1, p2])
    return segments


def _ring_points(coords, reverse: bool = False) -> list[tuple[float, float]]:
    points = [(float(p[0]), float(p[1])) for p in list(coords)]
    if len(points) < 3:
        return []
    if reverse:
        points = list(reversed(points))
    # Remove duplicated closing vertex for CAD entities that close implicitly.
    if points[0] == points[-1]:
        points = points[:-1]
    return points


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
    if geom is None or geom.is_empty:
        return []
    if start_offset < 0 or spacing < 0 or repetitions <= 0:
        return []

    loops: list[list[tuple[float, float]]] = []
    for step in range(repetitions):
        offset_distance = start_offset + (step * spacing)
        try:
            # Default behavior expands from contour. Inverted mode contracts
            # toward interior regions (useful for drill-hole processing).
            signed_distance = -offset_distance if invert_direction else offset_distance
            offset_geom = geom.buffer(signed_distance) if signed_distance != 0 else geom
        except Exception:
            break

        offset_geom = _normalize_polygonal(offset_geom)
        if offset_geom is None or offset_geom.is_empty:
            break

        for poly in _iter_polygons(offset_geom):
            oriented = orient(poly, sign=1.0)
            outer = _ring_points(oriented.exterior.coords, reverse=False)
            if len(outer) >= 3:
                loops.append(outer)
            for interior in oriented.interiors:
                inner = _ring_points(interior.coords, reverse=True)
                if len(inner) >= 3:
                    loops.append(inner)

    return loops
