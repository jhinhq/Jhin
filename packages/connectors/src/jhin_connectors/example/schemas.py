"""Tool input/output models for the example connector.

Inputs always ``forbid`` extra fields (strict schemas, plan 21.4) and always
carry ``connection_id`` — the gateway matches it against grant scopes.
"""

from pydantic import BaseModel, ConfigDict, Field


class PingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(description="The example connection to use.")
    message: str = Field(default="ping", max_length=200)


class PingOutput(BaseModel):
    reply: str
    connection_name: str
