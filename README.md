# German Electricity Market Data Scraper

A Python-based data collection and preprocessing tool for quantitative research on the German electricity market. The script retrieves historical day-ahead and intraday market data from [Energy-Charts](https://www.energy-charts.info/) using headless Selenium and exports standardized time-series datasets for further analysis, including electricity price dynamics, econometric modelling and battery storage arbitrage.

## Features

- Automated collection of German electricity market data for 2018–2026 (subject to availability).
- Extraction of price and trading volume series directly from Highcharts.
- Timestamp reconstruction and validation, with support for 15-minute, hourly and daily resolutions.
- Standardized column names across the DE-AT-LU and DE-LU bidding zones.
- Local JSON caching, automatic retries and missing-value diagnostics.
- CSV export with chronological, deduplicated UTC timestamps.

## Installation

Requirements: Python 3, Google Chrome and an internet connection for the initial data collection.

Install the Python dependencies:

```bash
pip install selenium webdriver-manager numpy "pandas>=2.0"
```

The ChromeDriver is managed automatically by `webdriver-manager`.

## Usage

Save the Python script as `market_data.py` (or use its existing filename), then run:

```bash
python market_data.py
```

To change the country or time period, edit the following settings in `main()`:

```python
country = 'DE'
years = range(2018, 2027)
```

The script automatically creates the `cache/` and `data/` directories in the working directory.

## Output

With the default configuration, the script attempts to generate:

| File | Contents |
| --- | --- |
| `data/DE_price_spot_market_2018_2026.csv` | Spot market series, including day-ahead and intraday prices, where available |
| `data/DE_price_volume_dayahead_2018_2026.csv` | Day-ahead auction prices and trading volumes |
| `data/DE_price_volume_intraday_2018_2026.csv` | Intraday continuous average prices and trading volumes |

The CSV files use `time_utc` as the timestamp index (UTC without an explicit timezone offset) and include `year` and `hour` columns.

## Data processing

1. Load yearly Energy-Charts pages in a headless Chrome browser.
2. Extract chart observations and axis metadata using JavaScript.
3. Reconstruct timestamps from epoch values, axis categories or explicit series intervals. If these are unavailable, infer the resolution from the observation count only when a supported frequency can be identified unambiguously.
4. Validate timestamps, standardize column names, remove duplicate timestamps and report columns with more than 20% missing values.
5. Combine successfully processed years and export the resulting datasets to CSV.

## Caching and reproducibility

Raw chart data and associated metadata are stored in `cache/*_xy2.json`. Existing readable, non-empty caches are reused to avoid unnecessary requests. Delete the relevant cache file before rerunning the script to refresh a dataset, particularly for the current year.

Older `*_xy.json` caches are not reused by the yearly market scraper.

## Limitations

- Data availability and temporal resolution depend on the source. Failed years and series with unresolvable timestamps are skipped, so output filenames do not guarantee complete coverage.
- Inferred timestamps assume a regular sequence; check for gaps and verify resolution before quantitative analysis.
- Harmonized bidding-zone labels do not remove underlying changes in market geography.
- The scraper depends on Energy-Charts' website and Highcharts implementation, which may change.
- Helper functions for power generation and weather data are included but are not called by default.
- Regression analysis and battery dispatch or profitability modelling are downstream tasks, not implemented in this script.

## Data source and acknowledgement

- [Energy-Charts](https://www.energy-charts.info/) — electricity market data.
