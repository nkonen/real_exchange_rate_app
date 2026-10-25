# Real Exchange Rate Explorer

A Streamlit app. It shows the real exchange rate (RER) between two economies. It uses monthly CPI and exchange-rate data from the IMF.

## Features

- Select Country 1 and Country 2 from IMF economies and the Euro area.
- Get monthly data from January 1976, or from the first available month if it is later.
- Select a start month and an end month. The app recalculates RER* each time you change them.
- Plot the real index, the nominal rate, or both. "Both" uses two y-axes.
- Download a CSV file with all months and all calculations in the selected period.

## Formulas

| Symbol | Meaning |
|---|---|
| `S(t)` | Units of country 2 currency per 1 unit of country 1 currency |
| `P1(t)`, `P2(t)` | Monthly CPI of country 1 and country 2 |
| `RER(t)` | `S(t) × P1(t) / P2(t)` |
| `RER*` | Average of `RER(t)` over the valid months in the selected period |
| `Real Index(t)` | `100 × RER(t) / RER*` |

The IMF gives the exchange rate as local currency per USD (`XDC_USD`). The app calculates `S(t) = XDC_USD(country 2) / XDC_USD(country 1)`. For the United States, `XDC_USD` is set to 1.

The nominal rate is plotted in actual currency units. It is not normalized.

A month is valid when all four inputs (two CPI values, two exchange rates) exist and `RER(t)` is positive.

## Install and run

Python 3.12 is required.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Data source

- API root: `https://api.imf.org/external/sdmx/3.0`
- URL pattern: `/data/dataflow/IMF.STA/{flow}/~/{key}?c[TIME_PERIOD]=ge:1976-M01`
- The version must be `~` (latest).
- CPI key: `{CODE}.CPI._T.IX.M` (dataflow `IMF.STA,CPI`)
- Exchange-rate key: `{CODE}.XDC_USD.PA_RT.M` (dataflow `IMF.STA,ER`)
- Request header: `Accept: text/csv`
- Monthly periods look like `2024-M01`.

A wrong code returns HTTP 200 with no rows. The app treats this as "no data" and shows the URL.

The app caches each download for 24 hours.

## Euro area code

The IMF code for the Euro area is not an ISO3 code. The app tries `G163`, `U2`, `EA`, `EA20`, `EA19`, `EMU` and `EUR` in order.

If none works:
1. Open the sidebar and click **Find non-country codes**. The app lists the non-ISO3 codes in the IMF CPI data.
2. Enter the Euro area code in **IMF code (optional)**. The app tries it first.
3. Optional: edit `EURO_AREA_CODES` in `app.py`.

## Notes

- The app tries four request formats. If the IMF changes its API, edit `REQUEST_VARIANTS` in `app.py`.
- Some countries do not have CPI or exchange-rate data for all months. The chart shows gaps for those months.
- Different CPI base years do not affect the real index. RER* removes the base-year level.
- The default selection is United States and Germany. Select Euro area after you confirm the code.

## Files

- `app.py` - the application
- `requirements.txt` - Python packages
- `README.md` - this file
