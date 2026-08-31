from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import settings
from api.routers import (
    adherence,
    agencies,
    alerts,
    directions,
    health,
    route_day,
    route_day_otp,
    route_shape,
    routes,
    segment_day,
    segment_speed_map,
    stop_day,
    stop_day_otp,
    stops,
    trip_metrics,
    line_delays,
)

app = FastAPI(title="Transit Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(agencies.router)
app.include_router(stop_day.router)
app.include_router(route_day.router)
app.include_router(stop_day_otp.router)
app.include_router(route_day_otp.router)
app.include_router(segment_day.router)
app.include_router(segment_speed_map.router)
app.include_router(routes.router)
app.include_router(stops.router)
app.include_router(route_shape.router)
app.include_router(directions.router)
app.include_router(adherence.router)
app.include_router(alerts.router)
app.include_router(trip_metrics.router)
app.include_router(line_delays.router)
