"""Request and response models for the generation API."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class Target(str, Enum):
    """What to build."""
    POWERBI_DESKTOP = "powerbi_desktop"
    FABRIC = "fabric"
    SEMANTIC_MODEL_ONLY = "semantic_model_only"


class Deploy(str, Enum):
    NONE = "none"
    FABRIC = "fabric"
    GITHUB = "github"
    DEVOPS = "devops"


class GenerateRequest(BaseModel):
    source_type: Optional[str] = "qlik"
    app_id: Optional[str] = None
    run_id: Optional[str] = None
    app_name: Optional[str] = None
    space_id: Optional[str] = None
    run_no: Optional[str] = None

    target: Target = Target.POWERBI_DESKTOP
    deploy: Deploy = Deploy.NONE
    deployment_type: Optional[str] = None

    # Supplied inline instead of fetched from the mapping store.
    mapping_result: Optional[Dict[str, Any]] = None

    # Fabric deployment
    workspace_id: Optional[str] = None
    fabric_group_id: Optional[str] = None
    fabric_access_token: Optional[str] = None
    folder_name: Optional[str] = None

    # Git deployment
    branch: Optional[str] = None
    git_branch: Optional[str] = None
    github_branch: Optional[str] = None
    repo: Optional[str] = None
    git_repo: Optional[str] = None
    github_repo: Optional[str] = None
    org: Optional[str] = None
    git_org: Optional[str] = None
    github_org: Optional[str] = None
    token: Optional[str] = None
    git_token: Optional[str] = None
    git_pat: Optional[str] = None
    github_pat: Optional[str] = None
    pat: Optional[str] = None
    commit_message: Optional[str] = None
    department_repo: Optional[str] = None

    # Keep the generated folder on disk (always true for desktop).
    write_to_disk: bool = True
    push_only: bool = False
    include_artifacts: bool = True
    index_only: bool = False
    offline_sample_data: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Normalize deployment_type -> deploy
            dt = str(data.get("deployment_type") or data.get("deploy") or "").lower().strip()
            if dt in ("direct_fabric", "direct-fabric", "fabric"):
                data["deploy"] = "fabric"
                if "target" not in data:
                    data["target"] = "fabric"
            elif dt in ("github", "git"):
                data["deploy"] = "github"
            elif dt in ("devops", "azure_devops"):
                data["deploy"] = "devops"
            elif dt in ("none", ""):
                data["deploy"] = "none"

            # Normalize fabric_group_id -> workspace_id
            fg_id = data.get("fabric_group_id")
            if fg_id and not str(fg_id).startswith("{{") and str(fg_id).strip():
                data["workspace_id"] = str(fg_id).strip()

            # Normalize git token / pat
            tok = data.get("git_pat") or data.get("github_pat") or data.get("git_token") or data.get("token") or data.get("pat")
            if tok and not str(tok).startswith("{{") and str(tok).strip():
                data["git_pat"] = str(tok).strip()
                data["token"] = str(tok).strip()

            # Normalize git repo / org
            r = data.get("repo") or data.get("git_repo") or data.get("github_repo")
            if r and not str(r).startswith("{{") and str(r).strip():
                data["repo"] = str(r).strip()

            # Normalize git branch. Callers send `git_branch`/`github_branch`
            # as often as `branch`; without this the value was dropped and
            # every push silently landed on config.BRANCH instead.
            b = data.get("branch") or data.get("git_branch") or data.get("github_branch")
            if b and not str(b).startswith("{{") and str(b).strip():
                data["branch"] = str(b).strip()

            o = data.get("org") or data.get("git_org") or data.get("github_org")
            if o and not str(o).startswith("{{") and str(o).strip():
                data["org"] = str(o).strip()

            # Cloud deployments push straight to the destination by default,
            # but every run still gets an on-disk artifact folder (see
            # generator.py) so multi-datasource / multi-report runs stay
            # inspectable after the fact — write_to_disk keeps its own
            # model default (True) here and is only overridden if the
            # caller explicitly opts out.
            if data.get("deploy") in ("fabric", "github", "devops") and "push_only" not in data:
                data["push_only"] = True

        return data


class DownloadRequest(BaseModel):
    """Retrieve a previously generated package as a .zip, for local use in
    Power BI Desktop. Looks up the package cached under `run_id` first
    (see package_store); if nothing is cached (different process, cache
    evicted, or `/generate` was never called for this run), the mapping
    result is re-fetched from Cosmos and generation is re-run to rebuild it.
    """

    run_id: str
    app_id: Optional[str] = None
    app_name: Optional[str] = None


class VisualNote(BaseModel):
    """Reported whenever a Qlik object has no exact Power BI equivalent."""

    object_id: Optional[str] = None
    sheet: Optional[str] = None
    title: Optional[str] = None
    qlik_type: str
    mapped_to: Optional[str] = None
    severity: str = Field(description="info | substituted | manual")
    reason: str
    suggestion: str


class GenerateResponse(BaseModel):
    status: str
    message: str
    target: str
    deploy: str
    app_id: Optional[str] = None
    app_name: Optional[str] = None
    run_id: Optional[str] = None
    output_path: Optional[str] = None
    file_count: int = 0
    summary: Dict[str, Any] = {}
    visual_notes: List[VisualNote] = []
    deployment: Dict[str, Any] = {}
    total_bytes: int = 0
    # Structural check of the emitted package.
    validation: Dict[str, Any] = {}
    # Every generated file: {pbip, semantic_model{}, report{}, other{}}.
    artifacts: Optional[Dict[str, Any]] = None
    semantic_model: Optional[Dict[str, Any]] = None
    report: Optional[Dict[str, Any]] = None
    # Present instead of `artifacts` when index_only is set.
    artifact_index: Optional[Dict[str, Any]] = None
