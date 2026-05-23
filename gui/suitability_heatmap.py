"""
Suitability Heatmap Generator

Generates a suitability heatmap raster (GeoTIFF) for a single waterbody
polygon. Instead of cutting out restricted regions, this produces a
continuous additive score where each pixel's value reflects the sum of
weighted layer contributions at that location.

Weight system (-5 to +5):
    - Negative weight = suitable contribution (accumulates on the negative side)
    - 0 = no contribution
    - Positive weight = unsuitable contribution (accumulates on the positive side)

    Negative and positive weights never cancel each other out. Instead, the side
    with the larger magnitude determines the final pixel score. Ties go to the
    positive (unsuitable) side.
    - e.g. weight=-3 and weight=+2 overlapping → score -3 (suitable wins)
    - e.g. weight=-2 and weight=+3 overlapping → score +3 (unsuitable wins)
    - e.g. weight=-2 and weight=+2 overlapping → score +2 (tie → unsuitable wins)

Score interpretation:
    - -5  = highly suitable (dark purple)
    - 0   = neutral (green)
    - +5  = highly unsuitable (dark red)
    - NoData = outside the waterbody polygon
"""

from osgeo import gdal, ogr, osr
gdal.UseExceptions()
import numpy as np
import os

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
)

from buildable_area_polygon import _collect_geometries_from_layer


# Target maximum raster dimension (width or height) in pixels
MAX_DIMENSION = 2000


def _rasterize_geometries(geometries, x_min, y_max, px_x, px_y, cols, rows):
    """
    Rasterize a list of QgsGeometry objects (EPSG:3857) into a binary numpy array.

    Args:
        geometries: List of QgsGeometry in EPSG:3857
        x_min: Left edge of raster extent
        y_max: Top edge of raster extent
        px_x: Pixel width in map units
        px_y: Pixel height in map units (positive)
        cols: Number of columns
        rows: Number of rows

    Returns:
        numpy array (rows, cols) of float32, 1.0 where geometry covers, 0.0 elsewhere
    """
    mem_driver = ogr.GetDriverByName('MEM')
    mem_ds = mem_driver.CreateDataSource('')

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)

    mem_layer = mem_ds.CreateLayer('temp', srs, ogr.wkbMultiPolygon)

    for geom in geometries:
        wkt = geom.asWkt()
        ogr_geom = ogr.CreateGeometryFromWkt(wkt)
        if ogr_geom is None:
            continue
        feat = ogr.Feature(mem_layer.GetLayerDefn())
        feat.SetGeometry(ogr_geom)
        mem_layer.CreateFeature(feat)

    if mem_layer.GetFeatureCount() == 0:
        mem_ds = None
        return np.zeros((rows, cols), dtype=np.float32)

    mem_raster_driver = gdal.GetDriverByName('MEM')
    target_ds = mem_raster_driver.Create('', cols, rows, 1, gdal.GDT_Float32)
    target_ds.SetGeoTransform((x_min, px_x, 0, y_max, 0, -px_y))
    target_ds.SetProjection(srs.ExportToWkt())

    band = target_ds.GetRasterBand(1)
    band.Fill(0)

    gdal.RasterizeLayer(target_ds, [1], mem_layer, burn_values=[1.0])

    result = band.ReadAsArray()

    target_ds = None
    mem_ds = None

    return result


def _read_raster_as_grid_mask(raster_path, x_min, y_max, px_size, cols, rows):
    """
    Warp a raster file onto the heatmap target grid (EPSG:3857) and return a
    binary mask (float32) — 1.0 where the source has positive, non-NoData
    data, 0.0 elsewhere. Returns None if the raster can't be opened.
    """
    src_ds = gdal.Open(raster_path)
    if src_ds is None:
        return None

    target_srs = osr.SpatialReference()
    target_srs.ImportFromEPSG(3857)

    x_max = x_min + cols * px_size
    y_min = y_max - rows * px_size

    try:
        warped = gdal.Warp(
            '', src_ds,
            format='MEM',
            outputBounds=(x_min, y_min, x_max, y_max),
            width=cols, height=rows,
            dstSRS=target_srs.ExportToWkt(),
            resampleAlg=gdal.GRA_NearestNeighbour,
        )
    except Exception:
        return None

    if warped is None:
        return None

    band = warped.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    data = band.ReadAsArray()
    if data is None:
        return None

    if nodata is not None:
        mask = (data != nodata) & (data > 0)
    else:
        mask = data > 0

    return mask.astype(np.float32)


def compute_heatmap(waterbody_geom, metadata_entries, weights, output_path):
    """
    Generate a suitability heatmap GeoTIFF for a single waterbody polygon.

    The waterbody geometry defines the raster extent and base mask. Negative
    and positive weights are accumulated separately. The final pixel score is
    determined by whichever side has the larger magnitude; if equal, the
    positive (unsuitable) side wins. This means a weight=-3 layer and a
    weight=+2 layer overlapping results in -3 (suitable side wins), while
    weight=-2 and weight=+3 overlapping results in +3 (unsuitable side wins).

    Inputs that influence the heatmap are scoped to two sources:
        * metadata_polygons/ — vector layers, weighted via layer_weights.csv
        * novel_water_level/min_water_*.tif — raster tiles that drive the
          +1 outside-min-water rule (used directly, not via gpkg conversion)
    All other entries (novel_border, other novel raster overlays, etc.)
    are skipped so unrelated layers can't drift the score.

    Special handling for min-water rasters: any raster entry whose filepath
    is inside novel_water_level/ and whose filename contains "min_water" is
    treated as the minimum-water-level extent. Areas of the waterbody
    OUTSIDE those rasters are forced to +5 (highly unsuitable), overriding
    the tie-break entirely; areas INSIDE are scored normally. Multiple
    overlapping min_water tiles are unioned before the override is applied.

    Args:
        waterbody_geom: QgsGeometry of the waterbody polygon in EPSG:3857
        metadata_entries: List of dicts with "source_layer", "filepath" keys
        weights: Dict mapping filename to weight (from load_layer_weights)
        output_path: Path to save the output .tif file

    Returns:
        Tuple of (output_path, max_score) on success, or None on failure.
        max_score is the highest score value in the raster.
    """
    if not waterbody_geom or waterbody_geom.isEmpty():
        return None

    # Use the waterbody's bounding box as the raster extent
    extent = waterbody_geom.boundingBox()
    extent.grow(50)  # small padding

    x_min = extent.xMinimum()
    y_min = extent.yMinimum()
    x_max = extent.xMaximum()
    y_max = extent.yMaximum()

    width = x_max - x_min
    height = y_max - y_min

    # Compute pixel size to fit within MAX_DIMENSION
    pixel_size = max(width, height) / MAX_DIMENSION
    pixel_size = max(pixel_size, 1.0)

    cols = max(1, int(width / pixel_size))
    rows = max(1, int(height / pixel_size))

    # Use the waterbody bounding box as the spatial filter for metadata
    buffer_geom = QgsGeometry.fromRect(extent)

    # Rasterize the waterbody polygon as the base water mask
    water_mask = _rasterize_geometries(
        [waterbody_geom], x_min, y_max, pixel_size, pixel_size, cols, rows
    )

    # Track positive and negative contributions separately
    pos_sum = np.zeros((rows, cols), dtype=np.float32)
    neg_sum = np.zeros((rows, cols), dtype=np.float32)

    # Combined min-water mask, unioned across all overlapping min_water tiles
    min_water_mask = np.zeros((rows, cols), dtype=np.float32)
    has_min_water = False

    for entry in metadata_entries:
        filepath = entry.get("filepath", "")
        filepath_norm = filepath.replace("\\", "/").lower()
        filename = os.path.basename(filepath)

        if entry.get("is_raster"):
            # Min-water rasters in novel_water_level/ feed the +1
            # outside-min-water rule. Other rasters aren't wired in.
            if "novel_water_level" in filepath_norm and "min_water" in filename.lower():
                tile_mask = _read_raster_as_grid_mask(
                    filepath, x_min, y_max, pixel_size, cols, rows
                )
                if tile_mask is not None and tile_mask.any():
                    min_water_mask = np.maximum(min_water_mask, tile_mask)
                    has_min_water = True
            continue

        source_layer = entry.get("source_layer")
        if not source_layer or not source_layer.isValid():
            continue

        # Vector allow-list: only metadata_polygons/ contributes (weighted).
        # Everything else — novel_border, etc. — is excluded.
        if "/metadata_polygons/" not in filepath_norm:
            continue

        weight = weights.get(filename, 1.0)

        geoms = _collect_geometries_from_layer(source_layer, buffer_geom)
        if not geoms:
            continue

        layer_mask = _rasterize_geometries(
            geoms, x_min, y_max, pixel_size, pixel_size, cols, rows
        )

        if weight > 0:
            overlap = (layer_mask > 0) & (water_mask > 0)
            pos_sum[overlap] += weight
        elif weight < 0:
            overlap = (layer_mask > 0) & (water_mask > 0)
            neg_sum[overlap] += weight
        # weight == 0: no contribution

    # Determine final score per pixel: whichever side has larger magnitude wins.
    # Ties go to the positive (unsuitable) side.
    neg_magnitude = np.abs(neg_sum)
    final_score = np.where(neg_magnitude > pos_sum, neg_sum, pos_sum).astype(np.float32)

    # Min-water override: any waterbody pixel outside an overlapping min_water
    # raster is forced to +5 (highly unsuitable), bypassing the tie-break
    # entirely. No-op when no min_water raster covers the area.
    if has_min_water:
        outside_min_water = (min_water_mask == 0) & (water_mask > 0)
        final_score[outside_min_water] = 5.0

    water_pixels = water_mask > 0
    max_overlap = float(np.max(final_score[water_pixels])) if np.any(water_pixels) else 0.0

    # Set pixels outside waterbody to NoData (-9999 avoids collision with the -5 to +5 score range)
    nodata = -9999.0
    final_score[~water_pixels] = nodata

    # Write the GeoTIFF
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    driver = gdal.GetDriverByName('GTiff')
    ds = driver.Create(output_path, cols, rows, 1, gdal.GDT_Float32)

    ds.SetGeoTransform((x_min, pixel_size, 0, y_max, 0, -pixel_size))

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    ds.SetProjection(srs.ExportToWkt())

    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(final_score)
    band.FlushCache()

    ds = None

    return output_path, max_overlap


def delete_heatmap_tif(script_dir):
    """
    Delete the suitability heatmap .tif file if it exists.

    Args:
        script_dir: Directory where the gui.py script is located
    """
    heatmap_file = os.path.join(script_dir, "buildable_areas", "suitability_heatmap.tif")
    if os.path.exists(heatmap_file):
        try:
            os.remove(heatmap_file)
        except OSError:
            pass
