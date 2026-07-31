from fastapi import FastAPI

from dashboard_api.routers import health, route_day, stop_day

app = FastAPI(title="Transit Dashboard API")

app.include_router(health.router)
app.include_router(stop_day.router)
app.include_router(route_day.router)