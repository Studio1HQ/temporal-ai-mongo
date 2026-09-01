"""OpenAI-compatible client for AI operations (works with any OpenAI wire-protocol endpoint: OpenAI, Nebius, vLLM, LM Studio, ...)."""

import json
import logging
from typing import Any, Dict, Optional

from openai import AsyncOpenAI, OpenAI

from utils.config import config

logger = logging.getLogger(__name__)


class OpenAIClient:
    def __init__(self):
        self.client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
        self.async_client = AsyncOpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
        self.model_id = config.OPENAI_MODEL

    async def analyze_transaction(self, prompt: str) -> Dict[str, Any]:
        try:
            response = await self.async_client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": _SYSTEM_MESSAGE},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1000,
                top_p=0.9,
                response_format={"type": "json_object"},
            )
            return self._parse_llm_response(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Error analyzing transaction with OpenAI: {e}")
            raise

    def analyze_transaction_sync(self, prompt: str) -> Dict[str, Any]:
        try:
            response = self.client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": _SYSTEM_MESSAGE},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1000,
                top_p=0.9,
                response_format={"type": "json_object"},
            )
            return self._parse_llm_response(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Error analyzing transaction with OpenAI (sync): {e}")
            raise

    async def generate_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
    ) -> str:
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            response = await self.async_client.chat.completions.create(
                model=self.model_id,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Error generating completion with OpenAI: {e}")
            raise

    def _parse_llm_response(self, response_text: str) -> Dict[str, Any]:
        try:
            if "```json" in response_text:
                start_idx = response_text.find("```json") + 7
                end_idx = response_text.find("```", start_idx)
                parsed = json.loads(response_text[start_idx:end_idx].strip())
            else:
                parsed = json.loads(response_text)

            if parsed.get("decision") == "flag":
                parsed["decision"] = "escalate"
            if parsed.get("decision") not in {"approve", "reject", "escalate"}:
                logger.warning(f"Invalid decision: {parsed.get('decision')}. Defaulting to escalate.")
                parsed["decision"] = "escalate"

            if isinstance(parsed.get("confidence"), str):
                parsed["confidence"] = float(parsed["confidence"].rstrip("%"))
            elif "confidence" not in parsed:
                parsed["confidence"] = 50

            parsed.setdefault("reasoning", "No reasoning provided")
            parsed.setdefault("risk_factors", [])
            parsed.setdefault("compliance_notes", "")
            return parsed
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON response: {e}")
            return {
                "decision": "escalate",
                "confidence": 50,
                "reasoning": response_text,
                "risk_factors": [],
                "compliance_notes": "",
            }


_SYSTEM_MESSAGE = """You are a financial transaction analyzer.
Always respond with a JSON object containing:
- decision: "approve", "reject", or "escalate"
- confidence: number between 0 and 100
- reasoning: explanation of your decision
- risk_factors: array of identified risks
- compliance_notes: relevant compliance considerations
"""


openai_client = OpenAIClient()
