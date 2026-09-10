"""AI assessment: strict schema, prompt and OpenRouter client."""

from news_monitor.ai.client import AIAssessor, AIRequestError, OpenRouterAssessor
from news_monitor.ai.prompt import SYSTEM_PROMPT, build_messages, build_user_prompt
from news_monitor.ai.schema import AIAssessment, AIAssessmentError, parse_ai_assessment

__all__ = [
    "AIAssessment",
    "AIAssessmentError",
    "AIAssessor",
    "AIRequestError",
    "OpenRouterAssessor",
    "SYSTEM_PROMPT",
    "build_messages",
    "build_user_prompt",
    "parse_ai_assessment",
]
