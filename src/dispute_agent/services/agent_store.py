from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from dispute_agent.agent_schemas import AgentResultEnvelope
from dispute_agent.ids import new_id
from dispute_agent.models import AgentOutput, utc_now
from dispute_agent.prompts import PROMPT_VERSION
from dispute_agent.serialization import content_hash, jsonable


PayloadT = TypeVar("PayloadT", bound=BaseModel)


class AgentOutputStore:
    def find(
        self,
        session: Session,
        *,
        case_run_id: str,
        role: str,
        output_type: str,
        input_fingerprint: str,
    ) -> AgentOutput | None:
        return session.scalar(
            select(AgentOutput).where(
                AgentOutput.case_run_id == case_run_id,
                AgentOutput.role == role,
                AgentOutput.output_type == output_type,
                AgentOutput.input_fingerprint == input_fingerprint,
            )
        )

    def persist(
        self,
        session: Session,
        *,
        case_id: str,
        case_run_id: str,
        role: str,
        output_type: str,
        payload: BaseModel,
        input_data: dict[str, Any],
        model_name: str,
        usage: dict[str, Any],
        input_fingerprint: str | None = None,
    ) -> AgentOutput:
        fingerprint = input_fingerprint or content_hash(input_data)
        existing = self.find(
            session,
            case_run_id=case_run_id,
            role=role,
            output_type=output_type,
            input_fingerprint=fingerprint,
        )
        if existing:
            return existing
        payload_json = jsonable(payload.model_dump(mode="json"))
        output = AgentOutput(
            id=new_id("agentout"),
            dispute_id=case_id,
            case_run_id=case_run_id,
            role=role,
            output_type=output_type,
            schema_version="1.0.0",
            prompt_version=PROMPT_VERSION,
            model_name=model_name,
            input_fingerprint=fingerprint,
            payload_json=payload_json,
            usage_json=usage,
            content_sha256=content_hash(payload_json),
        )
        session.add(output)
        session.flush()
        return output

    @staticmethod
    def envelope(output: AgentOutput) -> AgentResultEnvelope:
        return AgentResultEnvelope(
            output_id=output.id,
            case_id=output.dispute_id,
            case_run_id=output.case_run_id,
            role=output.role,  # type: ignore[arg-type]
            output_type=output.output_type,  # type: ignore[arg-type]
            schema_version=output.schema_version,
            prompt_version=output.prompt_version,
            model_name=output.model_name,
            input_fingerprint=output.input_fingerprint,
            payload=output.payload_json,
            usage=output.usage_json,
            content_sha256=output.content_sha256,
            created_at=output.created_at or utc_now(),
        )


def estimate_usage(input_data: dict[str, Any], output: BaseModel) -> dict[str, int]:
    input_chars = len(str(jsonable(input_data)))
    output_chars = len(output.model_dump_json())
    return {
        "input_tokens_estimate": max(1, input_chars // 4),
        "output_tokens_estimate": max(1, output_chars // 4),
        "total_tokens_estimate": max(1, (input_chars + output_chars) // 4),
    }
