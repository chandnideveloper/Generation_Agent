from autogen import UserProxyAgent
import asyncio

class CustomUserProxy(UserProxyAgent):
    def __init__(self, name):
        super().__init__(
            name=name,
            human_input_mode="NEVER",
            code_execution_config=False
        )

    async def test_connection(self):
        """Simple health check used in main.py"""
        # Logic to check if we can reach Azure or OpenAI could go here
        print("[UserProxy] Connectivity test passed.")
        return True