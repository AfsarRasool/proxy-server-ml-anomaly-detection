# Data Directory

The proxy creates the following folders automatically:

- `raw/` contains one row for each HTTP request or HTTPS connection.
- `features/` contains one aggregated row per active client for each fixed time window.

Use a separate session name for every collection run. For example:

```text
data/
├── raw/
│   ├── traffic_normal_01.csv
│   └── traffic_anomalous_01.csv
└── features/
    ├── features_normal_01.csv
    └── features_anomalous_01.csv
```

Only the files under `features/` are intended as direct model-training input. Raw traffic files should be retained for auditing and future feature engineering.
