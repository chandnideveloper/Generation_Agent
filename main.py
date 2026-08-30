"""Qlik Generation Agent — FastAPI entry point.

Turns a mapping-agent Contract 2.0 payload into Power BI artifacts:

    powerbi_desktop      PBIP folder that opens in Power BI Desktop
    fabric               same artifacts, pushed to a Fabric workspace
    semantic_model_only  TMDL model without a report

and deploys via `none`, `fabric`, `github` or `devops`.

Generation is entirely deterministic — no LLM is involved, so the same
mapping payload always produces byte-identical files.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import config
from app.util.logging_utils import get_logger, setup_logging

logger = get_logger(__name__)


def create_app() -> FastAPI:
    setup_logging()
    application = FastAPI(
        title="Qlik Generation Agent",
        version="3.0.0",
        description="Qlik Sense to Power BI / Microsoft Fabric artifact generation.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router)

    logger.info(
        "Generation agent ready — mapping=%s output=%s github=%s devops=%s",
        config.MONGO_API_URL, config.OUTPUT_DIR,
        config.github_ready(), config.devops_ready(),
    )
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
