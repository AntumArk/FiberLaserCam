"""Minimal, dependency-free DXF reader/writer.

Implements only the small subset of the DXF format this plugin needs:
reading LINE / LWPOLYLINE / POLYLINE+VERTEX / CIRCLE / ARC / SPLINE
entities out of the ENTITIES section (plus the $INSUNITS/$ACADVER header
variables), and writing LINE / LWPOLYLINE / POLYLINE entities back out.

This replaces the ezdxf dependency entirely. ezdxf (even through its
"pure python" fallback path) unconditionally imports numpy, which ships
platform/ABI-specific compiled wheels - bundling it reliably for every
KiCad-embedded Python build was impractical. Everything here is plain
Python with no third-party dependencies, so nothing needs to be bundled
at all.
"""
from __future__ import annotations

import io
from pathlib import Path


class Vec3(tuple):
    """A 3D point that supports both index access (p[0]) and attribute
    access (p.x), matching the parts of ezdxf's Vec3 API this plugin uses.
    """

    def __new__(cls, x: float = 0.0, y: float = 0.0, z: float = 0.0):
        return super().__new__(cls, (float(x), float(y), float(z)))

    @property
    def x(self) -> float:
        return self[0]

    @property
    def y(self) -> float:
        return self[1]

    @property
    def z(self) -> float:
        return self[2]


class DXFStructureError(Exception):
    pass


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class _DXFAttribs:
    """Simple attribute bag, mirrors ezdxf's `entity.dxf.<attr>` access."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Entity:
    def __init__(self, dxftype: str, layer: str = "0"):
        self._dxftype = dxftype
        self.layer = layer
        self.dxf = _DXFAttribs(layer=layer)

    def dxftype(self) -> str:
        return self._dxftype


class Line(Entity):
    def __init__(self, start=(0.0, 0.0, 0.0), end=(0.0, 0.0, 0.0), layer: str = "0"):
        super().__init__("LINE", layer)
        self.dxf.start = Vec3(*start[:3]) if len(start) >= 3 else Vec3(start[0], start[1])
        self.dxf.end = Vec3(*end[:3]) if len(end) >= 3 else Vec3(end[0], end[1])


class LWPolyline(Entity):
    def __init__(self, points=None, closed: bool = False, layer: str = "0"):
        super().__init__("LWPOLYLINE", layer)
        # points: list of (x, y) or (x, y, start_width, end_width, bulge)
        self.points = [tuple(p) for p in (points or [])]
        self.closed = closed

    def get_points(self):
        return self.points


class Vertex:
    def __init__(self, x: float, y: float, z: float = 0.0):
        self.dxf = _DXFAttribs(location=Vec3(x, y, z))


class Polyline(Entity):
    def __init__(self, vertices=None, is_closed: bool = False, layer: str = "0"):
        super().__init__("POLYLINE", layer)
        self.vertices = list(vertices or [])
        self.is_closed = is_closed


class Circle(Entity):
    def __init__(self, center=(0.0, 0.0, 0.0), radius: float = 0.0, layer: str = "0"):
        super().__init__("CIRCLE", layer)
        self.dxf.center = Vec3(*center[:3]) if len(center) >= 3 else Vec3(center[0], center[1])
        self.dxf.radius = float(radius)


class Arc(Entity):
    def __init__(self, center=(0.0, 0.0, 0.0), radius: float = 0.0, start_angle: float = 0.0, end_angle: float = 360.0, layer: str = "0"):
        super().__init__("ARC", layer)
        self.dxf.center = Vec3(*center[:3]) if len(center) >= 3 else Vec3(center[0], center[1])
        self.dxf.radius = float(radius)
        self.dxf.start_angle = float(start_angle)
        self.dxf.end_angle = float(end_angle)


class Spline(Entity):
    def __init__(self, layer: str = "0"):
        super().__init__("SPLINE", layer)
        self.degree = 3
        self.closed = False
        self.knots: list[float] = []
        self.control_points: list[Vec3] = []
        self.fit_points: list[Vec3] = []

    def flattening(self, distance: float = 0.02):
        """Approximate the spline as a polyline. Prefers fit points (they
        already lie on the curve); otherwise evaluates the B-spline via
        De Boor's algorithm; falls back to the control point polygon.
        """
        if len(self.fit_points) >= 2:
            return list(self.fit_points)

        if len(self.control_points) >= 2 and len(self.knots) >= 2:
            degree = max(1, self.degree)
            n = len(self.control_points) - 1
            if len(self.knots) >= n + degree + 2:
                samples = max(20, len(self.control_points) * 8)
                t0 = self.knots[degree]
                t1 = self.knots[n + 1]
                pts = []
                for i in range(samples + 1):
                    t = t0 + (t1 - t0) * (i / samples)
                    t = min(max(t, t0), t1 - 1e-9) if i == samples else t
                    pts.append(Vec3(*_bspline_point(t, degree, self.control_points, self.knots)))
                return pts

        return list(self.control_points)


def _bspline_point(t: float, degree: int, control_points, knots) -> tuple[float, float, float]:
    """Evaluate a B-spline curve at parameter t using De Boor's algorithm."""
    n = len(control_points) - 1
    span = degree
    for i in range(degree, n + 1):
        if knots[i] <= t < knots[i + 1]:
            span = i
            break
    else:
        span = n

    d = [list(control_points[span - degree + j]) for j in range(degree + 1)]
    for r in range(1, degree + 1):
        for j in range(degree, r - 1, -1):
            i = span - degree + j
            denom = knots[i + degree - r + 1] - knots[i]
            alpha = 0.0 if denom == 0 else (t - knots[i]) / denom
            d[j] = [(1 - alpha) * d[j - 1][k] + alpha * d[j][k] for k in range(3)]
    return tuple(d[degree])


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------


class Header:
    def __init__(self, values: dict | None = None):
        self._values = dict(values or {})

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def __getitem__(self, key: str):
        return self._values[key]

    def __setitem__(self, key: str, value) -> None:
        self._values[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def get(self, key: str, default=None):
        return self._values.get(key, default)


class LayerTable:
    def __init__(self):
        self._layers: dict[str, dict] = {"0": {"color": 7}}

    def __contains__(self, name: str) -> bool:
        return name in self._layers

    def new(self, name: str, dxfattribs: dict | None = None):
        self._layers[name] = dict(dxfattribs or {})
        return self._layers[name]

    def items(self):
        return self._layers.items()


class Modelspace:
    def __init__(self):
        self._entities: list[Entity] = []

    def __iter__(self):
        return iter(self._entities)

    def add_entity(self, entity: Entity) -> Entity:
        self._entities.append(entity)
        return entity

    def add_line(self, start, end, dxfattribs: dict | None = None) -> Line:
        layer = (dxfattribs or {}).get("layer", "0")
        return self.add_entity(Line(start, end, layer=layer))

    def add_lwpolyline(self, points, close: bool = False, dxfattribs: dict | None = None) -> LWPolyline:
        layer = (dxfattribs or {}).get("layer", "0")
        return self.add_entity(LWPolyline(list(points), closed=close, layer=layer))

    def add_polyline2d(self, points, close: bool = False, dxfattribs: dict | None = None) -> Polyline:
        layer = (dxfattribs or {}).get("layer", "0")
        vertices = [Vertex(p[0], p[1]) for p in points]
        return self.add_entity(Polyline(vertices, is_closed=close, layer=layer))


class Drawing:
    def __init__(self, dxfversion: str = "AC1015"):
        self.dxfversion = dxfversion
        self.header = Header({"$ACADVER": dxfversion, "$INSUNITS": 4})
        self.layers = LayerTable()
        self._modelspace = Modelspace()

    def modelspace(self) -> Modelspace:
        return self._modelspace

    def write(self, stream) -> None:
        stream.write(_serialize(self))

    def saveas(self, path) -> None:
        Path(path).write_text(_serialize(self), encoding="utf-8")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _iter_tags(text: str):
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i + 1 < n:
        code_line = lines[i].strip()
        value = lines[i + 1]
        if value.endswith("\r"):
            value = value[:-1]
        i += 2
        if not code_line:
            continue
        try:
            code = int(code_line)
        except ValueError:
            continue
        yield code, value


def _group_entities(tags):
    """Yield (entity_type, [(code, value), ...]) groups for each top-level
    code-0 record found while inside the ENTITIES section.
    """
    in_entities = False
    section_name = None
    current_type = None
    current_tags: list[tuple[int, str]] = []

    for code, value in tags:
        if code == 0:
            if current_type is not None:
                yield current_type, current_tags
                current_type = None
                current_tags = []

            if value == "SECTION":
                section_name = None
                continue
            if value == "ENDSEC":
                in_entities = False
                section_name = None
                continue
            if section_name is None and value not in ("SECTION",):
                # section name is read via code 2 right after SECTION; if we
                # get here we're either at top-level EOF or inside a section
                pass
            if in_entities:
                current_type = value
            continue

        if code == 2 and section_name is None:
            section_name = value
            in_entities = section_name == "ENTITIES"
            continue

        if current_type is not None:
            current_tags.append((code, value))

    if current_type is not None:
        yield current_type, current_tags


def _read_header(text: str) -> dict:
    values: dict = {}
    tags = list(_iter_tags(text))
    in_header = False
    section_name = None
    pending_var = None
    for code, value in tags:
        if code == 0 and value == "ENDSEC":
            in_header = False
            continue
        if code == 2 and section_name is None:
            section_name = value
            in_header = section_name == "HEADER"
            continue
        if code == 0:
            section_name = None
            continue
        if not in_header:
            continue
        if code == 9:
            pending_var = value
            continue
        if pending_var is not None:
            if code in (70, 90):
                try:
                    values[pending_var] = int(value)
                except ValueError:
                    values[pending_var] = value
            elif code in (40, 41, 42):
                try:
                    values[pending_var] = float(value)
                except ValueError:
                    values[pending_var] = value
            else:
                values[pending_var] = value
            pending_var = None
    return values


def _entity_from_tags(dxftype: str, tags: list[tuple[int, str]]):
    layer = "0"
    for code, value in tags:
        if code == 8:
            layer = value
            break

    if dxftype == "LINE":
        start = [0.0, 0.0, 0.0]
        end = [0.0, 0.0, 0.0]
        for code, value in tags:
            if code == 10:
                start[0] = float(value)
            elif code == 20:
                start[1] = float(value)
            elif code == 30:
                start[2] = float(value)
            elif code == 11:
                end[0] = float(value)
            elif code == 21:
                end[1] = float(value)
            elif code == 31:
                end[2] = float(value)
        return Line(start, end, layer=layer)

    if dxftype == "LWPOLYLINE":
        points: list[list[float]] = []
        closed = False
        cur: list[float] | None = None
        for code, value in tags:
            if code == 70:
                closed = bool(int(float(value)) & 1)
            elif code == 10:
                if cur is not None:
                    points.append(cur)
                cur = [float(value), 0.0]
            elif code == 20:
                if cur is not None:
                    cur[1] = float(value)
            elif code == 42 and cur is not None:
                # bulge value, kept for completeness
                if len(cur) == 2:
                    cur.append(0.0)
                    cur.append(0.0)
                    cur.append(float(value))
        if cur is not None:
            points.append(cur)
        entity = LWPolyline([tuple(p) for p in points], closed=closed, layer=layer)
        return entity

    if dxftype == "CIRCLE":
        center = [0.0, 0.0, 0.0]
        radius = 0.0
        for code, value in tags:
            if code == 10:
                center[0] = float(value)
            elif code == 20:
                center[1] = float(value)
            elif code == 30:
                center[2] = float(value)
            elif code == 40:
                radius = float(value)
        return Circle(center, radius, layer=layer)

    if dxftype == "ARC":
        center = [0.0, 0.0, 0.0]
        radius = 0.0
        start_angle = 0.0
        end_angle = 360.0
        for code, value in tags:
            if code == 10:
                center[0] = float(value)
            elif code == 20:
                center[1] = float(value)
            elif code == 30:
                center[2] = float(value)
            elif code == 40:
                radius = float(value)
            elif code == 50:
                start_angle = float(value)
            elif code == 51:
                end_angle = float(value)
        return Arc(center, radius, start_angle, end_angle, layer=layer)

    if dxftype == "SPLINE":
        entity = Spline(layer=layer)
        flags = 0
        cur_cp: list[float] | None = None
        cur_fp: list[float] | None = None
        for code, value in tags:
            if code == 70:
                flags = int(float(value))
            elif code == 71:
                entity.degree = int(float(value))
            elif code == 40:
                entity.knots.append(float(value))
            elif code == 10:
                if cur_cp is not None:
                    entity.control_points.append(Vec3(*cur_cp))
                cur_cp = [float(value), 0.0, 0.0]
            elif code == 20 and cur_cp is not None:
                cur_cp[1] = float(value)
            elif code == 30 and cur_cp is not None:
                cur_cp[2] = float(value)
            elif code == 11:
                if cur_fp is not None:
                    entity.fit_points.append(Vec3(*cur_fp))
                cur_fp = [float(value), 0.0, 0.0]
            elif code == 21 and cur_fp is not None:
                cur_fp[1] = float(value)
            elif code == 31 and cur_fp is not None:
                cur_fp[2] = float(value)
        if cur_cp is not None:
            entity.control_points.append(Vec3(*cur_cp))
        if cur_fp is not None:
            entity.fit_points.append(Vec3(*cur_fp))
        entity.closed = bool(flags & 1)
        return entity

    return None


def _read_entities(text: str) -> list[Entity]:
    entities: list[Entity] = []
    pending_polyline: Polyline | None = None

    for dxftype, tags in _group_entities(_iter_tags(text)):
        if dxftype == "POLYLINE":
            layer = "0"
            closed = False
            for code, value in tags:
                if code == 8:
                    layer = value
                elif code == 70:
                    closed = bool(int(float(value)) & 1)
            pending_polyline = Polyline([], is_closed=closed, layer=layer)
            entities.append(pending_polyline)
            continue

        if dxftype == "VERTEX" and pending_polyline is not None:
            x = y = z = 0.0
            for code, value in tags:
                if code == 10:
                    x = float(value)
                elif code == 20:
                    y = float(value)
                elif code == 30:
                    z = float(value)
            pending_polyline.vertices.append(Vertex(x, y, z))
            continue

        if dxftype == "SEQEND":
            pending_polyline = None
            continue

        entity = _entity_from_tags(dxftype, tags)
        if entity is not None:
            entities.append(entity)

    return entities


def readfile(path) -> Drawing:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    header_values = _read_header(text)

    dxfversion = header_values.get("$ACADVER", "AC1015")
    doc = Drawing(dxfversion=dxfversion)
    doc.header = Header(header_values)

    for entity in _read_entities(text):
        doc.modelspace().add_entity(entity)

    return doc


def new(dxfversion: str = "R2000") -> Drawing:
    version_map = {"R12": "AC1009", "R2000": "AC1015", "R2010": "AC1024"}
    return Drawing(dxfversion=version_map.get(dxfversion, dxfversion))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _fmt(value: float) -> str:
    return f"{float(value):.6f}"


def _serialize(doc: Drawing) -> str:
    out = io.StringIO()

    def tag(code: int, value) -> None:
        out.write(f"{code}\n{value}\n")

    # HEADER
    tag(0, "SECTION")
    tag(2, "HEADER")
    tag(9, "$ACADVER")
    tag(1, doc.header.get("$ACADVER", doc.dxfversion))
    if "$INSUNITS" in doc.header:
        tag(9, "$INSUNITS")
        tag(70, int(doc.header["$INSUNITS"]))
    tag(0, "ENDSEC")

    # TABLES (LAYER table only)
    tag(0, "SECTION")
    tag(2, "TABLES")
    tag(0, "TABLE")
    tag(2, "LAYER")
    tag(70, len(list(doc.layers.items())))
    for name, attribs in doc.layers.items():
        tag(0, "LAYER")
        tag(2, name)
        tag(70, 0)
        tag(62, int(attribs.get("color", 7)))
        tag(6, attribs.get("linetype", "CONTINUOUS"))
    tag(0, "ENDTAB")
    tag(0, "ENDSEC")

    # ENTITIES
    tag(0, "SECTION")
    tag(2, "ENTITIES")
    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind == "LINE":
            tag(0, "LINE")
            tag(8, entity.layer)
            tag(10, _fmt(entity.dxf.start.x))
            tag(20, _fmt(entity.dxf.start.y))
            tag(30, _fmt(entity.dxf.start.z))
            tag(11, _fmt(entity.dxf.end.x))
            tag(21, _fmt(entity.dxf.end.y))
            tag(31, _fmt(entity.dxf.end.z))
        elif kind == "LWPOLYLINE":
            pts = entity.get_points()
            tag(0, "LWPOLYLINE")
            tag(8, entity.layer)
            tag(90, len(pts))
            tag(70, 1 if entity.closed else 0)
            for p in pts:
                tag(10, _fmt(p[0]))
                tag(20, _fmt(p[1]))
        elif kind == "POLYLINE":
            tag(0, "POLYLINE")
            tag(8, entity.layer)
            tag(66, 1)
            tag(70, 1 if entity.is_closed else 0)
            for v in entity.vertices:
                tag(0, "VERTEX")
                tag(8, entity.layer)
                tag(10, _fmt(v.dxf.location.x))
                tag(20, _fmt(v.dxf.location.y))
                tag(30, _fmt(v.dxf.location.z))
            tag(0, "SEQEND")
    tag(0, "ENDSEC")

    tag(0, "EOF")
    return out.getvalue()
