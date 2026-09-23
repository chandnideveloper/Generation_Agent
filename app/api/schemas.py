"""Unified Migration Request and Response Models.

Standardizes public API contract for POST /migrate.
Zero credential leakage.
"""

import os
from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, Field, model_validator


class GitHubTargetConfig(BaseModel):
    owner: str = Field(..., min_length=1, description="GitHub user or organization name")
    repo_name: str = Field(..., min_length=1, description="GitHub repository name")
    branch: str = Field(default="main", min_length=1, description="Target branch")
    git_pat: str = Field(..., min_length=1, description="GitHub Personal Access Token")
    directory: Optional[str] = Field(
        default_factory=lambda: os.getenv("DESTINATION_BASE", "test-workspace"),
        description="Target repository subfolder (e.g. 'test-workspace') synchronized with Fabric",
    )
    folder: Optional[str] = Field(
        default=None,
        description="Alias for directory (e.g. 'test-workspace')",
    )

    @property
    def target_directory(self) -> str:
        res = self.folder or self.directory or os.getenv("DESTINATION_BASE", "test-workspace")
        if str(res).strip().lower() in ("test-workspace", "test workspace", "test_workspace"):
            return os.getenv("DESTINATION_BASE", "test-workspace")
        return res

    def __repr__(self) -> str:
        return (
            f"GitHubTargetConfig(owner='{self.owner}', repo_name='{self.repo_name}', "
            f"branch='{self.branch}', directory='{self.target_directory}', git_pat='***MASKED***')"
        )

    def __str__(self) -> str:
        return self.__repr__()


class TargetConfig(BaseModel):
    platform: Literal["fabric"] = Field(
        default="fabric", description="Target platform. Must be 'fabric'"
    )
    deployment_type: Optional[str] = Field(default="git", description="Deployment type (e.g. 'git')")
    github: GitHubTargetConfig


class UnifiedMigrateRequest(BaseModel):
    source_type: Literal["qlik", "tableau"] = Field(
        ..., description="Source platform: 'qlik' or 'tableau'"
    )
    run_id: str = Field(..., min_length=1, description="Unique migration run identifier")
    source: Dict[str, Any] = Field(
        ..., description="Source-specific parameters (app_id/space_id or workbook_id/project_id)"
    )
    mapping_result: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional inline Contract 2.0 mapping document"
    )
    target: TargetConfig

    @model_validator(mode="after")
    def validate_source_fields(self) -> "UnifiedMigrateRequest":
        st = self.source_type.lower()
        if st == "qlik":
            if not self.source.get("app_id"):
                raise ValueError("source.app_id is required when source_type is 'qlik'")
        elif st == "tableau":
            if not self.source.get("workbook_id") or not self.source.get("project_id"):
                raise ValueError(
                    "source.workbook_id and source.project_id are required when source_type is 'tableau'"
                )
        return self

    def sanitized_dict(self) -> Dict[str, Any]:
        """Return a copy of the request payload with sensitive tokens masked."""
        d = self.model_dump()
        if "target" in d and "github" in d["target"]:
            d["target"]["github"]["git_pat"] = "***MASKED***"
        return d


class DeploymentDetails(BaseModel):
    platform: Literal["github"] = "github"
    status: str = Field(..., description="'completed', 'failed', or 'partial'")
    owner: str = Field(..., description="GitHub organization or user")
    repo_name: str = Field(..., description="GitHub repository name")
    branch: str = Field(..., description="Target branch")
    commit_sha: Optional[str] = Field(default=None, description="Git commit SHA if created")
    commit_url: Optional[str] = Field(default=None, description="Web URL to the commit or repo")
    details: Dict[str, Any] = Field(default_factory=dict, description="Additional deployment telemetry")


class ArtifactsSummary(BaseModel):
    semantic_model: Dict[str, Any] = Field(
        default_factory=dict, description="Generated semantic model artifacts / metadata"
    )
    report: Dict[str, Any] = Field(
        default_factory=dict, description="Generated report (PBIR) artifacts / metadata"
    )


class UnifiedMigrateResponse(BaseModel):
    status: Literal["success", "error"] = "success"
    source_type: Literal["qlik", "tableau"]
    run_id: str
    target: Literal["fabric"] = "fabric"
    artifacts: ArtifactsSummary
    deployment: DeploymentDetails
    details: Dict[str, Any] = Field(default_factory=dict)


class UnifiedErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    source_type: Optional[str] = None
    run_id: Optional[str] = None
    service: str = "unified-generation-agent"
    error_code: str
    message: str
    details: Optional[Dict[str, Any]] = None
