import streamlit as st

import queries

st.set_page_config(page_title="PR Lifecycle & DORA Metrics", layout="wide")


@st.cache_data(ttl=60)
def load_window():
    return queries.data_window()


@st.cache_data(ttl=60)
def load_kpis(start, end, include_bots):
    return queries.kpis(start, end, include_bots)


@st.cache_data(ttl=60)
def load_trend(start, end, include_bots, grain):
    return queries.trend(start, end, include_bots, grain)


@st.cache_data(ttl=60)
def load_breakdown(start, end):
    return queries.lead_time_breakdown(start, end)


@st.cache_data(ttl=60)
def load_funnel(start, end, include_bots):
    return queries.review_funnel(start, end, include_bots)


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

st.divider()

st.subheader("Trends")
grain_label = st.radio("Group by", ["Monthly", "Weekly"], horizontal=True, key="grain")
grain = "month" if grain_label == "Monthly" else "week"
unit = grain  # "month" or "week", for captions
trend = load_trend(start, end, include_bots, grain)

st.markdown("**Deployment frequency: PRs merged per " + unit + "**")
st.bar_chart(trend, x="period_start", y="merged_prs", x_label=f"{unit} starting", y_label="merged PRs")
st.caption(
    f"How many PRs were merged each {unit} (a merge is used as a proxy for a deployment). "
    f"A {unit} with no merges shows as zero. Good looks like steady output without long silent gaps; "
    f"a run of empty {unit}s usually means shipping stopped. The first and last {unit} can be partial "
    "if the date range starts or ends part-way through."
)

left, right = st.columns(2)
with left:
    st.markdown("**Lead time: median hours per " + unit + "**")
    st.line_chart(trend, x="period_start", y="median_lead_time_hours", x_label=f"{unit} starting", y_label="hours")
    st.caption(
        f"The typical PR's time from opened to merged, for PRs merged in each {unit}. Good looks like low and "
        f"stable, or trending down. A {unit} with no merges has no point. With only a few merges in a {unit} the "
        "median jumps around, so read it together with the PR count chart above."
    )
with right:
    st.markdown("**Lead time: 90th percentile hours per " + unit + "**")
    st.line_chart(trend, x="period_start", y="p90_lead_time_hours", x_label=f"{unit} starting", y_label="hours")
    st.caption(
        f"How long the slowest tenth of PRs took in each {unit}: 9 in 10 were merged faster than this. Good looks "
        "like staying close to the median. Tall spikes mean a few PRs sat waiting for a long time, which the "
        "median hides."
    )

st.divider()

st.subheader("Where the time goes: waiting for a first review vs. in review")
st.caption(
    "Human-authored PRs only, whatever the bot checkbox says: bot PRs such as dependabot are not reviewed the "
    "same way. Covers merged PRs that were reviewed before they were merged, in the date range."
)
b = load_breakdown(start, end)

if b["split_prs"] == 0:
    st.info("No human-authored PRs in this date range were reviewed before being merged, so there is nothing to split.")
else:
    import pandas as pd

    share = pd.DataFrame(
        {
            "stage": ["Waiting for first review", "In review (first review to merge)"],
            "share of hours (%)": [b["wait_share_pct"], b["in_review_share_pct"]],
            "bar": ["Share of all hours", "Share of all hours"],
        }
    )
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Share of all hours these PRs spent open**")
        st.bar_chart(share, x="bar", y="share of hours (%)", color="stage", horizontal=True, x_label="", y_label="% of hours")
        st.caption(
            "Out of every hour these PRs spent open, how much was spent waiting for someone to look at them "
            "and how much after the first review. Good looks like a small waiting share: a large one means the "
            "work sat idle before anyone responded. This is pulled toward the slowest PRs; the table shows the "
            "typical PR."
        )
    with right:
        st.markdown("**Typical and slow PRs**")
        st.dataframe(
            pd.DataFrame(
                {
                    "Stage": ["Waiting for first review", "In review (first review to merge)", "Whole lead time"],
                    "Median (h)": [b["median_wait_hours"], b["median_in_review_hours"], b["median_lead_time_hours"]],
                    "p90 (h)": [b["p90_wait_hours"], b["p90_in_review_hours"], b["p90_lead_time_hours"]],
                }
            ).round(1),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            f"Based on {b['split_prs']:,} PRs. {b['not_split_prs']:,} merged human PRs had no review before they "
            "were merged and are left out, since there is no first review to split at. The two medians do not "
            "add up to the median of the whole, because the median of a sum is not the sum of medians."
        )

st.divider()

st.subheader("Review funnel: opened, reviewed, approved, merged")
f = load_funnel(start, end, include_bots)

if f["opened"] == 0:
    st.info("No PRs were opened in this date range with these filters.")
else:
    import pandas as pd

    stages = pd.DataFrame(
        {
            "stage": ["Opened", "Reviewed", "Approved", "Merged"],
            "PRs": [f["opened"], f["reviewed"], f["approved"], f["merged"]],
        }
    )
    stages["% of opened"] = (100.0 * stages["PRs"] / f["opened"]).round(1)

    left, right = st.columns([3, 2])
    with left:
        st.bar_chart(stages, x="stage", y="PRs", horizontal=True, sort=False, x_label="", y_label="PRs opened in the date range")
        st.caption(
            "What happened to the PRs opened in the date range. Good looks like most PRs getting a review, and most "
            "reviewed PRs ending up approved and merged. A big drop between two bars is where work stalls or "
            "gets abandoned. PRs opened recently may simply not have been reviewed or merged yet."
        )
    with right:
        st.dataframe(stages, hide_index=True, width="stretch")
        st.caption(
            f"Of the {f['opened']:,} PRs: {f['merged']:,} merged, {f['closed_without_merge']:,} closed without "
            f"merging, {f['still_open']:,} still open when the data was pulled. "
            "Reviewed means a review while the PR was still open. This is a count at each stage, not a strict "
            f"funnel: {f['merged_without_approval']:,} merged PRs were never approved, and "
            f"{f['merged_without_review']:,} of those had no review before they were merged."
        )
