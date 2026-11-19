"""Bar charts for the panels that count pull requests.

Streamlit's built-in bar chart draws thin bars along a date axis and can label a count axis with
decimals (0.5, 1.5) when the numbers are small. These charts fix both:

- the count axis is whole numbers only (format "d", never a tick step below 1), and starts at 0;
- categories sit on a band axis (one equal slot per bar, in the order given), so a monthly chart
  with 47 months gets 47 wide bars instead of 47 slivers on a time axis.

The data behind each chart is the DataFrame passed in, so the tests can read it back out of the page.
"""

import altair as alt
import pandas as pd

WHOLE_NUMBERS = alt.Axis(format="d", tickMinStep=1)  # integer labels, and no tick step under 1
BAR_PADDING = 0.12  # share of each slot left empty between bars; small, so the bars are wide
VERTICAL_HEIGHT = 260
HORIZONTAL_BAR_STEP = 28  # pixels per bar in a horizontal chart


def period_labels(period_start: pd.Series, grain: str) -> pd.Series:
    """Axis labels for a week or month chart: '2024-03' for months, the Monday ('2024-03-04') for weeks."""
    if grain not in ("month", "week"):
        raise ValueError(f"grain must be 'month' or 'week', got {grain!r}")
    return pd.to_datetime(period_start).dt.strftime("%Y-%m" if grain == "month" else "%Y-%m-%d")


def count_bar(
    df: pd.DataFrame,
    category: str,
    value: str,
    *,
    category_title: str,
    value_title: str,
    horizontal: bool = False,
    label_angle: int = 0,
) -> alt.Chart:
    """A bar chart of `value` (a count of PRs) for each `category`, in the row order of `df`.

    horizontal=True puts the categories down the side with the first row at the top, which suits a ranking.
    label_angle rotates the category labels on a vertical chart; labels that would overlap are dropped.
    """
    counts = alt.Axis(format="d", tickMinStep=1, title=value_title)
    count_scale = alt.Scale(domainMin=0, nice=True)
    slots = alt.Scale(paddingInner=BAR_PADDING, paddingOuter=BAR_PADDING / 2)
    tooltip = [alt.Tooltip(f"{category}:N", title=category_title), alt.Tooltip(f"{value}:Q", title=value_title, format="d")]

    if horizontal:
        chart = alt.Chart(df, height=alt.Step(HORIZONTAL_BAR_STEP)).encode(
            y=alt.Y(f"{category}:O", sort=None, scale=slots, axis=alt.Axis(title=None, labelLimit=260)),
            x=alt.X(f"{value}:Q", scale=count_scale, axis=counts),
            tooltip=tooltip,
        )
    else:
        chart = alt.Chart(df, height=VERTICAL_HEIGHT).encode(
            x=alt.X(
                f"{category}:O", sort=None, scale=slots,
                axis=alt.Axis(title=category_title, labelAngle=label_angle, labelOverlap=True),
            ),
            y=alt.Y(f"{value}:Q", scale=count_scale, axis=counts),
            tooltip=tooltip,
        )
    return chart.mark_bar(cornerRadiusEnd=2)
