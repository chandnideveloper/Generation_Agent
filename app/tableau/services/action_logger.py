import os
import logging
import asyncio
import uuid
import datetime
from typing import Optional, Dict, Any
from openai import AsyncAzureOpenAI
from app.tableau.core.config import Config, mongo_db
from app.tableau.core.logging_utils import log_error, log_info, log_warning, run_background_task, store_deadletter
from app.tableau.core.http_client import get_resilient_client
from app.tableau.api.schemas import CosmosActivityRecord

logger = logging.getLogger(__name__)

class ActionLogger:
    def __init__(self):
        self.agent_name = os.getenv("AGENT_NAME", "BI Engineering Agent")
        self.project_name = None
        
        self.llm_client = None
        if Config.AZURE_OPENAI_API_KEY and Config.AZURE_OPENAI_ENDPOINT:
            self.llm_client = AsyncAzureOpenAI(
                api_key=Config.AZURE_OPENAI_API_KEY,
                api_version=Config.AZURE_OPENAI_API_VERSION,
                azure_endpoint=Config.AZURE_OPENAI_ENDPOINT
            )
            self.deployment_name = getattr(Config, "AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

    async def _generate_llm_content(self, system_prompt: str, user_prompt: str) -> str:
        if not self.llm_client:
            return str(user_prompt)
        try:
            response = await self.llm_client.chat.completions.create(
                model=self.deployment_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            log_error(f"[ActionLogger] LLM failed: {e}")
            return str(user_prompt)

    async def send_activity_to_api(self, project_id, workbook_id, run_id, technical_message, token=None, project_name=None):
        """Generates a summary and sends it to Cosmos DB / MongoDB."""
        summary = await self._generate_llm_content(
            "Summarize this action in <100 chars:", technical_message
        )
        from app.tableau.core.logging_utils import get_project_name
        resolved_proj_name = (
            project_name
            or self.project_name
            or get_project_name()
            or os.getenv("PROJECT_NAME")
            or "Unknown"
        )
        payload = {
            "id": str(uuid.uuid4()),
            "project_name": resolved_proj_name,
            "run_id": run_id,
            "run_no": run_id,
            "status": "success",
            "created_at": datetime.datetime.utcnow().isoformat(),
            "activity_summary": summary,
            "project_id": project_id,
            "workbook_id": workbook_id,
            "agent_name": self.agent_name,
            "type": "agent_activity",
            "payload": {}
        }
        run_background_task(self._do_post(payload), task_name="send_activity_to_api")

    async def send_error_to_api(self, project_id, workbook_id, run_id, technical_message, error_detail=None, token=None, project_name=None):
        """Logs an error event to Cosmos DB / MongoDB with an LLM-generated summary."""
        summary = await self._generate_llm_content(
            "Summarize this error in <100 chars:", technical_message
        )
        from app.tableau.core.logging_utils import get_project_name
        resolved_proj_name = (
            project_name
            or self.project_name
            or get_project_name()
            or os.getenv("PROJECT_NAME")
            or "Unknown"
        )
        payload = {
            "id": str(uuid.uuid4()),
            "project_name": resolved_proj_name,
            "run_id": run_id,
            "run_no": run_id,
            "status": "error",
            "created_at": datetime.datetime.utcnow().isoformat(),
            "activity_summary": summary,
            "error_detail": str(error_detail) if error_detail else technical_message,
            "project_id": project_id,
            "workbook_id": workbook_id,
            "agent_name": self.agent_name,
            "type": "agent_activity",
            "payload": {}
        }
        run_background_task(self._do_post(payload), task_name="send_error_to_api")

    async def _do_post(self, payload):
        try:
            try:
                validated_payload = CosmosActivityRecord(**payload).model_dump()
            except Exception as ve:
                log_warning(f"[ActionLogger] Activity payload failed contract validation: {ve}. Sending raw.")
                validated_payload = payload

            # Direct MongoDB insert strictly into QT2F_Tableau.agent_actions and QT2F_Tableau.activities
            mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
            if mongo_uri and mongo_uri.startswith("mongodb"):
                try:
                    import pymongo
                    client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
                    client["QT2F_Tableau"]["agent_actions"].insert_one(dict(validated_payload))
                    client["QT2F_Tableau"]["activities"].insert_one(dict(validated_payload))
                except Exception as pe:
                    logger.warning(f"[ActionLogger] Direct MongoDB insert failed: {pe}")

            if mongo_db is not None:
                await mongo_db["agent_actions"].insert_one(validated_payload)
                log_info(f"[ActionLogger] Successfully posted action to QT2F_Tableau.agent_actions for {validated_payload.get('agent_name')}")
            else:
                log_warning("[ActionLogger] MONGODB_URL not configured. Skipping save.")
        except Exception as e:
            err_msg = f"Failed to post to MongoDB: {e}"
            log_error(f"[ActionLogger] {err_msg}")
            store_deadletter("mongodb://agent_actions", payload, err_msg)