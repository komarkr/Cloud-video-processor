from fastapi import FastAPI
from app.config import settings

app = FastAPI(title="Cloud Video Processor")

@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "server_id": settings.server_id
    }