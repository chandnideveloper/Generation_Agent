from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

class TmdlRequest(BaseModel):
    folder_name: str
    project_id: str
    workbook_id: str
    run_id: str
    group_id: Optional[str] = None
    department_repo: Optional[str] = None  # Change this line
    token: Optional[str] = None
    data_layer_triggered: Optional[bool] = False
    deployment_type: Optional[str] = None
    branch: Optional[str] = None
    fabric_access_token: Optional[str] = None
    git_org: Optional[str] = None
    git_repo: Optional[str] = None
    git_pat: Optional[str] = None

class CosmosLogRecord(BaseModel):
    project_name: str
    project_id: str
    run_id: str
    workbook_id: str
    agent_name: str = "Generation Agent"
    log_level: str
    message: str
    timestamp: str
    details: Dict[str, Any] = Field(default_factory=dict)

class CosmosActivityRecord(BaseModel):
    id: str
    run_id: str
    status: str
    created_at: str
    activity_summary: str
    error_detail: Optional[str] = None
    project_id: str
    workbook_id: str
    agent_name: str
    type: str = "agent_activity"

class CosmosGenerationRecord(BaseModel):
    run_id: str
    project_id: str
    workbook_id: str
    project_name: str
    status: str
    payload: Dict[str, Any] = Field(default_factory=dict)
