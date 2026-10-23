"""B-01 field and tenant projection boundaries."""

from uuid import uuid4

import pytest
from carepulse.identity.contracts import (
    ContractError,
    ResourceFields,
    permitted_write_fields,
    project_visible_resource,
)


def test_receptionist_projection_never_expands_nested_clinical_relations():
    tenant = uuid4()
    fields = ResourceFields(
        readable={"RECEPTIONIST": frozenset({"name_parts", "visits"})},
        writable={"RECEPTIONIST": frozenset({"name_parts"})},
    )
    patient = {
        "organization_id": tenant,
        "name_parts": ["Synthetic", "Patient", "Record"],
        "visits": [{"diagnosis": "restricted"}],
    }
    with pytest.raises(ContractError) as nested:
        project_visible_resource(patient, tenant, "RECEPTIONIST", fields)
    assert nested.value.status_code == 403
    assert project_visible_resource(
        {**patient, "visits": None}, tenant, "RECEPTIONIST", fields
    ) == {"name_parts": ["Synthetic", "Patient", "Record"], "visits": None}


def test_nonvisible_tenant_and_authority_fields_are_rejected():
    tenant = uuid4()
    fields = ResourceFields(
        readable={"RECEPTIONIST": frozenset({"name_parts"})},
        writable={"RECEPTIONIST": frozenset({"name_parts"})},
    )
    with pytest.raises(ContractError) as foreign:
        project_visible_resource(
            {"organization_id": uuid4()}, tenant, "RECEPTIONIST", fields
        )
    assert foreign.value.status_code == 404
    with pytest.raises(ContractError) as authority:
        permitted_write_fields(
            {"name_parts": ["A", "B", "C"], "role": "DOCTOR_OWNER"},
            "RECEPTIONIST",
            fields,
        )
    assert authority.value.status_code == 422
    with pytest.raises(ContractError) as nested_write:
        permitted_write_fields(
            {"name_parts": [{"diagnosis": "restricted"}]}, "RECEPTIONIST", fields
        )
    assert nested_write.value.status_code == 422
