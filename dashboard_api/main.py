from fastapi import FastAPI

from dashboard_api.routers import (
    adherence,
    alerts,
    health,
    route_day,
    route_day_otp,
    stop_day,
    stop_day_otp,
    line_delays,
)

app = FastAPI(title="Transit Dashboard API")

app.include_router(health.router)
app.include_router(stop_day.router)
app.include_router(route_day.router)
app.include_router(stop_day_otp.router)
app.include_router(route_day_otp.router)
app.include_router(adherence.router)
app.include_router(alerts.router)
app.include_router(line_delays.router)
