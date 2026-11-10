"""FastAPI application – health endpoint only for milestone 0."""

from fastapi import FastAPI

app = FastAPI(title="PaperRelay", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}
