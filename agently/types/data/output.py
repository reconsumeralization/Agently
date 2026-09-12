"""Typed output production declarations; returned business values stay strings."""

from typing import Annotated, TypeAlias

from pydantic import Field

LongContent: TypeAlias = Annotated[str, Field(json_schema_extra={"long_content": True})]
"""An explicit long-form string producer, independent of auto_continue.

Use ``(LongContent, "writing requirements")`` in Agently output schemas or
``body: LongContent = Field(description="writing requirements")`` in Pydantic.
The produced value is a normal str, not a wrapper or a new JSON data kind.
"""

__all__ = ["LongContent"]
