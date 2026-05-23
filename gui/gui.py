from qgis.core import *
from qgis.core import QgsWkbTypes, QgsMarkerSymbol, QgsFillSymbol
from qgis.gui import *
from qgis.PyQt.QtWidgets import *
from qgis.PyQt.QtCore import Qt, QDateTime, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import QSplitter, QScrollArea, QHeaderView, QTableWidget, QTableWidgetItem
import os
import csv
from buildable_area_polygon import (
    build_buffer_geometry,
    load_layer_weights,
    collect_metadata_geometries,
    collect_restricted_geometries,
    subtract_overlaps,
    save_buildable_areas_gpkg,
    delete_buildable_areas_gpkg,
)
from suitability_heatmap import compute_heatmap, delete_heatmap_tif
from substation_connector import connect_waterbody_to_substation

# Create a reference to the QgsApplication
# Setting the 2nd argument to True enables the GUI
qgs = QgsApplication([], True)

# Load providers
qgs.initQgis()


# ===========================================================================
# Substation-connection distance helpers
# ---------------------------------------------------------------------------
# Internal unit is meters (matches QgsGeometry.length() in EPSG:3857 and the
# rest of the codebase). User input is collected in miles via the dialog
# below and converted at the boundary.
# ===========================================================================

#: Exact conversion factor — 1 mile == 1609.344 meters by international
#: agreement. We derive miles_to_meters / meters_to_miles from this single
#: constant so the two functions can never drift apart.
_METERS_PER_MILE = 1609.344


def miles_to_meters(miles):
    """Convert a distance in miles to meters."""
    return float(miles) * _METERS_PER_MILE


def meters_to_miles(meters):
    """Convert a distance in meters to miles."""
    return float(meters) / _METERS_PER_MILE


def path_length_m_from_layer(path_layer):
    """Return the length, in meters, of the LineString stored in
    ``path_layer`` (the ``path_layer`` produced by
    ``substation_connector.connect_waterbody_to_substation``).

    Uses ``QgsGeometry.length()`` directly so straight-line and A* /
    cost-based paths are measured identically — the value reflects the
    *actual* polyline drawn on the map, not the straight-line Euclidean
    distance between endpoints.

    Returns ``None`` if the layer has no usable geometry.
    """
    if path_layer is None:
        return None
    feat = next(path_layer.getFeatures(), None)
    if feat is None:
        return None
    geom = feat.geometry()
    if geom is None or geom.isEmpty():
        return None
    return geom.length()


def evaluate_distance(distance_m, reasonable_m, maximum_m):
    """Bucket ``distance_m`` against the user's two thresholds.

    Returns a ``(verdict, message)`` tuple where ``verdict`` is one of
    ``"great"``, ``"okay"``, ``"too_long"`` and ``message`` is the
    user-facing string mandated by the spec.
    """
    if distance_m < reasonable_m:
        return ("great", "This distance is great for you")
    elif distance_m <= maximum_m:
        return ("okay", "This distance is okay, but not ideal")
    else:
        return ("too_long", "This distance is longer than desirable")


#: Default slope_penalty value used by the slider on first open.  Matches
#: ``substation_connector.SLOPE_PENALTY`` so the GUI default == module
#: default; if you ever bump the module constant, this picks it up
#: automatically.
try:
    from substation_connector import SLOPE_PENALTY as _DEFAULT_SLOPE_PENALTY
except Exception:
    _DEFAULT_SLOPE_PENALTY = 30

#: Bounds for the in-dialog slope_penalty slider.
SLOPE_PENALTY_MIN = 0
SLOPE_PENALTY_MAX = 50
SLOPE_PENALTY_STEP = 1


class DistancePreferencesDialog(QDialog):
    """Modal dialog that asks the user for path-shaping preferences
    BEFORE a substation-connection path is generated.

    On accept, exposes:
        ``self.reasonable_miles`` -- preferred max distance (float, miles)
        ``self.maximum_miles``    -- absolute tolerated max (float, miles)
        ``self.slope_penalty``    -- A* slope cost weight (int, 0..50)

    Validation rules enforced in-dialog:
        * Both distance fields must parse as positive numbers.
        * ``maximum_miles >= reasonable_miles``.
        * Slope penalty is constrained by the slider, no extra check needed.
    """

    def __init__(self, parent=None, has_dem=True):
        super().__init__(parent)
        self.setWindowTitle("Substation Connection Preferences")
        self.setMinimumWidth(480)

        self.reasonable_miles = None
        self.maximum_miles = None
        self.slope_penalty = None

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Before computing the path from the waterbody to the nearest "
            "substation, please set your preferences.\n\n"
            "Distances are in <b>miles</b>."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.reasonable_input = QLineEdit()
        self.reasonable_input.setPlaceholderText("e.g. 5.0")
        form.addRow("Reasonable distance (miles):", self.reasonable_input)

        self.maximum_input = QLineEdit()
        self.maximum_input.setPlaceholderText("e.g. 15.0")
        form.addRow("Maximum tolerable distance (miles):", self.maximum_input)
        layout.addLayout(form)

        # ----- slope_penalty slider --------------------------------------
        # Cost formula in cost_based_path():
        #     cost_multiplier = 1 + slope_penalty * |slope|
        # So 0 = ignore terrain (pure shortest-distance), higher = more
        # aggressive avoidance of steep ground.  The slider is integer-
        # backed (QSlider only does ints); we cast back to float when
        # passing it to the path generator.
        slope_box = QGroupBox("Slope penalty (A* terrain weight)")
        slope_layout = QVBoxLayout(slope_box)

        slope_help = QLabel(
            "Higher values make the path avoid steep terrain more "
            "aggressively. <b>0</b> ignores slope entirely; <b>50</b> "
            "strongly penalises hills. Default is "
            f"<b>{int(_DEFAULT_SLOPE_PENALTY)}</b>."
        )
        slope_help.setWordWrap(True)
        slope_layout.addWidget(slope_help)

        slider_row = QHBoxLayout()
        self.slope_slider = QSlider(Qt.Horizontal)
        self.slope_slider.setMinimum(SLOPE_PENALTY_MIN)
        self.slope_slider.setMaximum(SLOPE_PENALTY_MAX)
        self.slope_slider.setSingleStep(SLOPE_PENALTY_STEP)
        self.slope_slider.setPageStep(SLOPE_PENALTY_STEP * 5)
        self.slope_slider.setTickInterval(10)
        self.slope_slider.setTickPosition(QSlider.TicksBelow)
        # Clamp the default into the slider's range in case someone bumps
        # SLOPE_PENALTY in substation_connector.py beyond our bounds.
        clamped_default = max(
            SLOPE_PENALTY_MIN,
            min(SLOPE_PENALTY_MAX, int(round(_DEFAULT_SLOPE_PENALTY))),
        )
        self.slope_slider.setValue(clamped_default)

        self.slope_value_label = QLabel(str(clamped_default))
        self.slope_value_label.setMinimumWidth(28)
        self.slope_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # Live-update the readout as the user drags.
        self.slope_slider.valueChanged.connect(
            lambda v: self.slope_value_label.setText(str(v))
        )

        slider_row.addWidget(QLabel(str(SLOPE_PENALTY_MIN)))
        slider_row.addWidget(self.slope_slider, 1)
        slider_row.addWidget(QLabel(str(SLOPE_PENALTY_MAX)))
        slider_row.addWidget(self.slope_value_label)
        slope_layout.addLayout(slider_row)

        # If no DEM is loaded the cost-based path can't run, so the
        # slider value is ignored.  Tell the user that instead of letting
        # them think they're influencing something.
        if not has_dem:
            self.slope_slider.setEnabled(False)
            inert_note = QLabel(
                "<i>No DEM / elevation layer is loaded — the path will "
                "fall back to a straight line and this slider has no "
                "effect.</i>"
            )
            inert_note.setWordWrap(True)
            inert_note.setStyleSheet("color: #7f8c8d;")
            slope_layout.addWidget(inert_note)

        layout.addWidget(slope_box)
        # -----------------------------------------------------------------

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #c0392b;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        try:
            reasonable = float(self.reasonable_input.text().strip())
            maximum = float(self.maximum_input.text().strip())
        except ValueError:
            self.error_label.setText(
                "Please enter numeric values for both distances."
            )
            return
        if reasonable <= 0 or maximum <= 0:
            self.error_label.setText(
                "Distances must be greater than zero."
            )
            return
        if maximum < reasonable:
            self.error_label.setText(
                "Maximum distance must be greater than or equal to the "
                "reasonable distance."
            )
            return
        self.reasonable_miles = reasonable
        self.maximum_miles = maximum
        # Slider returns an int; the path generator's signature is float.
        # We cast here so the rest of the code can stay unit-pure.
        self.slope_penalty = float(self.slope_slider.value())
        self.accept()


class MapWindow(QMainWindow):
    # =====================================================================
    # METADATA POLYGON CONFIGURATION
    # =====================================================================
    # Configure display names and colors for .gpkg files in metadata_polygons/.
    # Key: filename (e.g., "ca_water_agency_districts.gpkg")
    # Value: dict with:
    #   "name"          - Display name shown next to the checkbox
    #   "fill_color"    - RGBA fill color (semi-transparent recommended)
    #   "outline_color" - RGBA outline color
    #
    # Files not listed here will use defaults:
    #   name    = filename with underscores replaced by spaces, title-cased,
    #             and leading 2-letter state prefix removed
    #   color   = a color auto-assigned from a built-in palette
    # =====================================================================
    METADATA_POLYGON_CONFIG = {
        "ca_water_agency_districts.gpkg": {
            "name": "Water Agency Districts",
        },
        "efh_pacific_fmc.gpkg": {
            "name": "Fish Habitats",
        },
        "tribal_lands.gpkg": {
            "name": "Tribal Lands",
        },
    }

    @staticmethod
    def _generate_distinct_color(index, total):
        """Generate a distinct fill/outline color pair using evenly spaced hues.
        Avoids the cyan range (hue 170-200) to not conflict with waterbody colors."""
        import colorsys
        # Use the golden angle (~137.5 degrees) for good hue spacing
        golden_angle = 0.618033988749895
        hue = (index * golden_angle) % 1.0
        # Remap hue to skip the cyan range (0.47-0.55 in 0-1 scale)
        # by compressing into the remaining range
        hue = (hue * 0.85) % 1.0  # compress and skip cyan-ish region
        if hue > 0.47:
            hue += 0.15
            if hue > 1.0:
                hue -= 1.0
        r, g, b = colorsys.hls_to_rgb(hue, 0.55, 0.85)
        fr, fg, fb = int(r * 255), int(g * 255), int(b * 255)
        # Outline is a darker version
        or_, og, ob = int(r * 200), int(g * 200), int(b * 200)
        fill_color = f"{fr},{fg},{fb},80"
        outline_color = f"{or_},{og},{ob},255"
        return fill_color, outline_color
    def find_closest_substation_for_waterbody(self, feature, source_layer=None):
        """
        Find the closest HydroGlow-derived substation row for the selected waterbody.
        This shows only one substation instead of every nearby substation.
        """
        filename = "waterbody_substations.csv"

        if filename not in self.metadata_csv_tables:
            return None

        rows = self.metadata_csv_tables[filename].get("rows", [])
        if not rows:
            return None

        waterbody_info = self.get_waterbody_core_info(feature, source_layer)
        waterbody_name = waterbody_info.get("name")

        feature_fields = feature.fields().names()

        waterbody_id = None
        for field_name in ["waterbody_id", "id", "fid", "objectid"]:
            if field_name in feature_fields:
                value = feature[field_name]
                if value is not None and value != NULL:
                    waterbody_id = str(value).strip()
                    break

        candidates = []

        for row in rows:
            row_waterbody_id = str(row.get("waterbody_id", "")).strip()
            row_waterbody_name = str(row.get("waterbody_name", "")).strip()

            if waterbody_id and row_waterbody_id == waterbody_id:
                candidates.append(row)
                continue

            if waterbody_name and row_waterbody_name.lower() == waterbody_name.lower():
                candidates.append(row)

        if not candidates:
            return None

        candidates.sort(key=lambda r: self.safe_float(r.get("distance_miles")) or 999999)
        return candidates[0]
    # =====================================================================
    # CSV METADATA DISPLAY CONFIGURATION
    # =====================================================================
    # Only these fields will be shown for each CSV file.
    # Anything not listed here will be ignored in the GUI.
    
    CSV_DISPLAY_FIELDS = {
        "expected_energy_nsrdb_lake_isabella_2020.csv": [
            "Location ID", "Elevation", "Version"
        ],
        "expected_energy_nsrdb_lake_isabella_2021.csv": [
            "Location ID", "Elevation", "Version"
        ],
        "expected_energy_nsrdb_lake_isabella_2022.csv": [
            "Location ID", "Elevation", "Version"
        ],
        "expected_energy_nsrdb_lake_isabella_2023.csv": [
            "Location ID", "Elevation", "Version"
        ],
        "expected_energy_nsrdb_lake_isabella_2024.csv": [
            "Location ID", "Elevation", "Version"
        ],
        "aquatic_growt_train.csv": [
            "data_provider", "region", "date", "severity", "distance_to_water_m"
        ],
        "bird_habitats_iba_polygons_centroids.csv": [
            "site_name", "state", "flyway", "iba_status", "iba_priority",
            "hectares", "acres"
        ],
        "electric-retail-service-territories.csv": [
            "name", "city", "state", "cntl_area", "holding_co", "customers", "year"
        ],
        "parcels_i15_Parcels_CVFPB.csv": [
            "titletype", "acreslegal", "acresgis", "sitecity", "sitecounty", "sitestate", "parcelnotes"
        ],
        "unesco_whc001.csv": [
            "name_en", "category", "states names", "region", "date inscribed", "danger"
        ],
        "census_areas_lookup.csv": [
            "state", "census_division"
        ],
        "regional_electricity_costs.csv": [
            "state", "avg_price_cents_per_kwh"
        ],
        "endangered_species_FWS_Species_Data_Explorer.csv": [
            "species_name", "federal_status", "state", "county"
        ],
        "fish_presence_agap_fish_dataset_v2_0.csv": [
            "species", "common_name", "state", "county"
        ],
        "fish_presence_species_list_v2_0.csv": [
            "species", "common_name"
        ],
        "protected_wildlife_habitats_WDPA_sources_Mar2026.csv": [
            "name", "designation", "state", "country"
        ],
        "tribal_lands_US_Domestic_Sovereign_Nations__Land_Areas_of_Federally-Recognized_Tribes_(BIA).csv": [
            "name", "state", "area"
        ],
    }

    # Never display these columns even if they appear in the CSV.
    CSV_ALWAYS_IGNORE_FIELDS = {
        "id", "fid", "objectid", "objectid_1", "globalid", "orig_fid",
        "shape_area", "shape_length", "shapestarea", "shapestlength",
        "x", "y", "lat", "lon", "latitude", "longitude", "uuid",
        "website", "website1", "website2", "ebird_link", "images",
        "geo_point_2d", "coordinates", "components", "components count",
        "source", "sourcedate", "val_method", "val_date",
        "public_db", "public_bound", "status_id", "priority_id",
        "state_id", "country_id", "module", "tempid"
    }

    # Friendly section names for the GUI
    CSV_SECTION_TITLES = {
        "expected_energy_nsrdb_lake_isabella_2020.csv": "Expected Solar Energy / NSRDB (2020)",
        "expected_energy_nsrdb_lake_isabella_2021.csv": "Expected Solar Energy / NSRDB (2021)",
        "expected_energy_nsrdb_lake_isabella_2022.csv": "Expected Solar Energy / NSRDB (2022)",
        "expected_energy_nsrdb_lake_isabella_2023.csv": "Expected Solar Energy / NSRDB (2023)",
        "expected_energy_nsrdb_lake_isabella_2024.csv": "Expected Solar Energy / NSRDB (2024)",
        "waterbody_substations.csv": "Nearest Power Substation",
        "aquatic_growt_train.csv": "Aquatic Growth / Water Quality",
        "bird_habitats_iba_polygons_centroids.csv": "Bird Habitat",
        "electric-retail-service-territories.csv": "Electric Utility Territory",
        "parcels_i15_Parcels_CVFPB.csv": "Parcel / Land Information",
        "unesco_whc001.csv": "UNESCO / Protected Area",
        "census_areas_lookup.csv": "Census Area",
        "regional_electricity_costs.csv": "Regional Electricity Costs",
        "endangered_species_FWS_Species_Data_Explorer.csv": "Endangered Species",
        "fish_presence_agap_fish_dataset_v2_0.csv": "Fish Presence",
        "fish_presence_species_list_v2_0.csv": "Fish Species",
        "protected_wildlife_habitats_WDPA_sources_Mar2026.csv": "Protected Habitat",
        "tribal_lands_US_Domestic_Sovereign_Nations__Land_Areas_of_Federally-Recognized_Tribes_(BIA).csv": "Tribal Lands",
    }
    CSV_NEARBY_CONFIG = {
        "aquatic_growt_train.csv": {
            "lat_fields": ["lat", "latitude"],
            "lon_fields": ["lon", "longitude"],
            "max_distance_km": 8.05,
            "display_fields": ["data_provider", "region", "date", "severity", "distance_to_water_m"],
        },
        "bird_habitats_iba_polygons_centroids.csv": {
            "lat_fields": ["latitude", "lat", "y"],
            "lon_fields": ["longitude", "lon", "x"],
            "max_distance_km": 8.05,
            "display_fields": ["site_name", "state", "flyway", "iba_status", "iba_priority", "hectares", "acres"],
        },
        "endangered_species_FWS_Species_Data_Explorer.csv": {
            "lat_fields": ["latitude", "lat"],
            "lon_fields": ["longitude", "lon"],
            "max_distance_km": 8.05,
            "display_fields": ["species_name", "federal_status", "state", "county"],
        },
        
    }
    GPKG_ALWAYS_IGNORE_FIELDS = {
        "fid", "id", "objectid", "objectid_1", "globalid", "orig_fid",
        "shape", "shape_length", "shape_area", "shape__area", "shape__length",
        "shape.starea()", "shape.stlength()", "shape_leng", "shape_le_1",
        "geometry", "geom", "wkb_geometry",
        "created_user", "created_date", "last_edited_user", "last_edited_date",
        "lastmodifieddate", "modifiedby"
    }

    GPKG_PREFERRED_FIELDS = {
    "ca_water_agency_districts.gpkg": [
        "AGENCYNAME",
        "SOURCE"
    ],

    "water_municipality_i03_waterdistricts.gpkg": [
        "AGENCYNAME",
        "SOURCECOMM"
    ],

    "utilities_electric_service_territories.gpkg": [
        "UTILITY",
        "STATE",
        "TYPE"
    ],

    "regional_us_census_counties_tiger.gpkg": [
        "NAMELSAD",
        "GEOID"
    ],

    "tribal_lands.gpkg": [
        "LARNAME",
        "REGION",
        "AGENCY"
    ],

    "tribal_lands_us_domestic_sovereign_nations3a_land_areas_of_federallyrecognized_tribes_bia.gpkg": [
        "NAME",
        "STATE",
        "AREAACRES"
    ],

    "efh_pacific_fmc.gpkg": [
        "SITENAME_L",
        "TYPE",
        "LIFESTAGE",
        "DATACAVEAT"
    ],

    "underserved_community_areas_USA.gpkg": [
        "GEOID10",
        "SF",
        "CF"
    ],

    "protected_wildlife_habitats_0.gpkg": [
        "NAME",
        "DESIG",
        "STATE"
    ],

    "protected_wildlife_habitats_1.gpkg": [
        "NAME",
        "DESIG",
        "STATE"
    ],

    "protected_wildlife_habitats_2.gpkg": [
        "NAME",
        "DESIG",
        "STATE"
    ],

    "protected_wildlife_habitats_0_points.gpkg": [
        "SPECIES",
        "STATUS",
        "OBS_DATE"
    ],

    "protected_wildlife_habitats_1_points.gpkg": [
        "SPECIES",
        "STATUS",
        "OBS_DATE"
    ],

    "protected_wildlife_habitats_2_points.gpkg": [
        "SPECIES",
        "STATUS",
        "OBS_DATE"
    ],

    "waterfowl_production_areas.gpkg": [
        "NAME",
        "STATE",
        "TYPE"
    ],

    "conservation_easments_cced_2025b_release.gpkg": [
        "EasementHolder",
        "County",
        "State_Nm",
        "Category"
    ],

    "tnc_lands.gpkg": [
        "NAME",
        "STATE",
        "OWNER"
    ],

    "public_lands_blm.gpkg": [
        "ADMIN_UNIT",
        "STATE",
        "MGMT_AGNCY"
    ],

    "national_conservation_areas_blm_natl_nlcs_national_monuments_national_conservation_areas_polygons_371246694430311854.gpkg": [
        "UNIT_NAME",
        "ADMIN_ST",
        "UNIT_TYPE"
    ],

    "national_parks_administrative_boundaries_of_national_park_system_units.gpkg": [
        "UNIT_NAME",
        "UNIT_TYPE",
        "STATE"
    ],

    "parcels_i15_parcels_cvfpb.gpkg": [
        "APN",
        "OWNER_NAME",
        "SITE_ADDR"
    ],

    "scenic_byways.gpkg": [
        "ROUTE_NAME",
        "DESIGNATION",
        "STATE"
    ],

    "unesco_whc001_points.gpkg": [
        "SITE_NAME",
        "CATEGORY",
        "COUNTRY"
    ],

    "CA_geopackage_wetlands.gpkg": [
        "WETLAND_TYPE",
        "WETLAND_SUBTYPE",
        "ACRES"
    ],

    "open_infrastructure_map.gpkg": [
        "NAME",
        "TYPE",
        "STATUS"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Actinopterygii.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Amphibia.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Arachnida.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Aves_2.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Chromista.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Fungi.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Insecta_8.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Mammalia.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Mollusca.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_OtherAnimalia.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Plantae_9.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Protozoa.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],

    "rare_plants_animals_iNaturalist_geomodel_Reptilia.gpkg": [
        "name",
        "rank",
        "iconic_taxon_name"
    ],
}
    SOURCE_LINKS = {
        "tribal_lands_US_Domestic_Sovereign_Nations__Land_Areas_of_Federally-Recognized_Tribes_(BIA).csv": {
            "source_name": "Bureau of Indian Affairs",
            "source_link": "https://www.conservation.gov/datasets/245ffcb63a0b44cb9ed467bbd5f9d7ea_0/about"
        },
        "regional_ppa_averages_proxy.csv": {
            "source_name": "U.S. Energy Information Administration (EIA) – Electricity Price Data (Proxy)",
            "source_link": "https://www.eia.gov/electricity/sales_revenue_price/"
        },
        "bird_habitats_iba_polygons_centroids_points.gpkg": {
            "source_name": "National Audubon Society – Important Bird Areas",
            "source_link": "https://databasin.org/datasets/fdb91971a11d46d39661f0a56c3585ca/"
        },
        "Geopackage Wetlands": {
            "source_name": "U.S. Fish & Wildlife Service",
            "source_link": "https://www.fws.gov/program/national-wetlands-inventory"
        },
        "Water Agency Districts": {
            "source_name": "California Water Boards",
            "source_link": "https://www.waterboards.ca.gov"
        },
        "Tribal Lands": {
        "source_name": "Bureau of Indian Affairs – Tribal Land Areas",
        "source_link": "https://www.conservation.gov/datasets/245ffcb63a0b44cb9ed467bbd5f9d7ea_0/about"
        },
        "Fish Habitats": {
            "source_name": "NOAA Fisheries – Essential Fish Habitat",
            "source_link": "https://www.fisheries.noaa.gov/resource/map/essential-fish-habitat-mapper"
        },
        "Conservation Easments Cced 2025B Release": {
            "source_name": "California Conservation Easement Database (CCED)",
            "source_link": "https://lab.data.ca.gov/dataset/california-conservation-easement-database"
        },
        "Protected Wildlife Habitats 0": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "Protected Wildlife Habitats 1": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "Protected Wildlife Habitats 2": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "Bird Habitat": {
            "source_name": "National Audubon Society – Important Bird Areas",
            "source_link": "https://databasin.org/datasets/fdb91971a11d46d39661f0a56c3585ca/"
        },
        "aquatic_growt_train.csv": {
            "source_name": "Mendeley Data – Algae Bloom Dataset",
            "source_link": "https://data.mendeley.com/datasets/5jb9ffpmvr/1"
        },
        "bird_habitats_iba_polygons_centroids.csv": {
            "source_name": "National Audubon Society – Important Bird Areas",
            "source_link": "https://databasin.org/datasets/fdb91971a11d46d39661f0a56c3585ca/"
        },
        "electric-retail-service-territories.csv": {
            "source_name": "OpenEnergyHub – Electric Retail Service Territories",
            "source_link": "https://openenergyhub.ornl.gov/explore/dataset/electric-retail-service-territories/information/"
        },
        "census_areas_lookup.csv": {
            "source_name": "U.S. Census Bureau",
            "source_link": "https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html"
        },
        "regional_electricity_costs.csv": {
            "source_name": "U.S. Energy Information Administration (EIA)",
            "source_link": "https://www.eia.gov/electricity/sales_revenue_price/"
        },
        "endangered_species_FWS_Species_Data_Explorer.csv": {
            "source_name": "U.S. Fish & Wildlife Service – ECOS Species Database",
            "source_link": "https://ecos.fws.gov/ecp/"
        },
        "expected_energy_nsrdb_lake_isabella_2020.csv": {
            "source_name": "NSRDB",
            "source_link": "https://maps.nrel.gov/nsrdb-viewer/"
        },
        "expected_energy_nsrdb_lake_isabella_2021.csv": {
            "source_name": "NSRDB",
            "source_link": "https://maps.nrel.gov/nsrdb-viewer/"
        },
        "expected_energy_nsrdb_lake_isabella_2022.csv": {
            "source_name": "NSRDB",
            "source_link": "https://maps.nrel.gov/nsrdb-viewer/"
        },
        "expected_energy_nsrdb_lake_isabella_2023.csv": {
            "source_name": "NSRDB",
            "source_link": "https://maps.nrel.gov/nsrdb-viewer/"
        },
        "expected_energy_nsrdb_lake_isabella_2024.csv": {
            "source_name": "NSRDB",
            "source_link": "https://maps.nrel.gov/nsrdb-viewer/"
        },
        "fish_presence_agap_fish_dataset_v2_0.csv": {
            "source_name": "USGS ScienceBase Fish Presence Database",
            "source_link": "https://www.sciencebase.gov/catalog/item/6086df60d34eadd49d31b04a"
        },
        "fish_presence_species_list_v2_0.csv": {
            "source_name": "USGS ScienceBase Fish Presence Database",
            "source_link": "https://www.sciencebase.gov/catalog/item/6086df60d34eadd49d31b04a"
        },
        "parcels_i15_Parcels_CVFPB.csv": {
            "source_name": "California Open Data – i15 Parcels CVFPB",
            "source_link": "https://lab.data.ca.gov/dataset/i15-parcels-cvfpb"
        },
        "protected_wildlife_habitats_WDPA_sources_Mar2026.csv": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "tribal_lands_US_Domestic_Sovereign_Nations%3A_Land_Areas_of_Federally-Recognized_Tribes_(BIA).csv": {
            "source_name": "Bureau of Indian Affairs – Tribal Land Areas",
            "source_link": "https://www.conservation.gov/datasets/245ffcb63a0b44cb9ed467bbd5f9d7ea_0/about"
        },
        "unesco_whc001.csv": {
            "source_name": "UNESCO DataHub – World Heritage List",
            "source_link": "https://data.unesco.org/explore/dataset/whc001/map/"
        },
        "CA_geopackage_wetlands.gpkg": {
            "source_name": "U.S. Fish & Wildlife Service",
            "source_link": "https://www.fws.gov/program/national-wetlands-inventory"
        },
        "ca_water_agency_districts.gpkg": {
            "source_name": "California Water Boards",
            "source_link": "https://www.waterboards.ca.gov"
        },
        "conservation_easments_cced_2025b_release.gpkg": {
            "source_name": "California Conservation Easement Database (CCED)",
            "source_link": "https://lab.data.ca.gov/dataset/california-conservation-easement-database"
        },
        "efh_pacific_fmc.gpkg": {
            "source_name": "NOAA Fisheries – Essential Fish Habitat",
            "source_link": "https://www.fisheries.noaa.gov/resource/map/essential-fish-habitat-mapper"
        },
        "national_conservation_areas_blm_natl_nlcs_national_monuments_national_conservation_areas_polygons_371246694430311854.gpkg": {
            "source_name": "Bureau of Land Management",
            "source_link": "https://www.blm.gov"
        },
        "national_parks_administrative_boundaries_of_national_park_system_units.gpkg": {
            "source_name": "National Park Service – IRMA DataStore",
            "source_link": "https://irma.nps.gov/DataStore/Reference/Profile/2316744"
        },
        "open_infrastructure_map.gpkg": {
            "source_name": "OpenInfraMap / OpenStreetMap",
            "source_link": "https://openinframap.org"
        },
        "parcels_i15_parcels_cvfpb.gpkg": {
            "source_name": "California Open Data – i15 Parcels CVFPB",
            "source_link": "https://lab.data.ca.gov/dataset/i15-parcels-cvfpb"
        },
        "protected_wildlife_habitats_0.gpkg": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "protected_wildlife_habitats_1.gpkg": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "protected_wildlife_habitats_2.gpkg": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "protected_wildlife_habitats_0_points.gpkg": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "protected_wildlife_habitats_1_points.gpkg": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "protected_wildlife_habitats_2_points.gpkg": {
            "source_name": "Protected Planet / WDPA",
            "source_link": "https://www.protectedplanet.net"
        },
        "public_lands_blm.gpkg": {
            "source_name": "Bureau of Land Management",
            "source_link": "https://www.blm.gov"
        },
        "rare_plants_animals_iNaturalist_geomodel_Actinopterygii.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Amphibia.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Arachnida.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Aves_2.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Chromista.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Fungi.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Insecta_8.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Mammalia.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Mollusca.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_OtherAnimalia.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Plantae_9.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Protozoa.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "rare_plants_animals_iNaturalist_geomodel_Reptilia.gpkg": {
            "source_name": "iNaturalist",
            "source_link": "https://www.inaturalist.org/pages/range_maps"
        },
        "regional_us_census_counties_tiger.gpkg": {
            "source_name": "U.S. Census Bureau – TIGER/Line",
            "source_link": "https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html"
        },
        "scenic_byways.gpkg": {
            "source_name": "U.S. Department of Transportation – Scenic Byways",
            "source_link": "https://geo.dot.gov/server/rest/services/US_Scenic_Byways/MapServer"
        },
        "tnc_lands.gpkg": {
            "source_name": "The Nature Conservancy – TNC Lands",
            "source_link": "https://experience.arcgis.com/experience/ff2f358c8ac743f68ec3bfc9f9b28aa6"
        },
        "tribal_lands.gpkg": {
            "source_name": "Bureau of Indian Affairs – Tribal Land Areas",
            "source_link": "https://www.conservation.gov/datasets/245ffcb63a0b44cb9ed467bbd5f9d7ea_0/about"
        },
        "tribal_lands_US_Domestic_Sovereign_Nations__Land_Areas_of_Federally-Recognized_Tribes_(BIA).csv": {
            "source_name": "Bureau of Indian Affairs – Tribal Land Areas",
            "source_link": "https://www.conservation.gov/datasets/245ffcb63a0b44cb9ed467bbd5f9d7ea_0/about"
        },
        "underserved_community_areas_USA.gpkg": {
            "source_name": "Underserved Communities Dataset",
            "source_link": "https://public-environmental-data-partners.github.io/j40-cejst-2/en/"
        },
        "unesco_whc001_points.gpkg": {
            "source_name": "UNESCO DataHub – World Heritage List",
            "source_link": "https://data.unesco.org/explore/dataset/whc001/map/"
        },
        "utilities_electric_service_territories.gpkg": {
            "source_name": "OpenEnergyHub – Electric Retail Service Territories",
            "source_link": "https://openenergyhub.ornl.gov/explore/dataset/electric-retail-service-territories/information/"
        },
        "water_municipality_i03_waterdistricts.gpkg": {
            "source_name": "Water Districts Dataset",
            "source_link": "https://data.ca.gov/dataset/i03-waterdistricts"
        },
        "waterfowl_production_areas.gpkg": {
            "source_name": "U.S. Fish & Wildlife Service – Waterfowl Production Areas (ArcGIS Hosted Layer)",
            "source_link": "https://arcgis.dnr.state.mn.us/host/rest/services/Hosted/USFWS_Waterfowl_Production_Areas_Current_/FeatureServer/0"
        },
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("HydroGlow GUI")
        self.setGeometry(100, 100, 1200, 800)
        
        # Initialize layer references
        self.marker_layer = None
        self.buffer_layer = None
        self.results_layer = None
        self.waterbody_layers = []  # List of waterbody layers
        self.selected_waterbody_layer = None  # Layer for highlighting selected waterbody
        self.nearest_agency_layer = None  # Layer for highlighting nearest water agency district
        self.buildable_area_layer = None  # Layer for buildable area polygons
        self.heatmap_layer = None  # Layer for suitability heatmap raster
        self.substation_path_layer = None  # LineString: waterbody → substation
        self.nearest_substation_layer = None  # Marker for the chosen substation
        self._heatmap_legend = None  # Legend overlay widget
        self._heatmap_max_val = None  # Max overlap value for legend
        
        # Metadata polygon data: list of dicts, each with:
        #   "checkbox", "filepath", "source_layer", "results_layer",
        #   "display_name", "fill_color", "outline_color"
        self.metadata_polygon_entries = []
        self.metadata_csv_tables = {}
        
        # Load regional electricity and PPA data
        self.load_regional_data()
        
        # Create central widget and layout
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # Create vertical splitter to separate map area from metadata panel
        self.vertical_splitter = QSplitter(Qt.Vertical)
        
        # Create top container for search panel and map
        top_container = QWidget()
        top_layout = QHBoxLayout(top_container)
        top_layout.setContentsMargins(0, 0, 0, 0)
        
        # Create horizontal splitter for resizable panels (search and map)
        self.splitter = QSplitter(Qt.Horizontal)
        
        # Create left panel for search criteria
        self.search_panel = self.create_search_panel()
        self.splitter.addWidget(self.search_panel)
        
        # Create map canvas
        self.canvas = QgsMapCanvas()
        self.canvas.setCanvasColor(Qt.white)
        self.splitter.addWidget(self.canvas)
        
        # Set initial sizes (search panel: 350px, map: remaining space)
        self.splitter.setSizes([350, 850])

        # Set minimum widths
        self.search_panel.setMinimumWidth(280)
        self.canvas.setMinimumWidth(400)

        # Prevent long data source names from stretching the search panel
        self.splitter.setStretchFactor(0, 0)  # search panel doesn't stretch
        self.splitter.setStretchFactor(1, 1)  # map gets all extra space
        
        # Add horizontal splitter to top container
        top_layout.addWidget(self.splitter)
        
        # Add top container to vertical splitter
        self.vertical_splitter.addWidget(top_container)
        
        # Create metadata panel at bottom
        self.metadata_panel = self.create_metadata_panel()
        self.vertical_splitter.addWidget(self.metadata_panel)
        
        # Set initial sizes for vertical splitter (map area: 600px, metadata: 150px)
        self.vertical_splitter.setSizes([600, 150])
        
        # Add vertical splitter to main layout
        main_layout.addWidget(self.vertical_splitter)
        
        # Create toolbar
        self.create_toolbar()
        
        # Initialize tools
        self.pan_tool = QgsMapToolPan(self.canvas)
        self.pick_tool = QgsMapToolEmitPoint(self.canvas)
        self.pick_tool.canvasClicked.connect(self.on_map_clicked)
        
        # Create waterbody selection tool
        self.select_waterbody_tool = QgsMapToolEmitPoint(self.canvas)
        self.select_waterbody_tool.canvasClicked.connect(self.on_waterbody_clicked)
        
        # Create single buildable area tool
        self.single_buildable_tool = QgsMapToolEmitPoint(self.canvas)
        self.single_buildable_tool.canvasClicked.connect(self.on_single_buildable_waterbody_clicked)

        # Create heatmap selection tool
        self.heatmap_tool = QgsMapToolEmitPoint(self.canvas)
        self.heatmap_tool.canvasClicked.connect(self.on_heatmap_waterbody_clicked)

        # Create "Connect to Substation" selection tool
        self.connect_substation_tool = QgsMapToolEmitPoint(self.canvas)
        self.connect_substation_tool.canvasClicked.connect(self.on_connect_substation_clicked)

        self.canvas.setMapTool(self.pan_tool)
        
        # Add a sample OpenStreetMap layer
        self.add_osm_layer()
        
        # Try to load waterbody data from default location
        self.try_load_default_waterbody_data()
    
    def load_regional_data(self):
        """Load regional electricity costs and PPA averages from CSV files"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        metadata_text_folder = os.path.join(script_dir, "metadata_text")
        
        # Initialize data dictionaries
        self.state_to_census_division = {}  # state -> census_division
        self.state_electricity_costs = {}   # state -> avg_price_cents_per_kwh
        self.division_ppa_averages = {}     # census_division -> avg_price_cents_per_kwh_proxy
        
        # Check if the folder exists
        if not os.path.exists(metadata_text_folder):
            print(f"Metadata text folder not found: metadata_text/")
            return
        
        # Load census areas lookup
        census_file = os.path.join(metadata_text_folder, "census_areas_lookup.csv")
        if os.path.exists(census_file):
            try:
                with open(census_file, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        state = row['state'].strip()
                        division = row['census_division'].strip()
                        self.state_to_census_division[state] = division
            except Exception as e:
                print(f"Error loading census_areas_lookup.csv: {e}")
        
        # Load regional electricity costs
        electricity_file = os.path.join(metadata_text_folder, "regional_electricity_costs.csv")
        if os.path.exists(electricity_file):
            try:
                with open(electricity_file, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        state = row['state'].strip()
                        try:
                            cost = float(row['avg_price_cents_per_kwh'])
                            self.state_electricity_costs[state] = cost
                        except (ValueError, KeyError):
                            pass
            except Exception as e:
                print(f"Error loading regional_electricity_costs.csv: {e}")
        
        # Load regional PPA averages
        ppa_file = os.path.join(metadata_text_folder, "regional_ppa_averages_proxy.csv")
        if os.path.exists(ppa_file):
            try:
                with open(ppa_file, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        division = row['census_division'].strip()
                        try:
                            ppa = float(row['avg_price_cents_per_kwh_proxy'])
                            self.division_ppa_averages[division] = ppa
                        except (ValueError, KeyError):
                            pass
            except Exception as e:
                print(f"Error loading regional_ppa_averages_proxy.csv: {e}")

        # Load all metadata_text CSV files for waterbody metadata lookup
        self.metadata_csv_tables = {}

        for filename in sorted(os.listdir(metadata_text_folder)):
            if not filename.lower().endswith(".csv"):
                continue

            filepath = os.path.join(metadata_text_folder, filename)

            try:
                with open(filepath, "r", newline="", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    rows = [row for row in reader]

                    self.metadata_csv_tables[filename] = {
                        "filepath": filepath,
                        "rows": rows,
                        "fieldnames": reader.fieldnames or [],
                    }

                print(f"Loaded metadata CSV: {filename} ({len(rows)} rows)")

            except Exception as e:
                print(f"Error loading metadata CSV {filename}: {e}")
                
    def get_regional_energy_data(self, feature, source_layer=None):
        """
        Get regional electricity cost and PPA average for a feature.
        First tries to find state from attributes, then falls back to coordinate-based lookup.
        Returns: (electricity_cost, ppa_average, census_division, state) or (None, None, None, None)
        """
        field_names = [field.name().lower() for field in feature.fields()]
        attributes = feature.attributes()
        
        # Create a mapping of lowercase field names to values
        field_values = {}
        for i, field in enumerate(feature.fields()):
            field_values[field.name().lower()] = attributes[i]
        
        # Try to find state from various possible field names
        state = None
        state_field_names = ['state', 'state_name', 'statename', 'st', 'state_abbr', 'stateabbr']
        
        for field_name in state_field_names:
            if field_name in field_values:
                value = field_values[field_name]
                if value and str(value).strip():
                    state = str(value).strip()
                    break
        
        # If no state found in attributes, try to determine from coordinates
        if not state:
            state = self.get_state_from_coordinates(feature, source_layer)
        
        if not state:
            return None, None, None, None
        
        # Get census division for the state
        census_division = self.state_to_census_division.get(state)
        
        # Get electricity cost for the state
        electricity_cost = self.state_electricity_costs.get(state)
        
        # Get PPA average for the census division
        ppa_average = None
        if census_division:
            ppa_average = self.division_ppa_averages.get(census_division)
        
        return electricity_cost, ppa_average, census_division, state
    
    def get_waterbody_match_values(self, feature):
        """
        Build a safer set of match values from the selected waterbody.
        Only use likely waterbody names/types, not generic IDs.
        """
        values = set()

        safe_name_fields = [
            "name", "gnis_name", "waterbody", "waterbody_name",
            "lake_name", "reservoir_name", "feature_name"
        ]

        for i, field in enumerate(feature.fields()):
            field_name = field.name().lower()
            value = feature.attributes()[i]

            if value is None or value == NULL:
                continue

            text = str(value).strip()
            if not text:
                continue

            if field_name in safe_name_fields:
                values.add(self.normalize_waterbody_name(text))

        return values
    
    def get_waterbody_core_info(self, feature, source_layer=None):
        """
        Extract the most important waterbody info for safer CSV matching.
        """
        info = {
            "name": None,
            "state": None,
            "county": None,
            "type": None,
            "lat": None,
            "lon": None,
        }

        field_names = [field.name().lower() for field in feature.fields()]

        for i, field in enumerate(feature.fields()):
            field_name = field.name().lower()
            value = feature.attributes()[i]

            if value is None or value == NULL:
                continue

            text = str(value).strip()
            if not text:
                continue

            if field_name in ["name", "gnis_name", "waterbody", "waterbody_name"] and not info["name"]:
                info["name"] = text
            elif field_name in ["state", "state_name", "statename"] and not info["state"]:
                info["state"] = text
            elif field_name in ["county", "county_name"] and not info["county"]:
                info["county"] = text
            elif field_name in ["waterbody_type", "type"] and not info["type"]:
                info["type"] = text

        if not info["state"]:
            info["state"] = self.get_state_from_coordinates(feature, source_layer)

        geom = feature.geometry()
        if geom and not geom.isEmpty():
            centroid = geom.centroid().asPoint()

            if source_layer:
                layer_crs = source_layer.crs()
                crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
                if layer_crs != crs_4326:
                    transform = QgsCoordinateTransform(layer_crs, crs_4326, QgsProject.instance())
                    centroid = transform.transform(centroid)

            info["lon"] = centroid.x()
            info["lat"] = centroid.y()

        return info

    def normalize_key(self, key):
        if key is None:
            return ""
        return str(key).strip().lower().replace("_", " ")
    
    def normalize_waterbody_name(self, name):
        if not name:
            return ""

        text = str(name).strip().lower()

        # Handle Lake Isabella vs Isabella Lake
        if text.startswith("lake "):
            text = text.replace("lake ", "", 1) + " lake"

        return text
    def filename_matches_waterbody(self, filename, waterbody_values):
        file_text = os.path.splitext(filename)[0].lower()
        file_text = file_text.replace("_", " ").replace("-", " ")

        for wb_name in waterbody_values:
            wb_words = wb_name.replace("_", " ").split()

            if all(word in file_text for word in wb_words):
                return True

        return False
    def row_matches_waterbody(self, normalized_row, waterbody_info, waterbody_values):
        """
        Safer matching:
        1. Prefer explicit waterbody/site/name fields
        2. Require state agreement when state exists
        3. Never match on generic id/objectid/fid alone
        """
        row_state = None
        for state_key in ["state", "state name", "site state", "state_abbr"]:
            if state_key in normalized_row and normalized_row[state_key]:
                row_state = str(normalized_row[state_key]).strip()
                break

        # If row has a state and waterbody has a state, they must agree.
        if row_state and waterbody_info["state"]:
            row_state_lower = row_state.strip().lower()
            wb_state_lower = waterbody_info["state"].strip().lower()

            state_aliases = {
                "california": {"california", "ca"},
                "north carolina": {"north carolina", "nc"},
                "south carolina": {"south carolina", "sc"},
                "new york": {"new york", "ny"},
                "texas": {"texas", "tx"},
            }

            row_group = None
            wb_group = None

            for full_name, aliases in state_aliases.items():
                if row_state_lower in aliases:
                    row_group = full_name
                if wb_state_lower in aliases:
                    wb_group = full_name

            if row_group and wb_group:
                if row_group != wb_group:
                    return False
            elif row_state_lower != wb_state_lower:
                return False

        name_like_fields = [
            "name", "site name", "national name", "waterbody", "waterbody name",
            "lake name", "reservoir name", "feature name", "name en"
        ]

        for col in name_like_fields:
            if col in normalized_row and normalized_row[col]:
                row_value = self.normalize_waterbody_name(normalized_row[col])
                if row_value in waterbody_values:
                    return True

        return False

    def get_display_fields_for_csv(self, filename, row):
        """
        Return only the allowed fields for this CSV.
        """
        allowed = self.CSV_DISPLAY_FIELDS.get(filename, [])
        if not allowed:
            return []

        result = []
        normalized_lookup = {}

        for key, value in row.items():
            norm_key = self.normalize_key(key)
            normalized_lookup[norm_key] = (key, value)

        for field_name in allowed:
            norm_field = self.normalize_key(field_name)
            if norm_field in normalized_lookup:
                original_key, value = normalized_lookup[norm_field]
                if value is None:
                    continue
                value_str = str(value).strip()
                if not value_str:
                    continue
                if norm_field in self.CSV_ALWAYS_IGNORE_FIELDS:
                    continue
                if value_str.upper() in ["NOT AVAILABLE", "N/A", "NULL", "<NULL>"]:
                    continue
                if value_str == "-999999.0":
                    continue
                result.append((original_key, value_str))

        return result
    
    def find_matching_csv_rows_for_waterbody(self, feature, source_layer=None):
        """
        Search loaded metadata CSVs for rows that safely match the selected waterbody.
        Generic IDs are ignored. Matching is based mainly on waterbody/site/name fields
        plus basic state agreement when available.
        """
        if not self.metadata_csv_tables:
            return {}

        waterbody_values = self.get_waterbody_match_values(feature)
        waterbody_info = self.get_waterbody_core_info(feature, source_layer)

        if not waterbody_values and not waterbody_info["state"]:
            return {}

        matched = {}

        for filename, table_info in self.metadata_csv_tables.items():
            rows = table_info.get("rows", [])
            file_matches = []

            if self.filename_matches_waterbody(filename, waterbody_values):
                if rows:
                    matched[filename] = [rows[0]]
                continue
        for filename, table_info in self.metadata_csv_tables.items():
            rows = table_info.get("rows", [])
            file_matches = []

            for row in rows:
                normalized_row = {}
                for key, value in row.items():
                    if key is None:
                        continue
                    normalized_row[self.normalize_key(key)] = value

                if self.row_matches_waterbody(normalized_row, waterbody_info, waterbody_values):
                    file_matches.append(row)

                if len(file_matches) >= 1:
                    break

            if file_matches:
                matched[filename] = file_matches

        return matched
    def get_displayable_gpkg_fields(self, filename, feature):
        """
        Return useful attribute fields from a GPKG feature.
        If the file has a preferred-field list, use that first.
        Otherwise fall back to generic filtering.
        """
        results = []

        preferred_fields = self.GPKG_PREFERRED_FIELDS.get(filename, [])
        feature_field_names = feature.fields().names()

        if preferred_fields:
            for field_name in preferred_fields:
                if field_name not in feature_field_names:
                    continue

                value = feature[field_name]

                if value is None or value == NULL:
                    continue

                value_str = str(value).strip()
                if not value_str:
                    continue

                if value_str.upper() in ["NOT AVAILABLE", "N/A", "NULL", "<NULL>"]:
                    continue

                results.append((field_name, value_str))

            if results:
                return results

        for field in feature.fields():
            field_name = field.name()
            norm_name = field_name.strip().lower()

            if norm_name in self.GPKG_ALWAYS_IGNORE_FIELDS:
                continue

            value = feature[field_name]

            if value is None or value == NULL:
                continue

            value_str = str(value).strip()
            if not value_str:
                continue

            if value_str.upper() in ["NOT AVAILABLE", "N/A", "NULL", "<NULL>"]:
                continue

            results.append((field_name, value_str))

        return results
    def get_source_info(self, source_value):
        """
        Convert a filename or current source label into:
        - a clean source name
        - a source link
        """
        if not source_value:
            return {"source_name": "", "source_link": ""}

        key = os.path.basename(str(source_value).strip())

        if key in self.SOURCE_LINKS:
            return self.SOURCE_LINKS[key]

        if key == "metadata polygon relation":
            return {
                "source_name": "HydroGlow Spatial Relation Logic",
                "source_link": ""
            }

        if key == "feature geometry / metadata lookup":
            return {
                "source_name": "HydroGlow Derived Lookup",
                "source_link": ""
            }

        if key == "status / not implemented":
            return {
                "source_name": "HydroGlow Status",
                "source_link": ""
            }

        if key == "Found Waterbodies":
            return {
                "source_name": "HydroGlow Waterbody Results",
                "source_link": ""
            }
        if key == "metadata lookup":
            return {
                "source_name": "HydroGlow Derived Lookup",
                "source_link": ""
            }
        if key == "not implemented":
            return {
                "source_name": "HydroGlow Status",
                "source_link": ""
            }
        if key == "HydroGlow":
            return {
                "source_name": "HydroGlow Derived Data",
                "source_link": ""
            }
        return {
            "source_name": key,
            "source_link": ""
        }

    def group_overlapping_gpkg_features(self, overlapping_polygons):
        """
        Group overlapping polygon features by source GPKG filename.
        """
        grouped = {}

        if not overlapping_polygons:
            return grouped

        for overlap in overlapping_polygons:
            entry = overlap.get("entry")
            feature = overlap.get("feature")

            if not entry or not feature:
                continue

            filepath = entry.get("filepath", "")
            filename = os.path.basename(filepath) if filepath else entry.get("display_name", "Unknown Layer")

            if filename not in grouped:
                grouped[filename] = []

            grouped[filename].append(feature)

        return grouped
    def group_polygon_features_with_proximity(self, overlapping_polygons, nearby_polygons):
        """
        Group polygon features by source GPKG filename and track whether each file
        contains Direct matches, Nearby matches, or both.
        """
        grouped = {}

        def add_feature(match, proximity_label):
            entry = match.get("entry")
            feature = match.get("feature")

            if not entry or not feature:
                return

            filepath = entry.get("filepath", "")
            filename = os.path.basename(filepath) if filepath else entry.get("display_name", "Unknown Layer")

            if filename not in grouped:
                grouped[filename] = {
                    "features": [],
                    "proximity_labels": set(),
                }

            grouped[filename]["features"].append(feature)
            grouped[filename]["proximity_labels"].add(proximity_label)

        if overlapping_polygons:
            for overlap in overlapping_polygons:
                add_feature(overlap, "Direct")

        if nearby_polygons:
            for nearby in nearby_polygons:
                add_feature(nearby, "Nearby")

        return grouped
    def find_nearby_point_features_for_waterbody(self, feature, source_layer=None, max_results=5, max_distance_km=30.0):
        """
        Find nearby point features from checked point-based GPKG layers.
        Returns a dict:
            {
                "filename.gpkg": [
                    {"feature": feat, "distance_km": 1.23, "entry": entry},
                    ...
                ]
            }
        """
        nearby = {}

        geom = feature.geometry()
        if not geom or geom.isEmpty():
            return nearby

        waterbody_geom = QgsGeometry(geom)

        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")

        # Transform selected waterbody to WGS84 for distance checks
        if source_layer and source_layer.crs() != crs_4326:
            transform = QgsCoordinateTransform(source_layer.crs(), crs_4326, QgsProject.instance())
            waterbody_geom.transform(transform)

        for entry in self.metadata_polygon_entries:
            if not entry["checkbox"].isChecked():
                continue

            if entry.get("geom_type") != QgsWkbTypes.PointGeometry:
                continue

            point_layer = entry.get("source_layer")
            if not point_layer or not point_layer.isValid():
                continue

            layer_geom_type = point_layer.geometryType()
            if layer_geom_type != QgsWkbTypes.PointGeometry:
                continue

            layer_crs = point_layer.crs()
            filename = os.path.basename(entry.get("filepath", "")) or entry.get("display_name", "Unknown Point Layer")

            candidates = []

            for pt_feature in point_layer.getFeatures():
                pt_geom = pt_feature.geometry()
                if not pt_geom or pt_geom.isEmpty():
                    continue

                pt_geom_wgs84 = QgsGeometry(pt_geom)
                if layer_crs != crs_4326:
                    transform = QgsCoordinateTransform(layer_crs, crs_4326, QgsProject.instance())
                    pt_geom_wgs84.transform(transform)

                # Distance from point to selected waterbody polygon
                distance_deg = pt_geom_wgs84.distance(waterbody_geom)

                # Rough conversion degrees -> km
                distance_km = distance_deg * 111.0

                # Also allow points directly inside the polygon
                inside = waterbody_geom.intersects(pt_geom_wgs84)

                if inside or distance_km <= max_distance_km:
                    candidates.append({
                        "feature": pt_feature,
                        "distance_km": 0.0 if inside else round(distance_km, 2),
                        "entry": entry,
                    })

            candidates.sort(key=lambda x: x["distance_km"])

            if candidates:
                nearby[filename] = candidates[:max_results]

        return nearby
    def safe_float(self, value):
        try:
            if value is None:
                return None
            text = str(value).strip()
            if not text:
                return None
            return float(text)
        except Exception:
            return None

    def haversine_km(self, lat1, lon1, lat2, lon2):
        import math

        r = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)

        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return r * c

    def get_row_coordinate(self, row, possible_fields):
        normalized = {}
        for key, value in row.items():
            normalized[self.normalize_key(key)] = value

        for field_name in possible_fields:
            norm = self.normalize_key(field_name)
            if norm in normalized:
                val = self.safe_float(normalized[norm])
                if val is not None:
                    return val
        return None

    def find_nearby_csv_rows_for_waterbody(self, feature, source_layer=None):
        """
        Find nearby CSV rows based on waterbody centroid distance.
        This is for datasets that are better associated by location than by exact name.
        """
        waterbody_info = self.get_waterbody_core_info(feature, source_layer)

        if waterbody_info["lat"] is None or waterbody_info["lon"] is None:
            return {}

        wb_lat = waterbody_info["lat"]
        wb_lon = waterbody_info["lon"]

        nearby_matches = {}

        for filename, config in self.CSV_NEARBY_CONFIG.items():
            if filename not in self.metadata_csv_tables:
                continue

            rows = self.metadata_csv_tables[filename].get("rows", [])
            max_distance_km = config.get("max_distance_km", 8.05)

            candidates = []

            for row in rows:
                row_lat = self.get_row_coordinate(row, config.get("lat_fields", []))
                row_lon = self.get_row_coordinate(row, config.get("lon_fields", []))

                if row_lat is None or row_lon is None:
                    continue

                # Handle projected X/Y fields for bird habitat file
                if filename == "bird_habitats_iba_polygons_centroids.csv":
                    # If X/Y look like projected coordinates, skip them here unless proper reprojection is added
                    if abs(row_lat) > 90 or abs(row_lon) > 180:
                        continue

                distance_km = self.haversine_km(wb_lat, wb_lon, row_lat, row_lon)

                if distance_km <= max_distance_km:
                    row_copy = dict(row)
                    row_copy["_distance_km"] = round(distance_km, 2)
                    candidates.append(row_copy)

            candidates.sort(key=lambda r: r.get("_distance_km", 999999))

            if candidates:
                nearby_matches[filename] = candidates[:3]

        return nearby_matches
    def get_state_from_coordinates(self, feature, source_layer=None):
        """
        Determine the US state based on the feature's coordinates.
        Uses the centroid of the feature and checks against state bounding boxes.
        """
        geom = feature.geometry()
        if not geom or geom.isEmpty():
            return None
        
        # Get centroid
        centroid = geom.centroid().asPoint()
        
        # Transform to WGS84 (EPSG:4326) if needed
        if source_layer:
            layer_crs = source_layer.crs()
            crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
            if layer_crs != crs_4326:
                transform = QgsCoordinateTransform(layer_crs, crs_4326, QgsProject.instance())
                centroid = transform.transform(centroid)
        
        lon = centroid.x()
        lat = centroid.y()
        
        # Use approximate state bounding boxes to determine state
        # Format: (min_lon, max_lon, min_lat, max_lat)
        state_bounds = {
            'Alabama': (-88.47, -84.89, 30.22, 35.01),
            'Alaska': (-179.15, -129.98, 51.21, 71.35),
            'Arizona': (-114.82, -109.05, 31.33, 37.00),
            'Arkansas': (-94.62, -89.64, 33.00, 36.50),
            'California': (-124.41, -114.13, 32.53, 42.01),
            'Colorado': (-109.06, -102.04, 36.99, 41.00),
            'Connecticut': (-73.73, -71.79, 40.95, 42.05),
            'Delaware': (-75.79, -75.05, 38.45, 39.84),
            'District of Columbia': (-77.12, -76.91, 38.79, 38.99),
            'Florida': (-87.63, -80.03, 24.52, 31.00),
            'Georgia': (-85.61, -80.84, 30.36, 35.00),
            'Hawaii': (-160.24, -154.81, 18.91, 22.23),
            'Idaho': (-117.24, -111.04, 41.99, 49.00),
            'Illinois': (-91.51, -87.50, 36.97, 42.51),
            'Indiana': (-88.10, -84.78, 37.77, 41.76),
            'Iowa': (-96.64, -90.14, 40.38, 43.50),
            'Kansas': (-102.05, -94.59, 36.99, 40.00),
            'Kentucky': (-89.57, -81.96, 36.50, 39.15),
            'Louisiana': (-94.04, -88.82, 28.93, 33.02),
            'Maine': (-71.08, -66.95, 43.06, 47.46),
            'Maryland': (-79.49, -75.05, 37.91, 39.72),
            'Massachusetts': (-73.50, -69.93, 41.24, 42.89),
            'Michigan': (-90.42, -82.42, 41.70, 48.19),
            'Minnesota': (-97.24, -89.49, 43.50, 49.38),
            'Mississippi': (-91.66, -88.10, 30.17, 35.00),
            'Missouri': (-95.77, -89.10, 35.99, 40.61),
            'Montana': (-116.05, -104.04, 44.36, 49.00),
            'Nebraska': (-104.05, -95.31, 40.00, 43.00),
            'Nevada': (-120.00, -114.04, 35.00, 42.00),
            'New Hampshire': (-72.56, -70.70, 42.70, 45.31),
            'New Jersey': (-75.56, -73.89, 38.93, 41.36),
            'New Mexico': (-109.05, -103.00, 31.33, 37.00),
            'New York': (-79.76, -71.86, 40.50, 45.02),
            'North Carolina': (-84.32, -75.46, 33.84, 36.59),
            'North Dakota': (-104.05, -96.55, 45.94, 49.00),
            'Ohio': (-84.82, -80.52, 38.40, 42.00),
            'Oklahoma': (-103.00, -94.43, 33.62, 37.00),
            'Oregon': (-124.57, -116.46, 41.99, 46.29),
            'Pennsylvania': (-80.52, -74.69, 39.72, 42.27),
            'Rhode Island': (-71.86, -71.12, 41.15, 42.02),
            'South Carolina': (-83.35, -78.54, 32.03, 35.22),
            'South Dakota': (-104.06, -96.44, 42.48, 45.95),
            'Tennessee': (-90.31, -81.65, 34.98, 36.68),
            'Texas': (-106.65, -93.51, 25.84, 36.50),
            'Utah': (-114.05, -109.04, 37.00, 42.00),
            'Vermont': (-73.44, -71.46, 42.73, 45.02),
            'Virginia': (-83.68, -75.24, 36.54, 39.47),
            'Washington': (-124.73, -116.92, 45.54, 49.00),
            'West Virginia': (-82.64, -77.72, 37.20, 40.64),
            'Wisconsin': (-92.89, -86.25, 42.49, 47.08),
            'Wyoming': (-111.06, -104.05, 40.99, 45.01),
        }
        
        # Find the state that contains this point
        best_match = None
        best_distance = float('inf')
        
        for state_name, (min_lon, max_lon, min_lat, max_lat) in state_bounds.items():
            if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
                # Point is within bounding box
                # Calculate distance to center for tie-breaking
                center_lon = (min_lon + max_lon) / 2
                center_lat = (min_lat + max_lat) / 2
                distance = ((lon - center_lon) ** 2 + (lat - center_lat) ** 2) ** 0.5
                
                if distance < best_distance:
                    best_distance = distance
                    best_match = state_name
        
        return best_match
    
    def create_metadata_panel(self):
        """Create the bottom panel to display selected waterbody metadata"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Header with title and clear button
        header_layout = QHBoxLayout()
        
        title = QLabel("Selected Waterbody Metadata")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        header_layout.addWidget(title)
        
        header_layout.addStretch()
        
        clear_selection_btn = QPushButton("Clear Selection")
        clear_selection_btn.clicked.connect(self.clear_waterbody_selection)
        header_layout.addWidget(clear_selection_btn)
        
        layout.addLayout(header_layout)
        
        # Metadata table
        self.metadata_table = QTableWidget()
        self.metadata_table.setColumnCount(5)
        self.metadata_table.setHorizontalHeaderLabels(["Field", "Value", "Proximity", "Source", "Source Link"])
        self.metadata_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.metadata_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.metadata_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.metadata_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.metadata_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.metadata_table.setAlternatingRowColors(True)
        self.metadata_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.metadata_table.setSelectionBehavior(QTableWidget.SelectRows)
        
        # Set placeholder message
        self.metadata_table.setRowCount(1)
        self.metadata_table.setItem(0, 0, QTableWidgetItem("No waterbody selected"))
        self.metadata_table.setItem(0, 1, QTableWidgetItem("Click on a waterbody polygon to view its metadata"))
        self.metadata_table.setItem(0, 2, QTableWidgetItem(""))
        self.metadata_table.setItem(0, 3, QTableWidgetItem(""))
        self.metadata_table.setItem(0, 4, QTableWidgetItem(""))
        self.metadata_table.cellClicked.connect(self.on_metadata_table_cell_clicked)

        layout.addWidget(self.metadata_table)
        
        return panel
    def on_metadata_table_cell_clicked(self, row, column):
        """
        Open the source link when the Source Link column is clicked.
        """
        if column != 4:
            return

        item = self.metadata_table.item(row, column)
        if not item:
            return

        link = item.data(Qt.UserRole)
        if link:
            QDesktopServices.openUrl(QUrl(link))

    def create_search_panel(self):
        # Create scroll area for the search panel
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        # Create search panel widget — set minimum width to 0 so long
        # checkbox labels don't force the panel to expand horizontally
        search_panel = QWidget()
        search_panel.setMinimumWidth(0)
        search_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        search_layout = QVBoxLayout(search_panel)
        
        # Title
        title = QLabel("Search Criteria")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        search_layout.addWidget(title)
        
        # Add separator
        search_layout.addWidget(self.create_separator())
        
        # ===== LOCATION (REQUIRED) SECTION =====
        location_title = QLabel("Location (Required)")
        location_title.setStyleSheet("font-weight: bold; color: #d32f2f;")
        search_layout.addWidget(location_title)
        
        # Latitude input
        lat_layout = QHBoxLayout()
        lat_layout.addWidget(QLabel("Latitude (DD):"))
        self.latitude_input = QLineEdit()
        self.latitude_input.editingFinished.connect(self.on_lat_lon_changed)
        lat_layout.addWidget(self.latitude_input)
        search_layout.addLayout(lat_layout)

        # Longitude input
        lon_layout = QHBoxLayout()
        lon_layout.addWidget(QLabel("Longitude (DD):"))
        self.longitude_input = QLineEdit()
        self.longitude_input.editingFinished.connect(self.on_lat_lon_changed)
        lon_layout.addWidget(self.longitude_input)
        search_layout.addLayout(lon_layout)
        
        # Pick from map button
        pick_location_btn = QPushButton("📍 Pick Location from Map")
        pick_location_btn.clicked.connect(self.activate_pick_location)
        search_layout.addWidget(pick_location_btn)
        
        # Buffer radius input
        buffer_layout = QHBoxLayout()
        buffer_layout.addWidget(QLabel("Buffer Radius:"))
        self.buffer_input = QLineEdit()
        self.buffer_input.setPlaceholderText("miles")
        self.buffer_input.editingFinished.connect(self.on_location_fields_changed)
        buffer_layout.addWidget(self.buffer_input)
        search_layout.addLayout(buffer_layout)

        # Show/hide buffer circle checkbox
        self.show_buffer_checkbox = QCheckBox("Show Buffer Circle")
        self.show_buffer_checkbox.setChecked(False)
        self.show_buffer_checkbox.setEnabled(False)
        self.show_buffer_checkbox.stateChanged.connect(self.toggle_buffer_visibility)
        search_layout.addWidget(self.show_buffer_checkbox)

        # Add separator
        search_layout.addWidget(self.create_separator())
        
        # ===== OPTIONAL FILTERS SECTION (collapsible) =====
        self.optional_filters_btn = QPushButton("\u25B8 Optional Filters")
        self.optional_filters_btn.setStyleSheet("font-weight: bold; text-align: left; padding: 4px;")
        self.optional_filters_btn.setFlat(True)
        self.optional_filters_btn.clicked.connect(self.toggle_optional_filters)
        search_layout.addWidget(self.optional_filters_btn)
        
        # Container for all optional filter content (hidden by default)
        self.optional_filters_widget = QWidget()
        optional_filters_layout = QVBoxLayout(self.optional_filters_widget)
        optional_filters_layout.setContentsMargins(10, 0, 0, 0)
        
        # Surface Area Range section
        self.surface_area_checkbox = QCheckBox("Surface Area Range")
        optional_filters_layout.addWidget(self.surface_area_checkbox)
        
        # Surface area inputs (initially disabled)
        self.surface_area_widget = QWidget()
        surface_area_layout = QVBoxLayout(self.surface_area_widget)
        surface_area_layout.setContentsMargins(20, 0, 0, 0)
        
        min_area_layout = QHBoxLayout()
        min_area_layout.addWidget(QLabel("Min (sq mi):"))
        self.min_area_input = QLineEdit()
        self.min_area_input.setPlaceholderText("0")
        min_area_layout.addWidget(self.min_area_input)
        surface_area_layout.addLayout(min_area_layout)
        
        max_area_layout = QHBoxLayout()
        max_area_layout.addWidget(QLabel("Max (sq mi):"))
        self.max_area_input = QLineEdit()
        self.max_area_input.setPlaceholderText("100")
        max_area_layout.addWidget(self.max_area_input)
        surface_area_layout.addLayout(max_area_layout)
        
        self.surface_area_widget.setEnabled(False)
        optional_filters_layout.addWidget(self.surface_area_widget)
        
        # Connect checkbox to enable/disable inputs
        self.surface_area_checkbox.stateChanged.connect(
            lambda state: self.surface_area_widget.setEnabled(state == Qt.Checked)
        )
        
        # Type of Waterbody section
        self.waterbody_type_checkbox = QCheckBox("Type of Waterbody")
        optional_filters_layout.addWidget(self.waterbody_type_checkbox)
        
        # Waterbody type options (initially disabled)
        self.waterbody_type_widget = QWidget()
        waterbody_layout = QVBoxLayout(self.waterbody_type_widget)
        waterbody_layout.setContentsMargins(20, 0, 0, 0)
        
        self.lake_checkbox = QCheckBox("Lake")
        self.pond_checkbox = QCheckBox("Pond")
        self.reservoir_checkbox = QCheckBox("Reservoir")
        self.river_checkbox = QCheckBox("River")
        self.stream_checkbox = QCheckBox("Stream")
        
        waterbody_layout.addWidget(self.lake_checkbox)
        waterbody_layout.addWidget(self.pond_checkbox)
        waterbody_layout.addWidget(self.reservoir_checkbox)
        waterbody_layout.addWidget(self.river_checkbox)
        waterbody_layout.addWidget(self.stream_checkbox)
        
        self.waterbody_type_widget.setEnabled(False)
        optional_filters_layout.addWidget(self.waterbody_type_widget)
        
        # Connect checkbox to enable/disable options
        self.waterbody_type_checkbox.stateChanged.connect(
            lambda state: self.waterbody_type_widget.setEnabled(state == Qt.Checked)
        )
        
        # Hide optional filters by default
        self.optional_filters_widget.setVisible(False)
        search_layout.addWidget(self.optional_filters_widget)
        
        # Add separator
        search_layout.addWidget(self.create_separator())
        
        # ===== SEARCH BUTTONS (moved above Data Sources) =====
        # Search button - disabled until required fields are filled
        self.search_button = QPushButton("🔍 Search Waterbodies")
        self.search_button.setStyleSheet("font-weight: bold; padding: 8px;")
        self.search_button.clicked.connect(self.apply_search)
        self.search_button.setEnabled(False)
        self.search_button.setToolTip("Enter Latitude, Longitude, and Buffer Radius to enable search")
        search_layout.addWidget(self.search_button)
        
        # Clear button
        clear_button = QPushButton("Clear Filters")
        clear_button.clicked.connect(self.clear_filters)
        search_layout.addWidget(clear_button)
        
        # Add separator
        search_layout.addWidget(self.create_separator())
        
        # ===== DATA SOURCES SECTION =====
        data_sources_title = QLabel("Data Sources")
        data_sources_title.setStyleSheet("font-weight: bold;")
        search_layout.addWidget(data_sources_title)
        
        # Container for all data source checkboxes (disabled until search is performed)
        self.data_sources_widget = QWidget()
        self.data_sources_layout = QVBoxLayout(self.data_sources_widget)
        self.data_sources_layout.setContentsMargins(20, 0, 0, 0)
        
        # Show waterbodies checkbox (with cyan swatch)
        wb_row = QWidget()
        wb_row_layout = QHBoxLayout(wb_row)
        wb_row_layout.setContentsMargins(0, 0, 0, 0)
        wb_row_layout.setSpacing(6)
        wb_swatch = QPushButton()
        wb_swatch.setFixedSize(14, 14)
        wb_swatch.setCursor(Qt.PointingHandCursor)
        wb_swatch.setStyleSheet(
            "QPushButton { background-color: rgba(0,255,200,150); border: 2px solid rgba(0,150,150,255); }"
            "QPushButton:hover { background-color: rgba(0,255,200,220); border: 2px solid rgba(0,150,150,255); }"
        )
        wb_swatch.clicked.connect(self.zoom_to_waterbodies)
        wb_row_layout.addWidget(wb_swatch)
        self.show_waterbodies_checkbox = QCheckBox("Waterbodies")
        self.show_waterbodies_checkbox.setChecked(False)
        self.show_waterbodies_checkbox.stateChanged.connect(self.toggle_waterbody_visibility)
        wb_row_layout.addWidget(self.show_waterbodies_checkbox)
        self.wb_buffer_indicator = QLabel("")
        self.wb_buffer_indicator.setStyleSheet("color: green; font-weight: bold;")
        wb_row_layout.addWidget(self.wb_buffer_indicator)
        wb_row_layout.addStretch()
        self.data_sources_layout.addWidget(wb_row)
        
        # Scan metadata_polygons folder for .gpkg files and create checkboxes
        self.populate_metadata_polygon_checkboxes()
        
        # Disable all data source checkboxes until search is performed
        self.set_data_source_checkboxes_enabled(False)
        search_layout.addWidget(self.data_sources_widget)

        # ===== NOVEL DATA LAYERS SECTION (populated when a waterbody is selected) =====
        self.novel_data_layers_title = QLabel("Novel Data Layers")
        self.novel_data_layers_title.setStyleSheet("font-weight: bold;")
        self.novel_data_layers_title.hide()
        search_layout.addWidget(self.novel_data_layers_title)

        self.novel_data_layers_widget = QWidget()
        self.novel_data_layers_layout = QVBoxLayout(self.novel_data_layers_widget)
        self.novel_data_layers_layout.setContentsMargins(20, 0, 0, 0)
        self.novel_data_layers_widget.hide()
        search_layout.addWidget(self.novel_data_layers_widget)

        # Add separator
        search_layout.addWidget(self.create_separator())

        # ===== BUILDABLE AREA ANALYSIS SECTION =====
        buildable_title = QLabel("Buildable Area Analysis")
        buildable_title.setStyleSheet("font-weight: bold;")
        search_layout.addWidget(buildable_title)
        
        # Container for buildable area controls (disabled until search is performed)
        self.buildable_area_widget = QWidget()
        buildable_area_layout = QVBoxLayout(self.buildable_area_widget)
        buildable_area_layout.setContentsMargins(20, 0, 0, 0)
        
        # Generate single waterbody button
        self.generate_single_buildable_btn = QPushButton("Generate Buildable Area Polygon for a Single Waterbody")
        self.generate_single_buildable_btn.clicked.connect(self.activate_single_buildable_area_tool)
        buildable_area_layout.addWidget(self.generate_single_buildable_btn)
        
        # Buildable areas checkbox
        self.show_buildable_areas_checkbox = QCheckBox("Buildable Areas")
        self.show_buildable_areas_checkbox.setChecked(False)
        self.show_buildable_areas_checkbox.setEnabled(False)
        self.show_buildable_areas_checkbox.stateChanged.connect(self.toggle_buildable_areas_visibility)
        buildable_area_layout.addWidget(self.show_buildable_areas_checkbox)

        # Generate suitability heatmap button (click-to-select a waterbody)
        self.generate_heatmap_btn = QPushButton("Generate Suitability Heatmap for a Single Waterbody")
        self.generate_heatmap_btn.clicked.connect(self.activate_heatmap_tool)
        buildable_area_layout.addWidget(self.generate_heatmap_btn)

        # Heatmap checkbox
        self.show_heatmap_checkbox = QCheckBox("Suitability Heatmap")
        self.show_heatmap_checkbox.setChecked(False)
        self.show_heatmap_checkbox.setEnabled(False)
        self.show_heatmap_checkbox.stateChanged.connect(self.toggle_heatmap_visibility)
        buildable_area_layout.addWidget(self.show_heatmap_checkbox)

        # Disable entire section until search is performed
        self.buildable_area_widget.setEnabled(False)
        search_layout.addWidget(self.buildable_area_widget)
        
        # Add stretch to push everything to the top
        search_layout.addStretch()
        
        # ===== SEARCH RESULTS SECTION =====
        search_layout.addWidget(self.create_separator())
        
        results_title = QLabel("Search Results")
        results_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        search_layout.addWidget(results_title)
        
        # Results log area
        self.results_log = QTextEdit()
        self.results_log.setReadOnly(True)
        self.results_log.setMinimumHeight(150)
        self.results_log.setPlaceholderText("Search results will appear here...")
        search_layout.addWidget(self.results_log)
        
        # Set the search panel as the scroll area's widget
        scroll_area.setWidget(search_panel)
        
        return scroll_area
        
    def populate_metadata_polygon_checkboxes(self):
        """Scan metadata_polygons folder for .gpkg files and create a checkbox for each,
        loading each file as its own source layer."""
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Folders to scan for .gpkg files and shapefile subfolders
        folders_to_scan = [
            os.path.join(script_dir, "metadata_polygons"),
            os.path.join(script_dir, "novel_border"),
        ]
        # Folders to scan for .tif raster files
        tif_folders = [
            os.path.join(script_dir, "novel_algae"),
            os.path.join(script_dir, "novel_bathymetry"),
            os.path.join(script_dir, "novel_temperature"),
            os.path.join(script_dir, "novel_water_level"),
        ]

        color_index = 0
        weights = load_layer_weights(script_dir)

        layer_files = []
        for folder in folders_to_scan:
            if not os.path.exists(folder):
                continue

            # Collect .gpkg files at the top level, plus .shp files inside subfolders
            for entry in sorted(os.listdir(folder)):
                entry_path = os.path.join(folder, entry)
                if os.path.isfile(entry_path) and entry.endswith('.gpkg'):
                    layer_files.append((entry, entry_path))
                elif os.path.isdir(entry_path):
                    sub_files = sorted([f for f in os.listdir(entry_path)
                                        if f.endswith('.shp') or f.endswith('.gpkg')])
                    for sub in sub_files:
                        layer_files.append((sub, os.path.join(entry_path, sub)))

        # Collect .tif files from the novel data folders
        for tif_folder in tif_folders:
            if not os.path.exists(tif_folder):
                continue
            for entry in sorted(os.listdir(tif_folder)):
                entry_path = os.path.join(tif_folder, entry)
                if os.path.isfile(entry_path) and entry.lower().endswith('.tif'):
                    layer_files.append((entry, entry_path))

        for filename, filepath in layer_files:
            lower_name = filename.lower()
            is_raster = lower_name.endswith('.tif')
            # A .gpkg may store a tiled gridded coverage (raster DEM) instead of
            # vector tables. GDAL's gpkg driver exposes the raster side when the
            # layer is opened as a raster; probe that here so ElevationData.gpkg
            # and similar files feed the A* cost surface in substation_connector
            # instead of being mis-loaded as an empty vector layer.
            if not is_raster and lower_name.endswith('.gpkg'):
                _probe = QgsRasterLayer(filepath, "__probe__", "gdal")
                if _probe.isValid() and _probe.bandCount() > 0:
                    is_raster = True
                # _probe goes out of scope here; the real QgsRasterLayer is
                # (re)constructed a few lines below with the proper display name.
            config = self.METADATA_POLYGON_CONFIG.get(filename, {})

            # Determine display name
            if "name" in config:
                display_name = config["name"]
            else:
                # Default: remove extension, strip leading state prefix, title-case
                base_name = os.path.splitext(filename)[0]
                parts = base_name.split('_', 1)
                if len(parts) > 1 and len(parts[0]) <= 2:
                    base_name = parts[1]
                display_name = base_name.replace('_', ' ').title()

            if is_raster:
                # Load as raster layer
                source_layer = QgsRasterLayer(filepath, f"Raster: {display_name}", "gdal")
                if not source_layer.isValid():
                    print(f"[RASTER] Failed to load: {filename}")
                    continue

                # Make 0-value pixels transparent so basemap shows through
                provider = source_layer.dataProvider()
                provider.setNoDataValue(1, 0)
                source_layer.setOpacity(1.0)

                QgsProject.instance().addMapLayer(source_layer)

                # Create row: swatch + checkbox
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(6)

                swatch = QPushButton()
                swatch.setFixedSize(14, 14)
                swatch.setCursor(Qt.PointingHandCursor)
                swatch.setStyleSheet(
                    "QPushButton { background-color: rgba(100,100,100,120); border: 2px solid rgba(60,60,60,255); }"
                    "QPushButton:hover { background-color: rgba(100,100,100,200); border: 2px solid rgba(60,60,60,255); }"
                )
                row_layout.addWidget(swatch)

                checkbox = QCheckBox(display_name)
                checkbox.setChecked(False)
                checkbox.setToolTip(f"Show {display_name} raster overlay")
                row_layout.addWidget(checkbox)

                buffer_indicator = QLabel("")
                buffer_indicator.setStyleSheet("color: green; font-weight: bold;")
                row_layout.addWidget(buffer_indicator)
                row_layout.addStretch()

                # Hide ML .tif rasters from UI, but keep ElevationData visible
                if filename.lower() == "elevationdata.gpkg":
                    self.data_sources_layout.addWidget(row_widget)

                entry = {
                    "checkbox": checkbox,
                    "row_widget": row_widget,
                    "buffer_indicator": buffer_indicator,
                    "filepath": filepath,
                    "source_layer": source_layer,
                    "results_layer": None,
                    "display_name": display_name,
                    "is_raster": True,
                }
                self.metadata_polygon_entries.append(entry)

                swatch.clicked.connect(lambda checked, e=entry: self.zoom_to_data_source(e))

                checkbox.stateChanged.connect(lambda state, e=entry: self.on_metadata_polygon_toggled(state, e))
            else:
                # Generate a unique color for each vector data source
                fill_color, outline_color = self._generate_distinct_color(color_index, len(layer_files))
                color_index += 1

                # Load the gpkg file as a source layer
                source_layer = QgsVectorLayer(filepath, f"Metadata: {display_name}", "ogr")
                if not source_layer.isValid():
                    print(f"Failed to load metadata polygon: {filename}")
                    continue
                geom_type = source_layer.geometryType()

                # Hide by default - add to project but don't show
                QgsProject.instance().addMapLayer(source_layer, False)

                # Create row: color swatch + checkbox
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(6)

                r, g, b, a = fill_color.split(',')
                br, bg, bb, ba = outline_color.split(',')
                # Compute a brighter alpha for hover
                hover_a = min(int(a) + 70, 255)
                swatch = QPushButton()
                swatch.setFixedSize(14, 14)
                swatch.setCursor(Qt.PointingHandCursor)
                swatch.setStyleSheet(
                    f"QPushButton {{ background-color: rgba({r},{g},{b},{a}); border: 2px solid rgba({br},{bg},{bb},{ba}); }}"
                    f"QPushButton:hover {{ background-color: rgba({r},{g},{b},{hover_a}); border: 2px solid rgba({br},{bg},{bb},{ba}); }}"
                )
                row_layout.addWidget(swatch)

                checkbox = QCheckBox(display_name)
                checkbox.setChecked(False)
                geom_label = "points" if geom_type == QgsWkbTypes.PointGeometry else "polygons"
                checkbox.setToolTip(f"Show {display_name} {geom_label} within the search buffer")
                row_layout.addWidget(checkbox)

                buffer_indicator = QLabel("")
                buffer_indicator.setStyleSheet("color: green; font-weight: bold;")
                row_layout.addWidget(buffer_indicator)
                row_layout.addStretch()

                self.data_sources_layout.addWidget(row_widget)

                # Store entry
                entry = {
                    "checkbox": checkbox,
                    "row_widget": row_widget,
                    "buffer_indicator": buffer_indicator,
                    "filepath": filepath,
                    "source_layer": source_layer,
                    "results_layer": None,
                    "display_name": display_name,
                    "fill_color": fill_color,
                    "outline_color": outline_color,
                    "geom_type": geom_type,
                    "is_raster": False,
                }
                self.metadata_polygon_entries.append(entry)

                swatch.clicked.connect(lambda checked, e=entry: self.zoom_to_data_source(e))

                # Connect checkbox - use a closure to capture the entry
                checkbox.stateChanged.connect(lambda state, e=entry: self.on_metadata_polygon_toggled(state, e))
    
    def set_data_source_checkboxes_enabled(self, enabled):
        """Enable or disable data source checkboxes without affecting swatches"""
        self.show_waterbodies_checkbox.setEnabled(enabled)
        for entry in self.metadata_polygon_entries:
            entry["checkbox"].setEnabled(enabled)

    def update_data_source_buffer_indicators(self, buffer_geom_3857):
        """Show a check mark next to data sources that have features in the buffer"""
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")

        for entry in self.metadata_polygon_entries:
            source_layer = entry.get("source_layer")
            indicator = entry.get("buffer_indicator")
            if not source_layer or not source_layer.isValid() or not indicator:
                continue

            # Get the layer's extent in EPSG:3857
            layer_extent = source_layer.extent()
            if layer_extent.isEmpty():
                indicator.setText("")
                continue

            if source_layer.crs().authid() != crs_3857.authid():
                transform = QgsCoordinateTransform(source_layer.crs(), crs_3857, QgsProject.instance())
                layer_extent = transform.transformBoundingBox(layer_extent)

            layer_extent_geom = QgsGeometry.fromRect(layer_extent)
            has_overlap = buffer_geom_3857.intersects(layer_extent_geom)

            if has_overlap and not entry.get("is_raster"):
                # Bounding box overlaps — check if any actual features intersect
                layer_crs = source_layer.crs()
                if layer_crs.authid() != crs_3857.authid():
                    transform_to_layer = QgsCoordinateTransform(crs_3857, layer_crs, QgsProject.instance())
                    buffer_in_layer_crs = QgsGeometry(buffer_geom_3857)
                    buffer_in_layer_crs.transform(transform_to_layer)
                    filter_rect = buffer_in_layer_crs.boundingBox()
                else:
                    filter_rect = buffer_geom_3857.boundingBox()

                request = QgsFeatureRequest().setFilterRect(filter_rect).setLimit(1)
                has_overlap = any(True for _ in source_layer.getFeatures(request))

            indicator.setText("  \u2714" if has_overlap else "")

    def update_search_button_state(self):
        """Enable/disable the Search Waterbodies button based on required fields"""
        lat_text = self.latitude_input.text().strip()
        lon_text = self.longitude_input.text().strip()
        buffer_text = self.buffer_input.text().strip()
        
        has_all_required = bool(lat_text) and bool(lon_text) and bool(buffer_text)
        self.search_button.setEnabled(has_all_required)
        
        if has_all_required:
            self.search_button.setToolTip("")
        else:
            self.search_button.setToolTip("Enter Latitude, Longitude, and Buffer Radius to enable search")
    
    def populate_novel_data_layers_for_waterbody(self, feature, source_layer):
        """Populate the Novel Data Layers section with .tif rasters from the
        novel_* folders whose extent intersects the selected waterbody. Pass
        feature=None to clear and hide the section."""
        # Clear any previously displayed rows
        while self.novel_data_layers_layout.count():
            item = self.novel_data_layers_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if feature is None:
            self.novel_data_layers_title.hide()
            self.novel_data_layers_widget.hide()
            return

        geom = feature.geometry()
        if not geom or geom.isEmpty():
            self.novel_data_layers_title.hide()
            self.novel_data_layers_widget.hide()
            return

        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        wb_geom = QgsGeometry(geom)
        if source_layer and source_layer.crs() != crs_3857:
            wb_geom.transform(QgsCoordinateTransform(source_layer.crs(), crs_3857, QgsProject.instance()))

        novel_keywords = ("novel_algae", "novel_bathymetry", "novel_temperature", "novel_water_level")
        overlapping = []

        for entry in self.metadata_polygon_entries:
            if not entry.get("is_raster"):
                continue
            filepath_lower = entry.get("filepath", "").replace("\\", "/").lower()
            if not any(kw in filepath_lower for kw in novel_keywords):
                continue

            raster_layer = entry.get("source_layer")
            if not raster_layer or not raster_layer.isValid():
                continue

            extent = raster_layer.extent()
            if raster_layer.crs() != crs_3857:
                try:
                    extent = QgsCoordinateTransform(
                        raster_layer.crs(), crs_3857, QgsProject.instance()
                    ).transformBoundingBox(extent)
                except Exception:
                    continue

            if wb_geom.intersects(QgsGeometry.fromRect(extent)):
                overlapping.append(entry)

        if not overlapping:
            self.novel_data_layers_title.hide()
            self.novel_data_layers_widget.hide()
            return

        self.novel_data_layers_title.show()
        self.novel_data_layers_widget.show()

        for entry in overlapping:
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)

            swatch = QPushButton()
            swatch.setFixedSize(14, 14)
            swatch.setCursor(Qt.PointingHandCursor)
            swatch.setStyleSheet(
                "QPushButton { background-color: rgba(100,100,100,120); border: 2px solid rgba(60,60,60,255); }"
                "QPushButton:hover { background-color: rgba(100,100,100,200); border: 2px solid rgba(60,60,60,255); }"
            )
            swatch.clicked.connect(lambda checked, e=entry: self.zoom_to_data_source(e))
            row_layout.addWidget(swatch)

            cb = QCheckBox(entry.get("display_name", os.path.basename(entry.get("filepath", ""))))
            cb.setChecked(entry["checkbox"].isChecked())
            # Drive the canonical entry checkbox so the existing toggle handler
            # (visibility, refresh) runs unchanged.
            cb.stateChanged.connect(
                lambda state, e=entry: e["checkbox"].setChecked(state == Qt.Checked)
            )
            row_layout.addWidget(cb)
            row_layout.addStretch()

            self.novel_data_layers_layout.addWidget(row_widget)

    def on_metadata_polygon_toggled(self, state, entry):
        """Called when a metadata polygon checkbox is toggled - load/show data for this specific gpkg/tif"""
        if entry.get("is_raster"):
            # Raster layers show/hide only if their extent intersects the buffer
            self.refresh_canvas_layers()
            return

        if state == Qt.Checked and self.buffer_layer:
            # Build buffer geometry and find features from this specific source layer
            try:
                lat = float(self.latitude_input.text())
                lon = float(self.longitude_input.text())
                buffer_radius = float(self.buffer_input.text())

                crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
                crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
                transform = QgsCoordinateTransform(crs_4326, crs_3857, QgsProject.instance())
                point_3857 = transform.transform(QgsPointXY(lon, lat))

                buffer_meters = buffer_radius * 1609.34
                buffer_geom_3857 = QgsGeometry.fromPointXY(point_3857).buffer(buffer_meters, 50)

                self.find_metadata_polygons_in_buffer(entry, buffer_geom_3857)
            except (ValueError, AttributeError):
                pass
        elif state != Qt.Checked:
            # Remove results layer for this entry when unchecked
            if entry["results_layer"]:
                if entry["results_layer"].id() in QgsProject.instance().mapLayers():
                    QgsProject.instance().removeMapLayer(entry["results_layer"])
                entry["results_layer"] = None

        self.refresh_canvas_layers()
    
    def find_metadata_polygons_in_buffer(self, entry, buffer_geom_3857):
        """Find and display polygons from a specific metadata gpkg that intersect with the buffer"""
        # Remove old results layer for this entry
        if entry["results_layer"]:
            if entry["results_layer"].id() in QgsProject.instance().mapLayers():
                QgsProject.instance().removeMapLayer(entry["results_layer"])
            entry["results_layer"] = None
        
        source_layer = entry["source_layer"]
        if not source_layer or not source_layer.isValid():
            return
        
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        layer_crs = source_layer.crs()

        need_transform = layer_crs != crs_3857
        if need_transform:
            transform_to_3857 = QgsCoordinateTransform(layer_crs, crs_3857, QgsProject.instance())
            transform_to_layer = QgsCoordinateTransform(crs_3857, layer_crs, QgsProject.instance())
            # Compute bounding box in layer CRS for the initial spatial index filter.
            # Clamp to valid EPSG:3857 range first to avoid transform errors on huge buffers.
            MAX_3857 = 20037508.342789244
            bbox_3857 = buffer_geom_3857.boundingBox()
            clamped = QgsRectangle(
                max(bbox_3857.xMinimum(), -MAX_3857),
                max(bbox_3857.yMinimum(), -MAX_3857),
                min(bbox_3857.xMaximum(),  MAX_3857),
                min(bbox_3857.yMaximum(),  MAX_3857),
            )
            try:
                tl = transform_to_layer.transform(QgsPointXY(clamped.xMinimum(), clamped.yMaximum()))
                tr = transform_to_layer.transform(QgsPointXY(clamped.xMaximum(), clamped.yMaximum()))
                bl = transform_to_layer.transform(QgsPointXY(clamped.xMinimum(), clamped.yMinimum()))
                br = transform_to_layer.transform(QgsPointXY(clamped.xMaximum(), clamped.yMinimum()))
                filter_bbox = QgsRectangle(
                    min(tl.x(), tr.x(), bl.x(), br.x()),
                    min(tl.y(), tr.y(), bl.y(), br.y()),
                    max(tl.x(), tr.x(), bl.x(), br.x()),
                    max(tl.y(), tr.y(), bl.y(), br.y()),
                )
            except Exception:
                filter_bbox = source_layer.extent()
        else:
            filter_bbox = buffer_geom_3857.boundingBox()

        # Find matching features.
        # The precise intersection check is done in EPSG:3857 space so that the
        # buffer circle is never distorted by re-projection (which causes points
        # to appear outside the visual circle, or valid points to be omitted).
        matching_features = []
        request = QgsFeatureRequest()
        request.setFilterRect(filter_bbox)

        for feature in source_layer.getFeatures(request):
            geom = QgsGeometry(feature.geometry())
            if need_transform:
                geom.transform(transform_to_3857)
            if geom.intersects(buffer_geom_3857):
                matching_features.append(feature)
        
        if not matching_features:
            return
        
        # Create memory layer for results in EPSG:3857
        geom_type = entry.get("geom_type", QgsWkbTypes.PolygonGeometry)
        geom_type_str = {
            QgsWkbTypes.PointGeometry: "Point",
            QgsWkbTypes.LineGeometry: "LineString",
            QgsWkbTypes.PolygonGeometry: "Polygon",
        }.get(geom_type, "Polygon")
        results_layer = QgsVectorLayer(
            f"{geom_type_str}?crs=EPSG:3857",
            f"{entry['display_name']} (in buffer)",
            "memory"
        )
        provider = results_layer.dataProvider()
        
        # Copy field definitions from source
        provider.addAttributes(source_layer.fields())
        results_layer.updateFields()
        
        # Transform features to EPSG:3857 and add to results layer
        for feature in matching_features:
            new_feature = QgsFeature(source_layer.fields())
            geom = QgsGeometry(feature.geometry())
            
            if layer_crs != crs_3857:
                transform = QgsCoordinateTransform(layer_crs, crs_3857, QgsProject.instance())
                geom.transform(transform)
            new_feature.setGeometry(geom)
            new_feature.setAttributes(feature.attributes())
            provider.addFeature(new_feature)
        
        # Use the entry's configured colors
        fill_color = entry["fill_color"]
        outline_color = entry["outline_color"]
        
        if geom_type == QgsWkbTypes.PointGeometry:
            symbol = QgsMarkerSymbol.createSimple({
                'color': fill_color,
                'outline_color': outline_color,
                'size': '3.0'
            })
        else:
            symbol = QgsFillSymbol.createSimple({
                'color': fill_color,
                'outline_color': outline_color,
                'outline_width': '2.0'
            })
        results_layer.renderer().setSymbol(symbol)
        
        # Add to map
        QgsProject.instance().addMapLayer(results_layer)
        entry["results_layer"] = results_layer
    
    def clear_search_results(self):
        """Remove all displayed waterbody and metadata point/polygon result layers."""
        if self.results_layer:
            if self.results_layer.id() in QgsProject.instance().mapLayers():
                QgsProject.instance().removeMapLayer(self.results_layer)
            self.results_layer = None

        for entry in self.metadata_polygon_entries:
            entry["checkbox"].setChecked(False)
            if entry["results_layer"]:
                if entry["results_layer"].id() in QgsProject.instance().mapLayers():
                    QgsProject.instance().removeMapLayer(entry["results_layer"])
                entry["results_layer"] = None

        # Uncheck waterbodies and gray out all data source checkboxes until a new search is run
        self.show_waterbodies_checkbox.setChecked(False)
        # Clear buffer indicators
        self.wb_buffer_indicator.setText("")
        for entry in self.metadata_polygon_entries:
            indicator = entry.get("buffer_indicator")
            if indicator:
                indicator.setText("")
        self.set_data_source_checkboxes_enabled(False)

    def is_any_metadata_polygon_checked(self):
        """Check if any metadata polygon checkbox is checked"""
        return any(e["checkbox"].isChecked() for e in self.metadata_polygon_entries)
    
    def get_checked_metadata_polygon_filepaths(self):
        """Return list of filepaths for all checked metadata polygon checkboxes"""
        return [e["filepath"] for e in self.metadata_polygon_entries if e["checkbox"].isChecked()]

    def toggle_optional_filters(self):
        """Toggle visibility of the optional filters section"""
        visible = not self.optional_filters_widget.isVisible()
        self.optional_filters_widget.setVisible(visible)
        if visible:
            self.optional_filters_btn.setText("\u25BE Optional Filters")
        else:
            self.optional_filters_btn.setText("\u25B8 Optional Filters")

    def create_separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line
        
    def create_toolbar(self):
        toolbar = QToolBar()
        self.addToolBar(toolbar)
        
        # Pan tool
        pan_action = QAction("Pan", self)
        pan_action.triggered.connect(self.activate_pan)
        toolbar.addAction(pan_action)
        
        # Select Waterbody tool
        select_action = QAction("Select Waterbody", self)
        select_action.triggered.connect(self.activate_select_waterbody)
        toolbar.addAction(select_action)

        # Connect to Substation tool
        connect_action = QAction("Connect to Substation", self)
        connect_action.triggered.connect(self.activate_connect_substation)
        toolbar.addAction(connect_action)
        
        toolbar.addSeparator()
        
        # Zoom in
        zoom_in_action = QAction("Zoom In", self)
        zoom_in_action.triggered.connect(self.zoom_in)
        toolbar.addAction(zoom_in_action)
        
        # Zoom out
        zoom_out_action = QAction("Zoom Out", self)
        zoom_out_action.triggered.connect(self.zoom_out)
        toolbar.addAction(zoom_out_action)
        
        # Zoom to full extent
        full_extent_action = QAction("Full Extent", self)
        full_extent_action.triggered.connect(self.zoom_full)
        toolbar.addAction(full_extent_action)
        
        toolbar.addSeparator()
    
    def activate_select_waterbody(self):
        """Activate the waterbody selection tool"""
        self.canvas.setMapTool(self.select_waterbody_tool)
        self.statusBar().showMessage("Click on a waterbody polygon to select it and view its metadata")

    def activate_connect_substation(self):
        """Activate the 'connect waterbody to nearest substation' tool."""
        self.canvas.setMapTool(self.connect_substation_tool)
        self.statusBar().showMessage(
            "Click a waterbody polygon to connect it to the nearest power substation"
        )

    def on_connect_substation_clicked(self, point, button):
        """Handle a click in 'connect to substation' mode.

        Workflow:
            1. Reuse the existing waterbody hit-testing.
            2. *NEW*: Prompt the user (via :class:`DistancePreferencesDialog`)
               for their reasonable / maximum tolerable distance — in miles —
               BEFORE handing the polygon to
               ``substation_connector.connect_waterbody_to_substation``
               (i.e. before ``SLOPE_PENALTY`` ever gets consulted by the
               A* cost function).
            3. Generate the path (existing pipeline; unchanged).
            4. *NEW*: Measure the ACTUAL polyline length of the resulting
               path geometry, compare it against the user's thresholds, and
               surface the verdict in the status bar + a QMessageBox.
        """
        import traceback as _tb
        try:
            # ---------- 1) Hit-test the waterbody (unchanged) ----------
            # Reuse the existing selection logic — it already populates
            # self.selected_waterbody_layer in EPSG:3857.
            self.on_waterbody_clicked(point, button)
            if not self.selected_waterbody_layer:
                return

            feature = next(self.selected_waterbody_layer.getFeatures(), None)
            if feature is None:
                return

            # ---------- 2) NEW: prompt for distance preferences --------
            # This must run BEFORE connect_waterbody_to_substation() —
            # that call is what eventually invokes cost_based_path() with
            # SLOPE_PENALTY. If the user cancels, abort cleanly without
            # touching any layers.
            #
            # Detect DEM presence up-front so the dialog can grey out the
            # slope-penalty slider when it would have no effect (no DEM
            # → straight-line fallback → slope ignored).
            from substation_connector import find_dem_layer
            has_dem = find_dem_layer(self.metadata_polygon_entries) is not None

            dlg = DistancePreferencesDialog(self, has_dem=has_dem)
            if dlg.exec_() != QDialog.Accepted:
                self.statusBar().showMessage(
                    "Substation connection cancelled — no distance "
                    "preferences provided."
                )
                return
            reasonable_m = miles_to_meters(dlg.reasonable_miles)
            maximum_m = miles_to_meters(dlg.maximum_miles)
            slope_penalty = dlg.slope_penalty

            # Remove any previous connection before drawing a new one.
            self._clear_substation_connection_layers()

            # ---------- 3) Generate the path (unchanged) ---------------
            print(f"[gui] calling connect_waterbody_to_substation "
                  f"(slope_penalty={slope_penalty})...")
            waterbody_geom_3857 = feature.geometry()
            result = connect_waterbody_to_substation(
                waterbody_geom_3857,
                self.metadata_polygon_entries,
                slope_penalty=slope_penalty,
            )
            print(f"[gui] connect_waterbody_to_substation returned keys: {list(result.keys())}")

            if "error" in result:
                QMessageBox.warning(self, "Substation Connection", result["error"])
                self.statusBar().showMessage(result["error"])
                return

            self.substation_path_layer = result["path_layer"]
            self.nearest_substation_layer = result["substation_layer"]
            QgsProject.instance().addMapLayer(self.substation_path_layer, False)
            QgsProject.instance().addMapLayer(self.nearest_substation_layer, False)

            # ---------- 4) NEW: measure & compare ----------------------
            # Read the length back from the LineString geometry rather
            # than from result["length_m"], so the straight-line fallback
            # (no DEM) is handled identically to the A* / cost-based case
            # — the comparison is against the actual path drawn on the
            # canvas, never the straight-line Euclidean distance.
            path_length_m = path_length_m_from_layer(self.substation_path_layer)
            if path_length_m is None:
                # Defensive — shouldn't happen, but never trust geometry.
                path_length_m = result.get("length_m", 0.0)

            verdict, verdict_msg = evaluate_distance(
                path_length_m, reasonable_m, maximum_m
            )

            miles = meters_to_miles(path_length_m)
            km = path_length_m / 1000.0
            kind = ("cost-based (slope-aware)"
                    if result["path_type"] == "cost" else "straight-line")
            name = result["substation_name"] or "unnamed substation"

            full_msg = (
                f"Connection distance: {miles:.2f} miles ({km:.2f} km) "
                f"— {verdict_msg}"
            )

            self.statusBar().showMessage(
                f"Nearest substation: {name} ({kind}) — {full_msg}"
            )

            # Pop a more visible dialog with the same verdict, so the user
            # can't miss it even if they're not watching the status bar.
            icon = (QMessageBox.Information if verdict == "great"
                    else QMessageBox.Warning if verdict == "okay"
                    else QMessageBox.Critical)
            box = QMessageBox(self)
            box.setIcon(icon)
            box.setWindowTitle("Substation Connection Distance")
            box.setText(full_msg)
            slope_line = (
                f"Slope penalty:           {int(slope_penalty)}"
                if result["path_type"] == "cost"
                else f"Slope penalty:           {int(slope_penalty)} (ignored — no DEM, straight-line path)"
            )
            box.setInformativeText(
                f"Nearest substation: {name}\n"
                f"Path type: {kind}\n"
                f"Your reasonable distance: {dlg.reasonable_miles:.2f} miles\n"
                f"Your maximum distance:   {dlg.maximum_miles:.2f} miles\n"
                f"{slope_line}"
            )
            box.exec_()

            self.refresh_canvas_layers()
        except Exception as exc:
            # Never let a Python exception crash the app window silently.
            _tb.print_exc()
            QMessageBox.critical(
                self,
                "Substation Connection – internal error",
                f"{type(exc).__name__}: {exc}\n\nSee terminal for full traceback.",
            )

    def _clear_substation_connection_layers(self):
        """Remove the path + marker layers from the project (if any)."""
        for attr in ("substation_path_layer", "nearest_substation_layer"):
            layer = getattr(self, attr, None)
            if layer and layer.id() in QgsProject.instance().mapLayers():
                QgsProject.instance().removeMapLayer(layer)
            setattr(self, attr, None)
        
    def activate_pan(self):
        self.canvas.setMapTool(self.pan_tool)
        
    def activate_pick_location(self):
        # Clear all displayed results, checkboxes, and location inputs
        self.clear_search_results()
        self.latitude_input.clear()
        self.longitude_input.clear()
        self.buffer_input.clear()

        # Remove marker if exists
        if self.marker_layer:
            QgsProject.instance().removeMapLayer(self.marker_layer)
            self.marker_layer = None

        # Remove buffer circle if exists
        if self.buffer_layer:
            QgsProject.instance().removeMapLayer(self.buffer_layer)
            self.buffer_layer = None

        self.show_buffer_checkbox.setChecked(False)
        self.show_buffer_checkbox.setEnabled(False)

        self.refresh_canvas_layers()
        
        # Activate pick tool
        self.canvas.setMapTool(self.pick_tool)
        QMessageBox.information(self, "Pick Location", 
                              "Click anywhere on the map to set the coordinates.\n\n" +
                              "Click 'Pan' in the toolbar when done to return to navigation mode.")
    
    def on_map_clicked(self, point, button):
        # Convert clicked point from canvas CRS (EPSG:3857) to WGS84 (EPSG:4326)
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(crs_3857, crs_4326, QgsProject.instance())

        # Transform point
        point_4326 = transform.transform(point)

        # Clear previous search results since the location is changing
        self.clear_search_results()

        # Update the input fields with the coordinates
        self.latitude_input.setText(f"{point_4326.y():.6f}")
        self.longitude_input.setText(f"{point_4326.x():.6f}")
        
        # Add a temporary marker at the clicked location
        self.add_marker(point)
        
        # Switch back to pan tool
        self.canvas.setMapTool(self.pan_tool)
    
    def on_waterbody_clicked(self, point, button):
        """Handle click events for waterbody selection"""
        if not self.waterbody_layers and not self.results_layer:
            self.statusBar().showMessage("No waterbody layers loaded")
            return
        
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        
        # Create a small search area around the clicked point
        search_radius = self.canvas.mapUnitsPerPixel() * 5  # 5 pixels tolerance
        search_rect = QgsRectangle(
            point.x() - search_radius,
            point.y() - search_radius,
            point.x() + search_radius,
            point.y() + search_radius
        )
        
        clicked_point_geom = QgsGeometry.fromPointXY(point)
        
        # First check the results layer (found waterbodies from search)
        found_feature = None
        source_layer = None
        
        if self.results_layer and self.results_layer.id() in QgsProject.instance().mapLayers():
            request = QgsFeatureRequest().setFilterRect(search_rect)
            for feature in self.results_layer.getFeatures(request):
                if feature.geometry().contains(clicked_point_geom):
                    found_feature = feature
                    source_layer = self.results_layer
                    break
        
        # If not found in results, check all waterbody layers
        if not found_feature:
            for waterbody_layer in self.waterbody_layers:
                if waterbody_layer.id() not in QgsProject.instance().mapLayers():
                    continue
                
                # Transform search rect to layer CRS if needed
                layer_crs = waterbody_layer.crs()
                if layer_crs != crs_3857:
                    transform = QgsCoordinateTransform(crs_3857, layer_crs, QgsProject.instance())
                    layer_search_rect = transform.transformBoundingBox(search_rect)
                    layer_point_geom = QgsGeometry(clicked_point_geom)
                    layer_point_geom.transform(transform)
                else:
                    layer_search_rect = search_rect
                    layer_point_geom = clicked_point_geom
                
                request = QgsFeatureRequest().setFilterRect(layer_search_rect)
                for feature in waterbody_layer.getFeatures(request):
                    if feature.geometry().contains(layer_point_geom):
                        found_feature = feature
                        source_layer = waterbody_layer
                        break
                
                if found_feature:
                    break
        
        if found_feature:
            self.select_waterbody(found_feature, source_layer)
            self.statusBar().showMessage(f"Selected waterbody: {self.get_feature_name(found_feature)}")
        else:
            self.statusBar().showMessage("No waterbody found at this location")
    
    def select_waterbody(self, feature, source_layer):
        """Highlight the selected waterbody and display its metadata"""
        # Remove old selection layer if exists
        if self.selected_waterbody_layer:
            QgsProject.instance().removeMapLayer(self.selected_waterbody_layer)
            self.selected_waterbody_layer = None
        
        # Remove old nearest agency layer if exists
        if self.nearest_agency_layer:
            QgsProject.instance().removeMapLayer(self.nearest_agency_layer)
            self.nearest_agency_layer = None
        
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        
        # Create memory layer for selected waterbody in EPSG:3857
        self.selected_waterbody_layer = QgsVectorLayer("Polygon?crs=EPSG:3857", "Selected Waterbody", "memory")
        provider = self.selected_waterbody_layer.dataProvider()
        
        # Copy field definitions from source
        provider.addAttributes(source_layer.fields())
        self.selected_waterbody_layer.updateFields()
        
        # Transform geometry to EPSG:3857 if needed
        new_feature = QgsFeature(source_layer.fields())
        geom = QgsGeometry(feature.geometry())
        
        source_crs = source_layer.crs()
        if source_crs != crs_3857:
            transform = QgsCoordinateTransform(source_crs, crs_3857, QgsProject.instance())
            geom.transform(transform)
        
        new_feature.setGeometry(geom)
        new_feature.setAttributes(feature.attributes())
        provider.addFeature(new_feature)
        
        # Style the selected waterbody - DARK RED to indicate selection
        # Add layer to project (no visible styling — used only for metadata lookup)
        self.selected_waterbody_layer.renderer().setSymbol(
            QgsFillSymbol.createSimple({'color': '0,0,0,0', 'outline_color': '0,0,0,0'})
        )
        QgsProject.instance().addMapLayer(self.selected_waterbody_layer)
        
        # Find direct overlaps
        overlapping_polygons = self.find_overlapping_polygons_for_waterbody(geom)

        # Build current search buffer geometry in EPSG:3857
        buffer_geom_3857 = None
        if self.buffer_layer:
            buffer_feature = next(self.buffer_layer.getFeatures(), None)
            if buffer_feature:
                buffer_geom_3857 = QgsGeometry(buffer_feature.geometry())

        # Nearby = inside search buffer, and also close enough to the selected waterbody
        nearby_polygons = self.find_nearby_polygon_features_for_waterbody(
            geom,
            buffer_geom_3857,
            max_distance_km=8.05
        )

        # Update the metadata panel
        self.display_waterbody_metadata(feature, overlapping_polygons, source_layer, nearby_polygons)
        
        self.refresh_canvas_layers()
    
    def find_overlapping_polygons_for_waterbody(self, waterbody_geom_3857):
        """
        Find all metadata polygons that overlap with the selected waterbody.
        
        Returns:
            List of dicts, each with:
                "feature_name": name of the overlapping polygon
                "display_name": name shown in Data Sources checkbox
                "weight": weight from layer_weights.csv or "Undefined"
                "feature": the QgsFeature
                "entry": the metadata_polygon_entry dict
        """
        if not self.metadata_polygon_entries:
            return []
        
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        
        # Load weights
        script_dir = os.path.dirname(os.path.abspath(__file__))
        weights = load_layer_weights(script_dir)
        
        overlapping = []
        
        for entry in self.metadata_polygon_entries:
            if entry.get("is_raster"):
                continue
            if not entry["checkbox"].isChecked():
                continue

            source_layer = entry["source_layer"]
            
            if entry.get("geom_type") == QgsWkbTypes.PointGeometry:
                continue
            
            layer_crs = source_layer.crs()
            needs_transform = (layer_crs.authid() != crs_3857.authid())
            
            # Transform waterbody geometry to layer CRS if needed
            if needs_transform:
                transform_to_layer = QgsCoordinateTransform(crs_3857, layer_crs, QgsProject.instance())
                waterbody_in_layer_crs = QgsGeometry(waterbody_geom_3857)
                waterbody_in_layer_crs.transform(transform_to_layer)
                search_geom = waterbody_in_layer_crs
            else:
                search_geom = waterbody_geom_3857
            
            # Query features near the waterbody
            request = QgsFeatureRequest().setFilterRect(search_geom.boundingBox())
            
            for feature in source_layer.getFeatures(request):
                if feature.geometry().intersects(search_geom):
                    # Get the polygon name
                    feature_name = self.get_agency_name(feature)
                    
                    # Get weight for this layer
                    filename = os.path.basename(entry.get("filepath", ""))
                    raw_weight = weights.get(filename)
                    weight_str = f"{raw_weight}" if raw_weight is not None else "Undefined"
                    
                    overlapping.append({
                        "feature_name": feature_name,
                        "display_name": entry["display_name"],
                        "weight": weight_str,
                        "feature": feature,
                        "entry": entry,
                    })
        
        return overlapping
    
    def find_nearby_polygon_features_for_waterbody(self, waterbody_geom_3857, buffer_geom_3857=None, max_distance_km=8.05, max_results=3):
        nearby = []
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")

        if not waterbody_geom_3857 or waterbody_geom_3857.isEmpty():
            return nearby

        if not buffer_geom_3857 or buffer_geom_3857.isEmpty():
            return nearby

        for entry in self.metadata_polygon_entries:
            if entry.get("is_raster"):
                continue
             
            if not entry["checkbox"].isChecked():
                continue

            if entry.get("geom_type") != QgsWkbTypes.PolygonGeometry:
                continue

            source_layer = entry.get("source_layer")
            if not source_layer or not source_layer.isValid():
                continue

            layer_crs = source_layer.crs()
            candidates = []

            # Use the GUI search buffer as the hard outer limit
            if layer_crs != crs_3857:
                transform_to_layer = QgsCoordinateTransform(crs_3857, layer_crs, QgsProject.instance())
                buffer_in_layer_crs = QgsGeometry(buffer_geom_3857)
                buffer_in_layer_crs.transform(transform_to_layer)
                request = QgsFeatureRequest().setFilterRect(buffer_in_layer_crs.boundingBox())
            else:
                request = QgsFeatureRequest().setFilterRect(buffer_geom_3857.boundingBox())

            for feature in source_layer.getFeatures(request):
                feature_geom = feature.geometry()
                if not feature_geom or feature_geom.isEmpty():
                    continue

                geom_3857 = QgsGeometry(feature_geom)
                if layer_crs != crs_3857:
                    transform = QgsCoordinateTransform(layer_crs, crs_3857, QgsProject.instance())
                    geom_3857.transform(transform)

                # Hard limit: ignore anything outside the GUI search buffer
                if not geom_3857.intersects(buffer_geom_3857):
                    continue

                # Direct overlaps are handled separately
                if geom_3857.intersects(waterbody_geom_3857):
                    continue

                # True proximity is based on distance to the selected waterbody
                distance_m = geom_3857.distance(waterbody_geom_3857)

                if distance_m <= max_distance_km * 1000.0:
                    candidates.append({
                        "feature_name": self.get_agency_name(feature),
                        "display_name": entry["display_name"],
                        "distance_km": round(distance_m / 1000.0, 2),
                        "feature": feature,
                        "entry": entry,
                        "proximity": "Nearby",
                    })

            candidates.sort(key=lambda x: x["distance_km"])

            if candidates:
                nearby.extend(candidates[:max_results])

        return nearby
    
    def highlight_water_agencies(self, agency_list):
        """Highlight the water agency district(s) in light red"""
        if not agency_list:
            return
        
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        
        # Create memory layer for agencies in EPSG:3857
        self.nearest_agency_layer = QgsVectorLayer("Polygon?crs=EPSG:3857", "Related Water Agency District(s)", "memory")
        provider = self.nearest_agency_layer.dataProvider()
        
        # Collect all unique fields from all source layers
        all_fields = QgsFields()
        for item in agency_list:
            if len(item) == 2:
                agency_feature, agency_layer = item
            else:
                agency_feature, agency_layer, _ = item
            
            for field in agency_layer.fields():
                if all_fields.lookupField(field.name()) == -1:
                    all_fields.append(field)
        
        provider.addAttributes(all_fields)
        self.nearest_agency_layer.updateFields()
        
        # Add all agency features
        for item in agency_list:
            if len(item) == 2:
                agency_feature, agency_layer = item
            else:
                agency_feature, agency_layer, _ = item
            
            new_feature = QgsFeature(all_fields)
            geom = QgsGeometry(agency_feature.geometry())
            
            layer_crs = agency_layer.crs()
            if layer_crs != crs_3857:
                transform = QgsCoordinateTransform(layer_crs, crs_3857, QgsProject.instance())
                geom.transform(transform)
            
            new_feature.setGeometry(geom)
            
            # Copy attributes
            for field in agency_feature.fields():
                field_idx = all_fields.lookupField(field.name())
                if field_idx >= 0:
                    new_feature.setAttribute(field_idx, agency_feature[field.name()])
            
            provider.addFeature(new_feature)
        
        # Style the agencies - LIGHT RED (pink) to differentiate from selected waterbody
        symbol = QgsFillSymbol.createSimple({
            'color': '255,182,193,120',  # Light pink with transparency
            'outline_color': '255,105,180,255',  # Hot pink outline
            'outline_width': '2.0'
        })
        self.nearest_agency_layer.renderer().setSymbol(symbol)
        
        # Add layer to project
        QgsProject.instance().addMapLayer(self.nearest_agency_layer)
    
    def format_ml_value(self, value, decimals=2):
        if value is None:
            return "Not available"

        try:
            value = float(value)
        except Exception:
            return "Not available"

        if value == 0 or value == -9999 or value == -999999:
            return "Not available"

        return f"{value:.{decimals}f}"

    def sample_raster_at_waterbody(self, raster_layer, feature, source_layer):
        if not raster_layer or not raster_layer.isValid():
            return None

        geom = feature.geometry()
        if not geom or geom.isEmpty():
            return None

        point = geom.centroid().asPoint()

        if source_layer and source_layer.crs() != raster_layer.crs():
            transform = QgsCoordinateTransform(
                source_layer.crs(),
                raster_layer.crs(),
                QgsProject.instance()
            )
            point = transform.transform(point)

        result = raster_layer.dataProvider().identify(
            point,
            QgsRaster.IdentifyFormatValue
        )

        if not result.isValid():
            return None

        values = result.results()
        if not values:
            return None

        return list(values.values())[0]

    def get_ml_raster_value_by_keyword(self, feature, source_layer, folder_keyword, filename_keywords):
        best_entry = None

        for entry in self.metadata_polygon_entries:
            if not entry.get("is_raster"):
                continue

            filepath = entry.get("filepath", "").lower()
            filename = os.path.basename(filepath)

            if folder_keyword not in filepath:
                continue

            if all(keyword in filename for keyword in filename_keywords):
                best_entry = entry
                break

        if not best_entry:
            return None, ""

        raster_layer = best_entry.get("source_layer")
        value = self.sample_raster_at_waterbody(raster_layer, feature, source_layer)

        return value, os.path.basename(best_entry.get("filepath", ""))
    
    def get_novel_border_feature_for_waterbody(self, feature, source_layer):
        geom = QgsGeometry(feature.geometry())

        if not geom or geom.isEmpty():
            return None

        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")

        if source_layer and source_layer.crs() != crs_3857:
            transform = QgsCoordinateTransform(source_layer.crs(), crs_3857, QgsProject.instance())
            geom.transform(transform)

        for entry in self.metadata_polygon_entries:
            filepath = entry.get("filepath", "").lower()

            if "novel_border" not in filepath:
                continue

            layer = entry.get("source_layer")
            if not layer or not layer.isValid():
                continue

            search_geom = QgsGeometry(geom)

            if layer.crs() != crs_3857:
                transform = QgsCoordinateTransform(crs_3857, layer.crs(), QgsProject.instance())
                search_geom.transform(transform)

            request = QgsFeatureRequest().setFilterRect(search_geom.boundingBox())

            for border_feature in layer.getFeatures(request):
                if border_feature.geometry().intersects(search_geom):
                    return border_feature

        return None
    
    def display_waterbody_metadata(self, feature, overlapping_polygons=None, source_layer=None, nearby_polygons=None):
        """Display the metadata of the selected waterbody in the bottom panel"""
        fields = feature.fields()
        attributes = feature.attributes()

        self.metadata_table.setRowCount(0)
        row = 0
        def get_layer_source_name(layer):
            if not layer:
                return ""

            try:
                layer_name = layer.name()
                if layer_name:
                    return layer_name
            except Exception:
                pass

            try:
                source_path = layer.source()
                if source_path:
                    return os.path.basename(source_path)
            except Exception:
                pass

            return ""

        def get_metadata_source_label(filename):
            for entry in self.metadata_polygon_entries:
                entry_filename = os.path.basename(entry.get("filepath", ""))
                if entry_filename == filename:
                    return entry.get("display_name", filename)
            return filename
        # ------------------------------------------------------------
        # Helper to add a normal row
        # ------------------------------------------------------------
        def add_row(label, value, source="", proximity="", field_color=None, value_color=None, tooltip=None):
            nonlocal row
            self.metadata_table.insertRow(row)

            source_info = self.get_source_info(source)
            source_name = source_info.get("source_name", "")
            source_link = source_info.get("source_link", "")

            field_item = QTableWidgetItem(label)
            value_item = QTableWidgetItem("" if value is None else str(value))
            proximity_item = QTableWidgetItem("" if proximity is None else str(proximity))
            source_item = QTableWidgetItem(source_name)

            if source_link:
                source_link_item = QTableWidgetItem("Open Link")
                source_link_item.setForeground(QColor("blue"))
                source_link_item.setToolTip(source_link)
                source_link_item.setData(Qt.UserRole, source_link)
            else:
                source_link_item = QTableWidgetItem("")
                source_link_item.setData(Qt.UserRole, "")

            if field_color:
                field_item.setForeground(field_color)
            if value_color:
                value_item.setForeground(value_color)

            if tooltip:
                value_item.setToolTip(tooltip)
            else:
                value_item.setToolTip("" if value is None else str(value))

            proximity_item.setToolTip("" if proximity is None else str(proximity))
            source_item.setToolTip(source_name)

            self.metadata_table.setItem(row, 0, field_item)
            self.metadata_table.setItem(row, 1, value_item)
            self.metadata_table.setItem(row, 2, proximity_item)
            self.metadata_table.setItem(row, 3, source_item)
            self.metadata_table.setItem(row, 4, source_link_item)
            row += 1
        # ------------------------------------------------------------
        # Helper to add a section header
        # ------------------------------------------------------------
        def add_section(title):
            nonlocal row
            self.metadata_table.insertRow(row)

            header_item = QTableWidgetItem(title)
            header_item.setBackground(Qt.lightGray)

            blank_item_1 = QTableWidgetItem("")
            blank_item_1.setBackground(Qt.lightGray)

            blank_item_2 = QTableWidgetItem("")
            blank_item_2.setBackground(Qt.lightGray)

            blank_item_3 = QTableWidgetItem("")
            blank_item_3.setBackground(Qt.lightGray)

            blank_item_4 = QTableWidgetItem("")
            blank_item_4.setBackground(Qt.lightGray)

            self.metadata_table.setItem(row, 0, header_item)
            self.metadata_table.setItem(row, 1, blank_item_1)
            self.metadata_table.setItem(row, 2, blank_item_2)
            self.metadata_table.setItem(row, 3, blank_item_3)
            self.metadata_table.setItem(row, 4, blank_item_4)
            row += 1

        def add_blank_row():
            nonlocal row
            self.metadata_table.insertRow(row)
            self.metadata_table.setItem(row, 0, QTableWidgetItem(""))
            self.metadata_table.setItem(row, 1, QTableWidgetItem(""))
            self.metadata_table.setItem(row, 2, QTableWidgetItem(""))
            self.metadata_table.setItem(row, 3, QTableWidgetItem(""))
            self.metadata_table.setItem(row, 4, QTableWidgetItem(""))
            row += 1

        # ------------------------------------------------------------
        # Build overlap summary from GPKG-based polygon intersections
        # ------------------------------------------------------------
        overlap_summary = {
            "Water Agency Districts": {"direct_count": 0, "nearby_count": 0, "direct_examples": [], "nearby_examples": []},
            "Tribal Lands": {"direct_count": 0, "nearby_count": 0, "direct_examples": [], "nearby_examples": []},
            "Fish Habitats": {"direct_count": 0, "nearby_count": 0, "direct_examples": [], "nearby_examples": []},
            "Protected / Restricted Areas": {"direct_count": 0, "nearby_count": 0, "direct_examples": [], "nearby_examples": []},
            "Other": {"direct_count": 0, "nearby_count": 0, "direct_examples": [], "nearby_examples": []},
        }

        def categorize_display_name(display_name):
            category = "Other"
            display_name_lower = display_name.lower()

            if any(x in display_name_lower for x in [
                "water agency", "water district", "water districts",
                "water municipality", "utility", "electric service territory"
            ]):
                category = "Water Agency Districts"

            elif "tribal" in display_name_lower:
                category = "Tribal Lands"

            elif any(x in display_name_lower for x in [
                "fish", "efh", "aquatic"
            ]):
                category = "Fish Habitats"

            elif any(x in display_name_lower for x in [
                "protected", "wildlife", "habitat", "bird", "park", "refuge",
                "wetland", "wetlands", "conservation", "blm", "unesco",
                "rare plants", "rare animals", "inaturalist"
            ]):
                category = "Protected / Restricted Areas"

            return category


        if overlapping_polygons:
            for overlap in overlapping_polygons:
                display_name = str(overlap.get("display_name", "")).strip()
                feature_name = str(overlap.get("feature_name", "")).strip()

                category = categorize_display_name(display_name)
                overlap_summary[category]["direct_count"] += 1

                if (
                    feature_name
                    and feature_name != "Unnamed"
                    and feature_name not in overlap_summary[category]["direct_examples"]
                    and len(overlap_summary[category]["direct_examples"]) < 3
                ):
                    overlap_summary[category]["direct_examples"].append(feature_name)


        if nearby_polygons:
            for nearby in nearby_polygons:
                display_name = str(nearby.get("display_name", "")).strip()
                feature_name = str(nearby.get("feature_name", "")).strip()

                category = categorize_display_name(display_name)
                overlap_summary[category]["nearby_count"] += 1

                if (
                    feature_name
                    and feature_name != "Unnamed"
                    and feature_name not in overlap_summary[category]["nearby_examples"]
                    and len(overlap_summary[category]["nearby_examples"]) < 3
                ):
                    overlap_summary[category]["nearby_examples"].append(feature_name)

        # ------------------------------------------------------------
        # SECTION 1: ASSOCIATED SPATIAL DATA
        # ------------------------------------------------------------
        add_section("=== ASSOCIATED SPATIAL DATA ===")

        for category in [
            "Water Agency Districts",
            "Tribal Lands",
            "Fish Habitats",
            "Protected / Restricted Areas",
        ]:
            direct_count = overlap_summary[category]["direct_count"]
            nearby_count = overlap_summary[category]["nearby_count"]
            direct_examples = overlap_summary[category]["direct_examples"]
            nearby_examples = overlap_summary[category]["nearby_examples"]

            value_parts = []
            proximity_parts = []

            if direct_count > 0:
                direct_text = f"{direct_count}"
                if direct_examples:
                    direct_text += f": {', '.join(direct_examples)}"
                value_parts.append(direct_text)
                proximity_parts.append("Direct")

            if nearby_count > 0:
                nearby_text = f"{nearby_count}"
                if nearby_examples:
                    nearby_text += f": {', '.join(nearby_examples)}"
                value_parts.append(nearby_text)
                proximity_parts.append("Nearby")

            if value_parts:
                value = " | ".join(value_parts)
                proximity = " / ".join(proximity_parts)
            else:
                value = "No"
                proximity = ""

            add_row(category, value, "metadata polygon relation", proximity=proximity, tooltip=value)

        other_direct_count = overlap_summary["Other"]["direct_count"]
        other_nearby_count = overlap_summary["Other"]["nearby_count"]
        other_direct_examples = overlap_summary["Other"]["direct_examples"]
        other_nearby_examples = overlap_summary["Other"]["nearby_examples"]

        other_parts = []

        if other_direct_count > 0:
            other_direct_text = f"Direct ({other_direct_count})"
            if other_direct_examples:
                other_direct_text += f": {', '.join(other_direct_examples)}"
            other_parts.append(other_direct_text)

        if other_nearby_count > 0:
            other_nearby_text = f"Nearby ({other_nearby_count})"
            if other_nearby_examples:
                other_nearby_text += f": {', '.join(other_nearby_examples)}"
            other_parts.append(other_nearby_text)

        if other_parts:
            other_value = " | ".join(other_parts)
            add_row("Other Spatial Overlaps", other_value, "metadata polygon relation", tooltip=other_value)
        add_blank_row()

        closest_substation = self.find_closest_substation_for_waterbody(feature, source_layer)

        if closest_substation:
            add_section("=== NEAREST POWER SUBSTATION ===")

            substation_name = closest_substation.get("substation_name", "").strip()

            if not substation_name:
                substation_name = (
                    closest_substation.get("name", "").strip()
                    or closest_substation.get("substation", "").strip()
                    or closest_substation.get("id", "").strip()
                    or closest_substation.get("osm_id", "").strip()
                    or "Unnamed Substation"
    )
            distance_miles = closest_substation.get("distance_miles", "")
            distance_m = closest_substation.get("distance_m", "")

            add_row("Substation Name", substation_name, "HydroGlow")
            add_row("Distance (miles)", distance_miles, "HydroGlow")
            add_row("Distance (meters)", distance_m, "HydroGlow")

            add_blank_row()

        # ------------------------------------------------------------
        # SECTION 2: GPKG POLYGON ATTRIBUTES
        # Show useful attribute fields from overlapping GPKG polygon datasets
        # ------------------------------------------------------------
        grouped_gpkg_features = self.group_polygon_features_with_proximity(overlapping_polygons, nearby_polygons)

        if grouped_gpkg_features:
            add_section("=== GPKG POLYGON ATTRIBUTES ===")

            for filename, info in grouped_gpkg_features.items():
                feature_list = info.get("features", [])
                proximity_labels = info.get("proximity_labels", set())

                if not feature_list:
                    continue

                first_feature = feature_list[0]
                display_fields = self.get_displayable_gpkg_fields(filename, first_feature)

                if not display_fields:
                    continue

                source_label = get_metadata_source_label(filename)

                if "Direct" in proximity_labels and "Nearby" in proximity_labels:
                    proximity_text = "Direct / Nearby"
                elif "Direct" in proximity_labels:
                    proximity_text = "Direct"
                elif "Nearby" in proximity_labels:
                    proximity_text = "Nearby"
                else:
                    proximity_text = ""

                add_row(
                    source_label,
                    f"{len(feature_list)} related feature(s)",
                    filename,
                    proximity=proximity_text
                )

                for field_name, value_str in display_fields:
                    add_row(
                        f"  {field_name}",
                        value_str,
                        filename,
                        proximity=proximity_text,
                        tooltip=value_str
                    )

                add_blank_row()
        # ------------------------------------------------------------
        # SECTION 2: REGIONAL ENERGY DATA
        # ------------------------------------------------------------
        electricity_cost, ppa_average, census_division, state = self.get_regional_energy_data(feature, source_layer)

        # Try overlap features if waterbody itself doesn't provide enough
        if electricity_cost is None and ppa_average is None and overlapping_polygons:
            for overlap in overlapping_polygons:
                overlap_feature = overlap["feature"]
                overlap_layer = overlap["entry"]["source_layer"]
                electricity_cost, ppa_average, census_division, state = self.get_regional_energy_data(overlap_feature, overlap_layer)
                if electricity_cost is not None or ppa_average is not None:
                    break

        add_section("=== REGIONAL ENERGY DATA ===")

        if state:
            add_row("State", state, "feature geometry / metadata lookup")

        if census_division:
            add_row("Census Division", census_division, "census_areas_lookup.csv")

        if electricity_cost is not None:
            add_row("Avg. Electricity Price", f"{electricity_cost:.2f} ¢/kWh", "regional_electricity_costs.csv")

        if ppa_average is not None:
            add_row("Avg. PPA Price (Regional)", f"{ppa_average:.2f} ¢/kWh", "regional_ppa_averages_proxy.csv")

        add_blank_row()

        # ------------------------------------------------------------
        # SECTION 3: CSV METADATA
        # Only show truly matched / approved display fields
        # ------------------------------------------------------------
        matched_csv_rows = self.find_matching_csv_rows_for_waterbody(feature, source_layer)

        if matched_csv_rows:
            added_csv_section = False

            for filename, matched_rows in matched_csv_rows.items():
                first_row = matched_rows[0]
                display_fields = self.get_display_fields_for_csv(filename, first_row)

                if not display_fields:
                    continue

                if not added_csv_section:
                    add_section("=== CSV METADATA ===")
                    added_csv_section = True

                section_title = self.CSV_SECTION_TITLES.get(filename, filename)
                add_row(section_title, filename, filename)

                for key, value_str in display_fields:
                    add_row(f"  {key}", value_str, filename, tooltip=value_str)

                add_blank_row()
        # ------------------------------------------------------------
        # SECTION 4: NEARBY CSV DATA
        # Show coordinate-based datasets near the selected waterbody
        # ------------------------------------------------------------
        nearby_csv_rows = self.find_nearby_csv_rows_for_waterbody(feature, source_layer)

        if nearby_csv_rows:
            add_section("=== NEARBY CSV DATA ===")

            for filename, nearby_rows in nearby_csv_rows.items():
                section_title = self.CSV_SECTION_TITLES.get(filename, filename)
                add_row(section_title, f"{len(nearby_rows)} nearby row(s)", filename)

                config = self.CSV_NEARBY_CONFIG.get(filename, {})
                display_fields = config.get("display_fields", [])

                first_row = nearby_rows[0]
                add_row("  Distance (km)", first_row.get("_distance_km", ""), filename)

                normalized_lookup = {}
                for key, value in first_row.items():
                    normalized_lookup[self.normalize_key(key)] = (key, value)

                for field_name in display_fields:
                    norm_field = self.normalize_key(field_name)
                    if norm_field in normalized_lookup:
                        original_key, value = normalized_lookup[norm_field]
                        if value is None:
                            continue
                        value_str = str(value).strip()
                        if not value_str:
                            continue
                        add_row(f"  {original_key}", value_str, filename, tooltip=value_str)

                add_blank_row()

        # ------------------------------------------------------------
        # SECTION 5: NEARBY GPKG POINT DATA
        # Show checked point datasets near or inside the selected waterbody
        # ------------------------------------------------------------
        nearby_point_features = self.find_nearby_point_features_for_waterbody(feature, source_layer)

        if nearby_point_features:
            add_section("=== NEARBY GPKG POINT DATA ===")

            for filename, point_matches in nearby_point_features.items():
                first_match = point_matches[0]
                pt_feature = first_match["feature"]
                distance_km = first_match["distance_km"]

                display_fields = self.get_displayable_gpkg_fields(filename, pt_feature)

                if not display_fields:
                    continue

                source_label = get_metadata_source_label(filename)

                add_row(source_label, f"{len(point_matches)} nearby point(s)", filename)
                add_row("  Distance (km)", distance_km, filename)

                for field_name, value_str in display_fields:
                    add_row(f"  {field_name}", value_str, filename, tooltip=value_str)

                add_blank_row()
        # ------------------------------------------------------------
        # SECTION 5: WATERBODY METADATA
        # ------------------------------------------------------------
        add_section("=== WATERBODY METADATA ===")

        preferred_waterbody_fields = [
            "name", "gnis_name", "waterbody", "waterbody_type",
            "area_sq_mi", "area_sq_km", "state", "county"
        ]

        displayed_any = False

        for i, field in enumerate(fields):
            field_name = field.name()
            value = attributes[i]

            if field_name.lower() not in preferred_waterbody_fields:
                continue

            if value is None or value == NULL:
                value_str = "<NULL>"
            elif isinstance(value, float):
                if abs(value) < 0.0001 and value != 0:
                    value_str = f"{value:.6e}"
                elif abs(value) < 1:
                    value_str = f"{value:.6f}"
                else:
                    value_str = f"{value:.4f}"
            else:
                value_str = str(value)

            add_row(field_name, value_str, get_layer_source_name(source_layer))
            displayed_any = True

        if not displayed_any:
            add_row("No preferred metadata fields found", "", get_layer_source_name(source_layer))

        # ------------------------------------------------------------
        # SECTION 6: GEOMETRY / DERIVED INFO
        # ------------------------------------------------------------
        geom = feature.geometry()
        if geom and not geom.isEmpty():
            add_blank_row()
            add_section("=== DERIVED GEOMETRY INFO ===")

            geom_type_text = str(geom.type())
            add_row("Geometry Type", geom_type_text, get_layer_source_name(source_layer))

            area_calc = QgsDistanceArea()
            area_calc.setEllipsoid("WGS84")

            try:
                if source_layer:
                    area_calc.setSourceCrs(source_layer.crs(), QgsProject.instance().transformContext())
                area_sq_m = area_calc.measureArea(geom)
                add_row("Area (sq meters)", f"{area_sq_m:.2f}", get_layer_source_name(source_layer))
            except Exception:
                add_row("Area (sq meters)", "Unavailable", get_layer_source_name(source_layer))

               # ------------------------------------------------------------
        # SECTION 7: ML-DERIVED WATERBODY DATA
        # ------------------------------------------------------------
        add_blank_row()
        add_section("=== ML-DERIVED WATERBODY DATA ===")

        novel_feature = self.get_novel_border_feature_for_waterbody(feature, source_layer)
        novel_source = "HydroGlow Novel Data"

        def novel_value(field_name):
            if not novel_feature:
                return None
            if field_name not in novel_feature.fields().names():
                return None
            value = novel_feature[field_name]
            if value is None or value == NULL:
                return None
            return value

        def pretty_text(value):
            return value.capitalize() if isinstance(value, str) else value

        add_row("Water Body ID", novel_value("water_body_id"), novel_source)
        add_row("Border ID", pretty_text(novel_value("border_id")), novel_source)
        add_row("Recreation", pretty_text(novel_value("recreation")), novel_source)
        add_row("Dry-up Status", pretty_text(novel_value("dry_up_status")), novel_source)

        add_row("Max Depth (m)", self.format_ml_value(novel_value("max_depth_m")), novel_source)
        add_row("Mean Depth (m)", self.format_ml_value(novel_value("mean_depth_m")), novel_source)
        add_row("Min Depth (m)", self.format_ml_value(novel_value("min_depth_m")), novel_source)

        add_row("Water Frequency", self.format_ml_value(novel_value("water_frequency")), novel_source)
        add_row("Max Water Extent (km²)", self.format_ml_value(novel_value("max_water_extent_km2")), novel_source)
        add_row("Min Water Extent (km²)", self.format_ml_value(novel_value("min_water_extent_km2")), novel_source)

        add_row("Avg Water Temperature (°C)", self.format_ml_value(novel_value("avg_temp_c")), novel_source)
        add_row("Max Water Temperature (°C)", self.format_ml_value(novel_value("max_temp_c")), novel_source)
        add_row("Min Water Temperature (°C)", self.format_ml_value(novel_value("min_temp_c")), novel_source)

        algae_level = novel_value("algae_level")
        avg_algae = novel_value("avg_algae_ndci")

        if algae_level:
            algae_presence = "Yes" if str(algae_level).strip().lower() not in ["none", "no", "0"] else "No"
        elif avg_algae is not None:
            algae_presence = "Yes" if float(avg_algae) > 0.01 else "No"
        else:
            algae_presence = "Not available"

        add_row("Algae Presence", algae_presence, novel_source)
        add_row("Algae Level", pretty_text(algae_level), novel_source)
        add_row("Algae Quantity / NDCI", self.format_ml_value(avg_algae), novel_source)

        # Populate the Novel Data Layers section in the left panel with rasters
        # whose extent intersects this waterbody.
        self.populate_novel_data_layers_for_waterbody(feature, source_layer)

    def clear_waterbody_selection(self):
        """Clear the selected waterbody and reset the metadata panel"""
        # Remove selection layer
        if self.selected_waterbody_layer:
            QgsProject.instance().removeMapLayer(self.selected_waterbody_layer)
            self.selected_waterbody_layer = None
        
        # Remove nearest agency layer
        if self.nearest_agency_layer:
            QgsProject.instance().removeMapLayer(self.nearest_agency_layer)
            self.nearest_agency_layer = None

        # Remove substation connection layers
        self._clear_substation_connection_layers()
        
        # Reset metadata table
        self.metadata_table.setRowCount(1)
        self.metadata_table.setItem(0, 0, QTableWidgetItem("No waterbody selected"))
        self.metadata_table.setItem(0, 1, QTableWidgetItem("Click on a waterbody polygon to view its metadata"))
        self.metadata_table.setItem(0, 2, QTableWidgetItem(""))
        self.metadata_table.setItem(0, 3, QTableWidgetItem(""))
        self.metadata_table.setItem(0, 4, QTableWidgetItem(""))

        # Hide the Novel Data Layers section
        self.populate_novel_data_layers_for_waterbody(None, None)

        self.refresh_canvas_layers()
        self.statusBar().showMessage("Selection cleared")
    
    def add_marker(self, point):
        # Remove old marker if exists
        if self.marker_layer:
            QgsProject.instance().removeMapLayer(self.marker_layer)
        
        # Create memory layer for marker in EPSG:3857
        self.marker_layer = QgsVectorLayer("Point?crs=EPSG:3857", "Selected Location", "memory")
        provider = self.marker_layer.dataProvider()
        
        # Create feature with point geometry
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromPointXY(point))
        provider.addFeature(feature)
        
        # Style the marker
        symbol = QgsMarkerSymbol.createSimple({
            'name': 'circle',
            'color': '255,0,0,255',
            'size': '4',
            'outline_color': 'white',
            'outline_width': '0.5'
        })
        self.marker_layer.renderer().setSymbol(symbol)
        
        # Add layer to project
        QgsProject.instance().addMapLayer(self.marker_layer)
        
        self.refresh_canvas_layers()
        
    def zoom_to_point(self, point_3857):
        """Zoom the map canvas to center on a point with a reasonable default extent"""
        # Default extent of roughly 3000 miles around the point
        buffer_size = 4828000  # ~3000 miles in meters
        extent = QgsRectangle(
            point_3857.x() - buffer_size,
            point_3857.y() - buffer_size,
            point_3857.x() + buffer_size,
            point_3857.y() + buffer_size
        )
        self.canvas.setExtent(extent)

    def zoom_in(self):
        self.canvas.zoomIn()
        
    def zoom_out(self):
        self.canvas.zoomOut()
        
    def zoom_full(self):
        self.canvas.zoomToFullExtent()

    def zoom_to_data_source(self, entry):
        """Zoom the map canvas to the extent of a data source layer"""
        # Prefer the results_layer (buffered subset) if it exists, else use source_layer
        layer = entry.get("results_layer") or entry.get("source_layer")
        if not layer or not layer.isValid():
            return

        extent = layer.extent()
        if extent.isEmpty():
            return

        # Transform extent to canvas CRS (EPSG:3857) if needed
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        if layer.crs().authid() != canvas_crs.authid():
            transform = QgsCoordinateTransform(layer.crs(), canvas_crs, QgsProject.instance())
            extent = transform.transformBoundingBox(extent)

        # Add a small margin around the extent
        extent.scale(1.1)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def zoom_to_waterbodies(self):
        """Zoom the map canvas to the combined extent of all waterbody layers"""
        if not self.waterbody_layers:
            return
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        combined = QgsRectangle()
        for layer in self.waterbody_layers:
            if layer and layer.isValid() and not layer.extent().isEmpty():
                ext = layer.extent()
                if layer.crs().authid() != canvas_crs.authid():
                    transform = QgsCoordinateTransform(layer.crs(), canvas_crs, QgsProject.instance())
                    ext = transform.transformBoundingBox(ext)
                combined.combineExtentWith(ext)
        if not combined.isEmpty():
            combined.scale(1.1)
            self.canvas.setExtent(combined)
            self.canvas.refresh()

    def add_osm_layer(self):
        # Add OpenStreetMap as base layer
        osm_url = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        self.osm_layer = QgsRasterLayer(osm_url, "OpenStreetMap", "wms")
        
        if self.osm_layer.isValid():
            QgsProject.instance().addMapLayer(self.osm_layer)
            self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
            self.canvas.setExtent(self.osm_layer.extent())
            self.canvas.setLayers([self.osm_layer])
            self.canvas.refresh()
        else:
            print("Failed to load OpenStreetMap layer")
    
    def try_load_default_waterbody_data(self):
        """Try to load all waterbody .gpkg files from the waterbody_polygons folder"""
        # Get the directory where this script is located
        script_dir = os.path.dirname(os.path.abspath(__file__))
        waterbody_folder = os.path.join(script_dir, "waterbody_polygons")
        
        # Check if the folder exists
        if not os.path.exists(waterbody_folder):
            self.results_log.append(f"Waterbody folder not found: waterbody_polygons/\n")
            return
        
        # Find all .gpkg files in the waterbody_polygons folder
        waterbody_files = []
        for file in os.listdir(waterbody_folder):
            if file.endswith('.gpkg'):
                waterbody_files.append(os.path.join(waterbody_folder, file))
        
        # Load all found waterbody files
        if waterbody_files:
            if self.load_all_waterbody_layers(waterbody_files):
                self.results_log.append(f"Auto-loaded {len(waterbody_files)} waterbody file(s) from waterbody_polygons/\n")
    
    def load_all_waterbody_layers(self, filepaths):
        """Load multiple waterbody polygon layers from files"""
        # Remove old waterbody layers if exist
        for layer in self.waterbody_layers:
            if layer.id() in QgsProject.instance().mapLayers():
                QgsProject.instance().removeMapLayer(layer)
        self.waterbody_layers = []
        
        for filepath in filepaths:
            layer_name = os.path.basename(filepath).split('.')[0]
            layer = QgsVectorLayer(filepath, f"Waterbodies ({layer_name})", "ogr")
            
            if not layer.isValid():
                print(f"Failed to load waterbody file: {os.path.basename(filepath)}")
                continue
            
            # Style the waterbody layer (semi-transparent blue)
            symbol = QgsFillSymbol.createSimple({
                'color': '100,150,255,80',
                'outline_color': '50,100,200,200',
                'outline_width': '0.5'
            })
            layer.renderer().setSymbol(symbol)
            
            # Add to project but hide by default
            QgsProject.instance().addMapLayer(layer, False)
            self.waterbody_layers.append(layer)
        
        if self.waterbody_layers:
            self.refresh_canvas_layers()
            return True
        else:
            return False
    
    def activate_single_buildable_area_tool(self):
        """Activate the map tool for selecting a single waterbody to generate buildable area"""
        QMessageBox.information(self, "Select a Waterbody", 
                              "Click on a waterbody polygon on the map to generate its buildable area.")
        self.canvas.setMapTool(self.single_buildable_tool)
        self.statusBar().showMessage("Click on a waterbody to generate its buildable area polygon")
    
    def on_single_buildable_waterbody_clicked(self, point, button):
        """Handle click events for single waterbody buildable area generation"""
        if not self.waterbody_layers and not self.results_layer:
            self.statusBar().showMessage("No waterbody layers loaded")
            return
        
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        
        # Create a small search area around the clicked point
        search_radius = self.canvas.mapUnitsPerPixel() * 5
        search_rect = QgsRectangle(
            point.x() - search_radius,
            point.y() - search_radius,
            point.x() + search_radius,
            point.y() + search_radius
        )
        
        clicked_point_geom = QgsGeometry.fromPointXY(point)
        
        # First check the results layer (found waterbodies from search)
        found_feature = None
        source_layer = None
        
        if self.results_layer and self.results_layer.id() in QgsProject.instance().mapLayers():
            request = QgsFeatureRequest().setFilterRect(search_rect)
            for feature in self.results_layer.getFeatures(request):
                if feature.geometry().contains(clicked_point_geom):
                    found_feature = feature
                    source_layer = self.results_layer
                    break
        
        # If not found in results, check all waterbody layers
        if not found_feature:
            for waterbody_layer in self.waterbody_layers:
                if waterbody_layer.id() not in QgsProject.instance().mapLayers():
                    continue
                
                layer_crs = waterbody_layer.crs()
                if layer_crs != crs_3857:
                    transform = QgsCoordinateTransform(crs_3857, layer_crs, QgsProject.instance())
                    layer_search_rect = transform.transformBoundingBox(search_rect)
                    layer_point_geom = QgsGeometry(clicked_point_geom)
                    layer_point_geom.transform(transform)
                else:
                    layer_search_rect = search_rect
                    layer_point_geom = clicked_point_geom
                
                request = QgsFeatureRequest().setFilterRect(layer_search_rect)
                for feature in waterbody_layer.getFeatures(request):
                    if feature.geometry().contains(layer_point_geom):
                        found_feature = feature
                        source_layer = waterbody_layer
                        break
                
                if found_feature:
                    break
        
        if not found_feature:
            self.statusBar().showMessage("No waterbody found at this location")
            return
        
        # Get the waterbody geometry in EPSG:3857
        waterbody_geom = QgsGeometry(found_feature.geometry())
        if source_layer.crs().authid() != crs_3857.authid():
            transform = QgsCoordinateTransform(source_layer.crs(), crs_3857, QgsProject.instance())
            waterbody_geom.transform(transform)

        # Collect restricted metadata geometries near this waterbody
        waterbody_buffer = waterbody_geom.boundingBox()
        buffer_geom = QgsGeometry.fromRect(waterbody_buffer)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        weights = load_layer_weights(script_dir)
        metadata_geometries = collect_restricted_geometries(self.metadata_polygon_entries, buffer_geom, weights)

        if not metadata_geometries:
            self.statusBar().showMessage("No restricted metadata polygons overlap this waterbody — entire waterbody is buildable")
            metadata_geometries = []

        # Use subtract_overlaps from buildable_area_polygon
        buildable_geom = subtract_overlaps(waterbody_geom, metadata_geometries)

        if not buildable_geom:
            QMessageBox.information(self, "No Buildable Area",
                                  "This waterbody is completely covered by metadata polygons. No buildable area remains.")
            self.canvas.setMapTool(self.pan_tool)
            return

        # Remove old buildable area layer if exists
        if self.buildable_area_layer:
            if self.buildable_area_layer.id() in QgsProject.instance().mapLayers():
                QgsProject.instance().removeMapLayer(self.buildable_area_layer)
            self.buildable_area_layer = None

        # Create the buildable area layer
        self.buildable_area_layer = QgsVectorLayer("MultiPolygon?crs=EPSG:3857", "Buildable Areas", "memory")
        provider = self.buildable_area_layer.dataProvider()

        # Copy field definitions from source layer
        provider.addAttributes(source_layer.fields())
        self.buildable_area_layer.updateFields()

        new_feature = QgsFeature(source_layer.fields())
        new_feature.setGeometry(buildable_geom)
        new_feature.setAttributes(found_feature.attributes())
        provider.addFeature(new_feature)

        # Save to gpkg
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_path = os.path.join(script_dir, "buildable_areas", "buildable_areas.gpkg")
        save_buildable_areas_gpkg(self.buildable_area_layer, output_path)

        # Style the buildable area layer - bright green
        symbol = QgsFillSymbol.createSimple({
            'color': '50,255,50,120',
            'outline_color': '0,180,0,255',
            'outline_width': '2.0'
        })
        self.buildable_area_layer.renderer().setSymbol(symbol)

        # Add to map
        QgsProject.instance().addMapLayer(self.buildable_area_layer)

        # Enable and check the Buildable Areas checkbox
        self.show_buildable_areas_checkbox.setEnabled(True)
        self.show_buildable_areas_checkbox.setChecked(True)

        self.refresh_canvas_layers()

        waterbody_name = self.get_feature_name(found_feature)
        self.statusBar().showMessage(f"Generated buildable area for: {waterbody_name}")

        # Switch back to pan tool
        self.canvas.setMapTool(self.pan_tool)

    def toggle_buildable_areas_visibility(self, state):
        """Toggle visibility of the buildable area layer on the map"""
        self.refresh_canvas_layers()

    def activate_heatmap_tool(self):
        """Activate the map tool for selecting a waterbody to generate a suitability heatmap"""
        QMessageBox.information(self, "Select a Waterbody",
                              "Click on a waterbody polygon on the map to generate its suitability heatmap.")
        self.canvas.setMapTool(self.heatmap_tool)
        self.statusBar().showMessage("Click on a waterbody to generate its suitability heatmap")

    def on_heatmap_waterbody_clicked(self, point, button):
        """Handle click events for heatmap generation on a single waterbody"""
        if not self.waterbody_layers and not self.results_layer:
            self.statusBar().showMessage("No waterbody layers loaded")
            return

        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")

        # Create a small search area around the clicked point
        search_radius = self.canvas.mapUnitsPerPixel() * 5
        search_rect = QgsRectangle(
            point.x() - search_radius, point.y() - search_radius,
            point.x() + search_radius, point.y() + search_radius
        )
        clicked_point_geom = QgsGeometry.fromPointXY(point)

        # Search results layer first, then waterbody layers
        found_feature = None
        source_layer = None

        if self.results_layer and self.results_layer.id() in QgsProject.instance().mapLayers():
            request = QgsFeatureRequest().setFilterRect(search_rect)
            for feature in self.results_layer.getFeatures(request):
                if feature.geometry().contains(clicked_point_geom):
                    found_feature = feature
                    source_layer = self.results_layer
                    break

        if not found_feature:
            for waterbody_layer in self.waterbody_layers:
                if not waterbody_layer.id() in QgsProject.instance().mapLayers():
                    continue
                layer_search_rect = search_rect
                layer_point_geom = clicked_point_geom
                if waterbody_layer.crs().authid() != crs_3857.authid():
                    transform = QgsCoordinateTransform(crs_3857, waterbody_layer.crs(), QgsProject.instance())
                    layer_search_rect = transform.transformBoundingBox(search_rect)
                    layer_point_geom = QgsGeometry(clicked_point_geom)
                    layer_point_geom.transform(transform)

                request = QgsFeatureRequest().setFilterRect(layer_search_rect)
                for feature in waterbody_layer.getFeatures(request):
                    if feature.geometry().contains(layer_point_geom):
                        found_feature = feature
                        source_layer = waterbody_layer
                        break
                if found_feature:
                    break

        if not found_feature:
            self.statusBar().showMessage("No waterbody found at this location")
            return

        # Get the waterbody geometry in EPSG:3857
        waterbody_geom = QgsGeometry(found_feature.geometry())
        if source_layer.crs().authid() != crs_3857.authid():
            transform = QgsCoordinateTransform(source_layer.crs(), crs_3857, QgsProject.instance())
            waterbody_geom.transform(transform)

        # Remove old heatmap layer if exists
        if self.heatmap_layer:
            if self.heatmap_layer.id() in QgsProject.instance().mapLayers():
                QgsProject.instance().removeMapLayer(self.heatmap_layer)
            self.heatmap_layer = None

        # Load weights and generate heatmap
        script_dir = os.path.dirname(os.path.abspath(__file__))
        weights = load_layer_weights(script_dir)
        output_path = os.path.join(script_dir, "buildable_areas", "suitability_heatmap.tif")

        result = compute_heatmap(
            waterbody_geom, self.metadata_polygon_entries, weights, output_path
        )

        if not result:
            QMessageBox.information(self, "Heatmap Failed",
                                  "Could not generate suitability heatmap for this waterbody.")
            self.canvas.setMapTool(self.pan_tool)
            return

        output_path, max_overlap = result

        # Load the raster layer
        self.heatmap_layer = QgsRasterLayer(output_path, "Suitability Heatmap", "gdal")
        if not self.heatmap_layer.isValid():
            QMessageBox.warning(self, "Load Failed", "Generated heatmap file could not be loaded.")
            self.heatmap_layer = None
            self.canvas.setMapTool(self.pan_tool)
            return

        # Style with blue-to-green-to-red color ramp (-5 = highly suitable, 0 = neutral, +5 = highly unsuitable)
        from qgis.core import (
            QgsRasterShader,
            QgsColorRampShader,
            QgsSingleBandPseudoColorRenderer,
        )

        max_val = max(max_overlap, 1.0)  # avoid division by zero

        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader()
        # Discrete mode: one hard color band per integer score value (-5 to +5).
        # Each stop's value is the upper boundary of that band (midpoints between integers).
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        color_ramp.setColorRampItemList([
            QgsColorRampShader.ColorRampItem(-4.5, QColor( 90,   0, 140, 200), "-5  Highly Suitable"),
            QgsColorRampShader.ColorRampItem(-3.5, QColor(130,  50, 200, 200), "-4"),
            QgsColorRampShader.ColorRampItem(-2.5, QColor( 80, 100, 230, 200), "-3"),
            QgsColorRampShader.ColorRampItem(-1.5, QColor( 40, 150, 255, 200), "-2"),
            QgsColorRampShader.ColorRampItem(-0.5, QColor(100, 200, 255, 200), "-1  Moderately Suitable"),
            QgsColorRampShader.ColorRampItem( 0.5, QColor(  0, 185,   0, 200), " 0  Neutral"),
            QgsColorRampShader.ColorRampItem( 1.5, QColor(255, 220,   0, 200), "+1  Moderately Unsuitable"),
            QgsColorRampShader.ColorRampItem( 2.5, QColor(255, 160,   0, 200), "+2"),
            QgsColorRampShader.ColorRampItem( 3.5, QColor(255,  80,   0, 200), "+3"),
            QgsColorRampShader.ColorRampItem( 4.5, QColor(220,   0,   0, 200), "+4"),
            QgsColorRampShader.ColorRampItem( 6.0, QColor(155,   0,   0, 200), "+5  Highly Unsuitable"),
        ])
        shader.setRasterShaderFunction(color_ramp)

        renderer = QgsSingleBandPseudoColorRenderer(
            self.heatmap_layer.dataProvider(), 1, shader
        )
        self.heatmap_layer.setRenderer(renderer)
        self.heatmap_layer.setOpacity(0.8)

        QgsProject.instance().addMapLayer(self.heatmap_layer)

        self.show_heatmap_checkbox.setEnabled(True)
        self.show_heatmap_checkbox.setChecked(True)

        # Show legend overlay on the canvas
        self._heatmap_max_val = max_val
        self._show_heatmap_legend(max_val)

        self.refresh_canvas_layers()

        waterbody_name = self.get_feature_name(found_feature)
        self.statusBar().showMessage(f"Generated suitability heatmap for: {waterbody_name}")

        # Switch back to pan tool
        self.canvas.setMapTool(self.pan_tool)

    def toggle_heatmap_visibility(self, state):
        """Toggle visibility of the heatmap layer on the map"""
        if state == Qt.Checked:
            if self._heatmap_max_val is not None:
                self._show_heatmap_legend(self._heatmap_max_val)
        else:
            self._remove_heatmap_legend()
        self.refresh_canvas_layers()

    def _show_heatmap_legend(self, max_val):
        """Show a color legend overlay on the map canvas"""
        self._remove_heatmap_legend()

        legend = QWidget(self.canvas)
        legend.setObjectName("heatmap_legend")
        legend.setStyleSheet(
            "QWidget#heatmap_legend { background-color: rgba(255,255,255,220); "
            "border: 1px solid #888; border-radius: 4px; }"
        )

        layout = QVBoxLayout(legend)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        title = QLabel("Suitability")
        title.setStyleSheet("font-weight: bold; font-size: 11px; border: none;")
        layout.addWidget(title)

        items = [
            ("rgba(90,0,140,200)",   "-5  Highly Suitable"),
            ("rgba(130,50,200,200)", "-4"),
            ("rgba(80,100,230,200)", "-3"),
            ("rgba(40,150,255,200)", "-2  Moderately Suitable"),
            ("rgba(100,200,255,200)","-1"),
            ("rgba(0,185,0,200)",    " 0  Neutral"),
            ("rgba(255,220,0,200)",  "+1"),
            ("rgba(255,160,0,200)",  "+2  Moderately Unsuitable"),
            ("rgba(255,80,0,200)",   "+3"),
            ("rgba(220,0,0,200)",    "+4"),
            ("rgba(155,0,0,200)",    "+5  Highly Unsuitable"),
        ]

        for color, label_text in items:
            row = QWidget()
            row.setStyleSheet("border: none;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)

            swatch = QLabel()
            swatch.setFixedSize(14, 14)
            swatch.setStyleSheet(
                f"background-color: {color}; border: 1px solid #666;"
            )
            row_layout.addWidget(swatch)

            label = QLabel(label_text)
            label.setStyleSheet("font-size: 10px; border: none;")
            row_layout.addWidget(label)
            row_layout.addStretch()

            layout.addWidget(row)

        legend.adjustSize()
        # Position in bottom-right corner of canvas
        legend.move(self.canvas.width() - legend.width() - 12,
                    self.canvas.height() - legend.height() - 12)
        legend.show()
        self._heatmap_legend = legend

    def _remove_heatmap_legend(self):
        """Remove the heatmap legend overlay if it exists"""
        if hasattr(self, '_heatmap_legend') and self._heatmap_legend:
            self._heatmap_legend.deleteLater()
            self._heatmap_legend = None

    def toggle_buffer_visibility(self, state):
        """Toggle visibility of the buffer circle on the map"""
        self.refresh_canvas_layers()

    def toggle_waterbody_visibility(self, state):
        """Toggle visibility of the waterbody results layer on the map"""
        self.refresh_canvas_layers()
    
    def refresh_canvas_layers(self):
        """Refresh canvas with proper layer ordering"""
        layers = []

        # Substation connection (marker + path) goes on top
        if self.nearest_substation_layer and self.nearest_substation_layer.id() in QgsProject.instance().mapLayers():
            layers.append(self.nearest_substation_layer)
        if self.substation_path_layer and self.substation_path_layer.id() in QgsProject.instance().mapLayers():
            layers.append(self.substation_path_layer)

        # Add selected waterbody layer on top (dark red highlight)
        if self.selected_waterbody_layer and self.selected_waterbody_layer.id() in QgsProject.instance().mapLayers():
            layers.append(self.selected_waterbody_layer)
        
        # Add nearest agency layer (light red/pink highlight) - below selected waterbody
        if self.nearest_agency_layer and self.nearest_agency_layer.id() in QgsProject.instance().mapLayers():
            layers.append(self.nearest_agency_layer)
        
        # Add metadata polygon results layers for checked entries
        for entry in self.metadata_polygon_entries:
            if entry["checkbox"].isChecked() and entry["results_layer"]:
                if entry["results_layer"].id() in QgsProject.instance().mapLayers():
                    layers.append(entry["results_layer"])
        
        # Add buildable area layer if checkbox is checked
        if self.show_buildable_areas_checkbox.isChecked() and self.buildable_area_layer:
            if self.buildable_area_layer.id() in QgsProject.instance().mapLayers():
                layers.append(self.buildable_area_layer)

        # Add heatmap layer if checkbox is checked
        if self.show_heatmap_checkbox.isChecked() and self.heatmap_layer:
            if self.heatmap_layer.id() in QgsProject.instance().mapLayers():
                layers.append(self.heatmap_layer)

        # Add results layer if Waterbodies checkbox is checked
        if self.show_waterbodies_checkbox.isChecked() and self.results_layer and self.results_layer.id() in QgsProject.instance().mapLayers():
            layers.append(self.results_layer)
        
        # Add marker layer: always show if no buffer exists, otherwise follow checkbox
        if self.marker_layer and self.marker_layer.id() in QgsProject.instance().mapLayers():
            if not self.buffer_layer or self.show_buffer_checkbox.isChecked():
                layers.append(self.marker_layer)

        # Add buffer layer if checkbox is checked
        if self.show_buffer_checkbox.isChecked() and self.buffer_layer and self.buffer_layer.id() in QgsProject.instance().mapLayers():
            layers.append(self.buffer_layer)
        
        
        # Add raster layers (above base map, below vectors) only if they intersect the buffer
        if self.buffer_layer:
            buffer_feat = next(self.buffer_layer.getFeatures(), None)
            buffer_geom = buffer_feat.geometry() if buffer_feat else None
        else:
            buffer_geom = None
        for entry in self.metadata_polygon_entries:
            if entry.get("is_raster") and entry["checkbox"].isChecked():
                if entry["source_layer"].id() not in QgsProject.instance().mapLayers():
                    continue
                if not buffer_geom:
                    continue
                # Check if raster extent intersects buffer (transform raster extent to buffer CRS)
                raster_extent = entry["source_layer"].extent()
                raster_crs = entry["source_layer"].crs()
                buffer_crs = self.buffer_layer.crs()
                if raster_crs != buffer_crs:
                    xform = QgsCoordinateTransform(raster_crs, buffer_crs, QgsProject.instance())
                    raster_extent = xform.transformBoundingBox(raster_extent)
                raster_rect_geom = QgsGeometry.fromRect(raster_extent)
                if raster_rect_geom.intersects(buffer_geom):
                    layers.append(entry["source_layer"])

        # Add OSM base layer
        if hasattr(self, 'osm_layer') and self.osm_layer:
            if self.osm_layer.id() in QgsProject.instance().mapLayers():
                layers.append(self.osm_layer)
        
        self.canvas.setLayers(layers)
        self.canvas.refresh()
            
    def apply_search(self):
        """Perform the waterbody search"""
        # Uncheck all data source checkboxes (this also removes their result layers via the toggle handler)
        for entry in self.metadata_polygon_entries:
            if entry["checkbox"].isChecked():
                entry["checkbox"].setChecked(False)

        # Check if waterbody data is loaded
        if not self.waterbody_layers:
            QMessageBox.warning(self, "No Data", 
                              "No waterbody data found. Please ensure .gpkg files are in the waterbody_polygons/ folder.")
            return
        
        # Validate required fields
        if not self.latitude_input.text() or not self.longitude_input.text() or not self.buffer_input.text():
            QMessageBox.warning(self, "Missing Required Fields", 
                              "Please enter Latitude, Longitude, and Buffer Radius before searching.")
            return
        
        # Validate coordinate format
        try:
            lat = float(self.latitude_input.text())
            lon = float(self.longitude_input.text())
            buffer_radius = float(self.buffer_input.text())
            
            if not (-90 <= lat <= 90):
                QMessageBox.warning(self, "Invalid Latitude", 
                                  "Latitude must be between -90 and 90 degrees.")
                return
            
            if not (-180 <= lon <= 180):
                QMessageBox.warning(self, "Invalid Longitude", 
                                  "Longitude must be between -180 and 180 degrees.")
                return
                
            if buffer_radius <= 0:
                QMessageBox.warning(self, "Invalid Buffer Radius", 
                                  "Buffer radius must be greater than 0.")
                return
                
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", 
                              "Please enter valid numeric values for coordinates and buffer radius.")
            return
        
        # Log search criteria
        self.results_log.append(f"=== New Search ===")
        self.results_log.append(f"Center: ({lat}°, {lon}°)")
        self.results_log.append(f"Buffer: {buffer_radius} miles")
        
        # Create the buffer geometry
        point_4326 = QgsPointXY(lon, lat)
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        
        # Transform point to Web Mercator for buffer creation
        transform_to_3857 = QgsCoordinateTransform(crs_4326, crs_3857, QgsProject.instance())
        point_3857 = transform_to_3857.transform(point_4326)
        
        # Create buffer in meters
        buffer_meters = buffer_radius * 1609.34  # miles to meters
        buffer_geom_3857 = QgsGeometry.fromPointXY(point_3857).buffer(buffer_meters, 50)
        
        # Perform spatial query across all waterbody layers
        matching_features = []
        source_layers = []  # Track which layer each feature came from
        
        for waterbody_layer in self.waterbody_layers:
            # Get waterbody layer CRS and transform buffer if needed
            waterbody_crs = waterbody_layer.crs()
            
            need_transform = waterbody_crs != crs_3857
            if need_transform:
                transform_to_3857 = QgsCoordinateTransform(waterbody_crs, crs_3857, QgsProject.instance())
                transform_to_layer = QgsCoordinateTransform(crs_3857, waterbody_crs, QgsProject.instance())
                MAX_3857 = 20037508.342789244
                bbox_3857 = buffer_geom_3857.boundingBox()
                clamped = QgsRectangle(
                    max(bbox_3857.xMinimum(), -MAX_3857),
                    max(bbox_3857.yMinimum(), -MAX_3857),
                    min(bbox_3857.xMaximum(),  MAX_3857),
                    min(bbox_3857.yMaximum(),  MAX_3857),
                )
                try:
                    tl = transform_to_layer.transform(QgsPointXY(clamped.xMinimum(), clamped.yMaximum()))
                    tr = transform_to_layer.transform(QgsPointXY(clamped.xMaximum(), clamped.yMaximum()))
                    bl = transform_to_layer.transform(QgsPointXY(clamped.xMinimum(), clamped.yMinimum()))
                    br = transform_to_layer.transform(QgsPointXY(clamped.xMaximum(), clamped.yMinimum()))
                    filter_bbox = QgsRectangle(
                        min(tl.x(), tr.x(), bl.x(), br.x()),
                        min(tl.y(), tr.y(), bl.y(), br.y()),
                        max(tl.x(), tr.x(), bl.x(), br.x()),
                        max(tl.y(), tr.y(), bl.y(), br.y()),
                    )
                except Exception:
                    filter_bbox = waterbody_layer.extent()
            else:
                filter_bbox = buffer_geom_3857.boundingBox()

            # Perform spatial query for waterbodies — intersection checked in EPSG:3857
            # to avoid distortion from re-projecting large buffer circles.
            request = QgsFeatureRequest()
            request.setFilterRect(filter_bbox)

            for feature in waterbody_layer.getFeatures(request):
                geom = QgsGeometry(feature.geometry())
                if need_transform:
                    geom.transform(transform_to_3857)
                if geom.intersects(buffer_geom_3857):
                    if self.passes_filters(feature, waterbody_layer):
                        matching_features.append(feature)
                        source_layers.append(waterbody_layer)
        
        # Display waterbody results
        self.display_results(matching_features, source_layers, buffer_geom_3857)
        
        # For any metadata polygon checkboxes already checked, refresh their results
        for entry in self.metadata_polygon_entries:
            if entry["checkbox"].isChecked():
                self.find_metadata_polygons_in_buffer(entry, buffer_geom_3857)

        # Enable the data source container and show buffer indicators
        self.set_data_source_checkboxes_enabled(True)
        self.wb_buffer_indicator.setText("  \u2714" if matching_features else "")
        self.update_data_source_buffer_indicators(buffer_geom_3857)
        
        # Enable the buildable area section (button only, checkbox stays disabled until generated)
        self.buildable_area_widget.setEnabled(True)
        self.show_buildable_areas_checkbox.setEnabled(False)
        
        # Auto-check the Waterbodies checkbox since results are now displayed
        self.show_waterbodies_checkbox.setChecked(True)
        
        # Log results
        self.results_log.clear()
        self.results_log.append(f"{len(matching_features)} waterbodies found")
    
    def get_agency_name(self, feature):
        """Try to get a meaningful name from the water agency feature"""
        field_names = [field.name() for field in feature.fields()]
        
        # Common name fields for agency/district data
        name_fields = ['name', 'NAME', 'Name', 'LARNAME', 'larname',
               'AGENCY', 'agency', 'Agency',
               'DISTRICT', 'district', 'District', 'LABEL', 'label',
               'ORG_NAME', 'org_name', 'AGENCYNAME', 'AgencyName']
        
        for name_field in name_fields:
            if name_field in field_names:
                value = feature[name_field]
                if value and str(value).strip():
                    return str(value).strip()
        
        return "Unnamed"
    
    def get_feature_name(self, feature):
        """Try to get a meaningful name and details from the feature"""
        field_names = [field.name() for field in feature.fields()]
        
        # Get name
        name = None
        name_fields = ['name', 'NAME', 'Name', 'GNIS_NAME', 'gnis_name', 
                       'WATERBODY', 'waterbody', 'LABEL', 'label']
        
        for name_field in name_fields:
            if name_field in field_names:
                value = feature[name_field]
                if value and str(value).strip() and str(value).strip() != 'Unnamed':
                    name = str(value).strip()
                    break
        
        if not name:
            name = "Unnamed"
        
        # Get type
        wtype = None
        if 'waterbody_type' in field_names:
            wtype = feature['waterbody_type']
        elif 'ftype' in field_names:
            ftype_code = feature['ftype']
            if ftype_code is not None:
                ftype_map = {
                    390: 'Lake/Pond', 436: 'Reservoir', 466: 'Swamp/Marsh',
                    361: 'Playa', 378: 'Ice Mass', 493: 'Estuary',
                    460: 'Stream/River', 558: 'Artificial Path', 336: 'Canal/Ditch',
                }
                wtype = ftype_map.get(int(ftype_code), f"Type {ftype_code}")
        
        # Get area
        area_str = ""
        if 'area_sq_mi' in field_names and feature['area_sq_mi']:
            area = float(feature['area_sq_mi'])
            if area < 0.01:
                area_str = f" ({area:.4f} sq mi)"
            elif area < 1:
                area_str = f" ({area:.3f} sq mi)"
            else:
                area_str = f" ({area:.2f} sq mi)"
        elif 'area_sq_km' in field_names and feature['area_sq_km']:
            area_km = float(feature['area_sq_km'])
            area_mi = area_km * 0.386102
            if area_mi < 0.01:
                area_str = f" ({area_mi:.4f} sq mi)"
            elif area_mi < 1:
                area_str = f" ({area_mi:.3f} sq mi)"
            else:
                area_str = f" ({area_mi:.2f} sq mi)"
        
        # Build result string
        if wtype:
            return f"{name} [{wtype}]{area_str}"
        else:
            return f"{name}{area_str}"
    
    def get_selected_waterbody_types(self):
        """Get list of selected waterbody types"""
        selected_types = []
        if self.lake_checkbox.isChecked():
            selected_types.append("Lake")
        if self.pond_checkbox.isChecked():
            selected_types.append("Pond")
        if self.reservoir_checkbox.isChecked():
            selected_types.append("Reservoir")
        if self.river_checkbox.isChecked():
            selected_types.append("River")
        if self.stream_checkbox.isChecked():
            selected_types.append("Stream")
        return selected_types
    
    def passes_filters(self, feature, source_layer):
        """Check if a waterbody feature passes the user's filter criteria"""
        field_names = [field.name() for field in feature.fields()]
        
        # Surface area filter
        if self.surface_area_checkbox.isChecked():
            try:
                min_area = float(self.min_area_input.text()) if self.min_area_input.text() else 0
                max_area = float(self.max_area_input.text()) if self.max_area_input.text() else float('inf')
                
                area_sq_mi = None
                
                # Check for area_sq_mi field first (from our download script)
                if 'area_sq_mi' in field_names:
                    raw_area = feature['area_sq_mi']
                    if raw_area is not None:
                        area_sq_mi = float(raw_area)
                
                # Check for area_sq_km field and convert
                elif 'area_sq_km' in field_names:
                    raw_area = feature['area_sq_km']
                    if raw_area is not None:
                        area_sq_mi = float(raw_area) * 0.386102
                
                # Check other common area field names
                else:
                    area_fields = ['AREASQKM', 'areasqkm', 'AreaSqKm', 'AREASQMI', 
                                  'Shape_Area', 'shape_area', 'AREA', 'area']
                    
                    for area_field in area_fields:
                        if area_field in field_names:
                            raw_area = feature[area_field]
                            if raw_area is not None:
                                if 'sqkm' in area_field.lower() or 'km' in area_field.lower():
                                    area_sq_mi = float(raw_area) * 0.386102
                                elif 'sqmi' in area_field.lower():
                                    area_sq_mi = float(raw_area)
                                else:
                                    # Assume square meters
                                    area_sq_mi = float(raw_area) * 0.0000003861
                                break
                
                # If still no area, calculate from geometry
                if area_sq_mi is None:
                    geom = feature.geometry()
                    if geom and not geom.isEmpty():
                        # For EPSG:4326, area() returns degrees squared, need proper calculation
                        # Transform to equal area projection for accurate measurement
                        area_calc = QgsDistanceArea()
                        area_calc.setSourceCrs(source_layer.crs(), QgsProject.instance().transformContext())
                        area_calc.setEllipsoid('WGS84')
                        area_sq_m = area_calc.measureArea(geom)
                        area_sq_mi = area_sq_m * 0.0000003861
                
                if area_sq_mi is not None:
                    if not (min_area <= area_sq_mi <= max_area):
                        return False
                        
            except (ValueError, TypeError):
                pass  # If we can't determine area, don't filter on it
        
        # Waterbody type filter
        if self.waterbody_type_checkbox.isChecked():
            selected_types = self.get_selected_waterbody_types()
            
            if selected_types:
                feature_type = None
                
                # First check waterbody_type field (from our download script)
                if 'waterbody_type' in field_names:
                    feature_type = feature['waterbody_type']
                    if feature_type:
                        feature_type = str(feature_type).strip()
                
                # If no waterbody_type, check ftype and map to readable names
                if not feature_type and 'ftype' in field_names:
                    ftype_code = feature['ftype']
                    if ftype_code is not None:
                        # NHD ftype code mappings
                        ftype_map = {
                            390: 'Lake/Pond',
                            436: 'Reservoir', 
                            466: 'Swamp/Marsh',
                            361: 'Playa',
                            378: 'Ice Mass',
                            493: 'Estuary',
                            460: 'Stream/River',
                            558: 'Artificial Path',
                            336: 'Canal/Ditch',
                        }
                        feature_type = ftype_map.get(int(ftype_code), str(ftype_code))
                
                # Check other type fields
                if not feature_type:
                    type_fields = ['FTYPE', 'TYPE', 'type', 'Type', 'CLASS', 'class']
                    for type_field in type_fields:
                        if type_field in field_names:
                            val = feature[type_field]
                            if val:
                                feature_type = str(val).strip()
                                break
                
                if feature_type:
                    feature_type_lower = feature_type.lower()
                    
                    # Build matching logic for each selected type
                    matches = False
                    for selected in selected_types:
                        selected_lower = selected.lower()
                        
                        # Direct containment check
                        if selected_lower in feature_type_lower or feature_type_lower in selected_lower:
                            matches = True
                            break
                        
                        # Special mappings for NHD types
                        if selected_lower == 'lake' and ('lake' in feature_type_lower or 'pond' in feature_type_lower):
                            matches = True
                            break
                        if selected_lower == 'pond' and ('pond' in feature_type_lower or 'lake' in feature_type_lower):
                            matches = True
                            break
                        if selected_lower == 'river' and ('river' in feature_type_lower or 'stream' in feature_type_lower):
                            matches = True
                            break
                        if selected_lower == 'stream' and ('stream' in feature_type_lower or 'river' in feature_type_lower):
                            matches = True
                            break
                        if selected_lower == 'reservoir' and 'reservoir' in feature_type_lower:
                            matches = True
                            break
                    
                    if not matches:
                        return False
                else:
                    # No type info available - exclude if filtering by type
                    return False
        
        return True
    
    def display_results(self, features, source_layers, buffer_geom_3857):
        """Create a layer showing the matching waterbodies"""
        # Remove old results layer if exists
        if self.results_layer:
            QgsProject.instance().removeMapLayer(self.results_layer)
            self.results_layer = None
        
        # Show buffer on map
        self.show_buffer_layer(buffer_geom_3857)
        
        if not features:
            self.refresh_canvas_layers()
            return
        
        # Create memory layer in EPSG:3857 for consistent display
        self.results_layer = QgsVectorLayer("Polygon?crs=EPSG:3857", "Found Waterbodies", "memory")
        provider = self.results_layer.dataProvider()
        
        # Collect all unique fields from all source layers
        all_fields = QgsFields()
        for layer in self.waterbody_layers:
            for field in layer.fields():
                if all_fields.lookupField(field.name()) == -1:
                    all_fields.append(field)
        
        provider.addAttributes(all_fields)
        self.results_layer.updateFields()
        
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        
        # Transform features to EPSG:3857 and add to results layer
        for feature, source_layer in zip(features, source_layers):
            new_feature = QgsFeature(all_fields)
            
            # Copy geometry, transforming if needed
            geom = QgsGeometry(feature.geometry())
            source_crs = source_layer.crs()
            if source_crs != crs_3857:
                transform = QgsCoordinateTransform(source_crs, crs_3857, QgsProject.instance())
                geom.transform(transform)
            new_feature.setGeometry(geom)
            
            # Copy attributes
            for field in feature.fields():
                field_idx = all_fields.lookupField(field.name())
                if field_idx >= 0:
                    new_feature.setAttribute(field_idx, feature[field.name()])
            
            provider.addFeature(new_feature)
        
        # Style the results layer - cyan/teal for found waterbodies
        symbol = QgsFillSymbol.createSimple({
            'color': '0,255,200,150',
            'outline_color': '0,150,150,255',
            'outline_width': '1.5'
        })
        self.results_layer.renderer().setSymbol(symbol)
        
        # Add to map
        QgsProject.instance().addMapLayer(self.results_layer)
        
        self.refresh_canvas_layers()
    
    def show_buffer_layer(self, buffer_geom_3857):
        """Show or update the buffer circle on the map"""
        # Remove old buffer layer if exists
        if self.buffer_layer:
            QgsProject.instance().removeMapLayer(self.buffer_layer)
        
        # Create memory layer for buffer in EPSG:3857
        self.buffer_layer = QgsVectorLayer("Polygon?crs=EPSG:3857", "Search Buffer", "memory")
        provider = self.buffer_layer.dataProvider()
        
        # Create feature with buffer geometry
        feature = QgsFeature()
        feature.setGeometry(buffer_geom_3857)
        provider.addFeature(feature)
        
        # Style the buffer layer - blue outline, very light fill
        symbol = QgsFillSymbol.createSimple({
            'color': '0,100,255,30',
            'outline_color': '0,0,255,255',
            'outline_width': '1'
        })
        self.buffer_layer.renderer().setSymbol(symbol)
        
        # Add layer to project
        QgsProject.instance().addMapLayer(self.buffer_layer)

        # Enable and check the buffer visibility toggle
        self.show_buffer_checkbox.setEnabled(True)
        self.show_buffer_checkbox.setChecked(True)

    def clear_filters(self):
        self.show_buffer_checkbox.setChecked(False)
        self.show_buffer_checkbox.setEnabled(False)
        # Clear required location fields
        self.latitude_input.clear()
        self.longitude_input.clear()
        self.buffer_input.clear()
        
        # Uncheck main checkboxes
        self.surface_area_checkbox.setChecked(False)
        self.waterbody_type_checkbox.setChecked(False)
        # Uncheck all metadata polygon checkboxes and remove their results layers
        for entry in self.metadata_polygon_entries:
            entry["checkbox"].setChecked(False)
            if entry["results_layer"]:
                if entry["results_layer"].id() in QgsProject.instance().mapLayers():
                    QgsProject.instance().removeMapLayer(entry["results_layer"])
                entry["results_layer"] = None
        self.show_waterbodies_checkbox.setChecked(False)
        for entry in self.metadata_polygon_entries:
            indicator = entry.get("buffer_indicator")
            if indicator:
                indicator.setText("")
        self.set_data_source_checkboxes_enabled(False)

        # Reset buildable area section
        self.show_buildable_areas_checkbox.setChecked(False)
        self.show_buildable_areas_checkbox.setEnabled(False)
        self.show_heatmap_checkbox.setChecked(False)
        self.show_heatmap_checkbox.setEnabled(False)
        self.buildable_area_widget.setEnabled(False)

        # Remove buildable area layer and delete gpkg file
        if self.buildable_area_layer:
            if self.buildable_area_layer.id() in QgsProject.instance().mapLayers():
                QgsProject.instance().removeMapLayer(self.buildable_area_layer)
            self.buildable_area_layer = None

        # Remove heatmap layer, legend, and delete tif file
        self._remove_heatmap_legend()
        if self.heatmap_layer:
            if self.heatmap_layer.id() in QgsProject.instance().mapLayers():
                QgsProject.instance().removeMapLayer(self.heatmap_layer)
            self.heatmap_layer = None

        # Delete the buildable areas gpkg and heatmap tif files
        script_dir = os.path.dirname(os.path.abspath(__file__))
        delete_buildable_areas_gpkg(script_dir)
        delete_heatmap_tif(script_dir)

        # Clear surface area inputs
        self.min_area_input.clear()
        self.max_area_input.clear()
        
        # Uncheck all waterbody types
        self.lake_checkbox.setChecked(False)
        self.pond_checkbox.setChecked(False)
        self.reservoir_checkbox.setChecked(False)
        self.river_checkbox.setChecked(False)
        self.stream_checkbox.setChecked(False)
        
        # Clear results log
        self.results_log.clear()
        
        # Remove buffer circle if exists
        if self.buffer_layer:
            QgsProject.instance().removeMapLayer(self.buffer_layer)
            self.buffer_layer = None
            
        # Remove marker if exists
        if self.marker_layer:
            QgsProject.instance().removeMapLayer(self.marker_layer)
            self.marker_layer = None
        
        # Remove results layer if exists
        if self.results_layer:
            QgsProject.instance().removeMapLayer(self.results_layer)
            self.results_layer = None
        
        # Clear waterbody selection
        self.clear_waterbody_selection()
        
        # Update search button state
        self.update_search_button_state()
            
        self.refresh_canvas_layers()
    
    def on_lat_lon_changed(self):
        """Called when lat or lon field changes — clears buffer then runs normal location update."""
        self.buffer_input.clear()
        self.on_location_fields_changed()

    def on_location_fields_changed(self):
        """Called when any location field (lat, lon, buffer) loses focus or Enter is pressed"""
        # Clear any previously displayed search results since the search parameters changed
        self.clear_search_results()

        # Update the search button enabled state
        self.update_search_button_state()
        
        lat_text = self.latitude_input.text().strip()
        lon_text = self.longitude_input.text().strip()
        buffer_text = self.buffer_input.text().strip()
        
        has_lat = bool(lat_text)
        has_lon = bool(lon_text)
        has_buffer = bool(buffer_text)
        
        # If either lat or lon is missing, clear both marker and buffer from the map
        if not has_lat or not has_lon:
            if self.marker_layer:
                QgsProject.instance().removeMapLayer(self.marker_layer)
                self.marker_layer = None
            if self.buffer_layer:
                QgsProject.instance().removeMapLayer(self.buffer_layer)
                self.buffer_layer = None
                self.show_buffer_checkbox.setChecked(False)
                self.show_buffer_checkbox.setEnabled(False)
            self.refresh_canvas_layers()
            return
        
        # Both lat and lon are present - validate them
        try:
            lat = float(lat_text)
            lon = float(lon_text)
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                return
        except ValueError:
            return
        
        # Transform point to EPSG:3857
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        transform = QgsCoordinateTransform(crs_4326, crs_3857, QgsProject.instance())
        point_3857 = transform.transform(QgsPointXY(lon, lat))
        
        # Always show marker when lat and lon are valid
        self.add_marker(point_3857)
        
        # Show buffer only if buffer radius is also provided and valid
        if has_buffer:
            try:
                buffer_radius = float(buffer_text)
                if buffer_radius > 0:
                    buffer_meters = buffer_radius * 1609.34
                    circle = QgsGeometry.fromPointXY(point_3857).buffer(buffer_meters, 50)
                    self.show_buffer_layer(circle)
                    self.canvas.setExtent(self.buffer_layer.extent())
                else:
                    # Invalid buffer, remove buffer layer if it exists
                    if self.buffer_layer:
                        QgsProject.instance().removeMapLayer(self.buffer_layer)
                        self.buffer_layer = None
                    # Zoom to marker with default extent
                    self.zoom_to_point(point_3857)
            except ValueError:
                # Invalid buffer, remove buffer layer if it exists
                if self.buffer_layer:
                    QgsProject.instance().removeMapLayer(self.buffer_layer)
                    self.buffer_layer = None
                # Zoom to marker with default extent
                self.zoom_to_point(point_3857)
        else:
            # No buffer value, remove buffer layer if it exists
            if self.buffer_layer:
                QgsProject.instance().removeMapLayer(self.buffer_layer)
                self.buffer_layer = None
            # Zoom to marker with default extent
            self.zoom_to_point(point_3857)
        
        self.refresh_canvas_layers()

    def show_location_on_map(self):
        # Validate inputs
        try:
            lat = float(self.latitude_input.text())
            lon = float(self.longitude_input.text())
            buffer_radius = float(self.buffer_input.text())
            
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or buffer_radius <= 0:
                QMessageBox.warning(self, "Invalid Input", 
                                  "Please enter valid coordinates and buffer radius.")
                return
                
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", 
                              "Please enter valid numeric values.")
            return
        
        # Create point in WGS84 (EPSG:4326)
        point_4326 = QgsPointXY(lon, lat)
        
        # Create CRS objects
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        crs_3857 = QgsCoordinateReferenceSystem("EPSG:3857")
        
        # Create transformer
        transform = QgsCoordinateTransform(crs_4326, crs_3857, QgsProject.instance())
        
        # Transform point to Web Mercator
        point_3857 = transform.transform(point_4326)
        
        # Create buffer in meters (now in EPSG:3857 which uses meters)
        buffer_meters = buffer_radius * 1609.34
        circle = QgsGeometry.fromPointXY(point_3857).buffer(buffer_meters, 50)
        
        # Show buffer layer
        self.show_buffer_layer(circle)
        
        # Add marker at center
        self.add_marker(point_3857)
        
        # Zoom to buffer extent
        self.canvas.setExtent(self.buffer_layer.extent())
        self.refresh_canvas_layers()


    def closeEvent(self, event):
        """Clean up buildable areas file when the application is closed"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        delete_buildable_areas_gpkg(script_dir)
        event.accept()


# Create and show the window
window = MapWindow()
window.show()

# Run the application
exit_code = qgs.exec_()

# Cleanup
qgs.exitQgis()