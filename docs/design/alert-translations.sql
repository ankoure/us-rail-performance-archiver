-- Reader views for the alert-translation tables (docs/design/alert-translations.md).
--
-- DuckDB. Point `root` at the curated tree: the hot bucket, or a local copy.
--
--   INSTALL httpfs; LOAD httpfs;             -- for s3://
--   SET VARIABLE root = 's3://rail-performance-archiver-hot';
--   SET VARIABLE gap_seconds = 90;           -- 3 x the feed's poll interval (MBTA alerts: 30 s)
--   .read docs/design/alert-translations.sql
--
-- Filter on feed_id (and year/month/day) in queries: the scans are hive
-- partitioned, so DuckDB prunes every other feed's files instead of reading them.

-- One row per poll attempt. `metadata` is the poll log the poller already
-- writes (one row per poll, including 304s, duplicates and errors); this adds
-- the decoded per-payload facts from alert_payloads, joined on digest within
-- the same day. Transport errors have no digest, so their payload columns are
-- null. A digest can recur on the next day (a 304 of yesterday's content),
-- which is why the join includes the day.
--
-- `metadata` has a declared schema (archiver/metadata_schema.py): every
-- partition has the same columns in the same order, and `timestamp` is
-- timestamp[us, UTC], equal to alert_text's fetched_at for the same poll.
-- Partitions from before it (no `schema_version` in the parquet key-value
-- metadata) had `timestamp` as epoch-seconds doubles and can't be read in the
-- same scan; pipeline/rewrite_metadata_schema.py rewrites them. union_by_name
-- stays so a later nullable column doesn't break reading older partitions.
--
-- `metadata.alert_count` is the poller's own per-poll count, written until
-- 2026-08-21; it is exposed as poller_alert_count. `alert_count` is decoded
-- from the payload by the alert_text build.
CREATE OR REPLACE VIEW poll_log AS
SELECT
    m.feed AS feed_id,
    m."timestamp" AS fetched_at,
    m.* EXCLUDE (feed, "timestamp", alert_count),
    m.alert_count AS poller_alert_count,
    p.first_fetched_at AS payload_first_fetched_at,
    p.parsed,
    p.carried_from_prev_day,
    p.feed_timestamp,
    p.gtfs_realtime_version,
    p.incrementality,
    p.entity_count,
    p.alert_count
FROM read_parquet(
        getvariable('root') || '/metadata/feed=*/year=*/month=*/day=*/data.parquet',
        hive_partitioning = true,
        union_by_name = true
    ) AS m
LEFT JOIN read_parquet(
        getvariable('root') || '/alert_payloads/feed=*/year=*/month=*/day=*/data.parquet',
        hive_partitioning = true
    ) AS p
    ON p.feed = m.feed
    AND p.digest = m.digest
    AND p.year = m.year
    AND p.month = m.month
    AND p.day = m.day;

-- Periods with no successful poll, derived from the poll log so they can never
-- disagree with it. A successful poll is one whose digest decoded (parsed);
-- errors, undecodable payloads and digests with no payload row are failures.
-- Meaningful only for feeds that have alert_payloads (without them every row
-- looks failed). Same rules the builder uses for alert_text.spans_gap.
--   downtime     consecutive polls more than gap_seconds apart: (prev, next)
--   failure      a maximal stretch of failed polls: (first, last)
--   missing_day  a UTC day inside the feed's range with no metadata at all
CREATE OR REPLACE VIEW gaps AS
WITH ordered AS (
    SELECT
        feed_id,
        fetched_at,
        coalesce(parsed, false) AS ok,
        lead(fetched_at) OVER w AS next_at,
        row_number() OVER w
            - row_number() OVER (
                PARTITION BY feed_id, coalesce(parsed, false) ORDER BY fetched_at
            ) AS island
    FROM poll_log
    WINDOW w AS (PARTITION BY feed_id ORDER BY fetched_at)
),
days AS (
    SELECT DISTINCT feed_id, CAST(date_trunc('day', fetched_at AT TIME ZONE 'UTC') AS DATE) AS d
    FROM poll_log
),
span AS (
    SELECT feed_id, min(d) AS lo, max(d) AS hi FROM days GROUP BY feed_id
)
SELECT feed_id, fetched_at AS gap_start, next_at AS gap_end, 'downtime' AS kind
FROM ordered
WHERE next_at - fetched_at > to_seconds(getvariable('gap_seconds'))
UNION ALL
SELECT feed_id, min(fetched_at), max(fetched_at), 'failure'
FROM ordered
WHERE NOT ok
GROUP BY feed_id, island
UNION ALL
SELECT
    s.feed_id,
    (cal.d::TIMESTAMP) AT TIME ZONE 'UTC',
    ((cal.d + 1)::TIMESTAMP) AT TIME ZONE 'UTC',
    'missing_day'
FROM span AS s
CROSS JOIN LATERAL (
    SELECT CAST(g AS DATE) AS d FROM generate_series(s.lo, s.hi, INTERVAL 1 DAY) AS t(g)
) AS cal
ANTI JOIN days USING (feed_id, d);

-- Full timeline of one alert in every language, runs stitched across days:
--
--   SELECT field, language, text,
--          min(first_seen) AS first_seen, max(last_seen) AS last_seen,
--          sum(poll_count) AS poll_count, bool_or(spans_gap) AS spans_gap
--   FROM read_parquet(getvariable('root') || '/alert_text/feed=*/year=*/month=*/day=*/data.parquet',
--                     hive_partitioning = true)
--   WHERE feed = 'massachusetts-bay-transportation-authority-mbta-alerts-1603'
--     AND alert_id = '1035121'
--   GROUP BY run_id, field, language, text
--   ORDER BY field, language, first_seen;
