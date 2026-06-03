from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from .database import engine, init_db
from .routers import analysis, catalog, submissions, tasks
from .seed import seed_defaults

app = FastAPI(title="铁道供电作业视频智能评价平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    with Session(engine) as session:
        seed_defaults(session)


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.include_router(catalog.router)
app.include_router(tasks.router)
app.include_router(submissions.router)
app.include_router(analysis.router)
