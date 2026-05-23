"""
download_assets.py

Downloads large data asset folders from Google Drive if any files are missing
locally. On each run it queries Drive for the current file listing, compares
it against local files, and downloads only what is absent.

Requirements:
    pip install gdown requests

Setup:
    1. Upload each folder to Google Drive and share it as "Anyone with the
       link can view".
    2. Get each folder's ID from its Drive URL:
           drive.google.com/drive/folders/<FOLDER_ID>
    3. Paste the IDs into ASSET_FOLDERS below.
    4. Get a free Google API key (no OAuth needed for public folders):
           console.cloud.google.com → APIs & Services → Credentials
           → Create Credentials → API key
       Enable the "Google Drive API" for your project, then paste the
       key into GOOGLE_API_KEY below.
"""

import os
import sys
import tkinter as tk
from tkinter import ttk

try:
    import gdown
except ImportError:
    print("ERROR: 'gdown' is not installed. Run:  pip install gdown")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run:  pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Google API key — free, no OAuth needed for publicly shared folders.
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = "USE_YOUR_OWN_API_KEY"

# ---------------------------------------------------------------------------
# Configure your Google Drive folder IDs here.
# ---------------------------------------------------------------------------
ASSET_FOLDERS = {
    "metadata_text":      "1JLBHvBlofam7KuSyV1na3mvLY6WBCMEK",
    "metadata_polygons":  "18cBEjOfnanwYArou2ggsGJ8KK-y-icto",
    "waterbody_polygons": "1qR_zTsR4RfSFqpBh0BnWHdo-F47vnOjD",

    "novel_water_level": "1GMQf0a7D74_pFXTbgRMskHpmx-hiVF3A",
    "novel_temperature": "1EmkAPQDi9uwqg8WjP9q_5vzSQbBfqmeS",
    "novel_border": "1kyRbByMD7xxry5M-mGM64Cg8Bth7jZTE",
    "novel_bathymetry": "1JRj6h2lbJfyMSmsVhm9qIDoialpArnB5",
    "novel_algae": "1kpaSyT4UoY2lVeAyzeyorMmY7_f2-jbP",
}
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _format_size(size_bytes):
    """Format a byte count into a human-readable string."""
    if size_bytes is None:
        return "unknown size"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _list_drive_folder(folder_id: str) -> list:
    """Return [{name, id, mimeType, size}, ...] for all files in a Google Drive folder."""
    url = "https://www.googleapis.com/drive/v3/files"
    files = []
    page_token = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "key": GOOGLE_API_KEY,
            "fields": "nextPageToken, files(name, id, mimeType, size)",
            "pageSize": 1000,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return files


def _build_display_label(f):
    """Build a display label for a file entry with size and type info."""
    name = f["name"]
    is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"

    if is_folder:
        # For folders (e.g. shapefiles), list sub-file extensions and sum sizes
        sub_files = f.get("_sub_files", [])
        if sub_files:
            extensions = sorted(set(
                os.path.splitext(sf["name"])[1].lower()
                for sf in sub_files if os.path.splitext(sf["name"])[1]
            ))
            ext_str = ", ".join(extensions) if extensions else "folder"
            total_size = sum(int(sf.get("size", 0)) for sf in sub_files)
            return f"{name} ({ext_str}) \u2014 {_format_size(total_size)}"
        return f"{name} (folder)"
    else:
        size_str = _format_size(int(f["size"])) if f.get("size") else "unknown size"
        return f"{name} \u2014 {size_str}"


def _enrich_folder_entries(files):
    """For folder entries, fetch sub-file listings to get extensions and total size."""
    for f in files:
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            try:
                f["_sub_files"] = _list_drive_folder(f["id"])
            except Exception:
                f["_sub_files"] = []


def _ask_which_files(folder_name: str, files: list) -> list:
    """
    Show a checkbox dialog listing missing files. Returns the subset the user
    selected, or an empty list if the user cancelled.
    """
    # Fetch sub-file info for any folder entries (shapefiles etc.)
    _enrich_folder_entries(files)

    selected = []

    root = tk.Tk()
    root.title(f"Missing files \u2014 {folder_name}")
    root.geometry("650x500")
    root.resizable(True, True)

    tk.Label(
        root,
        text=f"{len(files)} file(s) missing in '{folder_name}'.\nSelect which to download:",
        justify="left",
        padx=12,
        pady=8,
    ).pack(anchor="w")

    container = tk.Frame(root, padx=12)
    container.pack(fill="both", expand=True)

    canvas = tk.Canvas(container, height=300)
    v_scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
    h_scrollbar = tk.Scrollbar(container, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)

    v_scrollbar.pack(side="right", fill="y")
    h_scrollbar.pack(side="bottom", fill="x")
    canvas.pack(side="left", fill="both", expand=True)

    frame = tk.Frame(canvas)
    canvas_window = canvas.create_window((0, 0), window=frame, anchor="nw")

    def _on_frame_configure(event):
        canvas.configure(scrollregion=canvas.bbox("all"))

    frame.bind("<Configure>", _on_frame_configure)
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

    vars_ = []
    for f in files:
        var = tk.BooleanVar(value=False)
        label = _build_display_label(f)
        tk.Checkbutton(frame, text=label, variable=var, anchor="w").pack(fill="x")
        vars_.append(var)

    def select_all():
        for v in vars_:
            v.set(True)

    def deselect_all():
        for v in vars_:
            v.set(False)

    btn_frame = tk.Frame(root, padx=12, pady=4)
    btn_frame.pack(fill="x")
    tk.Button(btn_frame, text="Select All", command=select_all).pack(side="left", padx=2)
    tk.Button(btn_frame, text="Deselect All", command=deselect_all).pack(side="left", padx=2)

    def on_download():
        selected.extend(f for f, v in zip(files, vars_) if v.get())
        root.destroy()

    def on_skip():
        root.destroy()

    action_frame = tk.Frame(root, padx=12, pady=8)
    action_frame.pack(fill="x")
    tk.Button(action_frame, text="Download Selected", command=on_download).pack(side="right", padx=4)
    tk.Button(action_frame, text="Skip", command=on_skip).pack(side="right")

    root.mainloop()
    return selected


def download_assets(force: bool = False) -> None:
    """
    For each asset folder, query Drive for its current file listing and
    download any files not already present locally.

    Args:
        force: If True, re-download all files even if already present.
    """
    if GOOGLE_API_KEY == "REPLACE_WITH_YOUR_API_KEY":
        print("ERROR: GOOGLE_API_KEY is not configured in download_assets.py.")
        sys.exit(1)

    for folder_name, folder_id in ASSET_FOLDERS.items():
        local_path = os.path.join(BASE_DIR, folder_name)
        os.makedirs(local_path, exist_ok=True)

        print(f"[download_assets] Checking '{folder_name}'...")
        try:
            drive_files = _list_drive_folder(folder_id)
        except Exception as e:
            print(f"[download_assets] ERROR listing '{folder_name}' from Drive: {e}")
            continue

        def _is_missing(f):
            local = os.path.join(local_path, f["name"])
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                return not os.path.isdir(local) or not os.listdir(local)
            return not os.path.isfile(local)

        if force:
            to_download = drive_files
        else:
            to_download = [f for f in drive_files if _is_missing(f)]

        if not to_download:
            print(f"[download_assets] '{folder_name}' is up to date, skipping.")
            continue

        selected = _ask_which_files(folder_name, to_download)

        if not selected:
            print(f"[download_assets] Skipping '{folder_name}' (no files selected).")
            continue

        to_download = selected
        print(f"[download_assets] Downloading {len(to_download)} file(s) in '{folder_name}'...")
        for file in to_download:
            try:
                if file.get("mimeType") == "application/vnd.google-apps.folder":
                    subfolder_path = os.path.join(local_path, file["name"])
                    os.makedirs(subfolder_path, exist_ok=True)
                    sub_files = _list_drive_folder(file["id"])
                    for sub_file in sub_files:
                        sub_dest = os.path.join(subfolder_path, sub_file["name"])
                        gdown.download(id=sub_file["id"], output=sub_dest, quiet=False)
                else:
                    dest = os.path.join(local_path, file["name"])
                    gdown.download(id=file["id"], output=dest, quiet=False)
            except Exception as e:
                print(f"[download_assets] ERROR downloading '{file['name']}': {e}")

        print(f"[download_assets] '{folder_name}' complete.")


if __name__ == "__main__":
    force_flag = "--force" in sys.argv
    download_assets(force=force_flag)
