# Dashboard

Single-page Streamlit dashboard. Run it from the project root:

```
pip install -r dashboard/requirements.txt
streamlit run dashboard/app.py
```

Opens automatically at http://localhost:8501.

**Where the data comes from** (`data_source.py`, the caption names the one in use):

- the local database `../dev.duckdb` (or the file named by `DPIP_DB_PATH`) when it exists, opened read-only, so it is
  safe to run alongside `dbt build`;
- otherwise the snapshot the daily refresh publishes to the `data` branch: Parquet files checked against the SHA-256 in
  their manifest, cached for up to an hour, read through views named like the warehouse tables so the SQL in
  `queries.py` is the same for both. This is what the public deployment uses.

`DPIP_DATA_SOURCE=local` or `=snapshot` forces one of the two; `DPIP_SNAPSHOT_URL` points at another snapshot (a
`file://` folder works). A banner appears when the data is more than 48 hours old.

`requirements.txt` here is what Streamlit Community Cloud installs (it looks next to the entrypoint first), so it holds
only what the dashboard imports; `tests/test_deploy_footprint.py` keeps it that way.
