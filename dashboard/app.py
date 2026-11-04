import streamlit as st

import queries

st.set_page_config(page_title="PR Lifecycle & DORA Metrics", layout="wide")


@st.cache_data(ttl=60)
def load_window():
    return queries.data_window()


@st.cache_data(ttl=60)
def load_kpis(start, end, include_bots):
    return queries.kpis(start, end, include_bots)


def hours(value, digits=1):
    return "–" if value is None else f"{value:,.{digits}f} h"


def days_note(value):
    return None if value is None else f"≈ {value / 24:,.1f} days"


first_day, last_day, data_as_of = load_window()

st.title("PR Lifecycle & DORA Metrics")
st.caption(
    f"encode/httpx · Data as of {data_as_of:%Y-%m-%d %H:%M} UTC (when it was last pulled from GitHub). "
    "A merged pull request is used as a proxy for a deployment."
)

with st.sidebar:
    st.header("Filters")
    picked = st.date_input(
        "Date range",
        value=(first_day, last_day),
        min_value=first_day,
        max_value=last_day,
        key="date_range",
        help="Merged-PR figures use the day a PR was merged; opened-PR figures use the day it was opened.",
    )
    include_bots = st.checkbox(
        "Include bot PRs",
        value=False,
        key="include_bots",
        help="Bot PRs (for example dependabot) are opened and merged on a different rhythm from human work, "
        "so they are left out unless you ask for them.",
    )

# While someone is mid-selection the picker returns only a start date.
if not isinstance(picked, (tuple, list)) or len(picked) != 2:
    st.info("Pick an end date to apply the date range.")
    st.stop()
start, end = picked

st.caption(
    f"Showing {start:%Y-%m-%d} to {end:%Y-%m-%d} · "
    + ("bot PRs included" if include_bots else "human-authored PRs only")
)

k = load_kpis(start, end, include_bots)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Merged PRs", f"{k['merged_prs']:,}", help="Pull requests merged in the date range.")
c2.metric(
    "Median lead time",
    hours(k["median_lead_time_hours"]),
    help="Typical time from a PR being opened to being merged. Half of merged PRs were faster than this. "
    + (days_note(k["median_lead_time_hours"]) or ""),
)
c3.metric(
    "p90 lead time",
    hours(k["p90_lead_time_hours"]),
    help="9 in 10 merged PRs were merged faster than this, so it shows how long the slow ones take. "
    + (days_note(k["p90_lead_time_hours"]) or ""),
)
c4.metric(
    "Review coverage",
    "–" if k["review_coverage_pct"] is None else f"{k['review_coverage_pct']:.1f}%",
    help="Share of merged PRs that got at least one review before they were merged. "
    f"{k['reviewed_before_merge_prs']:,} of {k['merged_prs']:,}.",
)
c5.metric(
    "Median time to first review",
    hours(k["median_hours_to_first_review"]),
    help="Typical wait between opening a PR and its first review, for merged PRs reviewed before merging.",
)

if k["merged_prs"] == 0:
    st.warning("No PRs were merged in this date range with these filters.")
