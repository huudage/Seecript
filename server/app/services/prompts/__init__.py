"""System prompts for each business module.

Each prompt is kept in its own module so we can iterate independently and version-control
changes per-module. All prompts are designed to elicit a strict JSON reply that matches the
corresponding Pydantic schema in `app/schemas.py`.
"""
from .persona import PERSONA_SYSTEM_PROMPT  # noqa: F401
from .skeleton import SKELETON_SYSTEM_PROMPT  # noqa: F401
from .seo import SEO_SYSTEM_PROMPT  # noqa: F401
from .comments import COMMENTS_SYSTEM_PROMPT  # noqa: F401
from .qa import QA_SYSTEM_PROMPT  # noqa: F401
from .script import SCRIPT_SYSTEM_PROMPT  # noqa: F401
