"""Unified Generation Agent — Single FastAPI service running exclusively on port 5000.

Consolidates Qlik Generation Agent, Tableau Generation Agent, and Unified Gateway
into a single process.
Zero inter-service HTTP calls.
Direct in-process Python invocation.
"""

import sys
from pathlib import Path

# Add project root to sys.path so modules resolve cleanly
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Provide package compatibility mappings for internal sub-packages
import app.qlik
import app.tableau
sys.modules["app.qlik"] = app.qlik
sys.modules["app.tableau"] = app.tableau

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load unified environment configuration
load_dotenv(override=True)

from app.api.routes import router as migration_router


def create_app() -> FastAPI:
    application = FastAPI(
        title="Unified Generation Agent",
        version="1.0.0",
        description="Unified Qlik and Tableau to Fabric / Power BI Generation Service.",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(migration_router)
    return application


app = create_app()

if __name__ == "__main__":
    import uvicorn
    import os

    port = int(os.getenv("PORT", "5000"))
    print(f"============================================================")
    print(f" Starting Unified Generation Agent on http://0.0.0.0:{port}")
    print(f" Single service - Qlik and Tableau generation in one process")
    print(f"============================================================")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
