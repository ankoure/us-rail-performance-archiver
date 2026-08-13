-- PostGIS geo layer: route shapes, stops, and a route_id crosswalk.
--
-- Additive alongside the existing Parquet marts pipeline/gtfs.py already
-- writes (gtfs_stops, gtfs_routes, gtfs_route_aliases, route_shapes,
-- route_shape_stops, gtfs_versions) -- this is a read-optimized, spatially
-- queryable copy for dashboard map features, synced by
-- pipeline/postgis_sync.py. Tracks full GTFS version history: every table
-- is keyed by (feed, version_slug), mirroring the Parquet marts' own
-- partitioning, so historical snapshots coexist rather than being
-- overwritten by the latest one.
--
-- Also carries route_shape_edits: hand-corrected geometries for shapes whose
-- published GTFS source is low-quality or doesn't match reality, meant to be
-- edited directly (e.g. via QGIS connected to this database). Kept as a
-- separate table from route_shapes rather than edited in place, so
-- pipeline/postgis_sync.py can freely delete+reinsert route_shapes on every
-- resync without ever touching or clobbering a manual correction -- see
-- route_shapes_current, which transparently prefers the edit when present.
--
-- Apply once against the target database:
--   psql "$POSTGIS_DATABASE_URL" -f pipeline/sql/postgis_schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS gtfs_versions (
    feed TEXT NOT NULL,
    service_date DATE NOT NULL,
    version_slug TEXT NOT NULL,
    feed_version TEXT,
    feed_start_date DATE NOT NULL,
    feed_end_date DATE NOT NULL,
    PRIMARY KEY (feed, service_date)
);
CREATE INDEX IF NOT EXISTS gtfs_versions_slug_idx ON gtfs_versions (feed, version_slug);

CREATE TABLE IF NOT EXISTS stops (
    feed TEXT NOT NULL,
    version_slug TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    stop_code TEXT,
    stop_name TEXT,
    -- Groups direction-dependent platforms (e.g. Tower City) under one
    -- physical station -- see docs/design/stop-id-stability-findings.md,
    -- which recommends this over a general stop_id crosswalk.
    parent_station TEXT,
    geom geometry(Point, 4326) NOT NULL,
    PRIMARY KEY (feed, version_slug, stop_id)
);
CREATE INDEX IF NOT EXISTS stops_geom_idx ON stops USING GIST (geom);
CREATE INDEX IF NOT EXISTS stops_parent_idx ON stops (feed, version_slug, parent_station);

CREATE TABLE IF NOT EXISTS routes (
    feed TEXT NOT NULL,
    version_slug TEXT NOT NULL,
    route_id TEXT NOT NULL,
    route_short_name TEXT,
    route_long_name TEXT,
    mode TEXT NOT NULL,
    PRIMARY KEY (feed, version_slug, route_id)
);

-- Raw display token (route_short_name/route_long_name) -> canonical
-- route_id. Covers e.g. MBTA's Silver Line 5, whose static route_id is
-- "749" but whose route_short_name is "SL5". Does NOT cover the separate
-- case of RT feeds whose token matches no static route_id at all (MARTA,
-- CCRTA, BRTA, Passio, WRTA JSON feeds) -- those are resolved today by ad
-- hoc per-agency functions in analysis/normalize.py, not materialized here.
CREATE TABLE IF NOT EXISTS route_id_crosswalk (
    feed TEXT NOT NULL,
    version_slug TEXT NOT NULL,
    alias_token TEXT NOT NULL,
    alias_type TEXT NOT NULL CHECK (alias_type IN ('short_name', 'long_name')),
    route_id TEXT NOT NULL,
    PRIMARY KEY (feed, version_slug, alias_type, alias_token),
    FOREIGN KEY (feed, version_slug, route_id)
        REFERENCES routes (feed, version_slug, route_id)
);

CREATE TABLE IF NOT EXISTS route_shapes (
    feed TEXT NOT NULL,
    version_slug TEXT NOT NULL,
    route_id TEXT NOT NULL,
    direction_id SMALLINT NOT NULL,
    shape_id TEXT NOT NULL,
    geom geometry(LineString, 4326) NOT NULL,
    -- Cumulative arc length at the shape's last point, carried over from
    -- the Parquet mart's own haversine-based dist_m (analysis/geo.py)
    -- rather than recomputed via ST_Length, to avoid a geometry-vs-
    -- geography SRID discussion.
    length_m DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (feed, version_slug, route_id, direction_id, shape_id)
);
CREATE INDEX IF NOT EXISTS route_shapes_geom_idx ON route_shapes USING GIST (geom);

CREATE TABLE IF NOT EXISTS route_shape_stops (
    feed TEXT NOT NULL,
    version_slug TEXT NOT NULL,
    route_id TEXT NOT NULL,
    direction_id SMALLINT NOT NULL,
    shape_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    dist_m DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (feed, version_slug, route_id, direction_id, shape_id, stop_id),
    FOREIGN KEY (feed, version_slug, route_id, direction_id, shape_id)
        REFERENCES route_shapes (feed, version_slug, route_id, direction_id, shape_id),
    FOREIGN KEY (feed, version_slug, stop_id)
        REFERENCES stops (feed, version_slug, stop_id)
);

-- Hand-corrected replacement geometry for one route+direction's shape.
-- Keyed on shape_id (not just route_id/direction_id) because a route can run
-- more than one shape per direction (a branching line -- see
-- StaticGtfs.route_direction_shapes); keying on shape_id lets an edit target
-- exactly one branch instead of ambiguously covering all of them. Not tied
-- to version_slug: a correction is meant to persist across GTFS resyncs.
-- If an agency later republishes a *different* shape_id for that
-- route+direction (a genuine reroute, not just a resync), this edit simply
-- stops matching in route_shapes_current below rather than silently
-- applying to unrelated geometry -- a stale correction needs human review,
-- not automatic reattachment.
CREATE TABLE IF NOT EXISTS route_shape_edits (
    feed TEXT NOT NULL,
    route_id TEXT NOT NULL,
    direction_id SMALLINT NOT NULL,
    shape_id TEXT NOT NULL,
    geom geometry(LineString, 4326) NOT NULL,
    note TEXT,
    edited_by TEXT,
    edited_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (feed, route_id, direction_id, shape_id)
);

CREATE OR REPLACE FUNCTION route_shape_edits_touch() RETURNS trigger AS $$
BEGIN
    NEW.edited_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS route_shape_edits_touch_trg ON route_shape_edits;
CREATE TRIGGER route_shape_edits_touch_trg
    BEFORE UPDATE ON route_shape_edits
    FOR EACH ROW EXECUTE FUNCTION route_shape_edits_touch();

-- Latest synced GTFS version per feed, by the same rule
-- dashboard/api/services/data.py's _latest_version_slug already uses: the
-- version_slug attached to the gtfs_versions row with the max service_date.
CREATE OR REPLACE VIEW gtfs_latest_version AS
SELECT DISTINCT ON (feed) feed, service_date, version_slug
FROM gtfs_versions
ORDER BY feed, service_date DESC;

-- The shape a map should actually draw for each (feed, route_id,
-- direction_id, shape_id): the hand-corrected geometry when
-- route_shape_edits has one, otherwise the latest synced route_shapes
-- geometry. length_m is recomputed from whichever geometry wins (a
-- correction can change the path length) rather than trusting route_shapes'
-- precomputed length_m, which only ever reflects the unedited source.
--
-- Does not adjust route_shape_stops' stop offsets to match an edited
-- geometry -- those still reflect the original shape's projection. Known
-- gap, not addressed here: recomputing them would need re-projecting every
-- stop onto the edited polyline (ST_LineLocatePoint), a separate feature
-- from just being able to correct a shape's geometry.
CREATE OR REPLACE VIEW route_shapes_current AS
SELECT
    rs.feed,
    rs.route_id,
    rs.direction_id,
    rs.shape_id,
    rs.version_slug,
    COALESCE(e.geom, rs.geom) AS geom,
    (e.geom IS NOT NULL) AS is_edited,
    ST_Length(COALESCE(e.geom, rs.geom)::geography) AS length_m
FROM route_shapes rs
JOIN gtfs_latest_version lv
    ON lv.feed = rs.feed AND lv.version_slug = rs.version_slug
LEFT JOIN route_shape_edits e
    ON e.feed = rs.feed
    AND e.route_id = rs.route_id
    AND e.direction_id = rs.direction_id
    AND e.shape_id = rs.shape_id;
