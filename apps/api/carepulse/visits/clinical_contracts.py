"""Closed clinical and source decision command families for signed Visit dispatch."""

import json
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from carepulse.identity.contracts import ContractError
from carepulse.visits.eligibility_contracts import (
    FollowupDecisionPatch,
    FollowupDecisionPatchV2,
    Revision,
    _reason,
)


class ClinicalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_visit_revision: Revision
    examination: StrictStr | None = None
    findings: StrictStr | None = None
    diagnosis: StrictStr | None = None
    medical_notes: StrictStr | None = None
    treatment: StrictStr | None = None
    tests: StrictStr | None = None
    recommendation: StrictStr | None = None

    @field_validator(
        "examination",
        "findings",
        "diagnosis",
        "medical_notes",
        "treatment",
        "tests",
        "recommendation",
    )
    @classmethod
    def valid_text(cls, narrative: str | None) -> str | None:
        if narrative is not None and any(
            character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF
            for character in narrative
        ):
            raise ValueError("Invalid clinical text")
        return narrative

    @model_validator(mode="after")
    def explicit_fields(self):
        if not self.model_fields_set - {"expected_visit_revision"}:
            raise ValueError("At least one clinical field is required")
        return self


class TerminalRevisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_appointment_revision: Revision
    expected_visit_revision: Revision
    expected_claim_revision: Revision | None = None
    expected_source_decision_revision: Revision | None = None
    expected_followup_revision: Revision | None = None
    expected_authorization_revision: Revision | None = None


class CompleteVisit(TerminalRevisions):
    examination_occurred: Literal[True]

    @field_validator("examination_occurred", mode="before")
    @classmethod
    def positive_assertion(cls, assertion: object) -> object:
        if assertion is not True:
            raise ValueError("Examination must be explicitly attested")
        return assertion


class ExceptionalCancel(TerminalRevisions):
    reason: StrictStr

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, reason: str) -> str:
        return _reason(reason)


VisitPatch = ClinicalPatch | FollowupDecisionPatch | FollowupDecisionPatchV2


def _unique_json_pairs(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("Duplicate request key")
        fields[key] = value
    return fields


def strict_body(payload: bytes) -> dict:
    try:
        body = json.loads(
            payload,
            object_pairs_hook=_unique_json_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        if not isinstance(body, dict):
            raise TypeError("Expected an object")
        return body
    except (TypeError, ValueError, UnicodeError, RecursionError) as invalid:
        raise ContractError(422) from invalid


def parse_visit_patch(payload: bytes) -> VisitPatch:
    body = strict_body(payload)
    if "followup_decision" in body:
        v2_fields = {"expected_source_decision_revision", "expected_followup_revision"}
        contract = (
            FollowupDecisionPatchV2
            if body.keys() & v2_fields
            else FollowupDecisionPatch
        )
    else:
        contract = ClinicalPatch
    try:
        return contract.model_validate(body)
    except ValidationError as invalid:
        raise ContractError(422) from invalid


def visit_patch_purpose(command: VisitPatch) -> str:
    return "VISIT_CLINICAL" if isinstance(command, ClinicalPatch) else "VISIT_FOLLOWUP"


def signed_visit_patch_purpose(payload: bytes) -> str:
    return visit_patch_purpose(parse_visit_patch(payload))
