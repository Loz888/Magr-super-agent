"""Typed owner trial ceilings; a value alone never grants budget or execution."""

from typing import Annotated, Any

from pydantic import BeforeValidator

OWNER_TRIAL_PROJECT_LIMITS = (2_000_000, 10_000_000)


def validate_owner_trial_limit(value: Any) -> int:
    if type(value) is not int or value not in OWNER_TRIAL_PROJECT_LIMITS:
        raise ValueError("An explicitly supported integer owner trial ceiling is required")
    return value


OwnerTrialProjectLimit = Annotated[int, BeforeValidator(validate_owner_trial_limit)]
