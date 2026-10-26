"""
Build an interactive USNIC Antarctic iceberg HTML map.

Also prints the data source and best-available file/data age to the terminal.

Fixes:
- Correctly preserves negative decimal lat/lon values such as -58.59.
- Handles S/W hemisphere suffixes if the CSV ever uses DMS or hemisphere text.
- Adds tooltip/hover info with Length, Width, Area sq km, and distance to South Georgia.
- Adds optional --north-of-lat filter, e.g. --north-of-lat -60.

Usage:
  pip install pandas folium requests
  python iceberg_map_builder_v2.py --csv AntarcticIcebergs_20260430.csv --out antarc_icebergs_map.html
  python iceberg_map_builder_v2.py --csv AntarcticIcebergs_20260430.csv --out south_georgia_bergs.html --north-of-lat -60 --center-south-georgia

Without --csv, it tries the current USNIC download endpoint:
  python iceberg_map_builder_v2.py
"""

from __future__ import annotations

import argparse
import io
import math
import re
from pathlib import Path

import pandas as pd
import requests
import folium
from folium.plugins import MarkerCluster, MeasureControl, MiniMap

USNIC_CSV_URL = "https://usicecenter.gov/File/DownloadCurrent?pId=134"

SOUTH_GEORGIA_LAT = -54.43
SOUTH_GEORGIA_LON = -36.59


def coord_to_decimal(value):
    """
    Convert coordinate to decimal degrees.

    Handles:
      - Decimal numbers: -58.59
      - Decimal strings: "-58.59"
      - Hemisphere strings: "58.59S", "36.37W"
      - D/M/S-ish strings: "58 35 S", "58°35'S", "05835S"

    Important fix:
      For already-negative decimal values, do NOT flip the sign again.
    """
    if pd.isna(value):
        return None

    # If it is already numeric, preserve the sign exactly.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    original = str(value).strip()
    if not original:
        return None

    s = original.upper().replace("°", " ").replace("'", " ").replace('"', " ")
    hemi_match = re.search(r"([NSEW])", s)
    hemi = hemi_match.group(1) if hemi_match else None
    if hemi:
        s = s.replace(hemi, " ")

    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", s)
    if not nums:
        return None

    # Plain decimal string, including negative decimals.
    if len(nums) == 1 and "." in nums[0]:
        dec = float(nums[0])

    else:
        compact = re.sub(r"[^0-9.+-]", "", s)
        raw_for_compact = compact.lstrip("+-")

        # Compact DDMM or DDDMM, such as 5835 or 05835.
        if len(nums) == 1 and raw_for_compact.isdigit() and len(raw_for_compact) in (4, 5):
            sign = -1 if compact.startswith("-") else 1
            deg_digits = len(raw_for_compact) - 2
            deg = float(raw_for_compact[:deg_digits])
            minutes = float(raw_for_compact[deg_digits:])
            dec = sign * (deg + minutes / 60.0)
        else:
            # DMS / DM style. Keep explicit negative on degrees.
            deg = float(nums[0])
            sign = -1 if deg < 0 else 1
            dec = abs(deg)
            if len(nums) >= 2:
                dec += float(nums[1]) / 60.0
            if len(nums) >= 3:
                dec += float(nums[2]) / 3600.0
            dec *= sign

    # Hemisphere suffix overrides sign direction.
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

    for c in df.columns:
        ck = re.sub(r"[^a-z0-9]", "", c.lower())
        if any(re.sub(r"[^a-z0-9]", "", cand.lower()) in ck for cand in candidates):
            return c
    return None


def haversine_nm(lat1, lon1, lat2, lon2):
    r_nm = 3440.065
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def infer_date_from_text(text):
    """
    Try to infer a YYYY-MM-DD date from a filename/string.
    Handles examples like:
      AntarcticIcebergs_20260430.csv
      2026-04-30
      04/30/2026
    """
    if not text:
        return None

    s = str(text)

    m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
    if m:
        try:
            return pd.Timestamp(f"{m.group(1)}-{m.group(2)}-{m.group(3)}").date()
        except Exception:
            pass

    m = re.search(r"(20\d{2})[-_/\.](\d{1,2})[-_/\.](\d{1,2})", s)
    if m:
        try:
            return pd.Timestamp(f"{m.group(1)}-{m.group(2)}-{m.group(3)}").date()
        except Exception:
            pass

    m = re.search(r"(\d{1,2})[-_/\.](\d{1,2})[-_/\.](20\d{2})", s)
    if m:
        try:
            return pd.Timestamp(f"{m.group(3)}-{m.group(1)}-{m.group(2)}").date()
        except Exception:
            pass

    return None


def print_data_source_summary(df, source_label, source_date=None, http_last_modified=None):
    """
    Print a useful terminal summary of where the iceberg data came from
    and how old it appears to be.
    """
    print()
    print("Iceberg data source")
    print("-------------------")
    print(f"Source: {source_label}")

    today = pd.Timestamp.now(tz="America/Los_Angeles").date()

    if http_last_modified:
        print(f"HTTP Last-Modified: {http_last_modified}")

    if source_date:
        age_days = (today - source_date).days
        print(f"Inferred source/file date: {source_date} ({age_days} day{'s' if age_days != 1 else ''} old)")
    else:
        print("Inferred source/file date: not available")

    update_col = find_col(df, ["Last Update", "Update", "Date"])
    if update_col:
        updates = pd.to_datetime(df[update_col], errors="coerce")
        valid_updates = updates.dropna()
        if not valid_updates.empty:
            newest = valid_updates.max().date()
            oldest = valid_updates.min().date()
            newest_age = (today - newest).days
            print(f"CSV '{update_col}' newest date: {newest} ({newest_age} day{'s' if newest_age != 1 else ''} old)")
            print(f"CSV '{update_col}' oldest date: {oldest}")
        else:
            print(f"CSV '{update_col}' date range: could not parse dates")
    else:
        print("CSV update-date column: not found")

    print(f"CSV rows loaded: {len(df)}")
    print()


def load_csv(path):
    """
    Load the iceberg CSV and return:
      df, source_label, source_date, http_last_modified
    """
    if path:
        p = Path(path)
        df = pd.read_csv(p, encoding="utf-8-sig")
        source_label = f"Local CSV: {p.resolve()}"
        source_date = infer_date_from_text(p.name)

        # Fallback to filesystem modified date if filename has no usable date.
        if source_date is None:
            try:
                source_date = pd.Timestamp.fromtimestamp(p.stat().st_mtime).date()
            except Exception:
                source_date = None

        return df, source_label, source_date, None

    r = requests.get(USNIC_CSV_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()

    df = pd.read_csv(io.StringIO(r.text), encoding="utf-8-sig")
    source_label = f"USNIC live download: {USNIC_CSV_URL}"
    http_last_modified = r.headers.get("Last-Modified")

    source_date = None
    if http_last_modified:
        try:
            source_date = pd.to_datetime(http_last_modified, utc=True).date()
        except Exception:
            source_date = None

    return df, source_label, source_date, http_last_modified


def build_map(df, out_path, north_of_lat=None, center_south_georgia=False):
    lat_col = find_col(df, ["Latitude", "Lat"])
    lon_col = find_col(df, ["Longitude", "Lon", "Long"])
    name_col = find_col(df, ["Iceberg", "Name", "Berg", "Iceberg Name"])
    length_col = find_col(df, ["Length (NM)", "Length", "Length NM"])
    width_col = find_col(df, ["Width (NM)", "Width", "Width NM"])
    area_km_col = find_col(df, ["Area (sqKM)", "Area sqKM", "Area sq KM", "Area km2", "Area sqkm"])
    update_col = find_col(df, ["Last Update", "Update", "Date"])

    if not lat_col or not lon_col:
        raise SystemExit(f"Could not find latitude/longitude columns. Columns found: {list(df.columns)}")

    df = df.copy()
    df["_lat"] = df[lat_col].apply(coord_to_decimal)
    df["_lon"] = df[lon_col].apply(coord_to_decimal)
    df = df.dropna(subset=["_lat", "_lon"])

    if north_of_lat is not None:
        df = df[df["_lat"] > float(north_of_lat)].copy()

    if df.empty:
        raise SystemExit("No iceberg rows remain after filtering.")

    df["_dist_sg_nm"] = df.apply(
        lambda r: haversine_nm(r["_lat"], r["_lon"], SOUTH_GEORGIA_LAT, SOUTH_GEORGIA_LON),
        axis=1,
    )

    if center_south_georgia:
        start_location = [SOUTH_GEORGIA_LAT, SOUTH_GEORGIA_LON]
        zoom = 5
    else:
        start_location = [df["_lat"].mean(), df["_lon"].mean()]
        zoom = 3

    m = folium.Map(location=start_location, zoom_start=zoom, tiles="CartoDB positron", control_scale=True)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer("Esri WorldImagery", name="Esri World Imagery", attr="Esri").add_to(m)

    folium.Marker(
        [SOUTH_GEORGIA_LAT, SOUTH_GEORGIA_LON],
        tooltip="South Georgia",
        popup="South Georgia",
        icon=folium.Icon(color="green", icon="star"),
    ).add_to(m)

    cluster = MarkerCluster(name="USNIC Antarctic icebergs").add_to(m)

    for _, row in df.iterrows():
        name = str(row[name_col]) if name_col else "Iceberg"
        length = row.get(length_col, "")
        width = row.get(width_col, "")
        area_km = row.get(area_km_col, "")
        update = row.get(update_col, "")

        tooltip = (
            f"<b>{name}</b><br>"
            f"Lat/Lon: {row['_lat']:.3f}, {row['_lon']:.3f}<br>"
            f"Length: {length} NM<br>"
            f"Width: {width} NM<br>"
            f"Area: {area_km} sq km<br>"
            f"Distance to South Georgia: {row['_dist_sg_nm']:.0f} NM"
        )

        table_rows = []
        for col in df.columns:
            if col.startswith("_"):
                continue
            val = row[col]
            if pd.notna(val):
                table_rows.append(
                    f"<tr><th style='text-align:left;padding-right:8px'>{col}</th><td>{val}</td></tr>"
                )

        table_rows.append(
            f"<tr><th style='text-align:left;padding-right:8px'>Mapped Latitude</th><td>{row['_lat']:.5f}</td></tr>"
        )
        table_rows.append(
            f"<tr><th style='text-align:left;padding-right:8px'>Mapped Longitude</th><td>{row['_lon']:.5f}</td></tr>"
        )
        table_rows.append(
            f"<tr><th style='text-align:left;padding-right:8px'>Distance to South Georgia</th><td>{row['_dist_sg_nm']:.1f} NM</td></tr>"
        )

        popup = f"<b>{name}</b><br><table>{''.join(table_rows)}</table>"

        radius = 6
        try:
            area_val = float(area_km)
            radius = max(5, min(16, 4 + math.sqrt(area_val) / 6))
        except Exception:
            pass

        folium.CircleMarker(
            location=[row["_lat"], row["_lon"]],
            radius=radius,
            popup=folium.Popup(popup, max_width=460),
            tooltip=folium.Tooltip(tooltip, sticky=True),
            fill=True,
            fill_opacity=0.82,
            color="black",
            weight=1,
        ).add_to(cluster)

    MiniMap(toggle_display=True).add_to(m)
    MeasureControl(position="topleft").add_to(m)
    folium.LayerControl().add_to(m)

    filter_text = f"<br>Filter: latitude north of {north_of_lat}°" if north_of_lat is not None else ""
    title = f"""
    <div style="position: fixed; top: 10px; left: 50px; z-index: 9999; background: white;
                padding: 8px 12px; border: 1px solid #999; border-radius: 6px;
                font-family: Arial, sans-serif; font-size: 14px;">
      <b>USNIC Antarctic Icebergs</b><br>{len(df)} mapped positions{filter_text}<br>
      Hover markers for size/area. Click for full CSV details.
    </div>
    """
    m.get_root().html.add_child(folium.Element(title))
    m.save(out_path)
    print(f"Wrote {Path(out_path).resolve()} with {len(df)} iceberg markers")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="Optional local USNIC CSV file. If omitted, downloads current CSV.")
    ap.add_argument("--out", default="antarc_icebergs_map.html", help="Output HTML map filename.")
    ap.add_argument("--north-of-lat", type=float, help="Only show icebergs north of this latitude, e.g. -60.")
    ap.add_argument("--center-south-georgia", action="store_true", help="Start map centered on South Georgia.")
    args = ap.parse_args()

    df, source_label, source_date, http_last_modified = load_csv(args.csv)
    print_data_source_summary(df, source_label, source_date, http_last_modified)

    build_map(
        df,
        Path(args.out),
        north_of_lat=args.north_of_lat,
        center_south_georgia=args.center_south_georgia,
    )


if __name__ == "__main__":
    main()
