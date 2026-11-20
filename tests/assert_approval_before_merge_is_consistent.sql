-- "Approved before the merge" must be a stricter version of two other flags. If either rule is broken, the funnel
-- on the dashboard stops adding up. Returns a row (= test fails) for any PR where
--   * it was approved before merge but not approved at all (a contradiction), or
--   * it was approved before merge but not reviewed before merge (an approval is a review), or
--   * the flag is true for a PR that was never merged.

select pr_number
from {{ ref('fct_pull_requests') }}
where was_approved_before_merge
  and (not was_approved or not was_reviewed_before_merge or not is_merged)
