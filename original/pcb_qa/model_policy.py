from __future__ import annotations

from dataclasses import dataclass


ROUTER_MODEL_DEFAULT = "gpt-5-mini"
PLANNER_MODEL_DEFAULT = "gpt-5-mini"
ANSWER_MODEL_DEFAULT = "gpt-5"
GENERAL_MODEL_DEFAULT = "gpt-5"


@dataclass(frozen=True)
class ModelPolicy:
    router_model: str = ROUTER_MODEL_DEFAULT
    planner_model: str = PLANNER_MODEL_DEFAULT
    answer_model: str = ANSWER_MODEL_DEFAULT
    general_model: str = GENERAL_MODEL_DEFAULT

