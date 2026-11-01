-- The daily mart and the fact table must agree on how many PRs were merged. If the mart's
-- total is not the merged-PR count in fct_pull_requests, a join or filter is dropping or
-- duplicating PRs somewhere. Returns a row (= test fails) only when the totals differ.

with mart as (

    select sum(deployment_frequency) as merges_in_mart
    from {{ ref('mart_dora_metrics_daily') }}

),

fact as (

    select count(*) as merges_in_fct
    from {{ ref('fct_pull_requests') }}
    where is_merged

)

select
    mart.merges_in_mart,
    fact.merges_in_fct
from mart
cross join fact
where mart.merges_in_mart <> fact.merges_in_fct
