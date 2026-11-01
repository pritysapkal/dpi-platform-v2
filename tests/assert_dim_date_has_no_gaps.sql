-- dim_date must be a continuous calendar: with no gaps, the number of rows equals the
-- number of days from the first to the last date. A gap would silently drop days from
-- any chart built on it. Returns a row (= test fails) only if days are missing.

select
    count(*) as day_rows,
    date_diff('day', min(date_day), max(date_day)) + 1 as expected_days
from {{ ref('dim_date') }}
having count(*) <> date_diff('day', min(date_day), max(date_day)) + 1
