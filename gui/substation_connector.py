"""
substation_connector.py
=======================

Connect a waterbody polygon to the nearest power substation from
``open_infrastructure_map.gpkg``.

Layered design (each concern lives in its own section):

    1. Spatial querying — find the substation nearest to the waterbody
       boundary (straight-line / Euclidean).
    2. Path generation  — produce a LineString from the waterbody edge to
       that substation.
            * If a DEM raster is available, use A* over a rasterised cost
              surface that penalises steep slopes.
            * Otherwise, emit a direct straight-line.
    3. Layer creation   — wrap the results in styled QgsVectorLayers so the
       caller can drop them straight onto the map canvas.

Everything is computed in EPSG:3857 (meters), which is the canvas CRS
used by ``gui.py``. Layers with a different CRS are reprojected on the fly.
"""
from __future__ import annotations

import heapq
import math
import os

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsRaster,
    QgsRasterLayer,          # NEW
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

#: Filename we look for in the app's ``metadata_polygon_entries`` list.
SUBSTATION_GPKG_NAME = "open_infrastructure_map.gpkg"

#: Filename fragments that identify a raster as a DEM / elevation model.
DEM_NAME_HINTS = ("dem", "elevation", "srtm", "aster", "ned")

#: Fields whose value may tag a feature as a substation.
_SUBSTATION_TAG_FIELDS = ("TYPE", "type", "power", "POWER", "tags", "fclass")

#: Fields we consult to label the chosen substation in the output layer.
_SUBSTATION_NAME_FIELDS = ("NAME", "name", "ref", "REF", "operator", "OPERATOR")

#: Default grid cell size for the A* cost surface, in meters.
DEFAULT_CELL_SIZE_M = 30.0

#: Hard ceiling on grid size so A* stays fast.  Cells auto-enlarge if
#: the start-to-end bounding box would exceed this.  Lowered from 90 000
#: to 10 000 because ``QgsRasterDataProvider.identify()`` is ~0.3 ms per
#: call on macOS — at 90 000 cells the main thread stalls for ~30 s and
#: the OS may force-quit QGIS.
MAX_GRID_CELLS = 200 * 200 

#: Upper bound on how many substation candidates we consider.  Global
#: OpenInfraMap extracts can have millions of features; without this cap
#: the spatial-index build phase exhausts memory on large datasets.
MAX_SUBSTATION_CANDIDATES = 50_000

#: Slope cost weight. ``cost_multiplier = 1 + SLOPE_PENALTY * |slope|``,
#: where slope is rise/run between adjacent cells.
SLOPE_PENALTY = 30.0

#: Padding added around the start/end bounding box so A* can route around
#: obstacles rather than getting boxed in by the edge of the grid.
GRID_PADDING_FRAC = 0.15

def _crs_3857():
    """Return a fresh EPSG:3857 CRS.

    Construct on demand rather than at module load time: this module is
    imported before ``QgsApplication`` / ``initQgis()`` run, and creating
    a CRS before QGIS is initialised silently corrupts QGIS's internal
    CRS cache (making every later EPSG:3857 lookup return a broken
    projection). Creating it inside functions — exactly as ``gui.py``
    does elsewhere — avoids that trap entirely.
    """
    return QgsCoordinateReferenceSystem("EPSG:3857")


# ===========================================================================
# 1) SPATIAL QUERYING
# ===========================================================================

def find_substations_layer(metadata_polygon_entries):
    """Return the ``QgsVectorLayer`` backing ``open_infrastructure_map.gpkg``,
    or ``None`` if it is not loaded."""
    target = SUBSTATION_GPKG_NAME.lower()
    for entry in metadata_polygon_entries:
        filepath = entry.get("filepath", "")
        if os.path.basename(filepath).lower() == target:
            layer = entry.get("source_layer")
            if layer is not None and layer.isValid():
                return layer
    return None


def find_dem_layer(metadata_polygon_entries):
    """Return the first loaded layer whose filename looks like a DEM /
    elevation model. Accepts both raster layers (GeoTIFF, tiled-coverage
    GPKG) and vector layers (contour lines or spot-elevation points);
    ``_sample_dem_grid`` dispatches on the concrete type."""
    for entry in metadata_polygon_entries:
        filepath = entry.get("filepath", "")
        name = os.path.basename(filepath).lower()
        if not any(hint in name for hint in DEM_NAME_HINTS):
            continue
        layer = entry.get("source_layer")
        if layer is not None and layer.isValid():
            return layer
    return None


def _is_substation(feature):
    """Best-effort check that a feature represents a substation.

    OpenInfraMap data typically exposes a ``TYPE`` or ``power`` tag.  If
    *no* recognisable tag column exists on the layer we assume the layer
    is already filtered to substations and accept everything.
    """
    tag_field_seen = False
    for field_name in _SUBSTATION_TAG_FIELDS:
        idx = feature.fields().indexFromName(field_name)
        if idx == -1:
            continue
        tag_field_seen = True
        value = feature[idx]
        if value is None:
            continue
        if "substation" in str(value).lower():
            return True
    return not tag_field_seen


def _reproject(geom, src_crs, dst_crs):
    """Return a copy of ``geom`` reprojected from ``src_crs`` to ``dst_crs``."""
    out = QgsGeometry(geom)
    if src_crs != dst_crs:
        xform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        out.transform(xform)
    return out


def find_nearest_substation(waterbody_geom_3857, substations_layer,
                            candidate_k=10, prefilter_radius_m=200_000,
                            verbose=True):
    """Find the substation nearest to the waterbody boundary.

    Uses a multi-stage approach for performance on large datasets:
        1. A bounding-box prefilter around the waterbody (``prefilter_radius_m``).
        2. A hard cap on candidate count (:data:`MAX_SUBSTATION_CANDIDATES`).
        3. A ``QgsSpatialIndex`` on those candidates for k-nearest lookup.
        4. A true polygon-boundary distance check against the top ``k``.

    Every risky call is wrapped in try/except — one malformed feature in a
    huge dataset must not take the whole app down.

    Returns a dict (keys: ``feature``, ``substation_point_3857``,
    ``closest_boundary_point_3857``, ``straight_line_distance_m``) or
    ``None`` if no substation is found.
    """
    def log(msg):
        if verbose:
            print(f"[substation_connector] {msg}")

    if waterbody_geom_3857 is None or waterbody_geom_3857.isEmpty():
        log("waterbody geometry is empty; aborting")
        return None

    src_crs = substations_layer.crs()
    crs_3857 = _crs_3857()

    # Build a 200 km bounding box around the waterbody in the layer CRS.
    wb_bbox_3857 = waterbody_geom_3857.boundingBox()
    if wb_bbox_3857.isEmpty():
        log("waterbody bounding box is empty; aborting (would otherwise "
            "fall back to a full-layer scan at the world origin)")
        return None

    search_bbox_3857 = QgsRectangle(
        wb_bbox_3857.xMinimum() - prefilter_radius_m,
        wb_bbox_3857.yMinimum() - prefilter_radius_m,
        wb_bbox_3857.xMaximum() + prefilter_radius_m,
        wb_bbox_3857.yMaximum() + prefilter_radius_m,
    )
    try:
        if src_crs != crs_3857:
            xform = QgsCoordinateTransform(crs_3857, src_crs, QgsProject.instance())
            search_bbox_layer = xform.transformBoundingBox(search_bbox_3857)
        else:
            search_bbox_layer = search_bbox_3857
    except Exception as exc:
        log(f"failed to reproject search bbox: {exc}; aborting")
        return None

    log(f"layer '{substations_layer.name()}' CRS={src_crs.authid()}, "
        f"feature count={substations_layer.featureCount()}")
    log(f"prefilter bbox (layer CRS): {search_bbox_layer.toString(2)}")

    request = QgsFeatureRequest().setFilterRect(search_bbox_layer)

    # Collect candidate points (reprojected to 3857) and index them.
    # We use sequential proxy IDs so duplicate feature IDs in the gpkg
    # cannot corrupt the spatial index.
    index = QgsSpatialIndex()
    id_to_data = {}
    proxy_id = 0
    skipped = 0
    for feat in substations_layer.getFeatures(request):
        if proxy_id >= MAX_SUBSTATION_CANDIDATES:
            log(f"hit MAX_SUBSTATION_CANDIDATES ({MAX_SUBSTATION_CANDIDATES}); "
                "stopping candidate collection")
            break
        try:
            if not _is_substation(feat):
                continue
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                skipped += 1
                continue
            geom_3857 = _reproject(geom, src_crs, crs_3857)
            if geom_3857.isEmpty():
                skipped += 1
                continue
            pt = geom_3857.centroid().asPoint()
            proxy = QgsFeature(proxy_id)
            proxy.setGeometry(QgsGeometry.fromPointXY(pt))
            index.insertFeature(proxy)
            # QgsFeature(feat) makes a deep copy so references stay valid.
            id_to_data[proxy_id] = (QgsFeature(feat), pt)
            proxy_id += 1
        except Exception as exc:
            skipped += 1
            log(f"skipped malformed feature: {exc}")
            continue

    log(f"indexed {proxy_id} candidate(s), skipped {skipped}")

    if not id_to_data:
        log("no candidates found within prefilter bbox; aborting")
        return None

    # k-NN probe from the waterbody centroid.
    probe = waterbody_geom_3857.centroid().asPoint()
    k = min(candidate_k, len(id_to_data))
    try:
        nearest_ids = index.nearestNeighbor(probe, k)
    except Exception as exc:
        log(f"nearestNeighbor failed: {exc}; aborting")
        return None

    # Closest boundary point math.
    # Note: we deliberately do NOT call `waterbody_geom_3857.boundary()`
    # here.  That method isn't exposed on `QgsGeometry` in every QGIS
    # build (it was added later and some bindings still lack it).
    # Luckily we don't need it — `QgsGeometry.nearestPoint(target)` on a
    # polygon returns the closest point on the polygon's boundary when
    # the target is outside the polygon, which is our case.  If the
    # target is inside, `nearestPoint` returns the target itself
    # (distance = 0) — a reasonable sentinel meaning "substation already
    # sits inside the waterbody".
    boundary = waterbody_geom_3857

    best = None  # (distance_m, feature, substation_pt, closest_boundary_pt)
    for fid in nearest_ids:
        if fid not in id_to_data:
            continue
        feat, sub_pt = id_to_data[fid]
        try:
            sub_geom = QgsGeometry.fromPointXY(sub_pt)
            closest_on_edge = boundary.nearestPoint(sub_geom)
            if closest_on_edge.isEmpty():
                continue
            dist = sub_geom.distance(closest_on_edge)
        except Exception as exc:
            log(f"nearest-point calc failed for candidate {fid}: {exc}")
            continue
        if best is None or dist < best[0]:
            best = (dist, feat, sub_pt, closest_on_edge.asPoint())

    if best is None:
        log("no candidate survived boundary-distance check; aborting")
        return None

    dist, feat, sub_pt, edge_pt = best
    log(f"chosen substation {dist/1000:.2f} km from waterbody edge")
    return {
        "feature": feat,
        "substation_point_3857": sub_pt,
        "closest_boundary_point_3857": edge_pt,
        "straight_line_distance_m": dist,
    }


# ===========================================================================
# 2) PATH GENERATION
# ===========================================================================

def straight_line_path(start_3857, end_3857):
    """Direct LineString between two EPSG:3857 points."""
    return QgsGeometry.fromPolylineXY([start_3857, end_3857])

# --- Vector DEM support ----------------------------------------------------
#
# When the elevation source is a QgsVectorLayer (contour lines or
# spot-elevation points) we can't call provider.identify(). Instead we
# build a QgsSpatialIndex once per layer, then for each grid cell do an
# inverse-distance-weighted sample of the k nearest features.  Works
# uniformly for points (Euclidean distance) and lines (point-to-line
# distance, which is what QgsGeometry.distance returns for LineStrings).

#: Exact field names we check first when hunting for the elevation column.
_ELEVATION_FIELD_EXACT = (
    "elevation", "ELEVATION", "elev", "ELEV",
    "height", "HEIGHT", "altitude", "ALTITUDE",
    "z", "Z", "contour", "CONTOUR", "ele", "ELE",
)
#: Substring hints used as a fallback across any numeric field.
_ELEVATION_FIELD_HINTS = ("elev", "height", "altitude", "contour", "ele")

#: Per-layer cache so repeated routings don't rebuild the spatial index.
_VECTOR_DEM_SAMPLER_CACHE = {}


def _find_elevation_field(layer):
    """Return the field index of the numeric elevation attribute, or -1."""
    fields = layer.fields()
    for name in _ELEVATION_FIELD_EXACT:
        idx = fields.indexFromName(name)
        if idx != -1 and fields.at(idx).isNumeric():
            return idx
    for i, field in enumerate(fields):
        if not field.isNumeric():
            continue
        lname = field.name().lower()
        if any(hint in lname for hint in _ELEVATION_FIELD_HINTS):
            return i
    return -1


def _build_vector_elevation_sampler(vector_layer, verbose=True):
    """Return a ``(x_3857, y_3857) -> elevation_or_None`` callable.

    Performs IDW over the k geometrically-nearest features.  We ask the
    spatial index for a wider pool (``candidates``) than we actually use
    (``k``) and then rescore by true geometric distance, because
    ``QgsSpatialIndex.nearestNeighbor`` ranks by bounding-box centroid —
    fine for points, but unreliable for long meandering contour lines
    whose bbox centroid may sit far from the segment closest to the
    query point.
    """
    def log(msg):
        if verbose:
            print(f"[substation_connector] {msg}")

    layer_key = vector_layer.id()
    if layer_key in _VECTOR_DEM_SAMPLER_CACHE:
        return _VECTOR_DEM_SAMPLER_CACHE[layer_key]

    elev_idx = _find_elevation_field(vector_layer)
    if elev_idx == -1:
        log(f"vector DEM {vector_layer.name()!r} has no numeric elevation "
            f"field; cannot sample")
        return None

    index = QgsSpatialIndex()
    feature_elev = {}
    feature_geom = {}
    for feat in vector_layer.getFeatures():
        val = feat[elev_idx]
        if val is None:
            continue
        try:
            z = float(val)
        except (TypeError, ValueError):
            continue
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        index.insertFeature(feat)
        feature_elev[feat.id()] = z
        # Keep a detached copy: iterator-owned geometries get invalidated
        # once the feature iterator advances past them.
        feature_geom[feat.id()] = QgsGeometry(geom)

    if not feature_elev:
        log("vector DEM contained no usable elevation features")
        return None

    field_name = vector_layer.fields().at(elev_idx).name()
    log(f"built vector DEM sampler: {len(feature_elev)} features, "
        f"elevation field '{field_name}'")

    crs_3857 = _crs_3857()
    layer_crs = vector_layer.crs()
    need_reproj = layer_crs != crs_3857
    fwd_xform = (QgsCoordinateTransform(crs_3857, layer_crs, QgsProject.instance())
                 if need_reproj else None)

    def sample(x_3857, y_3857, k=4, candidates=16):
        try:
            pt = QgsPointXY(x_3857, y_3857)
            if fwd_xform is not None:
                pt = fwd_xform.transform(pt)
            nearest_ids = index.nearestNeighbor(pt, candidates)
            if not nearest_ids:
                return None
            sample_geom = QgsGeometry.fromPointXY(pt)
            scored = []
            for fid in nearest_ids:
                z = feature_elev.get(fid)
                g = feature_geom.get(fid)
                if z is None or g is None:
                    continue
                d = g.distance(sample_geom)
                scored.append((d, z))
            if not scored:
                return None
            scored.sort(key=lambda pair: pair[0])
            wsum = 0.0
            wtot = 0.0
            for d, z in scored[:k]:
                if d <= 1e-6:
                    return z          # sample lies on the feature
                w = 1.0 / (d * d)
                wsum += z * w
                wtot += w
            return (wsum / wtot) if wtot > 0.0 else None
        except Exception:
            return None

    _VECTOR_DEM_SAMPLER_CACHE[layer_key] = sample
    return sample

# --- DEM sampling ---------------------------------------------------------

def _sample_dem_grid(dem_layer, bbox_3857, cell_size_m, verbose=True):
    """Pre-sample the DEM at every grid-cell centre in ``bbox_3857``.

    Works with ``QgsRasterLayer`` (GeoTIFF or tiled-coverage GPKG) and
    with ``QgsVectorLayer`` (contour lines or spot-elevation points with
    an elevation attribute).  Returns a dict describing the grid, or
    ``None`` if the grid would exceed :data:`MAX_GRID_CELLS` or the DEM
    doesn't cover the requested area.  ``elevs`` is a flat row-major
    list with row 0 at the TOP of the bbox; missing values are ``None``
    and the A* cost function handles them gracefully.
    """
    def log(msg):
        if verbose:
            print(f"[substation_connector] {msg}")

    width = max(2, int(math.ceil(bbox_3857.width() / cell_size_m)))
    height = max(2, int(math.ceil(bbox_3857.height() / cell_size_m)))
    if width * height > MAX_GRID_CELLS:
        log(f"grid {width}x{height}={width*height} cells exceeds "
            f"MAX_GRID_CELLS={MAX_GRID_CELLS}; refusing cost-based path")
        return None

    # Confirm the DEM covers the area before doing thousands of probes.
    crs_3857 = _crs_3857()
    dem_crs = dem_layer.crs()
    try:
        if dem_crs != crs_3857:
            xform_bbox = QgsCoordinateTransform(crs_3857, dem_crs, QgsProject.instance())
            bbox_dem_crs = xform_bbox.transformBoundingBox(bbox_3857)
        else:
            bbox_dem_crs = bbox_3857
        if not dem_layer.extent().intersects(bbox_dem_crs):
            log(f"DEM extent {dem_layer.extent().toString(1)} does not cover "
                f"path bbox {bbox_dem_crs.toString(1)}; skipping cost-based path")
            return None
    except Exception as exc:
        log(f"DEM extent check failed: {exc}; skipping cost-based path")
        return None

    log(f"sampling DEM on {width}x{height} grid ({width*height} cells)")

    origin_x = bbox_3857.xMinimum()
    origin_y = bbox_3857.yMaximum()  # row 0 is at top

    # Choose a point-sampler suited to the layer type.  Both variants
    # take (x_3857, y_3857) and return a float or None so the grid-fill
    # loop below doesn't care which source produced the value.
    if isinstance(dem_layer, QgsRasterLayer):
        provider = dem_layer.dataProvider()
        need_reproj = dem_crs != crs_3857
        xform = (QgsCoordinateTransform(crs_3857, dem_crs, QgsProject.instance())
                 if need_reproj else None)

        def probe(x, y):
            try:
                pt = QgsPointXY(x, y)
                if xform is not None:
                    pt = xform.transform(pt)
                result = provider.identify(pt, QgsRaster.IdentifyFormatValue)
                if not result.isValid():
                    return None
                values = result.results()
                if not values:
                    return None
                val = next(iter(values.values()))
                return None if val is None else float(val)
            except (TypeError, ValueError, Exception):
                # One bad cell must not abort the whole sampling pass.
                return None
    else:
        probe = _build_vector_elevation_sampler(dem_layer, verbose=verbose)
        if probe is None:
            return None

    elevs = [None] * (width * height)
    for row in range(height):
        y = origin_y - (row + 0.5) * cell_size_m
        for col in range(width):
            x = origin_x + (col + 0.5) * cell_size_m
            elevs[row * width + col] = probe(x, y)

    return {
        "width": width,
        "height": height,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "cell": cell_size_m,
        "elevs": elevs,
    }


def _world_to_grid(grid, x, y):
    col = int((x - grid["origin_x"]) / grid["cell"])
    row = int((grid["origin_y"] - y) / grid["cell"])
    col = max(0, min(grid["width"] - 1, col))
    row = max(0, min(grid["height"] - 1, row))
    return row, col


def _grid_to_world(grid, row, col):
    x = grid["origin_x"] + (col + 0.5) * grid["cell"]
    y = grid["origin_y"] - (row + 0.5) * grid["cell"]
    return QgsPointXY(x, y)


# --- A* --------------------------------------------------------------------

_NEIGHBOURS = (
    (-1,  0, 1.0),         ( 1,  0, 1.0),
    ( 0, -1, 1.0),         ( 0,  1, 1.0),
    (-1, -1, math.sqrt(2)), (-1,  1, math.sqrt(2)),
    ( 1, -1, math.sqrt(2)), ( 1,  1, math.sqrt(2)),
)


def _astar(grid, start_rc, goal_rc, slope_penalty=SLOPE_PENALTY):
    """Classic A* on an 8-connected grid.

    Cost to move from cell A to cell B::

        horizontal_distance(A, B) * (1 + slope_penalty * |slope_AB|)

    slope = |Δelev| / horizontal_distance, so the multiplier is 1.0 for
    flat ground and grows as the terrain steepens.  The straight-line
    Euclidean distance is an admissible heuristic because the cost
    multiplier is always ≥ 1.0 and the heuristic uses no multiplier.
    """
    width, height = grid["width"], grid["height"]
    elevs = grid["elevs"]
    cell = grid["cell"]

    def heuristic(rc):
        return math.hypot(rc[0] - goal_rc[0], rc[1] - goal_rc[1]) * cell

    open_heap = [(heuristic(start_rc), 0.0, start_rc)]
    g_score = {start_rc: 0.0}
    came_from = {}

    while open_heap:
        _, g, current = heapq.heappop(open_heap)
        if current == goal_rc:
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path
        if g > g_score.get(current, math.inf):
            continue  # stale entry
        r, c = current
        elev_a = elevs[r * width + c]
        for dr, dc, step in _NEIGHBOURS:
            nr, nc = r + dr, c + dc
            if nr < 0 or nr >= height or nc < 0 or nc >= width:
                continue
            horizontal = step * cell
            elev_b = elevs[nr * width + nc]
            if elev_a is None or elev_b is None:
                # Missing DEM data: apply a mild penalty so paths still work.
                multiplier = 1.0 + slope_penalty * 0.25
            else:
                slope = abs(elev_b - elev_a) / horizontal
                multiplier = 1.0 + slope_penalty * slope
            tentative = g + horizontal * multiplier
            neigh = (nr, nc)
            if tentative < g_score.get(neigh, math.inf):
                g_score[neigh] = tentative
                came_from[neigh] = current
                heapq.heappush(
                    open_heap,
                    (tentative + heuristic(neigh), tentative, neigh),
                )

    return None


def cost_based_path(start_3857, end_3857, dem_layer,
                    cell_size_m=DEFAULT_CELL_SIZE_M,
                    slope_penalty=SLOPE_PENALTY):
    """Slope-penalised shortest path from ``start`` to ``end``.

    Returns a ``QgsGeometry`` (LineString) or ``None`` if the grid would
    be too large / the DEM yields no valid path.
    """
    def log(msg):
        print(f"[substation_connector] {msg}")

    xmin = min(start_3857.x(), end_3857.x())
    xmax = max(start_3857.x(), end_3857.x())
    ymin = min(start_3857.y(), end_3857.y())
    ymax = max(start_3857.y(), end_3857.y())
    pad_x = max((xmax - xmin) * GRID_PADDING_FRAC, cell_size_m * 3)
    pad_y = max((ymax - ymin) * GRID_PADDING_FRAC, cell_size_m * 3)
    bbox = QgsRectangle(xmin - pad_x, ymin - pad_y, xmax + pad_x, ymax + pad_y)

    # Adaptive cell size with ceiling-safe headroom.  The previous math
    # computed a cell size that produced *exactly* MAX_GRID_CELLS, so
    # the subsequent math.ceil() inside _sample_dem_grid would push the
    # grid to e.g. 101x100=10100 cells and trip the cell-cap guard,
    # dropping us back to straight-line.  Multiplying the target cell
    # count by 0.95 reserves ~5 % slack so ceiling never overflows.
    target_cells = MAX_GRID_CELLS * 0.95
    approx_cells = (bbox.width() / cell_size_m) * (bbox.height() / cell_size_m)
    if approx_cells > target_cells:
        cell_size_m = math.sqrt(bbox.width() * bbox.height() / target_cells)
        log(f"bbox {bbox.width():.0f}x{bbox.height():.0f} m -> "
            f"adaptive cell {cell_size_m:.1f} m "
            f"(approx_cells {approx_cells:.0f} > target {target_cells:.0f})")
    else:
        log(f"bbox {bbox.width():.0f}x{bbox.height():.0f} m -> "
            f"cell {cell_size_m:.1f} m ({approx_cells:.0f} cells)")

    grid = _sample_dem_grid(dem_layer, bbox, cell_size_m)
    if grid is None:
        log("grid sampling returned None; cost-based path unavailable")
        return None

    start_rc = _world_to_grid(grid, start_3857.x(), start_3857.y())
    goal_rc = _world_to_grid(grid, end_3857.x(), end_3857.y())
    if start_rc == goal_rc:
        return straight_line_path(start_3857, end_3857)

    path_rc = _astar(grid, start_rc, goal_rc, slope_penalty=slope_penalty)
    if not path_rc:
        log(f"A* found no path from {start_rc} to {goal_rc} on "
            f"{grid['width']}x{grid['height']} grid")
        return None

    log(f"A* path found: {len(path_rc)} cells on "
        f"{grid['width']}x{grid['height']} grid, cell={grid['cell']:.1f} m")

    pts = [start_3857]
    for rc in path_rc[1:-1]:
        pts.append(_grid_to_world(grid, *rc))
    pts.append(end_3857)
    return QgsGeometry.fromPolylineXY(pts)


# ===========================================================================
# 3) LAYER CREATION
# ===========================================================================

def build_path_layer(line_geom_3857, path_type, length_m,
                     layer_name="Substation Connection"):
    """Create a styled LineString memory layer carrying the connection path."""
    layer = QgsVectorLayer("LineString?crs=EPSG:3857", layer_name, "memory")
    pr = layer.dataProvider()
    pr.addAttributes([
        QgsField("path_type", QVariant.String),
        QgsField("length_m", QVariant.Double),
    ])
    layer.updateFields()

    feat = QgsFeature(layer.fields())
    feat.setGeometry(line_geom_3857)
    feat.setAttributes([path_type, float(length_m)])
    pr.addFeature(feat)

    # Orange for cost-based, red for straight-line — easy to distinguish.
    color = "255,140,0,255" if path_type == "cost" else "220,30,30,255"
    symbol = QgsLineSymbol.createSimple({"color": color, "width": "0.8"})
    layer.renderer().setSymbol(symbol)
    return layer


def build_substation_marker_layer(point_3857, name,
                                  layer_name="Nearest Substation"):
    """Create a small triangle-marker layer for the chosen substation."""
    layer = QgsVectorLayer("Point?crs=EPSG:3857", layer_name, "memory")
    pr = layer.dataProvider()
    pr.addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()

    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromPointXY(point_3857))
    feat.setAttributes([name or "Substation"])
    pr.addFeature(feat)

    symbol = QgsMarkerSymbol.createSimple({
        "name": "triangle",
        "color": "255,210,0,255",
        "size": "5",
        "outline_color": "black",
        "outline_width": "0.6",
    })
    layer.renderer().setSymbol(symbol)
    return layer


# ===========================================================================
# High-level entry point
# ===========================================================================

def _substation_display_name(feature):
    for field_name in _SUBSTATION_NAME_FIELDS:
        idx = feature.fields().indexFromName(field_name)
        if idx != -1 and feature[idx]:
            return str(feature[idx])
    return None


def connect_waterbody_to_substation(waterbody_geom_3857, metadata_polygon_entries,
                                    slope_penalty=None):
    """End-to-end convenience wrapper used by the GUI.

    Parameters
    ----------
    waterbody_geom_3857 : QgsGeometry
        Selected waterbody polygon in EPSG:3857.
    metadata_polygon_entries : list of dict
        The GUI's loaded layer registry.
    slope_penalty : float, optional
        Per-call override for the A* terrain weight used by
        :func:`cost_based_path`. ``None`` (the default) keeps the
        module-level :data:`SLOPE_PENALTY` constant, so existing callers
        that don't know about this kwarg behave unchanged.

    Returns a dict with ``path_layer``, ``substation_layer``, ``path_type``
    (``"cost"`` or ``"straight"``), ``length_m``, ``straight_line_distance_m``,
    and ``substation_name`` — or ``{"error": "..."}`` on failure.

    Never raises — all exceptions are turned into ``{"error": ...}`` so a
    bad feature or flaky gpkg can never crash QGIS.
    """
    import traceback

    def log(msg):
        print(f"[substation_connector] {msg}")

    # Resolve the effective slope_penalty once, here at the boundary, so
    # the rest of the function doesn't have to keep checking for None.
    effective_slope_penalty = (
        SLOPE_PENALTY if slope_penalty is None else float(slope_penalty)
    )

    try:
        log("=== connect_waterbody_to_substation start ===")
        log(f"slope_penalty = {effective_slope_penalty}")
        substations_layer = find_substations_layer(metadata_polygon_entries)
        if substations_layer is None:
            return {"error": f"{SUBSTATION_GPKG_NAME} is not loaded."}
        log(f"using substations layer: {substations_layer.name()}")

        nearest = find_nearest_substation(waterbody_geom_3857, substations_layer)
        if nearest is None:
            return {"error": "No substations found near the selected waterbody."}

        start = nearest["closest_boundary_point_3857"]
        end = nearest["substation_point_3857"]
        straight_dist = nearest["straight_line_distance_m"]

        dem_layer = find_dem_layer(metadata_polygon_entries)
        if dem_layer is None:
            log("no DEM layer found; using straight-line path")
        else:
            log(f"DEM layer detected: {dem_layer.name()}; attempting cost-based path")

        path_type = "straight"
        line_geom = None
        if dem_layer is not None:
            try:
                line_geom = cost_based_path(
                    start, end, dem_layer,
                    slope_penalty=effective_slope_penalty,
                )
            except Exception as exc:
                log(f"cost_based_path raised {type(exc).__name__}: {exc}")
                log(traceback.format_exc())
                line_geom = None
            if line_geom is not None and not line_geom.isEmpty():
                path_type = "cost"
            else:
                log("cost-based path unavailable; falling back to straight line")

        if line_geom is None:
            line_geom = straight_line_path(start, end)

        length_m = line_geom.length() if path_type == "cost" else straight_dist
        name = _substation_display_name(nearest["feature"])

        result = {
            "path_layer": build_path_layer(line_geom, path_type, length_m),
            "substation_layer": build_substation_marker_layer(end, name),
            "path_type": path_type,
            "length_m": length_m,
            "straight_line_distance_m": straight_dist,
            "substation_name": name,
        }
        log(f"=== done: {path_type} path, {length_m/1000:.2f} km to {name or 'unnamed'} ===")
        return result

    except Exception as exc:
        log(f"UNCAUGHT {type(exc).__name__}: {exc}")
        log(traceback.format_exc())
        return {"error": f"Unexpected error: {type(exc).__name__}: {exc}"}