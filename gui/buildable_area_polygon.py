"""
Buildable Area Generator

Generates a buildable area polygon by subtracting metadata polygon overlaps
from a waterbody polygon. The resulting polygon represents the area on a waterbody
where floating solar panels could potentially be installed.

Layer weights are read from layer_weights.csv (-5 to +5 range):
    - Negative weight = suitable contribution (shifts heatmap toward blue)
    - 0 = neutral
    - Positive weight = unsuitable contribution (shifts heatmap toward red)

Core functions:
    load_layer_weights()              - Reads weights from layer_weights.csv
    collect_metadata_geometries()     - Gathers metadata polygons near a buffer (all layers)
    collect_restricted_geometries()   - Gathers only weight>1 metadata polygons near a buffer
    subtract_overlaps()               - Subtracts metadata from a waterbody
    save_buildable_areas_gpkg()       - Saves results to a .gpkg file
    delete_buildable_areas_gpkg()     - Cleans up the .gpkg file
"""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorFileWriter,
)
import os
import csv


# Default weight for layers not listed in layer_weights.csv
DEFAULT_WEIGHT = 1.0


def load_layer_weights(script_dir):
    """
    Read layer weights from layer_weights.csv.
    
    The CSV should have two columns: filename, weight
    
    Args:
        script_dir: Directory where layer_weights.csv is located
    
    Returns:
        Dict mapping filename (str) to weight (float)
    """
    weights = {}
    csv_path = os.path.join(script_dir, "layer_weights.csv")
    
    if not os.path.exists(csv_path):
        return weights
    
    try:
        with open(csv_path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                filename = row['filename'].strip()
                try:
                    weight = float(row['weight'].strip())
                    weights[filename] = weight
                except (ValueError, KeyError):
                    pass
    except Exception as e:
        print(f"Error reading layer_weights.csv: {e}")
    
    return weights


def get_weight(filename, weights):
    """
    Get the weight for a given .gpkg filename.
    
    Args:
        filename: The .gpkg filename (e.g., "NationalParks.gpkg")
        weights: Dict from load_layer_weights()
    
    Returns:
        Float between 0.0 and 1.0
    """
    return weights.get(filename, DEFAULT_WEIGHT)


def build_buffer_geometry(lat, lon, buffer_radius_miles):
    """
    Create a circular buffer geometry in EPSG:3857 from lat/lon coordinates.
    
    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees
        buffer_radius_miles: Buffer radius in miles
    
    Returns:
        QgsGeometry of the buffer circle in EPSG:3857, or None on failure
    """
    try:
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        transform = QgsCoordinateTransform(crs_4326, crs_3857, QgsProject.instance())
        point_3857 = transform.transform(QgsPointXY(lon, lat))
        buffer_meters = buffer_radius_miles * 1609.34
        return QgsGeometry.fromPointXY(point_3857).buffer(buffer_meters, 50)
    except Exception:
        return None


def _collect_geometries_from_layer(source_layer, buffer_geom_3857):
    """
    Internal helper: collect geometries from a single source layer that
    intersect the buffer, all transformed to EPSG:3857.
    
    Args:
        source_layer: QgsVectorLayer to query
        buffer_geom_3857: QgsGeometry of the buffer in EPSG:3857
    
    Returns:
        List of QgsGeometry objects in EPSG:3857
    """
    crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
    geometries = []
    
    if not source_layer or not source_layer.isValid():
        return geometries
    
    layer_crs = source_layer.crs()
    needs_transform = (layer_crs.authid() != crs_3857.authid())
    
    if needs_transform:
        transform_to_layer = QgsCoordinateTransform(crs_3857, layer_crs, QgsProject.instance())
        transform_from_layer = QgsCoordinateTransform(layer_crs, crs_3857, QgsProject.instance())
        buffer_in_layer_crs = QgsGeometry(buffer_geom_3857)
        buffer_in_layer_crs.transform(transform_to_layer)
        filter_rect = buffer_in_layer_crs.boundingBox()
    else:
        transform_from_layer = None
        filter_rect = buffer_geom_3857.boundingBox()
    
    request = QgsFeatureRequest().setFilterRect(filter_rect)
    
    for feature in source_layer.getFeatures(request):
        geom = QgsGeometry(feature.geometry())
        if geom.isEmpty():
            continue
        
        if needs_transform and transform_from_layer:
            geom.transform(transform_from_layer)
        
        if not geom.isGeosValid():
            geom = geom.makeValid()
        
        if not geom.isEmpty():
            geometries.append(geom)
    
    return geometries


def collect_metadata_geometries(metadata_polygon_entries, buffer_geom_3857):
    """
    Collect ALL metadata polygon geometries that intersect the buffer area,
    all transformed to EPSG:3857. This collects from every entry regardless
    of weight.
    
    Args:
        metadata_polygon_entries: List of dicts, each with "source_layer" key
        buffer_geom_3857: QgsGeometry of the buffer in EPSG:3857
    
    Returns:
        List of QgsGeometry objects in EPSG:3857
    """
    all_geometries = []
    
    for entry in metadata_polygon_entries:
        geoms = _collect_geometries_from_layer(entry["source_layer"], buffer_geom_3857)
        all_geometries.extend(geoms)
    
    return all_geometries


def collect_restricted_geometries(metadata_polygon_entries, buffer_geom_3857, weights):
    """
    Collect metadata polygon geometries from layers that are restricted
    (weight > 1), transformed to EPSG:3857.

    Layers with weight == 1 (no restriction) are skipped. Layers not listed
    in the weights dict use DEFAULT_WEIGHT (1.0) and are also skipped.

    Args:
        metadata_polygon_entries: List of dicts with "source_layer" and "filepath" keys
        buffer_geom_3857: QgsGeometry of the buffer in EPSG:3857
        weights: Dict from load_layer_weights()

    Returns:
        List of QgsGeometry objects in EPSG:3857
    """
    restricted_geometries = []

    for entry in metadata_polygon_entries:
        filepath = entry.get("filepath", "")
        filename = os.path.basename(filepath)
        weight = get_weight(filename, weights)

        # Collect geometries from restricted layers (weight > 1)
        if weight > 1.0:
            geoms = _collect_geometries_from_layer(entry["source_layer"], buffer_geom_3857)
            restricted_geometries.extend(geoms)

    return restricted_geometries


def subtract_overlaps(waterbody_geom, metadata_geometries):
    """
    Subtract all overlapping metadata geometries from a single waterbody geometry.
    
    Args:
        waterbody_geom: QgsGeometry of the waterbody polygon
        metadata_geometries: List of QgsGeometry objects to subtract
    
    Returns:
        QgsGeometry with overlaps removed, or None if nothing remains
    """
    # Make waterbody geometry valid
    if not waterbody_geom.isGeosValid():
        waterbody_geom = waterbody_geom.makeValid()
    
    # Find only the metadata geometries that intersect this waterbody
    overlapping = [mg for mg in metadata_geometries if waterbody_geom.intersects(mg)]
    
    if not overlapping:
        # No overlap - return the original geometry
        return QgsGeometry(waterbody_geom)
    
    # Subtract each overlapping metadata geometry one at a time
    result_geom = QgsGeometry(waterbody_geom)
    
    for overlap_geom in overlapping:
        if result_geom.isEmpty():
            break
        
        # Make both geometries valid before differencing
        if not result_geom.isGeosValid():
            result_geom = result_geom.makeValid()
        
        valid_overlap = QgsGeometry(overlap_geom)
        if not valid_overlap.isGeosValid():
            valid_overlap = valid_overlap.makeValid()
        
        # Clip the overlap geometry to the waterbody bounding area
        # to avoid issues with huge metadata polygons
        clipped_overlap = valid_overlap.intersection(waterbody_geom)
        if clipped_overlap and not clipped_overlap.isEmpty():
            diff = result_geom.difference(clipped_overlap)
            if diff and not diff.isEmpty():
                result_geom = diff
            else:
                # difference returned None or empty - try buffer(0) fix
                try:
                    fixed_result = result_geom.buffer(0, 5)
                    fixed_overlap = clipped_overlap.buffer(0, 5)
                    if not fixed_result.isEmpty() and not fixed_overlap.isEmpty():
                        diff = fixed_result.difference(fixed_overlap)
                        if diff and not diff.isEmpty():
                            result_geom = diff
                except Exception:
                    pass
    
    # Make result valid
    if result_geom and not result_geom.isGeosValid():
        result_geom = result_geom.makeValid()
    
    # Return only if non-empty with positive area
    if result_geom and not result_geom.isEmpty() and result_geom.area() > 0:
        return result_geom
    
    return None


def save_buildable_areas_gpkg(buildable_layer, output_path):
    """
    Save a buildable areas layer to a .gpkg file.
    
    Args:
        buildable_layer: QgsVectorLayer to save
        output_path: Full path to the output .gpkg file
    
    Returns:
        True on success, False on failure
    """
    # Ensure the output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    error = QgsVectorFileWriter.writeAsVectorFormatV3(
        buildable_layer,
        output_path,
        QgsProject.instance().transformContext(),
        QgsVectorFileWriter.SaveVectorOptions()
    )
    
    return True


def delete_buildable_areas_gpkg(script_dir):
    """
    Delete the buildable_areas.gpkg file if it exists.
    
    Args:
        script_dir: Directory where the gui.py script is located
    """
    buildable_file = os.path.join(script_dir, "buildable_areas", "buildable_areas.gpkg")
    if os.path.exists(buildable_file):
        try:
            os.remove(buildable_file)
        except OSError:
            pass