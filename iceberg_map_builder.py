"""
Download the current U.S. National Ice Center Antarctic iceberg CSV and build an interactive HTML map.

Usage:
  python iceberg_map_builder.py
  python iceberg_map_builder.py --csv path\to\AntarcIcebergs.csv
  python iceberg_map_builder.py --out antarc_icebergs_map.html

Dependencies:
  pip install pandas folium requests
"""
from __future__ import annotations

import argparse
import io
import re
from pathlib import Path

import pandas as pd
import requests
import folium
from folium.plugins import MarkerCluster, MeasureControl, MiniMap

USNIC_CSV_URL = "https://usicecenter.gov/File/DownloadCurrent?pId=134"


def dms_to_decimal(value):
    """Handle decimal degrees or strings like 63-30S, 63 30 S, 06330S, 63.5S."""
    if pd.isna(value):
        return None
    s = str(value).strip().upper().replace("°", " ").replace("'", " ").replace('"', " ")
    hemi = None
    m = re.search(r"([NSEW])", s)
    if m:
        hemi = m.group(1)
        s = s.replace(hemi, " ")

    # Plain decimal?
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", s)
    if not nums:
        return None

    # Compact DDMM or DDDMM style, e.g. 6330 / 06330
    compact = re.sub(r"[^0-9.+-]", "", s)
    if len(nums) == 1 and compact.replace(".", "", 1).lstrip("+-").isdigit() and "." not in compact:
        raw = compact.lstrip("+-")
        sign = -1 if compact.startswith("-") else 1
        if len(raw) in (4, 5):
            deg_digits = len(raw) - 2
            deg = float(raw[:deg_digits])
            minutes = float(raw[deg_digits:])
            dec = sign * (deg + minutes / 60)
        else:
            dec = float(compact)
    else:
        dec = float(nums[0])
        if len(nums) >= 2:
            dec += float(nums[1]) / 60
        if len(nums) >= 3:
            dec += float(nums[2]) / 3600
        if str(value).strip().startswith("-"):
            dec *= -1

    if hemi in ("S", "W"):
        dec = -abs(dec)
    elif hemi in ("N", "E"):
        dec = abs(dec)
    return dec


def find_col(df, candidates):
    norm = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in df.columns}
    for cand in candidates:
        key = re.sub(r"[^a-z0-9]", "", cand.lower())
        if key in norm:
            return norm[key]
    # fuzzy contains fallback
    for c in df.columns:
        ck = re.sub(r"[^a-z0-9]", "", c.lower())
        if any(re.sub(r"[^a-z0-9]", "", cand.lower()) in ck for cand in candidates):
            return c
    return None


def load_csv(path: str | None) -> pd.DataFrame:
    if path:
        return pd.read_csv(path)
    r = requests.get(USNIC_CSV_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


def build_map(df: pd.DataFrame, out_path: Path) -> None:
    lat_col = find_col(df, ["Latitude", "Lat", "LAT"])
    lon_col = find_col(df, ["Longitude", "Lon", "Long", "LON"])
    name_col = find_col(df, ["Iceberg", "Name", "Berg", "Iceberg Name"])

    if not lat_col or not lon_col:
        raise SystemExit(f"Could not find latitude/longitude columns. Columns found: {list(df.columns)}")

    df = df.copy()
    df["_lat"] = df[lat_col].apply(dms_to_decimal)
    df["_lon"] = df[lon_col].apply(dms_to_decimal)
    df = df.dropna(subset=["_lat", "_lon"])

    m = folium.Map(location=[-72, 0], zoom_start=3, tiles="CartoDB positron", control_scale=True)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer("Esri WorldImagery", name="Esri World Imagery", attr="Esri").add_to(m)

    cluster = MarkerCluster(name="USNIC Antarctic icebergs").add_to(m)

    for _, row in df.iterrows():
        name = str(row[name_col]) if name_col else "Iceberg"
        rows = []
        for col in df.columns:
            if col.startswith("_"):
                continue
            val = row[col]
            if pd.notna(val):
                rows.append(f"<tr><th style='text-align:left;padding-right:8px'>{col}</th><td>{val}</td></tr>")
        popup = f"<b>{name}</b><br><table>{''.join(rows)}</table>"
        folium.CircleMarker(
            location=[row["_lat"], row["_lon"]],
            radius=6,
            popup=folium.Popup(popup, max_width=420),
            tooltip=name,
            fill=True,
            fill_opacity=0.8,
        ).add_to(cluster)

    MiniMap(toggle_display=True).add_to(m)
    MeasureControl(position="topleft").add_to(m)
    folium.LayerControl().add_to(m)

    title = f"""
    <div style="position: fixed; top: 10px; left: 50px; z-index: 9999; background: white;
                padding: 8px 12px; border: 1px solid #999; border-radius: 6px;
                font-family: Arial, sans-serif; font-size: 14px;">
      <b>USNIC Antarctic Icebergs</b><br>{len(df)} mapped positions<br>
      Source: U.S. National Ice Center current CSV
    </div>
    """
    m.get_root().html.add_child(folium.Element(title))
    m.save(out_path)
    print(f"Wrote {out_path.resolve()} with {len(df)} iceberg markers")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="Optional local USNIC CSV file. If omitted, downloads current CSV.")
    ap.add_argument("--out", default="antarc_icebergs_map.html", help="Output HTML map filename.")
    args = ap.parse_args()

    df = load_csv(args.csv)
    build_map(df, Path(args.out))


if __name__ == "__main__":
    main()
