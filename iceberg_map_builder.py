"""USNIC current iceberg map with optional official archived observation tracks.

Requires iceberg_map_builder_v5.py alongside this script.
Archive discovery uses Selenium with locally installed Chrome or Edge.
Historical snapshots are cached in data/usnic_archive/; later runs reuse them.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse, parse_qs

import pandas as pd
import requests
import iceberg_map_builder_v5 as v5

ARCHIVE_URL = 'https://usicecenter.gov/Products/ArchiveSearchMulti?linkChange=ant-three&table=IcebergProducts'
DOWNLOAD_URL = 'https://usicecenter.gov/File/DownloadArchive?prd={}'
EARLIEST = date(2024, 8, 27)


def normalize_name(value):
    return re.sub(r'[^A-Z0-9]', '', str(value).upper())


def archive_browser(start, end, browser_name):
    """Discover product IDs using the archive's JavaScript-powered search form."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys

    if browser_name == 'edge':
        options = webdriver.EdgeOptions()
        options.add_argument('--headless=new')
        driver = webdriver.Edge(options=options)
    else:
        options = webdriver.ChromeOptions()
        options.add_argument('--headless=new')
        driver = webdriver.Chrome(options=options)

    def set_date(element_id, value):
        el = driver.find_element(By.ID, element_id)
        driver.execute_script('arguments[0].removeAttribute("readonly");', el)
        el.clear()
        el.send_keys(value.strftime('%m/%d/%Y'))
        driver.execute_script('arguments[0].dispatchEvent(new Event("change", {bubbles:true}));', el)

    def read_rows():
        return driver.execute_script('''
            return Array.from(document.querySelectorAll('a[href*="DownloadArchive"]'))
                .map(a => ({name:(a.textContent||'').trim(), href:a.href}));
        ''')

    try:
        driver.get(ARCHIVE_URL)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, 'startdate-pick')))
        set_date('startdate-pick', start)
        # Archive interface has changed before; handle optional end date selectors.
        end_ids = ('enddate-pick', 'endDate-pick', 'enddate', 'end-date-pick')
        for element_id in end_ids:
            if driver.find_elements(By.ID, element_id):
                set_date(element_id, end)
                break
        driver.find_element(By.ID, 'search').click()
        WebDriverWait(driver, 45).until(EC.presence_of_element_located((By.ID, 'tbl-berg')))
        time.sleep(2)  # allow asynchronous table population

        # DataTables may show only 10 entries at once. Try 'All' first.
        selects = driver.find_elements(By.CSS_SELECTOR, 'select[name$="_length"]')
        for sel in selects:
            try:
                choices = Select(sel)
                all_option = next((o for o in choices.options if o.get_attribute('value') == '-1'), None)
                if all_option:
                    choices.select_by_value('-1')
                else:
                    choices.select_by_value(max((o.get_attribute('value') for o in choices.options if (o.get_attribute('value') or '').isdigit()), key=int))
                time.sleep(1)
            except Exception:
                pass

        found = {}
        for _ in range(100):
            for row in read_rows():
                match = re.search(r'[?&]prd=(\d+)', row['href'], re.I)
                if match and row['name'].lower().endswith('.csv'):
                    found[match.group(1)] = row['name']
            # Advance any enabled DataTables Next link, avoiding endless loops.
            next_links = driver.find_elements(By.CSS_SELECTOR, '#tbl-berg_next, a.paginate_button.next, button[aria-label="Next"]')
            next_link = next((a for a in next_links if a.is_displayed() and 'disabled' not in (a.get_attribute('class') or '').lower() and a.get_attribute('aria-disabled') != 'true'), None)
            if not next_link:
                break
            before = driver.execute_script('return document.querySelector("#tbl-berg tbody")?.innerText || "";')
            driver.execute_script('arguments[0].click();', next_link)
            try:
                WebDriverWait(driver, 10).until(lambda d: d.execute_script('return document.querySelector("#tbl-berg tbody")?.innerText || "";') != before)
            except Exception:
                break
        return found
    finally:
        driver.quit()


def valid_csv(content):
    if not content or b'<html' in content[:512].lower():
        raise ValueError('Archive returned HTML instead of a CSV')
    df = pd.read_csv(io.BytesIO(content))
    if not all(v5.find_col(df, [c]) for c in ('Iceberg', 'Latitude', 'Longitude')):
        raise ValueError(f'Unexpected USNIC CSV columns: {list(df.columns)}')
    return df


def update_archive(cache_dir, start, end, browser_name, refresh_index=False):
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = cache_dir / 'index.json'
    if manifest.exists() and not refresh_index:
        index = json.loads(manifest.read_text(encoding='utf-8'))
        # A previous index may cover only a smaller date window.
        covered_start = date.fromisoformat(index['search_start'])
        covered_end = date.fromisoformat(index['search_end'])
        if covered_start > start or covered_end < end:
            refresh_index = True
    else:
        refresh_index = True
    if refresh_index:
        print(f'Searching USNIC archive: {start} to {end} ({browser_name})...')
        products = archive_browser(start, end, browser_name)
        if not products:
            raise RuntimeError('No archive CSV links discovered. The USNIC search interface may have changed; no historical tracks were generated.')
        index = {'search_start':start.isoformat(), 'search_end':end.isoformat(), 'products':products}
        manifest.write_text(json.dumps(index, indent=2), encoding='utf-8')
        print(f'Archive search found {len(products)} CSV products.')
    products = index['products']
    session = requests.Session()
    session.headers.update({'User-Agent':'ISSAbove-IcebergMap/1.0 (USNIC archive research)'})
    errors=[]; downloaded=0
    for product_id, filename in products.items():
        path = cache_dir / f'{product_id}.csv'
        if path.exists():
            continue
        try:
            response = session.get(DOWNLOAD_URL.format(product_id), timeout=35)
            response.raise_for_status()
            valid_csv(response.content)
            path.write_bytes(response.content)
            downloaded += 1
        except Exception as exc:
            errors.append(f'{product_id} ({filename}): {exc}')
    print(f'Archive: {len(products)} products indexed, {downloaded} newly downloaded, {len(errors)} failed.')
    if errors:
        print('Archive download errors (first five):', *errors[:5], sep='\n  ')
    return [cache_dir / f'{product_id}.csv' for product_id in products if (cache_dir / f'{product_id}.csv').exists()]


def historical_observations(paths, start, end):
    records=[]
    for path in paths:
        try:
            df = pd.read_csv(path)
            cols = [v5.find_col(df, names) for names in (['Iceberg'], ['Latitude'], ['Longitude'], ['Last Update', 'Date'])]
            if any(c is None for c in cols):
                continue
            subset = df[cols].copy()
            subset.columns = ['name','lat','lon','date']
            subset['name'] = subset['name'].map(normalize_name)
            subset['lat'] = subset['lat'].map(v5.coord_to_decimal)
            subset['lon'] = subset['lon'].map(v5.coord_to_decimal)
            subset['date'] = pd.to_datetime(subset['date'], errors='coerce').dt.date
            subset['source_file'] = path.name
            records.append(subset)
        except Exception as exc:
            print(f'WARNING: Cannot parse {path}: {exc}')
    if not records:
        return pd.DataFrame(columns=['name','lat','lon','date','source_file'])
    history = pd.concat(records, ignore_index=True).dropna(subset=['name','lat','lon','date'])
    history = history[history['date'].between(start,end) & history['lat'].between(-90,90) & history['lon'].between(-180,180)]
    # Same observation may appear in several weekly files. Keep one copy.
    return history.drop_duplicates(['name','date','lat','lon']).sort_values(['name','date'])


def add_tracks(html_path, history, start, end, names):
    html = html_path.read_text(encoding='utf-8')
    match = re.search(r'var (map_[a-f0-9]+)\s*=\s*L\.map\(', html)
    if not match:
        raise RuntimeError('Unable to identify the Folium map variable')
    map_name = match.group(1)
    lines=[]; tracks=0
    for name, group in history[history['name'].isin(names)].groupby('name'):
        group = group.sort_values('date')
        segments=[]; segment=[]
        for row in group.itertuples(index=False):
            pos=[float(row.lat),float(row.lon)]
            if segment and abs(pos[1]-segment[-1][1]) > 180:
                if len(segment)>1: segments.append(segment)
                segment=[]
            segment.append(pos)
        if len(segment)>1: segments.append(segment)
        if not segments:
            continue
        tracks+=1
        for seg in segments:
            lines.append(f'L.polyline({json.dumps(seg)}, {{color:"#0087c5",weight:3,opacity:0.8}}).bindTooltip({json.dumps(name+" observed track")}).addTo(trackLayer);')
        for row in group.itertuples(index=False):
            tooltip=f'{name} | {row.date} | {row.lat:.3f}, {row.lon:.3f}'
            lines.append(f'L.circleMarker([{float(row.lat)},{float(row.lon)}],{{radius:3,color:"#006a9e",fillOpacity:0.7,weight:1}}).bindTooltip({json.dumps(tooltip)}).addTo(trackLayer);')
    control_match = re.search(r'let (layer_control_[a-f0-9]+)\s*=\s*L\.control\.layers\(', html)
    if not control_match:
        raise RuntimeError('Unable to locate Folium layer control')
    control_name = control_match.group(1)
    script = ('\n// Historical iceberg tracks\nvar trackLayer = L.layerGroup();\n'
              + '\n'.join(lines)
              + f'\ntrackLayer.addTo({map_name});\n'
              + f'{control_name}.addOverlay(trackLayer, {json.dumps("Historical tracks (" + str(start) + " to " + str(end) + ")")});\n')
    last_script_end = html.rfind('</script>')
    if last_script_end < 0:
        raise RuntimeError('Unable to locate final Folium script block')
    html_path.write_text(html[:last_script_end] + script + html[last_script_end:], encoding='utf-8')
    return tracks


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--csv', help='Use local current USNIC CSV instead of live download')
    p.add_argument('--out', default='iceberg-map.html')
    p.add_argument('--days', type=int, default=90)
    p.add_argument('--since', help='History start YYYY-MM-DD; not before 2024-08-27')
    p.add_argument('--iceberg', help='Show historical track for this iceberg only')
    p.add_argument('--no-tracks', action='store_true')
    p.add_argument('--history-dir', default='data/usnic_archive')
    p.add_argument('--refresh-history', action='store_true', help='Refresh archive index; cached CSV files reused')
    p.add_argument('--browser', choices=['chrome','edge'], default='chrome')
    p.add_argument('--north-of-lat', type=float)
    p.add_argument('--center-south-georgia', action='store_true')
    p.add_argument('--marker-color', default='yellow')
    p.add_argument('--marker-outline', default='black')
    p.add_argument('--marker-shape', choices=['triangle','circle','square','diamond','star'], default='triangle')
    p.add_argument('--cluster', action='store_true')
    args=p.parse_args()
    if args.days<1: p.error('--days must be positive')
    df, label, source_date, lastmod=v5.load_csv(args.csv)
    v5.print_data_source_summary(df,label,source_date,lastmod)
    out=Path(args.out)
    v5.build_map(df,out,north_of_lat=args.north_of_lat,center_south_georgia=args.center_south_georgia,
                 marker_color=args.marker_color,marker_outline=args.marker_outline,
                 marker_shape=args.marker_shape,cluster=args.cluster)
    if args.no_tracks: return
    dt_col=v5.find_col(df,['Last Update','Update','Date'])
    dates=pd.to_datetime(df[dt_col],errors='coerce') if dt_col else pd.Series(dtype='datetime64[ns]')
    end=dates.max().date() if not dates.dropna().empty else date.today()
    try:
        start=max(EARLIEST, date.fromisoformat(args.since) if args.since else end-timedelta(days=args.days))
    except ValueError:
        p.error('--since must be YYYY-MM-DD')
    if start>end: p.error('History start is later than current observation')
    try:
        paths=update_archive(Path(args.history_dir),start,end,args.browser,args.refresh_history)
        history=historical_observations(paths,start,end)
        name_col=v5.find_col(df,['Iceberg','Name'])
        lat_col=v5.find_col(df,['Latitude'])
        lon_col=v5.find_col(df,['Longitude'])
        current=df.copy()
        current['_name']=current[name_col].map(normalize_name)
        current['_lat']=current[lat_col].map(v5.coord_to_decimal)
        current['_lon']=current[lon_col].map(v5.coord_to_decimal)
        current=current.dropna(subset=['_lat','_lon'])
        if args.north_of_lat is not None:
            current=current[current['_lat']>args.north_of_lat]
        names=set(current['_name'])
        if args.iceberg: names &= {normalize_name(args.iceberg)}
        count=add_tracks(out,history,start,end,names)
        print(f'Historical observations: {len(history)} unique positions; {count} drawn tracks; {start} to {end}')
        if count==0:
            print('WARNING: No tracks drawn (no matching observations or insufficient distinct positions).')
    except Exception as exc:
        print(f'WARNING: Official USNIC archive retrieval failed: {exc}')
        print(f'Current-only map remains available: {out}')

if __name__=='__main__':
    main()
