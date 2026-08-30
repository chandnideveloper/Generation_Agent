"""Configuration for the Qlik Generation Agent.

Nothing here raises at import. The previous version called
`raise ValueError` at module scope for Azure OpenAI, DevOps PAT and repo, so
the service could not start at all without credentials it no longer needs —
generation is fully deterministic and uses no LLM.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


class Config:
    # --- upstream -------------------------------------------------------
    # Mapping results come from the MongoDB microservice (az-repo-mongodb-vl).
    MONGO_API_URL = (
        os.getenv("MONGO_API_URL")
        or os.getenv("MAPPING_API_URL")
        or os.getenv("BASE_API_URL")
        or "https://qlik-tableau-mapping.onrender.com"
    ).rstrip("/")
    HTTP_CONNECT_TIMEOUT = _int("HTTP_CONNECT_TIMEOUT", 30)
    HTTP_READ_TIMEOUT = _int("HTTP_READ_TIMEOUT", 180)

    # --- output ---------------------------------------------------------
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "generated")
    # Power BI canvas. Qlik grid coordinates are scaled onto this.
    CANVAS_WIDTH = _int("CANVAS_WIDTH", 1280)
    CANVAS_HEIGHT = _int("CANVAS_HEIGHT", 720)

    # --- semantic model -------------------------------------------------
    TMDL_COMPATIBILITY_LEVEL = _int("TMSL_COMPATIBILITY_LEVEL", 1567)
    TMDL_CULTURE = os.getenv("TMSL_CULTURE", "en-US")
    TMDL_SOURCE_QUERY_CULTURE = os.getenv("TMSL_SOURCE_QUERY_CULTURE", "en-US")
    DESKTOP_VERSION = os.getenv(
        "TMSL_DESKTOP_VERSION", "2.146.1133.0 (25.08)"
    )

    # --- Fabric ---------------------------------------------------------
    FABRIC_API = os.getenv("FABRIC_API", "https://api.fabric.microsoft.com/v1")

    # --- Azure DevOps ---------------------------------------------------
    DEVOPS_PAT = os.getenv("AZURE_DEVOPS_PAT", "")
    DEVOPS_ORG = os.getenv("AZURE_ORGANIZATION", "")
    DEVOPS_PROJECT = os.getenv("AZURE_PROJECT", "")
    DEVOPS_REPO = os.getenv("AZURE_REPO", "")
    DEVOPS_API_VERSION = os.getenv("API_VERSION", "7.0")

    # --- GitHub ---------------------------------------------------------
    GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")
    GITHUB_PAT = os.getenv("GITHUB_PAT", "")
    GITHUB_ORG = os.getenv("GITHUB_ORG", "")
    GITHUB_REPO = os.getenv("GITHUB_REPO", "")

    BRANCH = os.getenv("BRANCH", "main")
    DESTINATION_BASE = os.getenv("DESTINATION_BASE", "pbip-deploy-tmdl")

    # --- service --------------------------------------------------------
    PORT = _int("PORT", 5000)
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    AGENT_NAME = os.getenv("AGENT_NAME", "Generation")

    @classmethod
    def devops_ready(cls) -> bool:
        return all([cls.DEVOPS_PAT, cls.DEVOPS_ORG, cls.DEVOPS_PROJECT, cls.DEVOPS_REPO])

    @classmethod
    def github_ready(cls) -> bool:
        return all([cls.GITHUB_PAT, cls.GITHUB_ORG, cls.GITHUB_REPO])


config = Config()
