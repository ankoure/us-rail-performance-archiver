from typing import Literal

from pydantic import BaseModel


class SegmentFeatureProperties(BaseModel):
    from_stop_id: str
    to_stop_id: str
    direction_id: int
    bucket: int
    avg_speed_mph: float
    sample_count: int
    from_name: str
    to_name: str
    direction_label: str
    is_interpolated: bool


class SegmentGeometry(BaseModel):
    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]]


class SegmentFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: SegmentGeometry
    properties: SegmentFeatureProperties


class SegmentFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[SegmentFeature]


class StopFeatureProperties(BaseModel):
    stop_id: str
    name: str


class StopGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float]


class StopFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: StopGeometry
    properties: StopFeatureProperties


class StopFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[StopFeature]


class SpeedBucketLegendEntry(BaseModel):
    bucket: int
    label: str
    min_mph: float
    max_mph: float


class SegmentSpeedMapResponse(BaseModel):
    """Ready-to-render payload for the segment speed map: per-segment
    LineString features (colored by a bucket relative to this route/range),
    the route's stops as Point features, and the bucket legend. See
    api.services.segment_speed_map.build_segment_speed_map."""

    segments: SegmentFeatureCollection
    stops: StopFeatureCollection
    legend: list[SpeedBucketLegendEntry]
