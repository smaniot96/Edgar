from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import API_TITLE, API_VERSION
from routers.campaign import router as campaign_router
from routers.character import router as character_router
from routers.health import router as health_router
from routers.session import router as session_router
from routers.seed import router as seed_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    from dependencies import close_redis
    await close_redis()


app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan)

app.include_router(health_router)
app.include_router(campaign_router, prefix="/api")
app.include_router(session_router, prefix="/api")
app.include_router(character_router, prefix="/api")
app.include_router(seed_router, prefix="/api")


@app.get("/")
def read_root():
    return {"message": "Hello from api!"}

if __name__ == "__main__":
    # Run the FastAPI app using uvicorn if this script is executed directly
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
