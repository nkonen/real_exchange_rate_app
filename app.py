"""Real Exchange Rate Explorer.

A Streamlit app that uses IMF monthly CPI and exchange-rate data to compute
and plot the real exchange rate between two economies.

Definitions
-----------
S(t)   = units of country-2 currency per one unit of country-1 currency
P1, P2 = monthly CPI of country 1 and country 2
RER(t) = S(t) * P1(t) / P2(t)
RER*   = average RER(t) over the valid months in the selected period
Real Index(t) = 100 * RER(t) / RER*

Run with:  streamlit run app.py
"""

from __future__ import annotations

import io
import re

import pandas as pd
import plotly.graph_objects as go
import pycountry
import requests
import streamlit as st
from plotly.subplots import make_subplots

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
API_ROOT = "https://api.imf.org/external/sdmx"
AGENCY = "IMF.STA"
CPI_FLOW = "CPI"  # key: {code}.CPI._T.IX.M
ER_FLOW = "ER"  # key: {code}.XDC_USD.PA_RT.M
START_FLOOR = pd.Timestamp("1976-01-01")
TIMEOUT_SECONDS = 90

# IMF SDMX 3.0 data URL:  /3.0/data/dataflow/{agency}/{flow}/{version}/{key}
# - The version must be "~" (latest). "+" and "*" can return errors.
# - Monthly periods look like 2024-M01.
# - A wrong code returns HTTP 200 with no rows. It does not return an error.
# Each tuple is (URL template, Accept header). The app tries them in order.
_V3 = API_ROOT + "/3.0/data/dataflow/" + AGENCY + "/{flow}/~/{key}"
_V21 = API_ROOT + "/2.1/data/" + AGENCY + ",{flow}/{key}"
REQUEST_VARIANTS = [
    (_V3 + "?c[TIME_PERIOD]=ge:1976-M01", "text/csv"),
    (_V3, "text/csv"),
    (_V3 + "?c[TIME_PERIOD]=ge:1976-M01", "application/json"),
    (_V21 + "?startPeriod=1976-01", "application/vnd.sdmx.data+csv;version=1.0.0"),
]

# IMF member countries and other economies with IMF statistics (ISO3 codes).
ISO3_CODES = (
    "AFG ALB DZA AGO ATG ARG ARM AUS AUT AZE BHS BHR BGD BRB BLR BEL BLZ BEN "
    "BTN BOL BIH BWA BRA BRN BGR BFA BDI CPV KHM CMR CAN CAF TCD CHL CHN COL "
    "COM COD COG CRI CIV HRV CYP CZE DNK DJI DMA DOM ECU EGY SLV GNQ ERI EST "
    "SWZ ETH FJI FIN FRA GAB GMB GEO DEU GHA GRC GRD GTM GIN GNB GUY HTI HND "
    "HUN ISL IND IDN IRN IRQ IRL ISR ITA JAM JPN JOR KAZ KEN KIR KOR KWT KGZ "
    "LAO LVA LBN LSO LBR LBY LTU LUX MDG MWI MYS MDV MLI MLT MHL MRT MUS MEX "
    "FSM MDA MNG MNE MAR MOZ MMR NAM NRU NPL NLD NZL NIC NER NGA MKD NOR OMN "
    "PAK PLW PAN PNG PRY PER PHL POL PRT QAT ROU RUS RWA WSM SMR STP SAU SEN "
    "SRB SYC SLE SGP SVK SVN SLB SOM ZAF SSD ESP LKA KNA LCA VCT SDN SUR SWE "
    "CHE SYR TJK TZA THA TLS TGO TON TTO TUN TUR TKM TUV UGA UKR ARE GBR USA "
    "URY UZB VUT VEN VNM YEM ZMB ZWE HKG MAC XKX PSE ABW"
).split()

NAME_OVERRIDES = {
    "XKX": "Kosovo",
    "PSE": "West Bank and Gaza",
    "BOL": "Bolivia",
    "BRN": "Brunei Darussalam",
    "COD": "Congo, Democratic Republic of the",
    "COG": "Congo, Republic of",
    "CIV": "Côte d'Ivoire",
    "CZE": "Czech Republic",
    "FSM": "Micronesia",
    "HKG": "Hong Kong SAR",
    "IRN": "Iran",
    "KOR": "Korea, Republic of",
    "KGZ": "Kyrgyz Republic",
    "LAO": "Lao P.D.R.",
    "MAC": "Macao SAR",
    "MDA": "Moldova",
    "MKD": "North Macedonia",
    "RUS": "Russian Federation",
    "SYR": "Syria",
    "SVK": "Slovak Republic",
    "TZA": "Tanzania",
    "TUR": "Türkiye",
    "VEN": "Venezuela",
    "VNM": "Vietnam",
    "GBR": "United Kingdom",
    "USA": "United States",
}

# The IMF code for the euro area is not an ISO3 code.
# The app tries each code in order and uses the first one that returns data.
# The sidebar lets you enter your own code. You can find it with the
# "Find non-country codes" button.
EURO_AREA_LABEL = "Euro area"
EURO_AREA_CODES = ("EA",)

# The U.S. CPI for October 2025 is missing in the IMF data.
# For the U.S. only, the app fills it with the average of September and
# November 2025. It fills the month only if it is missing.
US_GAP_MONTH = pd.Timestamp("2025-10-01")
US_GAP_BEFORE = pd.Timestamp("2025-09-01")
US_GAP_AFTER = pd.Timestamp("2025-11-01")


class FetchError(Exception):
    """Raised when the IMF API does not return usable data."""


# --------------------------------------------------------------------------
# Country list
# --------------------------------------------------------------------------
def _country_name(iso3: str) -> str:
    if iso3 in NAME_OVERRIDES:
        return NAME_OVERRIDES[iso3]
    record = pycountry.countries.get(alpha_3=iso3)
    return record.name if record else iso3


def build_country_options(euro_codes: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Map a display label to the tuple of candidate IMF codes."""
    options: dict[str, tuple[str, ...]] = {
        _country_name(code) + f" ({code})": (code,) for code in ISO3_CODES
    }
    options[EURO_AREA_LABEL] = euro_codes
    return dict(sorted(options.items(), key=lambda item: item[0].lower()))


# --------------------------------------------------------------------------
# IMF data access
# --------------------------------------------------------------------------
def _period_to_timestamp(period: str) -> pd.Timestamp | None:
    """Convert '1976-M01', '1976-01' or '1976-01-01' to a month-start date."""
    match = re.match(r"^(\d{4})-?M?(\d{2})", str(period))
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return pd.Timestamp(year=year, month=month, day=1)


def _read_csv_frame(text: str) -> pd.DataFrame:
    frame = pd.read_csv(io.StringIO(text), low_memory=False)
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame


def _find_column(frame: pd.DataFrame, name: str) -> str | None:
    """Find a column by name. Ignore case and an optional ':label' suffix."""
    for column in frame.columns:
        if column.upper().split(":")[0].strip() == name:
            return column
    return None


def _parse_csv(text: str) -> pd.Series:
    frame = _read_csv_frame(text)
    time_col = _find_column(frame, "TIME_PERIOD")
    value_col = _find_column(frame, "OBS_VALUE")
    if time_col is None or value_col is None:
        raise FetchError(
            "CSV has no TIME_PERIOD or OBS_VALUE column. "
            f"Columns found: {list(frame.columns)[:8]}"
        )
    periods = frame[time_col].map(_period_to_timestamp)
    values = pd.to_numeric(frame[value_col], errors="coerce")
    series = pd.Series(values.to_numpy(), index=periods).dropna()
    series = series[series.index.notna()]
    return series[~series.index.duplicated(keep="first")]


def _parse_json(payload: dict) -> pd.Series:
    data = payload.get("data", payload)
    structures = data.get("structures") or [data.get("structure")]
    structure = structures[0]
    time_values = structure["dimensions"]["observation"][0]["values"]
    periods = [v.get("id") or v.get("value") or v.get("name") for v in time_values]
    series_map = data["dataSets"][0].get("series", {})
    if not series_map:
        raise FetchError("JSON response has no series.")
    first_series = next(iter(series_map.values()))
    records = {}
    for index, obs in first_series.get("observations", {}).items():
        stamp = _period_to_timestamp(periods[int(index)])
        value = obs[0] if obs else None
        if stamp is not None and value is not None:
            records[stamp] = float(value)
    return pd.Series(records, dtype=float).sort_index()


def _fetch_one(flow: str, key: str) -> pd.Series:
    errors: list[str] = []
    for template, accept in REQUEST_VARIANTS:
        url = template.format(flow=flow, key=key)
        try:
            response = requests.get(
                url, headers={"Accept": accept}, timeout=TIMEOUT_SECONDS
            )
            if response.status_code != 200:
                errors.append(f"HTTP {response.status_code}: {url}")
                continue
            content_type = response.headers.get("Content-Type", "").lower()
            if "json" in content_type or response.text.lstrip().startswith("{"):
                series = _parse_json(response.json())
            else:
                series = _parse_csv(response.text)
            if series.empty:
                errors.append(f"HTTP 200 but no data rows: {url}")
                continue
            return series.sort_index()
        except (requests.RequestException, ValueError, KeyError, IndexError,
                TypeError, FetchError, pd.errors.ParserError) as exc:
            errors.append(f"{type(exc).__name__}: {exc} | {url}")
    raise FetchError("\n".join(errors))


def _fetch_with_codes(flow: str, template: str, codes: tuple[str, ...]) -> pd.Series:
    failures: list[str] = []
    for code in codes:
        try:
            return _fetch_one(flow, template.format(code=code))
        except FetchError as exc:
            failures.append(f"[{code}] {exc}")
    raise FetchError(f"No data for {flow} with codes {codes}.\n" + "\n".join(failures))


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def load_country(codes: tuple[str, ...]) -> pd.DataFrame:
    """Return monthly CPI and XDC per USD for one economy (since 1976-01)."""
    cpi = _fetch_with_codes(CPI_FLOW, "{code}.CPI._T.IX.M", codes)
    if codes == ("USA",):
        # The U.S. dollar per U.S. dollar rate is always 1.
        xdc_usd = pd.Series(1.0, index=cpi.index)
    else:
        xdc_usd = _fetch_with_codes(ER_FLOW, "{code}.XDC_USD.PA_RT.M", codes)
    cpi_imputed = pd.Series(False, index=cpi.index)
    if codes == ("USA",) and US_GAP_MONTH not in cpi.index:
        if US_GAP_BEFORE in cpi.index and US_GAP_AFTER in cpi.index:
            cpi.loc[US_GAP_MONTH] = (cpi[US_GAP_BEFORE] + cpi[US_GAP_AFTER]) / 2
            cpi_imputed.loc[US_GAP_MONTH] = True
            cpi = cpi.sort_index()
            cpi_imputed = cpi_imputed.sort_index()
            if US_GAP_MONTH not in xdc_usd.index:
                xdc_usd.loc[US_GAP_MONTH] = 1.0
                xdc_usd = xdc_usd.sort_index()
    frame = pd.concat(
        {"cpi": cpi, "xdc_usd": xdc_usd, "cpi_imputed": cpi_imputed}, axis=1
    ).sort_index()
    frame["cpi_imputed"] = frame["cpi_imputed"].fillna(False).astype(bool)
    return frame[frame.index >= START_FLOOR]


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def discover_extra_codes(flow: str, tail: str) -> list[str]:
    """List codes in a dataflow that are not ISO3 countries (e.g. aggregates)."""
    url = f"{API_ROOT}/3.0/data/dataflow/{AGENCY}/{flow}/~/*.{tail}"
    url += "?c[TIME_PERIOD]=ge:2024-M01"
    response = requests.get(url, headers={"Accept": "text/csv"}, timeout=TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code}: {url}")
    frame = _read_csv_frame(response.text)
    column = _find_column(frame, "COUNTRY") or _find_column(frame, "REF_AREA")
    if column is None:
        raise FetchError(f"No COUNTRY column. Columns found: {list(frame.columns)[:8]}")
    codes = {str(v).split(":")[0].strip() for v in frame[column].dropna().unique()}
    return sorted(codes - set(ISO3_CODES))


# --------------------------------------------------------------------------
# Calculations
# --------------------------------------------------------------------------
def build_panel(df1: pd.DataFrame, df2: pd.DataFrame) -> pd.DataFrame:
    """Join the two economies and compute S(t) and RER(t) for all months."""
    start = min(df1.index.min(), df2.index.min())
    end = max(df1.index.max(), df2.index.max())
    index = pd.date_range(start, end, freq="MS")
    panel = pd.DataFrame(index=index)
    panel["xdc_usd_1"] = df1["xdc_usd"].reindex(index)
    panel["xdc_usd_2"] = df2["xdc_usd"].reindex(index)
    panel["cpi_1"] = df1["cpi"].reindex(index)
    panel["cpi_2"] = df2["cpi"].reindex(index)
    panel["cpi_1_imputed"] = df1["cpi_imputed"].reindex(index).fillna(False).astype(bool)
    panel["cpi_2_imputed"] = df2["cpi_imputed"].reindex(index).fillna(False).astype(bool)
    # Units of country-2 currency per one unit of country-1 currency.
    panel["S"] = panel["xdc_usd_2"] / panel["xdc_usd_1"]
    panel["RER"] = panel["S"] * panel["cpi_1"] / panel["cpi_2"]
    panel.loc[~panel["RER"].between(0, float("inf"), inclusive="neither"), "RER"] = (
        float("nan")
    )
    return panel


def apply_period(panel: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp):
    """Cut the selected period, then compute RER* and the real index."""
    subset = panel.loc[start:end].copy()
    rer_star = subset["RER"].mean(skipna=True)
    subset["RER_star"] = rer_star
    subset["real_index"] = 100 * subset["RER"] / rer_star
    return subset, rer_star


# --------------------------------------------------------------------------
# Chart
# --------------------------------------------------------------------------
def make_chart(data: pd.DataFrame, mode: str, label1: str, label2: str) -> go.Figure:
    show_real = mode in ("Real", "Both")
    show_nominal = mode in ("Nominal", "Both")
    both = show_real and show_nominal
    fig = make_subplots(specs=[[{"secondary_y": both}]])

    if show_real:
        fig.add_trace(
            go.Scatter(
                x=data.index,
                y=data["real_index"],
                name="Real index (RER* = 100)",
                mode="lines",
                connectgaps=False,
            ),
            secondary_y=False,
        )
        fig.add_hline(y=100, line_dash="dot", line_color="gray", opacity=0.6)
    if show_nominal:
        fig.add_trace(
            go.Scatter(
                x=data.index,
                y=data["S"],
                name="Nominal S(t)",
                mode="lines",
                connectgaps=False,
            ),
            secondary_y=both,
        )

    nominal_title = f"Nominal S(t): {label2} currency per 1 {label1} currency"
    real_title = "Real index (average over period = 100)"
    if both:
        fig.update_yaxes(title_text=real_title, secondary_y=False)
        fig.update_yaxes(title_text=nominal_title, secondary_y=True)
    elif show_real:
        fig.update_yaxes(title_text=real_title)
    else:
        fig.update_yaxes(title_text=nominal_title)

    fig.update_layout(
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08},
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        height=520,
    )
    fig.update_xaxes(title_text="Month")
    return fig


# --------------------------------------------------------------------------
# User interface
# --------------------------------------------------------------------------
def sidebar_euro_codes() -> tuple[str, ...]:
    """Sidebar controls for the Euro area code. Return the codes to try."""
    st.sidebar.header("Euro area code")
    st.sidebar.write(
        "The IMF code for the Euro area is not an ISO3 code. "
        "Enter your own code to try it first."
    )
    custom = st.sidebar.text_input("IMF code (optional)", value="").strip().upper()
    if st.sidebar.button("Find non-country codes"):
        try:
            with st.spinner("Asking the IMF API..."):
                extra = discover_extra_codes(CPI_FLOW, "CPI._T.IX.M")
            if extra:
                st.sidebar.write("Codes in the IMF CPI data that are not ISO3 countries:")
                st.sidebar.code(", ".join(extra))
            else:
                st.sidebar.warning("No extra codes found.")
        except (FetchError, requests.RequestException) as exc:
            st.sidebar.error("The code search failed.")
            st.sidebar.code(str(exc))
    codes = tuple(dict.fromkeys(([custom] if custom else []) + list(EURO_AREA_CODES)))
    return codes


def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Explorer", layout="wide")
    st.title("Real Exchange Rate Explorer")
    st.caption(
        "Data: IMF monthly CPI and exchange rates. "
        "RER(t) = S(t) × P1(t) / P2(t). "
        "S(t) is units of country 2 currency per 1 unit of country 1 currency."
    )

    euro_codes = sidebar_euro_codes()
    options = build_country_options(euro_codes)
    labels = list(options)
    default1 = labels.index("United States (USA)")
    default2 = labels.index(EURO_AREA_LABEL)

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        choice1 = st.selectbox("Country 1", labels, index=default1)
    with col2:
        choice2 = st.selectbox("Country 2", labels, index=default2)
    with col3:
        mode = st.radio("Chart", ["Real", "Nominal", "Both"], index=2, horizontal=True)

    if choice1 == choice2:
        st.warning("Select two different countries.")
        st.stop()

    try:
        with st.spinner("Fetching IMF data..."):
            df1 = load_country(options[choice1])
            df2 = load_country(options[choice2])
    except FetchError as exc:
        st.error(
            "The IMF API did not return the data. "
            "Select other countries or try again later."
        )
        with st.expander("Technical details", expanded=True):
            st.code(str(exc))
        st.stop()

    panel = build_panel(df1, df2)
    valid_months = panel.index[panel["RER"].notna()]
    if len(valid_months) < 2:
        st.error("These two countries have no overlapping CPI and exchange-rate data.")
        st.stop()

    month_options = [m.strftime("%Y-%m") for m in valid_months.sort_values()]
    first_valid, last_valid = month_options[0], month_options[-1]
    start_label, end_label = st.select_slider(
        "Period (start and end month)",
        options=month_options,
        value=(first_valid, last_valid),
    )
    start = pd.Timestamp(start_label + "-01")
    end = pd.Timestamp(end_label + "-01")

    data, rer_star = apply_period(panel, start, end)
    n_valid = int(data["RER"].notna().sum())

    short1 = choice1.split("(")[-1].rstrip(")") if "(" in choice1 else choice1
    short2 = choice2.split("(")[-1].rstrip(")") if "(" in choice2 else choice2

    metric1, metric2, metric3 = st.columns(3)
    metric1.metric("RER* (average RER)", f"{rer_star:,.4f}")
    metric2.metric("Valid months", f"{n_valid}")
    metric3.metric("Selected period", f"{start_label} to {end_label}")

    st.plotly_chart(make_chart(data, mode, short1, short2), use_container_width=True)

    st.caption(
        "A high real index means country 1 is relatively expensive. "
        "A low real index means country 1 is relatively cheap. "
        "Months with missing data appear as gaps."
    )
    if bool(data["cpi_1_imputed"].any() or data["cpi_2_imputed"].any()):
        st.info(
            "U.S. CPI for October 2025 is missing in the IMF data. "
            "The app uses the average of September and November 2025. "
            "The CSV marks this month in the *_imputed column."
        )

    export = data.rename(
        columns={
            "xdc_usd_1": f"{short1}_currency_per_USD",
            "xdc_usd_2": f"{short2}_currency_per_USD",
            "cpi_1": f"CPI_{short1}",
            "cpi_2": f"CPI_{short2}",
            "cpi_1_imputed": f"CPI_{short1}_imputed",
            "cpi_2_imputed": f"CPI_{short2}_imputed",
            "S": f"S_{short2}_per_{short1}",
            "RER": "RER",
            "RER_star": "RER_star",
            "real_index": "Real_index",
        }
    )
    export.index = export.index.strftime("%Y-%m-%d")
    export.index.name = "date"
    st.download_button(
        "Download CSV (selected period)",
        data=export.reset_index().to_csv(index=False).encode("utf-8"),
        file_name=f"rer_{short1}_{short2}_{start_label}_{end_label}.csv",
        mime="text/csv",
    )
    with st.expander("Show data table"):
        st.dataframe(export, use_container_width=True)


if __name__ == "__main__":
    main()
