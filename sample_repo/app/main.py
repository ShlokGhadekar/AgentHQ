"""Application entry point."""

from fastapi import FastAPI

from app import auth, notes


def create_app() -> FastAPI:
    app = FastAPI(title="Notes API")
    app.include_router(auth.router)
    app.include_router(notes.router)
    return app


app = create_app()
