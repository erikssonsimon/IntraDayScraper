"""
    Fetch market price data from https://www.energy-charts.info/
    Inspired by this [repo](https://github.com/chris1869/grid_data)

    The code scrapes the data from the 'highcharts' charts of the webpage using headless selenium.

    Produces (in data/):
    - DE_price_spot_market_<first>_<last>.csv
    - DE_price_volume_dayahead_<first>_<last>.csv
    - DE_price_volume_intraday_<first>_<last>.csv

    How it works:
    - For yearly pages ('xy' mode) the x values of every series are read from the chart together
      with the x-axis metadata (categories, pointStart/pointInterval, tick labels).
    - On energy-charts the x values are often NOT timestamps but positions (0, 1, 2, ...).
      Read as milliseconds those land in January 1970, which is the bug this version fixes.
      The real dates are worked out in this order:
        1. x values that are already real epoch-ms timestamps (>= year 2000) are used as is;
        2. otherwise the positions are looked up in the x-axis categories (the date labels);
        3. otherwise an explicit pointInterval on the series is used, starting at 1 Jan of the year;
        4. otherwise the frequency is inferred from the number of points, but only when it matches
           a standard resolution (15 min / hourly / daily) exactly for a complete year
           (or within 2 days of "now" for the running year).
      A series whose dates cannot be worked out is dropped with a message, never guessed.
    - Every year's index is checked to lie inside that year, and nothing is written to a csv
      if any timestamp is before 2000 (i.e. still in 1970).
    - Raw scraped data is cached in cache/*_xy2.json. Re-running skips cached files.
      Delete a cache file if you want to re-fetch it. Old cache/*_xy.json files from the buggy
      version are ignored (they lack the axis metadata) and can be deleted.
    - Trading zones changed over the years (DE-AT-LU -> DE-LU). The mappings below
      unify the column names.

    The power and weather functions are still here, but main() does not call them.
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

import numpy as np
import pandas as pd
import os
import gc
import json
import time
import calendar
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

''' -------------- energy-charts URLs -------------------- '''

def create_energy_url(year=2024, country: str = 'DE', source="public"):
    """URL for the 'power' charts (yearly)."""
    base_url = "https://www.energy-charts.info/charts/power/chart.htm?l=en"
    return base_url + f"&c={country}&source={source}&interval=year&year={year}&timezone=utc"


def create_market_url(year=2024, market: str = "price_spot_market", country: str = 'DE',
                      period: Optional[str] = None):
    """URL for the market / price charts (yearly)."""
    base_url = f"https://www.energy-charts.info/charts/{market}/chart.htm?l=en"
    url = base_url + f"&c={country}&interval=year&year={year}&timezone=utc"
    if period is not None:
        url += "&period=" + period
    return url


def create_weather_url(year=2024, month: str = '01', country: str = 'DE', source: str = 'wind_speed'):
    """URL for the weather charts (monthly)."""
    base_url = "https://www.energy-charts.info/charts/climate_hours/chart.htm?l=en"
    return base_url + f"&c={country}&interval=month&year={year}&source={source}&month={month}&timezone=utc"


''' -------------- selenium helper functions --------------------- '''

def safe_webdriver_initialization() -> Optional[webdriver.Chrome]:
    """ create headless selenium driver """
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-gpu')

    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    except Exception as e:
        print(f"Error initializing WebDriver: {e}")
        driver = None
    return driver


def close_driver(driver: Optional[webdriver.Chrome]):
    if driver:
        try:
            driver.delete_all_cookies()
        except Exception:
            pass
        driver.quit()


# JavaScript snippets. Each one picks the chart with the most data points (instead of
# assuming Highcharts.charts[0], which can be undefined) and returns its series.
JS_MAX_POINTS = """
    if (typeof Highcharts === 'undefined') return 0;
    var best = 0;
    (Highcharts.charts || []).forEach(function(c){
        if (c && c.series) c.series.forEach(function(s){
            if (!s) return;
            var n = (s.xData && s.xData.length) ? s.xData.length : (s.data ? s.data.length : 0);
            best = Math.max(best, n);
        });
    });
    return best;
"""

# 'simple': list of y values per series (used by the monthly weather pages)
JS_SIMPLE = """
    var best = null, bestN = 0;
    (Highcharts.charts || []).forEach(function(c){
        if (!c || !c.series) return;
        var n = 0;
        c.series.forEach(function(s){ if (s && s.data) n = Math.max(n, s.data.length); });
        if (n > bestN) { best = c; bestN = n; }
    });
    if (!best) { return null; }
    var series_vals = {};
    best.series.forEach(function(s){
        if (s && s.data) {
            series_vals[s.name] = s.data.map(function(d){ return d.y; });
        }
    });
    return series_vals;
"""

# 'xy': [x, y] pairs per series PLUS the x-axis metadata needed to turn x into real dates.
# x is whatever Highcharts stores: a timestamp on a datetime axis, a position (0, 1, 2, ...)
# on a category axis. Returns {"meta": {...}, "series": {name: [[x, y], ...]}}.
JS_XY = """
    var best = null, bestN = 0;
    (Highcharts.charts || []).forEach(function(c){
        if (!c || !c.series) return;
        var n = 0;
        c.series.forEach(function(s){
            var len = (s && s.xData && s.xData.length) ? s.xData.length
                    : (s && s.data ? s.data.length : 0);
            n = Math.max(n, len);
        });
        if (n > bestN) { best = c; bestN = n; }
    });
    if (!best) { return null; }

    function clean(v){
        if (v === null || v === undefined) return null;
        if (typeof v === 'number') return v;
        if (v instanceof Date) return v.getTime();
        return String(v);
    }

    var ax = (best.xAxis && best.xAxis.length) ? best.xAxis[0] : null;
    var cats = null;
    if (ax) {
        if (ax.categories && ax.categories.length) cats = ax.categories.map(clean);
        else if (ax.names && ax.names.length) cats = ax.names.map(clean);
    }
    var ticks = {};
    if (ax && ax.ticks) {
        Object.keys(ax.ticks).slice(0, 60).forEach(function(k){
            var t = ax.ticks[k];
            if (t && t.label) ticks[k] = t.label.textStr || (t.label.element ? t.label.element.textContent : null);
        });
    }
    var meta = {
        axis_type: (ax && ax.options) ? (ax.options.type || null) : null,
        categories: cats,
        tick_labels: ticks,
        series: {}
    };

    var out = {};
    best.series.forEach(function(s){
        if (!s) return;
        var pts = null;
        if (s.xData && s.xData.length) {
            pts = s.xData.map(function(x, i){ return [clean(x), s.yData[i]]; });
        } else if (s.data && s.data.length) {
            pts = s.data.map(function(d){ return [clean(d.x), d.y]; });
        }
        if (!pts) return;
        out[s.name] = pts;
        var o = s.options || {};
        meta.series[s.name] = {
            point_start: (o.pointStart === undefined) ? null : clean(o.pointStart),
            point_interval: (o.pointInterval === undefined) ? null : o.pointInterval,
            point_interval_unit: (o.pointIntervalUnit === undefined) ? null : o.pointIntervalUnit
        };
    });
    return {meta: meta, series: out};
"""


def _max_points(driver) -> int:
    try:
        return int(driver.execute_script(JS_MAX_POINTS) or 0)
    except Exception:
        return 0


def extract_data_headless(url: str, wait_for_html_load: bool, sleep_time: int,
                          script_option: str, cache_fname: str) -> None:
    """
    Open the page headless, wait until the chart has loaded and its number of points has
    stopped changing, then pull the series out of Highcharts and write them to cache_fname.
    Raises if nothing usable was found (so an empty result is never cached).
    """
    if script_option == 'simple':
        script = JS_SIMPLE
    elif script_option == 'xy':
        script = JS_XY
    else:
        raise NameError(f"Script option {script_option} not recognized")

    driver = safe_webdriver_initialization()
    if driver is None:
        raise RuntimeError("WebDriver not initialized")

    try:
        driver.get(url)

        if wait_for_html_load:
            print(f"Waiting for HTML load for up to {sleep_time + 10}s...")
            WebDriverWait(driver, sleep_time + 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.highcharts-container"))
            )
            # wait until a chart with data exists
            WebDriverWait(driver, sleep_time + 10).until(lambda d: _max_points(d) > 0)

        # wait until the number of points stops changing (chart loads progressively)
        last, stable = -1, 0
        deadline = time.time() + sleep_time + 30
        while stable < 3 and time.time() < deadline:
            n = _max_points(driver)
            stable = stable + 1 if n == last else 0
            last = n
            time.sleep(2)
        print(f"Chart point count settled at {last}")

        print(f"Executing java script option={script_option}")
        json_data = driver.execute_script(script)
        if not json_data:
            raise RuntimeError("No chart with data found on page")
        if script_option == 'xy':
            if not json_data.get("series"):
                raise RuntimeError("Chart found, but it has no series with data")
            json_data["meta"]["url"] = url
            json_data["meta"]["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        print(f"Saving cache data {cache_fname}")
        with open(cache_fname, "w") as f:
            json.dump(json_data, f)
    finally:
        close_driver(driver)  # always release Chrome, even on failure


def fetch_or_load_json_for_url_request(url: str, cache_fname: str, script_option: str = 'simple') -> str:
    """
    Return the path of the cached json for this url. If it is not cached yet, scrape it
    (up to 5 attempts with increasing wait time).
    'xy' caches use the suffix _xy2, so the old (buggy, metadata-less) _xy caches are never reused.
    """
    suffix = "_xy2" if script_option == 'xy' else ""
    cache_path = f"cache/{cache_fname}{suffix}.json"
    print(f"Caching data {cache_path}")

    if os.path.exists(cache_path):
        # make sure the cached file is valid, otherwise refetch
        try:
            with open(cache_path) as f:
                if json.load(f):
                    print("Cache file already exists. Reading... ", url)
                    return cache_path
            print("Cache file is empty. Removing and refetching...")
        except Exception:
            print("Cache file is corrupt. Removing and refetching...")
        os.remove(cache_path)
    else:
        print("Cache file does not exist. Fetching... ", url)

    max_attempts = 5
    attempt = 0
    sleep_time = 5

    while attempt < max_attempts:
        try:
            extract_data_headless(url, True, sleep_time, script_option=script_option,
                                  cache_fname=cache_path)
            break
        except Exception as e:
            print(f"Attempt {attempt + 1}/{max_attempts} (sleep_time={sleep_time}) failed with {str(e)[:300]}")
            attempt += 1
            sleep_time += 10
        gc.collect()

    if not os.path.exists(cache_path):
        raise FileNotFoundError(f"Failed to fetch the data for {url}. See errors above.")

    print(f"Successfully extracted data after {attempt} failed attempts.")
    return cache_path


''' -------------- turning chart x values into real dates --------------------- '''

# anything before this is treated as "not a real date" (1970 = positions read as ms)
MIN_VALID_TS = pd.Timestamp("2000-01-01")
MIN_VALID_MS = MIN_VALID_TS.value // 10**6          # 946684800000

# resolutions tried when the date has to be inferred from the number of points
CANDIDATE_FREQS = [("15min", pd.Timedelta(minutes=15)),
                   ("hourly", pd.Timedelta(hours=1)),
                   ("daily", pd.Timedelta(days=1))]


def _parse_categories(cats, year: int) -> Optional[pd.DatetimeIndex]:
    """Turn the x-axis category labels into a DatetimeIndex (UTC, naive), or None if they are not dates."""
    if not cats:
        return None

    s = pd.Series(cats, dtype="object")

    # numeric categories: epoch seconds or ms
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().mean() > 0.99:
        lo = num.min()
        if lo >= MIN_VALID_MS:
            return pd.DatetimeIndex(pd.to_datetime(num, unit="ms"))
        if lo >= MIN_VALID_MS / 1000:
            return pd.DatetimeIndex(pd.to_datetime(num, unit="s"))
        return None

    # string categories: try month-first and day-first, keep the one that parses best
    best = None
    for dayfirst in (False, True):
        try:
            parsed = pd.to_datetime(s.astype(str), errors="coerce", dayfirst=dayfirst,
                                    format="mixed", utc=True)
        except (TypeError, ValueError):
            continue
        parsed = parsed.dt.tz_convert(None)
        ok = parsed.notna().mean()
        in_year = (parsed.dt.year == year).mean()
        mono = parsed.dropna().is_monotonic_increasing
        score = (ok, in_year, mono)
        if best is None or score > best[0]:
            best = (score, parsed)

    if best is None:
        return None
    (ok, in_year, mono), parsed = best
    if ok < 0.99 or not mono or in_year < 0.95:
        print(f"  x-axis categories are not usable dates (parsed={ok:.0%}, in {year}={in_year:.0%}, "
              f"increasing={mono}); sample: {list(s[:3])}")
        return None
    return pd.DatetimeIndex(parsed)


def _infer_from_count(n: int, year: int, fetched_at: pd.Timestamp):
    """
    Last resort for positions 0..n-1 without any axis information: accept a resolution only if
    n matches it exactly for a complete year, or, for the running year, the time span from 1 Jan up
    to the fetch time (+/- 2 days). Returns (start, step, label) or None.
    """
    start = pd.Timestamp(year, 1, 1)
    end_of_year = pd.Timestamp(year + 1, 1, 1)
    matches = []
    for label, step in CANDIDATE_FREQS:
        full = int((end_of_year - start) / step)
        if fetched_at >= end_of_year + pd.Timedelta(days=2):
            if n == full:
                matches.append((start, step, f"{label} (exactly {full} points = full year {year})"))
        else:
            lo = int((min(fetched_at - pd.Timedelta(days=3), end_of_year) - start) / step)
            hi = int((min(fetched_at + pd.Timedelta(days=2), end_of_year) - start) / step)
            if lo <= n <= hi:
                matches.append((start, step, f"{label} (running year, {n} points up to "
                                             f"{fetched_at:%Y-%m-%d}; assumes no missing points)"))
    return matches[0] if len(matches) == 1 else None


def resolve_dates(x: np.ndarray, year: int, meta: dict, smeta: dict,
                  fetched_at: pd.Timestamp):
    """
    Map the chart x values of one series to real timestamps. Returns (DatetimeIndex, how).
    Raises ValueError if the dates cannot be determined safely.
    """
    x = np.asarray(x, dtype="float64")

    # 1. already real epoch-ms timestamps
    if len(x) and np.nanmin(x) >= MIN_VALID_MS:
        return pd.DatetimeIndex(pd.to_datetime(x, unit="ms")), "chart timestamps"

    # everything below needs whole-number positions
    if len(x) == 0 or np.nanmin(x) < 0 or not np.all(np.mod(x, 1) == 0):
        raise ValueError(f"x values are neither timestamps nor positions (first: {x[:3].tolist()})")
    pos = x.astype("int64")

    # 2. positions into the x-axis categories
    cats = meta.get("categories")
    if cats and pos.max() < len(cats):
        cat_index = _parse_categories(cats, year)
        if cat_index is not None:
            return pd.DatetimeIndex(cat_index[pos]), "x-axis category labels"

    # 3. explicit pointInterval (ms) on the series, starting at 1 Jan of the year
    pi = smeta.get("point_interval")
    if pi and pi >= 1000 and not smeta.get("point_interval_unit"):
        steps = pos // int(pi) if np.all(pos % int(pi) == 0) and pos.max() >= pi else pos
        idx = pd.Timestamp(year, 1, 1) + pd.to_timedelta(steps * int(pi), unit="ms")
        return pd.DatetimeIndex(idx), f"pointInterval={pi} ms from 1 Jan {year}"

    # 4. number of points, only for contiguous positions 0..n-1 that match a standard resolution
    n = len(pos)
    if np.array_equal(np.sort(pos), np.arange(n)):
        found = _infer_from_count(n, year, fetched_at)
        if found is not None:
            start, step, label = found
            return pd.DatetimeIndex(start + step * pos), f"inferred from point count: {label}"

    ticks = list((meta.get("tick_labels") or {}).items())[:5]
    raise ValueError(f"cannot work out dates: x are positions 0..{pos.max()} ({n} points), no usable "
                     f"category labels or pointInterval, and the count matches no standard resolution "
                     f"for {year}. Tick labels seen: {ticks}")


def check_index_in_year(idx: pd.DatetimeIndex, year: int, what: str):
    """Refuse an index that still lands in 1970 or outside the requested year."""
    if len(idx) == 0:
        raise ValueError(f"{what}: empty index")
    if idx.min() < MIN_VALID_TS:
        raise ValueError(f"{what}: dates start at {idx.min()} (positions read as 1970 timestamps)")
    lo = pd.Timestamp(year, 1, 1) - pd.Timedelta(days=1)
    hi = pd.Timestamp(year + 1, 1, 1) + pd.Timedelta(days=2)
    if idx.min() < lo or idx.max() > hi:
        raise ValueError(f"{what}: dates {idx.min()} .. {idx.max()} are outside year {year}")


''' -------------- data processing --------------------- '''

def load_xy_cache(path: str, year: int) -> pd.DataFrame:
    """Build a DataFrame with a real DatetimeIndex (UTC, naive) from an 'xy' cache file."""
    with open(path) as f:
        raw = json.load(f)

    if isinstance(raw, dict) and "series" in raw:
        series, meta = raw["series"], raw.get("meta") or {}
    else:                                   # old format: {name: [[x, y], ...]} without metadata
        series, meta = raw, {}

    fetched_at = pd.Timestamp(meta["fetched_at"]) if meta.get("fetched_at") \
        else pd.Timestamp(os.path.getmtime(path), unit="s")

    cols, used = {}, Counter()
    for name, pts in series.items():
        if not pts or not isinstance(pts[0], list):
            continue
        try:
            x = pd.to_numeric(pd.Series([p[0] for p in pts]), errors="coerce").to_numpy()
            y_raw = [p[1] for p in pts]
            if any(isinstance(v, (list, dict)) for v in y_raw):
                raise ValueError("y values are not single numbers (range/area series), skipped")
            y = pd.to_numeric(pd.Series(y_raw, dtype="object"), errors="coerce").to_numpy(dtype="float64")
            keep = ~np.isnan(x)
            idx, how = resolve_dates(x[keep], year, meta, (meta.get("series") or {}).get(name, {}),
                                     fetched_at)
            check_index_in_year(idx, year, f"series '{name}'")
        except (ValueError, TypeError) as e:
            print(f"  Dropping series '{name}': {e}")
            continue
        s = pd.Series(y[keep], index=idx)
        cols[name] = s[~s.index.duplicated(keep='first')]
        used[how] += 1

    if not cols:
        raise ValueError(f"No series with usable dates in {path}. See messages above; "
                         f"delete this file to re-fetch it.")
    print(f"  Dates from: {dict(used)}")
    return pd.DataFrame(cols).sort_index()


def prepare_power_dataframe(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Add year / hour columns and print what the page actually returned."""
    step = df.index.to_series().diff().mode()
    print(f"year={year} rows={len(df)} first={df.index.min()} last={df.index.max()} "
          f"step={step.iloc[0] if len(step) else None}")
    df["year"] = df.index.year
    df["hour"] = df.index.hour
    return df


def process_dataframe(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    df = df.rename(columns=mapping)

    # merge columns that got the same name after mapping (keep the one with the fewest NaNs)
    if df.columns.duplicated().any():
        out = {}
        for name in df.columns.unique():
            sub = df.loc[:, name]
            if isinstance(sub, pd.DataFrame):
                sub = sub.iloc[:, int(sub.isna().sum().values.argmin())]
            out[name] = sub
        df = pd.DataFrame(out, index=df.index)

    for col in df.columns:
        if df[col].isna().sum() > 0.2 * len(df[col]):
            print(f"Warning for col {col}: NaNs > 20%")

    return df


def check_json_file_for_monthly_data(cache_fname: str) -> pd.DataFrame:
    """Monthly weather: keep only the series that have the most common length, build hourly index."""
    with open(cache_fname, 'r') as file:
        data = json.load(file)

    lengths = {key: (len(v) if isinstance(v, list) else None) for key, v in data.items()}
    most_common_length = Counter(lengths.values()).most_common(1)[0][0]

    keys_ok = [k for k, n in lengths.items() if n == most_common_length]
    keys_bad = [k for k, n in lengths.items() if n != most_common_length]
    print(f"Keys with the most common length ({most_common_length}) N={len(keys_ok)}/{len(lengths)}: {keys_ok}")
    print(f"Keys with different lengths: N={len(keys_bad)}/{len(lengths)}: {keys_bad}")

    def hours_in_month(year: int, month: int):
        now = datetime.now()
        if year == now.year and month == now.month:
            return (now.day - 1) * 24 + now.hour
        return 24 * calendar.monthrange(year, month)[1]

    pieces = os.path.basename(cache_fname).split('_')   # DE_weather_<year>_<month>_...
    year, month = int(pieces[2]), int(pieces[3])
    expected_hours = hours_in_month(year, month)
    print(f"Expected number of hourly entries for {calendar.month_name[month]} {year}: {expected_hours}")
    print(f"Does the most common length match? {'Yes' if most_common_length == expected_hours else 'No'}")

    start = datetime(year, month, 1)
    df = pd.DataFrame({k: data[k] for k in keys_ok})
    df.index = pd.date_range(start=start, periods=len(df), freq='h')
    return df


''' -------------- scrape + store --------------------- '''

def scrape_yearly(years, make_url, make_cache_name, mapping: dict, res_name: str):
    """Scrape one yearly page per year, combine, and write a csv. Failing years are skipped."""
    frames = []
    for a_year in years:
        print(f"\n=== {res_name} | year={a_year} ===")
        try:
            cache_path = fetch_or_load_json_for_url_request(
                make_url(a_year), cache_fname=make_cache_name(a_year), script_option='xy'
            )
            df_i = load_xy_cache(cache_path, a_year)
            check_index_in_year(df_i.index, a_year, f"year {a_year}")
        except Exception as e:
            print(f"Skipping year={a_year}: {e}")
            continue

        df_i = prepare_power_dataframe(df_i, a_year)
        df_i = process_dataframe(df_i, mapping)
        print(list(df_i.columns))
        frames.append(df_i)

    if not frames:
        print(f"No data collected for {res_name}, nothing written.")
        return

    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep='last')].sort_index()

    # final guard: never write a file with 1970 (or otherwise bogus) dates
    if df.index.min() < MIN_VALID_TS:
        raise RuntimeError(f"Refusing to write {res_name}: index starts at {df.index.min()}")

    df.index.name = "time_utc"
    df.to_csv(res_name)
    print(f"Wrote {res_name} ({len(df)} rows, {df.index.min()} .. {df.index.max()})")


def prepare_and_store_power(country, years, area, mapping):
    res_name = (f"data/{country}_power_{years[0]}_{years[-1]}.csv" if area is None
                else f"data/{country}_power_{area}_{years[0]}_{years[-1]}.csv")
    scrape_yearly(
        years,
        make_url=lambda y: create_energy_url(year=y, country=country, source=area),
        make_cache_name=lambda y: (f"{country}_power_{y}_yearly" if area is None
                                   else f"{country}_power_{y}_{area}_yearly"),
        mapping=mapping,
        res_name=res_name,
    )


def prepare_and_store_market(country, years, market, period: Optional[str], mapping: dict):
    res_name = (f"data/{country}_{market}_{years[0]}_{years[-1]}.csv" if period is None
                else f"data/{country}_{market}_{period}_{years[0]}_{years[-1]}.csv")
    scrape_yearly(
        years,
        make_url=lambda y: create_market_url(year=y, market=market, country=country, period=period),
        make_cache_name=lambda y: (f"{country}_{market}_{y}_yearly" if period is None
                                   else f"{country}_{market}_{y}_{period}_yearly"),
        mapping=mapping,
        res_name=res_name,
    )


def prepare_and_store_weather(country, years, quantity, mapping: dict):
    frames = []
    now = datetime.now()
    for a_year in years:
        for j in range(1, 13):
            if a_year >= now.year and j > now.month:
                break
            month = f"{j:02}"
            try:
                print(f"Processing weather for year={a_year} month={month} quantity={quantity}")
                cache_fpath = fetch_or_load_json_for_url_request(
                    create_weather_url(year=a_year, month=month, country=country, source=quantity),
                    cache_fname=f'{country}_weather_{a_year}_{month}_{quantity}_monthly',
                    script_option='simple',
                )
                df_i = check_json_file_for_monthly_data(cache_fpath)
                frames.append(process_dataframe(df_i, mapping=mapping))
            except Exception as e:
                print(f"Failed to get data for year={a_year} month={month} quantity={quantity}: {e}")

    if not frames:
        print(f"No weather data collected for {quantity}")
        return
    pd.concat(frames).to_csv(f"data/{country}_weather_{quantity}_{years[0]}_{years[-1]}.csv")


def main():
    os.makedirs("cache", exist_ok=True)
    os.makedirs("data", exist_ok=True)

    country = 'DE'
    years = range(2018, 2027)

    # spot electricity market prices and volumes
    market = "price_spot_market"
    mapping = {
        # pre 2018
        "CO2 Emission Allowances, Auction DE": "CO2_Emission_Allowance_DE",
        "CO2 Emission Allowances, Auction EU": "CO2_Emission_Allowance_EU",
        "Day Ahead Auction (DE-AT-LU)": "DA_Auction",
        "Intraday Continuous Average Price (DE-AT-LU)": "ID_Continuous_Average_Price",
        "Intraday Continuous High Price (DE-AT-LU)": "ID_Continuous_High_Price",
        "Intraday Continuous Low Price (DE-AT-LU)": "ID_Continuous_Low_Price",
        "Intraday Continuous ID3-Price (DE-AT-LU)": "ID_Continuous_ID3_Price",
        "Intraday auction, average of the 15 min auctions (DE-AT-LU)": "ID_Auction",
        #
        "Cross border electricity trading": "Cross_Border_Electricity_Trading",
        "Day Ahead Auction (DE-AT-LU, DE-LU)": "DA_Auction",
        "Hydro pumped storage consumption": "Hydro_Pumped_Storage_Consumption",
        "Intraday Continuous Average Price (DE-AT-LU, DE-LU)": "ID_Continuous_Average_Price",
        "Intraday Continuous High Price (DE-AT-LU, DE-LU)": "ID_Continuous_High_Price",
        "Intraday Continuous ID1-Price (DE-AT-LU, DE-LU)": "ID_Continuous_ID1_Price",
        "Intraday Continuous ID3-Price (DE-AT-LU, DE-LU)": "ID_Continuous_ID3_Price",
        "Intraday Continuous Low Price (DE-AT-LU, DE-LU)": "ID_Continuous_Low_Price",
        "Intraday auction, average of the 15 min auctions (DE-AT-LU, DE-LU)": "ID_Auction",
        #
        "Intraday Continuous Average Price (DE-LU)": "ID_Continuous_Average_Price",
        "Day Ahead Auction (DE-LU)": "DA_Auction",
        "Intraday Continuous High Price (DE-LU)": "ID_Continuous_High_Price",
        "Intraday Continuous ID1-Price (DE-LU)": "ID_Continuous_ID1_Price",
        "Intraday Continuous ID3-Price (DE-LU)": "ID_Continuous_ID3_Price",
        "Intraday Continuous Low Price (DE-LU)": "ID_Continuous_Low_Price",
        "Intraday auction, average of the 15 min auctions (DE-LU)": "ID_Auction",
    }
    prepare_and_store_market(country, years, market, None, mapping=mapping)

    # day-ahead electricity market prices and volumes
    market = "price_volume"
    mapping = {
        "Day Ahead Auction (DE-AT-LU)": "DA_Auction",
        "Day Ahead Auction (DE-LU)": "DA_Auction",
        "Day Ahead Auction (DE-AT-LU, DE-LU)": "DA_Auction",
        # ---
        "Day Ahead Auction (DE-AT-LU), volume": "DA_Auction_Volume",
        "Day Ahead Auction (DE-LU), volume": "DA_Auction_Volume",
        "Day Ahead Auction (DE-AT-LU, DE-LU), volume": "DA_Auction_Volume",
    }
    prepare_and_store_market(country, years, market, "dayahead", mapping=mapping)

    # intraday electricity market prices and volumes
    mapping = {
        "Intraday Continuous Average Price (DE-AT-LU)": "ID_Continuous_Average_Price",
        "Intraday Continuous Average Price (DE-LU)": "ID_Continuous_Average_Price",
        "Intraday Continuous Average Price (DE-AT-LU, DE-LU)": "ID_Continuous_Average_Price",
        # ---
        "Intraday Continuous (DE-AT-LU), volume": "ID_Continuous_Volume",
        "Intraday Continuous (DE-LU), volume": "ID_Continuous_Volume",
        "Intraday Continuous (DE-AT-LU, DE-LU), volume": "ID_Continuous_Volume",
        "Intraday Continuous  (DE-LU), volume": "ID_Continuous_Volume",
        "Intraday Continuous  (DE-AT-LU), volume": "ID_Continuous_Volume",
        "Intraday Continuous  (DE-AT-LU, DE-LU), volume": "ID_Continuous_Volume",
    }
    prepare_and_store_market(country, years, market, "intraday", mapping=mapping)

    print("All data acquired")


if __name__ == '__main__':
    main()