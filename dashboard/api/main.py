from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import settings
from api.routers import (
    adherence,
    agencies,
    alerts,
    health,
    route_day,
    route_day_otp,
    segment_day,
    stop_day,
    stop_day_otp,
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
app.include_router(adherence.router)
app.include_router(alerts.router)
app.include_router(line_delays.router)
