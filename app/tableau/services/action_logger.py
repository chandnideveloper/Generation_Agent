import asyncio
import uuid
import datetime
from typing import Optional, Dict, Any
from openai import AsyncAzureOpenAI
from app.tableau.core.config import Config, mongo_db
from app.tableau.core.logging_utils import log_error, log_info, log_warning, run_background_task, store_deadletter
from app.tableau.core.http_client import get_resilient_client
from app.tableau.api.schemas import CosmosActivityRecord

class ActionLogger:
    def __init__(self):
        self.agent_name = "GenerationAgent"
        
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

    async def send_activity_to_api(self, project_id, workbook_id, run_id, technical_message, token=None):
        """Generates a summary and sends it to Cosmos DB."""
        summary = await self._generate_llm_content(
            "Summarize this action in <100 chars:", technical_message
        )
        payload = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "status": "success",
            "created_at": datetime.datetime.utcnow().isoformat(),
            "activity_summary": summary,
            "project_id": project_id,
            "workbook_id": workbook_id,
            "agent_name": self.agent_name,
            "type": "agent_activity"
        }
        run_background_task(self._do_post(payload), task_name="send_activity_to_api")

    async def send_error_to_api(self, project_id, workbook_id, run_id, technical_message, error_detail=None, token=None):
        """Logs an error event to Cosmos DB with an LLM-generated summary."""
        summary = await self._generate_llm_content(
            "Summarize this error in <100 chars:", technical_message
        )
        payload = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "status": "error",
            "created_at": datetime.datetime.utcnow().isoformat(),
            "activity_summary": summary,
            "error_detail": str(error_detail) if error_detail else technical_message,
            "project_id": project_id,
            "workbook_id": workbook_id,
            "agent_name": self.agent_name,
            "type": "agent_activity"
        }
        run_background_task(self._do_post(payload), task_name="send_error_to_api")

    async def _do_post(self, payload):
        try:
            try:
                validated_payload = CosmosActivityRecord(**payload).model_dump()
            except Exception as ve:
                log_warning(f"[ActionLogger] Activity payload failed contract validation: {ve}. Sending raw.")
                validated_payload = payload

            if mongo_db is not None:
                await mongo_db["activities"].insert_one(validated_payload)
                log_info(f"[ActionLogger] Successfully posted activity to MongoDB")
            else:
                log_warning("[ActionLogger] MONGODB_URL not configured. Skipping save.")
        except Exception as e:
            err_msg = f"Failed to post to MongoDB: {e}"
            log_error(f"[ActionLogger] {err_msg}")
            store_deadletter("mongodb://activities", payload, err_msg)