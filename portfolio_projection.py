"""
Portfolio Projection — deterministic + Monte Carlo
Standalone Streamlit app.

Cashflow logic mirrors the Excel model exactly:
  - Each month you fund `contribution`, split into VOO and SGOV by the splits.
  - VOO market value compounds monthly; SGOV price is flat (pure income).
  - VOO dividends paid quarterly (on cost); SGOV distributions monthly (on cost).
  - Income is held as CASH (no DRIP), consistent with the workbook.
  - Total Value if Sold = market value + accumulated cash & income.
  - Net Gain = Total Value − cumulative contributions.

Only VOO price return is stochastic. In Monte Carlo, a fresh annual return is
drawn for each year (Normal(mean, vol)) and compounded monthly within the year —
this is what surfaces sequence-of-returns risk.

NOTE on units: every percent slider works in PERCENTAGE POINTS (e.g. 15.5 = 15.5%)
and is divided by 100 in the model. This avoids the fraction/percent display bug.
"""

import numpy as np
import pandas as pd
import altair as alt
import streamlit as st

# ----------------------------------------------------------------------------
# Page config + palette
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Portfolio Projection", layout="wide",
                   initial_sidebar_state="expanded")

NAVY  = "#1F4E78"   # median / primary
BLUE  = "#4C78A8"   # bands / bars
GREY  = "#8C8C8C"   # contributions reference
GREEN = "#2E9E5B"   # upside
RED   = "#C0504D"   # downside
USD   = "$,.0f"

alt.data_transformers.disable_max_rows()

# ----------------------------------------------------------------------------
# Sidebar inputs  (all percent inputs are in PERCENTAGE POINTS)
# ----------------------------------------------------------------------------
st.sidebar.title("Assumptions")

st.sidebar.subheader("Contributions")
contribution = st.sidebar.number_input(
    "Monthly funding ($)", min_value=0, value=3000, step=100,
    help="Cash added to the account each month.")
voo_split_pct = st.sidebar.slider(
    "VOO allocation (%)", 0, 100, 90, 1,
    help="Percent of each month's funding that buys VOO. The rest buys SGOV.")
voo_split = voo_split_pct / 100.0
sgov_split = 1.0 - voo_split
st.sidebar.caption(f"→ SGOV allocation: **{sgov_split:.0%}**")
horizon = st.sidebar.slider("Horizon (years)", 1, 100, 10, 1)

st.sidebar.subheader("VOO — equity")
voo_mean = st.sidebar.slider(
    "Expected price return (% p.a.)", -5.0, 15.0, 7.0, 0.5,
    help="Price-only return, in percent. Dividends added separately below.") / 100.0
voo_vol = st.sidebar.slider(
    "Volatility — σ (% p.a.)", 0.0, 40.0, 15.5, 0.5,
    help="Std dev of annual returns, in percent. Long-run S&P ≈ 15–16%. "
         "Set 0 for a smooth, deterministic-style path.") / 100.0
voo_yield = st.sidebar.slider(
    "Dividend yield (%)", 0.0, 4.0, 1.25, 0.05,
    help="Paid quarterly on cost basis. Held as cash (no reinvestment).") / 100.0

st.sidebar.subheader("SGOV — cash equivalent")
sgov_yield = st.sidebar.slider(
    "Distribution yield (%)", 0.0, 6.0, 4.30, 0.05,
    help="Paid monthly on cost basis. Price assumed flat.") / 100.0

st.sidebar.subheader("Monte Carlo")
n_sims = st.sidebar.select_slider(
    "Simulations", options=[1000, 2000, 5000, 10000, 20000], value=10000,
    help="More paths = smoother percentiles, especially in the tails.")
seed = st.sidebar.number_input(
    "Random seed", value=42, step=1,
    help="The starting point for the random number generator. Using the SAME "
         "seed reproduces the exact same set of simulated paths every run, so "
         "results don't jump around while you tweak other inputs. Change it to "
         "draw a fresh random sample.")

# ----------------------------------------------------------------------------
# Deterministic backbone (shared across all sims)
# ----------------------------------------------------------------------------
M = horizon * 12
months = np.arange(1, M + 1)
dates = pd.date_range("2026-08-01", periods=M, freq="MS")

buy_voo = contribution * voo_split
buy_sgov = contribution * sgov_split

voo_cost = buy_voo * months
sgov_cost = buy_sgov * months
cum_funding = contribution * months

sgov_income = sgov_cost * (sgov_yield / 12.0)                    # monthly
is_div_month = (dates.month % 3 == 0)                            # Mar/Jun/Sep/Dec
voo_income = np.where(is_div_month, voo_cost * (voo_yield / 4.0), 0.0)
income = sgov_income + voo_income

leftover = contribution - buy_voo - buy_sgov                     # normally 0
cash_and_returns = np.cumsum(leftover + income)
sgov_mv = sgov_cost                                              # flat price


def voo_market_value(monthly_factor):
    """VOO market value recursion. monthly_factor: (S, M) → returns (S, M)."""
    S = monthly_factor.shape[0]
    mv = np.zeros((S, M))
    prev = np.zeros(S)
    for t in range(M):
        prev = prev * monthly_factor[:, t] + buy_voo
        mv[:, t] = prev
    return mv


# Deterministic (constant mean, no vol) ---------------------------------------
det_factor = np.full((1, M), (1 + voo_mean) ** (1 / 12))
det_voo_mv = voo_market_value(det_factor)[0]
det_total = det_voo_mv + sgov_mv + cash_and_returns
det_net = det_total - cum_funding

# Monte Carlo -----------------------------------------------------------------
rng = np.random.default_rng(int(seed))
annual = np.clip(rng.normal(voo_mean, voo_vol, size=(n_sims, horizon)), -0.95, None)
monthly_factor = np.repeat((1 + annual) ** (1 / 12), 12, axis=1)      # (S, M)

mc_voo_mv = voo_market_value(monthly_factor)
del monthly_factor                                                    # free memory
mc_total = mc_voo_mv + sgov_mv[None, :] + cash_and_returns[None, :]
del mc_voo_mv

pct_levels = [10, 25, 50, 75, 90]
bands = {p: np.percentile(mc_total, p, axis=0) for p in pct_levels}
final_total = mc_total[:, -1].copy()
del mc_total

contrib_total = cum_funding[-1]
final_net = final_total - contrib_total

# ----------------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------------
st.title("Portfolio Projection")
st.markdown(
    f"Projecting a **VOO / SGOV** portfolio funded **\\${contribution:,.0f}/month** "
    f"({voo_split:.0%} VOO · {sgov_split:.0%} SGOV) over **{horizon} years**, "
    f"starting **{dates[0]:%b %Y}**."
)

with st.expander("Model assumptions & method", expanded=False):
    st.markdown(f"""
| Assumption | Value | Notes |
|---|---|---|
| Monthly funding | **\\${contribution:,.0f}** | split {voo_split:.0%} VOO / {sgov_split:.0%} SGOV |
| VOO expected return | **{voo_mean:.1%}** | price-only, dividends added separately |
| VOO volatility (σ) | **{voo_vol:.1%}** | annual std dev — drives the spread |
| VOO dividend yield | **{voo_yield:.2%}** | quarterly, on cost, held as cash |
| SGOV distribution yield | **{sgov_yield:.2%}** | monthly, on cost; price flat |
| Horizon | **{horizon} yrs** | {M} months |
| Simulations | **{n_sims:,}** | random seed {int(seed)} |

**Method** — Only VOO's price return is random. Each simulated year draws one
annual return from a Normal(μ, σ) and compounds it monthly, so the *order* of
good/bad years varies across paths — this reveals sequence-of-returns risk.
Dividends are **not** reinvested (held as cash); SGOV price is flat, so its
entire return is the monthly distribution.

⚠️ Returns are drawn from a **Normal** distribution. Real equity returns have
**fatter tails**, so the most extreme return periods (1-in-500, 1-in-10,000)
understate true tail risk and are also statistically noisy at these sim counts —
treat them as indicative, not precise.
""")

st.divider()

tab_mc, tab_det = st.tabs(["📊  Monte Carlo", "📈  Deterministic"])

# ============================================================================
# MONTE CARLO
# ============================================================================
with tab_mc:
    # ---- Headline metrics ---------------------------------------------------
    st.subheader("Summary")
    a1, a2, a3, a4, a5 = st.columns(5)
    a1.metric("Monthly contribution", f"${contribution:,.0f}")
    a2.metric("Total contributed", f"${contrib_total:,.0f}",
              help=f"${contribution:,.0f} × {M} months.")
    p50 = np.percentile(final_total, 50)
    a3.metric("Median ending value", f"${p50:,.0f}",
              delta=f"${p50 - contrib_total:,.0f} vs contributed")
    a4.metric("Probability of a gain", f"{(final_net > 0).mean():.0%}",
              help="Share of simulations ending above total contributions.")
    a5.metric("Median annualised return",
              f"{(p50/contrib_total)**(1/horizon)-1:.1%}",
              help="Rough IRR proxy: median value ÷ contributions, annualised.")

    st.caption("**Downside → upside band of ending value**")
    b1, b2, b3 = st.columns(3)
    b1.metric("Pessimistic — P10", f"${np.percentile(final_total,10):,.0f}",
              help="Only 10% of outcomes fall below this.")
    b2.metric("Median — P50", f"${p50:,.0f}")
    b3.metric("Optimistic — P90", f"${np.percentile(final_total,90):,.0f}",
              help="Only 10% of outcomes exceed this.")

    # ---- Fan chart ----------------------------------------------------------
    st.markdown("##### Projected value over time")
    fan = pd.DataFrame({"Date": dates,
                        "P10": bands[10], "P25": bands[25], "P50": bands[50],
                        "P75": bands[75], "P90": bands[90],
                        "Contributions": cum_funding})
    base = alt.Chart(fan).encode(
        x=alt.X("Date:T", title=None, axis=alt.Axis(format="%Y")))
    band_outer = base.mark_area(opacity=0.15, color=BLUE).encode(
        y=alt.Y("P10:Q", title="Total value if sold", axis=alt.Axis(format=USD)),
        y2="P90:Q",
        tooltip=[alt.Tooltip("Date:T", format="%b %Y"),
                 alt.Tooltip("P10:Q", format=USD, title="P10"),
                 alt.Tooltip("P90:Q", format=USD, title="P90")])
    band_inner = base.mark_area(opacity=0.30, color=BLUE).encode(
        y="P25:Q", y2="P75:Q",
        tooltip=[alt.Tooltip("Date:T", format="%b %Y"),
                 alt.Tooltip("P25:Q", format=USD, title="P25"),
                 alt.Tooltip("P75:Q", format=USD, title="P75")])
    line_med = base.mark_line(color=NAVY, strokeWidth=3).encode(
        y="P50:Q", tooltip=[alt.Tooltip("Date:T", format="%b %Y"),
                            alt.Tooltip("P50:Q", format=USD, title="Median")])
    line_contrib = base.mark_line(color=GREY, strokeDash=[5, 4],
                                  strokeWidth=1.8).encode(
        y="Contributions:Q", tooltip=[alt.Tooltip("Date:T", format="%b %Y"),
                                      alt.Tooltip("Contributions:Q", format=USD)])
    st.altair_chart(
        (band_outer + band_inner + line_med + line_contrib)
        .properties(height=430).interactive(), use_container_width=True)
    st.caption("**Navy** = median (P50).  **Dark band** = P25–P75 (middle 50%).  "
               "**Light band** = P10–P90 (80% of outcomes).  **Dashed grey** = "
               "cumulative contributions — you're ahead when the median is above it.")

    # ---- Return-period analysis --------------------------------------------
    st.markdown("##### Outcomes by return period")
    st.caption(
        "Read a **1-in-N** row as: there's a **1/N** chance of ending *below* the "
        "downside value (and 1/N of ending *above* the upside value). "
        "**TVaR** = the *average* outcome in the tail beyond the downside point "
        "(a conditional-tail-expectation, i.e. 'if it's a bad one, how bad on average')."
    )
    return_periods = [10000, 500, 250, 100, 50, 20, 10, 5, 2]
    rows = []
    for T in return_periods:
        q_down = 100.0 / T           # e.g. T=100 → 1st percentile
        q_up = 100.0 - q_down        # e.g. T=100 → 99th percentile
        dval = np.percentile(final_total, q_down)
        uval = np.percentile(final_total, q_up)
        tail = final_total[final_total <= dval]
        tvar = tail.mean() if tail.size else dval
        rows.append({
            "Return period": f"1-in-{T:,}",
            "Prob. below": q_down / 100.0,
            "Downside value": dval,
            "Downside net gain": dval - contrib_total,
            "TVaR (avg if worse)": tvar,
            "Upside value": uval,
        })
    rp = pd.DataFrame(rows)
    rp_styled = rp.style.format({
        "Prob. below": "{:.2%}",
        "Downside value": "${:,.0f}",
        "Downside net gain": "${:,.0f}",
        "TVaR (avg if worse)": "${:,.0f}",
        "Upside value": "${:,.0f}",
    })
    st.dataframe(rp_styled, hide_index=True, use_container_width=True)

    # Return-period chart (log x)
    rp_long = pd.DataFrame({
        "Return period": return_periods * 2,
        "Value": list(rp["Downside value"]) + list(rp["Upside value"]),
        "Tail": ["Downside"] * len(return_periods) + ["Upside"] * len(return_periods),
    })
    rp_chart = (alt.Chart(rp_long).mark_line(point=True, strokeWidth=2.5).encode(
        x=alt.X("Return period:Q",
                scale=alt.Scale(type="log"),
                title="Return period (years, log scale)"),
        y=alt.Y("Value:Q", title="Ending value", axis=alt.Axis(format=USD)),
        color=alt.Color("Tail:N", title=None,
                        scale=alt.Scale(domain=["Downside", "Upside"],
                                        range=[RED, GREEN])),
        tooltip=[alt.Tooltip("Return period:Q", title="1-in-N"),
                 alt.Tooltip("Tail:N"),
                 alt.Tooltip("Value:Q", format=USD)])
        .properties(height=320))
    contrib_rule = (alt.Chart(pd.DataFrame({"y": [contrib_total]}))
                    .mark_rule(color=GREY, strokeDash=[5, 4])
                    .encode(y="y:Q"))
    st.altair_chart(rp_chart + contrib_rule, use_container_width=True)
    st.caption("Red = bad-tail (downside) ending value at each return period; "
               "green = good-tail (upside). Dashed grey = total contributions. "
               "Rarer events (higher 1-in-N) push further into each tail.")

    # ---- Ending distribution ------------------------------------------------
    st.markdown("##### Distribution of ending value")
    hist_df = pd.DataFrame({"Ending value": final_total})
    hist = (alt.Chart(hist_df).mark_bar(opacity=0.8, color=BLUE).encode(
        x=alt.X("Ending value:Q", bin=alt.Bin(maxbins=45),
                title="Ending total value", axis=alt.Axis(format=USD)),
        y=alt.Y("count()", title="Number of simulations"))
        .properties(height=280))
    ref = (alt.Chart(pd.DataFrame(
                {"v": [contrib_total, p50], "label": ["Contributions", "Median"]}))
           .mark_rule(strokeWidth=2, strokeDash=[5, 4])
           .encode(x="v:Q",
                   color=alt.Color("label:N", title=None,
                                   scale=alt.Scale(domain=["Contributions", "Median"],
                                                   range=[GREY, NAVY]))))
    st.altair_chart(hist + ref, use_container_width=True)
    st.caption("Each bar counts simulations landing in that value range. "
               "Grey line = money contributed; navy line = median outcome.")

    # ---- Percentile table ---------------------------------------------------
    st.markdown("##### Ending outcomes by percentile")
    pt = pd.DataFrame({
        "Scenario": ["Pessimistic (P10)", "Lower (P25)", "Median (P50)",
                     "Upper (P75)", "Optimistic (P90)"],
        "Ending value": [np.percentile(final_total, p) for p in pct_levels],
        "Net gain": [np.percentile(final_total, p) - contrib_total
                     for p in pct_levels],
    })
    pt["Multiple of contributions"] = pt["Ending value"] / contrib_total
    st.dataframe(pt.style.format({
        "Ending value": "${:,.0f}", "Net gain": "${:,.0f}",
        "Multiple of contributions": "{:.2f}×"}),
        hide_index=True, use_container_width=True)

# ============================================================================
# DETERMINISTIC
# ============================================================================
with tab_det:
    st.subheader("Single smooth path")
    st.caption(f"Constant **{voo_mean:.1%}** VOO return every year (no volatility). "
               "This is the baseline the Monte Carlo spread is centred on.")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Monthly contribution", f"${contribution:,.0f}")
    d2.metric("Total contributed", f"${cum_funding[-1]:,.0f}")
    d3.metric("Ending value", f"${det_total[-1]:,.0f}",
              delta=f"${det_net[-1]:,.0f} net gain")
    d4.metric("Net gain", f"${det_net[-1]:,.0f}")

    st.markdown("##### Value vs. contributions over time")
    det_df = pd.DataFrame({"Date": dates, "Total value": det_total,
                           "Contributions": cum_funding, "Net gain": det_net})
    long = det_df.melt("Date", ["Total value", "Contributions"],
                       var_name="Series", value_name="Value")
    chart = (alt.Chart(long).mark_line(strokeWidth=3).encode(
        x=alt.X("Date:T", title=None, axis=alt.Axis(format="%Y")),
        y=alt.Y("Value:Q", title="$", axis=alt.Axis(format=USD)),
        color=alt.Color("Series:N", title=None,
                        scale=alt.Scale(domain=["Total value", "Contributions"],
                                        range=[NAVY, GREY])),
        strokeDash=alt.StrokeDash("Series:N",
                        scale=alt.Scale(domain=["Total value", "Contributions"],
                                        range=[[1, 0], [5, 4]]), legend=None),
        tooltip=[alt.Tooltip("Date:T", format="%b %Y"),
                 alt.Tooltip("Series:N"), alt.Tooltip("Value:Q", format=USD)])
        .properties(height=430).interactive())
    st.altair_chart(chart, use_container_width=True)
    st.caption("**Navy** = projected total value.  **Dashed grey** = cumulative "
               "contributions.  The gap between them is your net gain.")

    with st.expander("Monthly detail table"):
        show = det_df.copy()
        show["Date"] = show["Date"].dt.strftime("%b %Y")
        st.dataframe(show.style.format({
            "Total value": "${:,.0f}", "Contributions": "${:,.0f}",
            "Net gain": "${:,.0f}"}), hide_index=True, use_container_width=True)
