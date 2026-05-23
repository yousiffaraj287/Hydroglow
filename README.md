# HydroGlow

HydroGlow is a Python and QGIS-based geospatial screening tool developed as part of a senior design project. The project helps analyze water bodies for potential renewable-energy site planning by combining hydrologic data, geospatial overlays, proximity analysis, buildable area generation, suitability heatmaps, and infrastructure connections.

This repository serves as a public portfolio version of HydroGlow and highlights the geospatial data processing, QGIS GUI workflow, and analysis tools used in the project.

---

## Features

- QGIS-based GUI for viewing waterbodies and geospatial overlays
- CSV and GeoPackage metadata integration for environmental, utility, and land-use datasets
- Waterbody proximity analysis for nearby and directly overlapping features
- Buildable area generation by subtracting restricted polygon overlaps from waterbody polygons
- Suitability heatmap generation using weighted geospatial layers
- Nearest substation lookup and connection path visualization
- Google Drive asset downloader for large project datasets
- Source-link tracking for datasets displayed in the GUI

---

## Main Components

### QGIS GUI

The main HydroGlow interface is built with Python and QGIS. It allows users to load waterbody layers, toggle geospatial datasets, view metadata, and analyze features related to a selected waterbody.

### Buildable Area Generator

The buildable area tool removes restricted or unsuitable polygon overlaps from a waterbody polygon. This helps estimate where floating solar or related infrastructure could potentially be placed.

### Suitability Heatmap

The heatmap tool generates a raster-based suitability score for a selected waterbody using weighted geospatial layers. Areas can be scored as more suitable, neutral, or less suitable based on the configured layer weights.

### Substation Connector

The substation connector identifies nearby power infrastructure and can generate a connection path from a selected waterbody to the nearest substation.

### Asset Downloader

Large datasets are not stored directly in this repository. The asset downloader retrieves required project data from Google Drive when needed.

---

## Technologies Used

- Python
- QGIS / PyQGIS
- GeoPandas
- GDAL / OGR
- NumPy
- CSV and GeoPackage data workflows
- Google Drive data storage
- Git and GitHub

---

## Data Sources

HydroGlow integrates public geospatial and environmental datasets, including:

- Waterbody polygon datasets
- Environmental and protected-area overlays
- Utility and substation infrastructure data
- Tribal lands, wetlands, parks, and habitat layers
- Solar-energy and regional electricity datasets
- ML-derived raster and waterbody analysis layers

Some large datasets are stored externally and downloaded when the GUI is launched.

---

## Project Structure

hydroglow/
├── gui/
│   ├── gui.py
│   ├── buildable_area_polygon.py
│   ├── suitability_heatmap.py
│   ├── substation_connector.py
│   ├── download_assets.py
│   └── layer_weights.csv
├── run_gui.bat
├── run_gui.sh
├── README.md
├── .gitignore
└── .gitattributes

---

## How to Run

### Requirements

Before running HydroGlow, install:

- Git
- QGIS LTR version
- Python dependencies used by the project

QGIS is required because the GUI uses PyQGIS.

### Windows

Clone the repository:

git clone https://github.com/yousiffaraj287/hydroglow.git
cd hydroglow

Run:

.\run_gui.bat

On the first run, the script may create a qgis_python_path.txt file. Add your local QGIS Python path inside that file, then rerun the script.

Example Windows QGIS Python path:

C:\Program Files\QGIS 3.40.15\bin\python-qgis-ltr.bat

### macOS / Linux

Clone the repository:

git clone https://github.com/yousiffaraj287/hydroglow.git
cd hydroglow

Run:

./run_gui.sh

On the first run, the script may create a qgis_python_path.txt file. Add your local QGIS Python path inside that file, then rerun the script.

---

## Notes

- Large geospatial datasets are excluded from GitHub and downloaded separately.
- Local environment files, virtual environments, raster files, and generated outputs should not be committed.
- qgis_python_path.txt is machine-specific and should remain ignored.
- API keys or private configuration values should not be committed publicly.

---

## Senior Design Context

HydroGlow was developed as a senior design project by a 5-person engineering team. My contributions focused on the Python/QGIS workflow, geospatial data integration, GUI functionality, proximity logic, buildable area analysis, suitability mapping, and project deployment support.

---

## License

This project is licensed under the MIT License.
