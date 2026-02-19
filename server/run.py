import uvicorn
from app.config import settings
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.SERVER_HOST,
        port=80,
        reload=settings.DEBUG,
        log_level="info"
    )