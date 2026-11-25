"""
Macro Outlook Dashboard
Live U.S. monetary policy, rates, inflation, labor, consumer, leading indicators,
markets/credit and fiscal data from FRED, plus a recession risk scorecard.
Data refreshes automatically (cached for 1 hour), so new releases appear on their own.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from fredapi import Fred

# ----------------------------------------------------------------------------
# Page setup & design tokens
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Macro Outlook Dashboard", page_icon="📈", layout="wide")

NAVY = "#0B3C5D"      # primary series
GOLD = "#C8963E"      # secondary series
GRAY = "#8A94A6"      # context / recession shading
GRID = "#E6E9EF"
TEXT = "#1A202C"
STATUS = {  # (dot/text color, soft background)
    "green":  ("#1E7B4F", "#E8F5EE"),
    "yellow": ("#B7791F", "#FDF6E3"),
    "red":    ("#B54747", "#FBECEC"),
    "gray":   ("#64748B", "#F1F3F6"),
}
REFRESH_SECONDS = 3600  # re-pull FRED at most once an hour

st.markdown(
    """
<style>
.block-container {padding-top: 2rem; max-width: 1400px;}
h1 {font-weight: 700; letter-spacing: -0.02em;}
.card-grid {display: grid; gap: 14px; margin: 6px 0 18px 0;}
.kpi-grid {grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));}
.score-grid {grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));}
.card {background: #FFFFFF; border: 1px solid #E6E9EF; border-radius: 12px;
       padding: 14px 18px; box-shadow: 0 1px 2px rgba(16,24,40,.04);}
.card .label {font-size: 0.76rem; color: #64748B; text-transform: uppercase; letter-spacing: .05em;}
.card .value {font-size: 1.7rem; font-weight: 600; color: #0B3C5D; margin: 4px 0 6px 0;}
.pill {display: inline-block; font-size: 0.8rem; padding: 2px 10px; border-radius: 999px;}
.score .value {font-size: 1.3rem;}
.score .rule {font-size: 0.75rem; color: #64748B; margin-top: 6px;}
.dot {display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px;}
.banner {border-radius: 12px; padding: 16px 20px; margin-bottom: 10px; display: flex;
         align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;}
.banner .big {font-size: 1.35rem; font-weight: 700;}
.banner .small {font-size: 0.85rem;}
</style>
""",
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# Data catalogue: add an indicator by adding one line here
#   id: FRED series code | transform: None, "yoy" (monthly % y/y), "diff" | scale: multiplier
# ----------------------------------------------------------------------------
SERIES = {
    # Monetary policy
    "fed_funds":    dict(id="DFF",      transform=None,  scale=1),
    "fed_assets":   dict(id="WALCL",    transform=None,  scale=1e-6),   # $mm -> $tn
    "m2_yoy":       dict(id="M2SL",     transform="yoy", scale=1),
    # Rates
    "ust_2y":       dict(id="DGS2",     transform=None,  scale=1),
    "ust_10y":      dict(id="DGS10",    transform=None,  scale=1),
    "curve":        dict(id="T10Y2Y",   transform=None,  scale=1),
    "curve_3m":     dict(id="T10Y3M",   transform=None,  scale=1),
    "real_10y":     dict(id="DFII10",   transform=None,  scale=1),
    "mortgage":     dict(id="MORTGAGE30US", transform=None, scale=1),
    # Inflation
    "cpi_yoy":      dict(id="CPIAUCSL", transform="yoy", scale=1),
    "core_cpi_yoy": dict(id="CPILFESL", transform="yoy", scale=1),
    "core_pce_yoy": dict(id="PCEPILFE", transform="yoy", scale=1),
    "breakeven_5y": dict(id="T5YIE",    transform=None,  scale=1),
    # Labor & growth
    "unemployment": dict(id="UNRATE",   transform=None,  scale=1),
    "payrolls_chg": dict(id="PAYEMS",   transform="diff", scale=1),     # thousands of jobs
    "real_gdp":     dict(id="A191RL1Q225SBEA", transform=None, scale=1),
    "wages_yoy":    dict(id="CES0500000003", transform="yoy", scale=1),
    # Consumer
    "retail_yoy":   dict(id="RSAFS",    transform="yoy", scale=1),
    "saving_rate":  dict(id="PSAVERT",  transform=None,  scale=1),
    # Leading indicators
    "claims_4wk":   dict(id="IC4WSA",   transform=None,  scale=1e-3),   # -> thousands
    "sentiment":    dict(id="UMCSENT",  transform=None,  scale=1),
    "housing":      dict(id="HOUST",    transform=None,  scale=1e-3),   # thousands -> millions
    "indpro_yoy":   dict(id="INDPRO",   transform="yoy", scale=1),
    "sahm":         dict(id="SAHMREALTIME", transform=None, scale=1),
    # Markets & credit
    "sp500":        dict(id="SP500",    transform=None,  scale=1),
    "vix":          dict(id="VIXCLS",   transform=None,  scale=1),
    "hy_spread":    dict(id="BAMLH0A0HYM2", transform=None, scale=1),
    "dollar":       dict(id="DTWEXBGS", transform=None,  scale=1),
    # Fiscal
    "deficit":      dict(id="FYFSD",    transform=None,  scale=1e-6),   # $mm -> $tn
    "debt_gdp":     dict(id="GFDEGDQ188S", transform=None, scale=1),
    "interest":     dict(id="A091RC1Q027SBEA", transform=None, scale=1e-3),  # $bn -> $tn
    # Context
    "recession":    dict(id="USREC",    transform=None,  scale=1),
}

# Series calculated from other series (key -> source label for chart captions)
DERIVED = {"real_wage": "CES0500000003 minus CPIAUCSL, y/y"}


# ----------------------------------------------------------------------------
# Data loading (cached = auto refresh)
# ----------------------------------------------------------------------------
def _transform(s: pd.Series, how, scale) -> pd.Series:
    s = s.astype(float).dropna()
    if how == "yoy":
        s = s.pct_change(12, fill_method=None) * 100
    elif how == "diff":
        s = s.diff()
    return (s * scale).dropna()


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner="Pulling latest data from FRED…")
def load_all():
    fred = Fred(api_key=st.secrets["FRED_API_KEY"])

    def fetch(item):
        key, spec = item
        try:
            raw = fred.get_series(spec["id"], observation_start="1990-01-01")
            return key, _transform(raw, spec["transform"], spec["scale"]), None
        except Exception as exc:  # one bad series shouldn't break the page
            return key, None, f"{spec['id']}: {exc}"

    data, errors = {}, []
    with ThreadPoolExecutor(max_workers=6) as pool:  # fetch in parallel = faster loads
        for key, series, err in pool.map(fetch, SERIES.items()):
            if err:
                errors.append(err)
            elif series is not None:
                data[key] = series

    if "wages_yoy" in data and "cpi_yoy" in data:
        data["real_wage"] = (data["wages_yoy"] - data["cpi_yoy"]).dropna()

    fetched_at = datetime.now(ZoneInfo("America/Los_Angeles"))
    return data, errors, fetched_at


def recession_periods(rec: pd.Series):
    if rec is None or rec.empty:
        return []
    r = rec.astype(int)
    starts = r[(r == 1) & (r.shift(1, fill_value=0) == 0)].index
    ends = r[(r == 1) & (r.shift(-1, fill_value=0) == 0)].index
    return [(s, e + pd.offsets.MonthEnd(0)) for s, e in zip(starts, ends)]


# ----------------------------------------------------------------------------
# KPI tiles
# ----------------------------------------------------------------------------
def kpi_card(data, key, label, unit="%", decimals=2, up_is_bad=None):
    """up_is_bad: True (rising = red), False (rising = green), None (neutral gray)."""
    s = data.get(key)
    if s is None or s.empty:
        return f"<div class='card'><div class='label'>{label}</div><div class='value'>—</div></div>"
    last = s.iloc[-1]
    prior = s[s.index <= s.index[-1] - pd.Timedelta(days=28)]
    pill = ""
    if not prior.empty:
        chg = round(last - prior.iloc[-1], decimals)
        if chg == 0:
            arrow, tone = "→", "gray"
            text = "Unchanged vs ~1 mo ago"
        else:
            arrow = "▲" if chg > 0 else "▼"
            if up_is_bad is None:
                tone = "gray"
            else:
                tone = "red" if (chg > 0) == up_is_bad else "green"
            text = f"{abs(chg):.{decimals}f} pts vs ~1 mo ago"
        fg, bg = STATUS[tone]
        pill = f"<span class='pill' style='color:{fg};background:{bg}'>{arrow} {text}</span>"
    return (f"<div class='card'><div class='label'>{label}</div>"
            f"<div class='value'>{last:.{decimals}f}{unit}</div>{pill}</div>")


# ----------------------------------------------------------------------------
# Recession risk scorecard (rules of thumb — tweak thresholds as you learn)
# ----------------------------------------------------------------------------
def _last(data, key):
    s = data.get(key)
    return None if s is None or s.empty else s


def _band(v, red, yellow, higher_is_worse=True):
    if higher_is_worse:
        return "red" if v >= red else "yellow" if v >= yellow else "green"
    return "red" if v <= red else "yellow" if v <= yellow else "green"


def score_items(data):
    items = []

    s = _last(data, "curve_3m")
    if s is not None:
        v = s.iloc[-1]
        items.append(("Yield curve (10Y–3M)", f"{v:+.2f} pts", _band(v, 0, 0.5, False),
                      "Red if inverted (below 0); yellow under +0.50"))

    s = _last(data, "sahm")
    if s is not None:
        v = s.iloc[-1]
        items.append(("Sahm rule", f"{v:.2f}", _band(v, 0.5, 0.3),
                      "Red at 0.50+ (historical recession trigger)"))

    s = _last(data, "claims_4wk")
    if s is not None:
        window = s[s.index >= s.index[-1] - pd.Timedelta(days=365)]
        v = (s.iloc[-1] / window.min() - 1) * 100
        items.append(("Jobless claims (4-wk avg)", f"{s.iloc[-1]:.0f}k · +{v:.0f}% vs 1-yr low",
                      _band(v, 20, 10), "Red if 20%+ above its 1-year low"))

    s = _last(data, "hy_spread")
    if s is not None:
        v = s.iloc[-1]
        items.append(("High-yield credit spread", f"{v:.2f}%", _band(v, 6, 4.5),
                      "Red above 6%; yellow above 4.5%"))

    s = _last(data, "sentiment")
    if s is not None:
        prior = s[s.index <= s.index[-1] - pd.DateOffset(months=12)]
        if not prior.empty:
            v = s.iloc[-1] - prior.iloc[-1]
            items.append(("Consumer sentiment", f"{s.iloc[-1]:.1f} ({v:+.1f} vs 1 yr ago)",
                          _band(v, -10, -5, False), "Red if down 10+ points in a year"))

    s = _last(data, "real_wage")
    if s is not None:
        v = s.iloc[-1]
        items.append(("Real wage growth", f"{v:+.1f}% y/y", _band(v, 0, 1, False),
                      "Red if wages trail inflation (below 0)"))

    s = _last(data, "indpro_yoy")
    if s is not None:
        v = s.iloc[-1]
        items.append(("Industrial production", f"{v:+.1f}% y/y", _band(v, -1, 1, False),
                      "Red if shrinking more than 1% y/y"))

    s = _last(data, "vix")
    if s is not None:
        v = s.iloc[-1]
        items.append(("Market stress (VIX)", f"{v:.1f}", _band(v, 30, 20),
                      "Red above 30; yellow above 20"))
    return items


def render_scorecard(data):
    items = score_items(data)
    if not items:
        return
    reds = sum(1 for i in items if i[2] == "red")
    yellows = sum(1 for i in items if i[2] == "yellow")
    points, max_points = reds * 2 + yellows, len(items) * 2
    share = points / max_points
    level, tone = ("Low", "green") if share <= 0.25 else ("Moderate", "yellow") if share <= 0.5 else ("High", "red")
    fg, bg = STATUS[tone]

    html = [
        f"<div class='banner' style='background:{bg};border:1px solid {fg}33'>",
        f"<div><div class='small' style='color:{fg}'>RECESSION RISK SCORECARD</div>",
        f"<div class='big' style='color:{fg}'>{level} risk</div></div>",
        f"<div class='small' style='color:{fg}'>{reds} red · {yellows} yellow · "
        f"{len(items) - reds - yellows} green of {len(items)} signals</div></div>",
        "<div class='card-grid score-grid'>",
    ]
    for name, value, status, rule in items:
        dot = STATUS[status][0]
        html.append(
            f"<div class='card score'><div class='label'><span class='dot' style='background:{dot}'></span>{name}</div>"
            f"<div class='value'>{value}</div><div class='rule'>{rule}</div></div>"
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)
    st.caption("Thresholds are rules of thumb, not forecasts. No single signal calls a recession; look at how many flash together.")


# ----------------------------------------------------------------------------
# Chart helper
# ----------------------------------------------------------------------------
def make_chart(data, lines, title, units, start, recessions,
               kind="line", ref_line=None, ref_label=None):
    fig = go.Figure()
    colors = [NAVY, GOLD, GRAY]
    latest_dates = []
    for i, (key, label) in enumerate(lines):
        s = data.get(key)
        if s is None or s.empty:
            continue
        s = s[s.index >= start]
        if s.empty:
            continue
        latest_dates.append(s.index[-1])
        color = colors[i % len(colors)]
        if kind == "bar":
            bar_colors = [NAVY if v >= 0 else "#B54747" for v in s.values]
            fig.add_bar(x=s.index, y=s.values, name=label, marker_color=bar_colors,
                        hovertemplate="%{y:,.2f}<extra>" + label + "</extra>")
        else:
            fig.add_scatter(x=s.index, y=s.values, name=label, mode="lines",
                            line=dict(color=color, width=2.2),
                            hovertemplate="%{y:,.2f}<extra>" + label + "</extra>")

    for x0, x1 in recessions:
        if x1 >= start:
            fig.add_vrect(x0=max(x0, start), x1=x1, fillcolor=GRAY, opacity=0.15,
                          line_width=0, layer="below")

    if ref_line is not None:
        fig.add_hline(y=ref_line, line=dict(color=GRAY, width=1, dash="dot"),
                      annotation_text=ref_label or "", annotation_position="top left",
                      annotation_font=dict(size=10, color=GRAY))

    fig.update_layout(
        title=dict(text=f"<b>{title}</b>", x=0, xanchor="left", font=dict(size=15, color=TEXT)),
        height=330, margin=dict(l=8, r=8, t=56, b=8),
        paper_bgcolor="white", plot_bgcolor="white",
        font=dict(family="Inter, -apple-system, Segoe UI, sans-serif", color="#4A5568", size=12),
        hovermode="x unified", showlegend=len(lines) > 1,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1),
        xaxis=dict(showgrid=False, linecolor=GRID),
        yaxis=dict(gridcolor=GRID, zeroline=False, title=dict(text=units, font=dict(size=11))),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    ids = ", ".join(SERIES[k]["id"] if k in SERIES else DERIVED.get(k, k) for k, _ in lines)
    as_of = max(latest_dates).strftime("%b %d, %Y") if latest_dates else "n/a"
    st.caption(f"Source: FRED ({ids}) · Latest observation: {as_of}")


def two_up(data, start, recs, left, right):
    a, b = st.columns(2)
    with a:
        make_chart(data, *left[:3], start, recs, **(left[3] if len(left) > 3 else {}))
    with b:
        make_chart(data, *right[:3], start, recs, **(right[3] if len(right) > 3 else {}))


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    horizon = st.select_slider("History shown", options=["1Y", "3Y", "5Y", "10Y", "20Y", "Max"], value="10Y")
    show_recessions = st.toggle("Shade recessions", value=True)
    if st.button("Refresh data now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.markdown("---")
    st.caption("Built by Sam Sambhi · Data: Federal Reserve Bank of St. Louis (FRED)")

data, errors, fetched_at = load_all()

years = {"1Y": 1, "3Y": 3, "5Y": 5, "10Y": 10, "20Y": 20, "Max": 40}[horizon]
start = pd.Timestamp.today().normalize() - pd.DateOffset(years=years)
recs = recession_periods(data.get("recession")) if show_recessions else []

# ----------------------------------------------------------------------------
# Header, KPI strip, scorecard, outlook
# ----------------------------------------------------------------------------
st.title("Macro Outlook Dashboard")
st.markdown(
    f"<span style='color:#64748B'>U.S. monetary policy, rates, inflation, labor, consumer, markets and fiscal conditions · "
    f"Data refreshed {fetched_at:%b %d, %Y %I:%M %p} PT</span>",
    unsafe_allow_html=True,
)
if errors:
    st.warning("Some series could not be loaded: " + "; ".join(errors))

st.markdown(
    "<div class='card-grid kpi-grid'>"
    + kpi_card(data, "fed_funds", "Fed Funds (eff.)")
    + kpi_card(data, "ust_10y", "10Y Treasury")
    + kpi_card(data, "curve", "10Y–2Y Spread", unit=" pts")
    + kpi_card(data, "cpi_yoy", "CPI Inflation y/y", decimals=1, up_is_bad=True)
    + kpi_card(data, "unemployment", "Unemployment", decimals=1, up_is_bad=True)
    + "</div>",
    unsafe_allow_html=True,
)

render_scorecard(data)

outlook_path = Path(__file__).parent / "outlook.md"
with st.container(border=True):
    st.subheader("My Market Outlook")
    st.markdown(outlook_path.read_text() if outlook_path.exists() else "_Add your outlook in outlook.md_")

# ----------------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------------
tabs = st.tabs(["Monetary Policy", "Rates", "Inflation", "Labor & Growth", "Consumer",
                "Leading Indicators", "Markets & Credit", "Fiscal"])

with tabs[0]:
    two_up(data, start, recs,
           ([("fed_funds", "Effective fed funds")], "Fed Funds Rate", "%"),
           ([("fed_assets", "Total assets")], "Fed Balance Sheet", "$ trillions"))
    make_chart(data, [("m2_yoy", "M2 growth")], "Money Supply (M2) Growth", "% y/y", start, recs, ref_line=0)

with tabs[1]:
    two_up(data, start, recs,
           ([("ust_10y", "10-year"), ("ust_2y", "2-year")], "Treasury Yields", "%"),
           ([("curve", "10Y minus 2Y"), ("curve_3m", "10Y minus 3M")], "Yield Curve Spreads",
            "percentage points", dict(ref_line=0, ref_label="Inverted below 0")))
    two_up(data, start, recs,
           ([("real_10y", "10-yr real yield (TIPS)")], "Real 10-Year Yield (after inflation)", "%",
            dict(ref_line=0)),
           ([("mortgage", "30-yr fixed")], "30-Year Mortgage Rate", "%"))

with tabs[2]:
    two_up(data, start, recs,
           ([("cpi_yoy", "Headline CPI"), ("core_cpi_yoy", "Core CPI")], "CPI Inflation", "% y/y",
            dict(ref_line=2, ref_label="2% target")),
           ([("core_pce_yoy", "Core PCE")], "Core PCE Inflation (Fed's preferred)", "% y/y",
            dict(ref_line=2, ref_label="2% target")))
    make_chart(data, [("breakeven_5y", "5-yr breakeven")], "Market Inflation Expectations (5Y)",
               "%", start, recs, ref_line=2, ref_label="2% target")

with tabs[3]:
    two_up(data, start, recs,
           ([("unemployment", "Unemployment rate")], "Unemployment Rate", "%"),
           ([("payrolls_chg", "Jobs added")], "Monthly Payroll Change", "thousands of jobs",
            dict(kind="bar")))
    make_chart(data, [("real_gdp", "Real GDP growth")], "Real GDP Growth", "% annualized q/q",
               start, recs, kind="bar")

with tabs[4]:
    two_up(data, start, recs,
           ([("wages_yoy", "Avg hourly earnings"), ("cpi_yoy", "CPI inflation")],
            "Wage Growth vs Inflation", "% y/y"),
           ([("real_wage", "Real wage growth")], "Real Wage Growth (wages minus inflation)", "% y/y",
            dict(kind="bar", ref_line=0)))
    two_up(data, start, recs,
           ([("retail_yoy", "Retail sales")], "Retail Sales Growth", "% y/y", dict(ref_line=0)),
           ([("saving_rate", "Saving rate")], "Personal Saving Rate", "% of disposable income"))

with tabs[5]:
    two_up(data, start, recs,
           ([("claims_4wk", "Initial claims, 4-wk avg")], "Weekly Jobless Claims", "thousands"),
           ([("sentiment", "UMich sentiment")], "Consumer Sentiment", "index"))
    two_up(data, start, recs,
           ([("housing", "Housing starts")], "Housing Starts", "millions, annualized"),
           ([("indpro_yoy", "Industrial production")], "Industrial Production Growth", "% y/y",
            dict(ref_line=0)))
    make_chart(data, [("sahm", "Sahm rule")], "Sahm Rule Recession Indicator", "percentage points",
               start, recs, ref_line=0.5, ref_label="0.50 recession trigger")

with tabs[6]:
    two_up(data, start, recs,
           ([("sp500", "S&P 500")], "S&P 500", "index level"),
           ([("vix", "VIX")], "Market Volatility (VIX)", "index", dict(ref_line=20, ref_label="20 = elevated")))
    two_up(data, start, recs,
           ([("hy_spread", "HY spread")], "High-Yield Credit Spread", "% over Treasuries"),
           ([("dollar", "Broad dollar index")], "U.S. Dollar (trade-weighted)", "index"))
    st.caption("Note: FRED only provides about 10 years of S&P 500 history and about 3 years of the high-yield spread.")

with tabs[7]:
    two_up(data, start, recs,
           ([("deficit", "Surplus / deficit")], "Federal Budget Balance (fiscal year)", "$ trillions",
            dict(kind="bar", ref_line=0)),
           ([("debt_gdp", "Debt to GDP")], "Federal Debt to GDP", "% of GDP"))
    make_chart(data, [("interest", "Interest payments")], "Federal Interest Payments",
               "$ trillions, annualized", start, recs)

st.markdown("---")
st.caption("For educational purposes only; not investment advice. Gray bands mark NBER recessions.")
