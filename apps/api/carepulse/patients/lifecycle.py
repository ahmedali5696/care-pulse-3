"""One patient-row lock for archive and future appointment persistence."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from carepulse.identity.contracts import ContractError


@dataclass(frozen=True)
class PatientLifecycle:
    archived: bool
    revision: int


def lock_patient_lifecycle(
    connection: Connection, organization_id: UUID, patient_id: UUID
) -> PatientLifecycle:
    """Call inside the live-authorized tenant transaction before related writes."""
    patient = connection.execute(
        text(
            "SELECT archived, lifecycle_revision FROM public.patients "
            "WHERE organization_id = :organization_id AND id = :patient_id FOR UPDATE"
        ),
        {"organization_id": organization_id, "patient_id": patient_id},
    ).one_or_none()
    if patient is None:
        raise ContractError(404)
    return PatientLifecycle(
        archived=patient.archived, revision=patient.lifecycle_revision
    )


def lock_patient_for_new_appointment(
    connection: Connection, organization_id: UUID, patient_id: UUID
) -> PatientLifecycle:
    patient = lock_patient_lifecycle(connection, organization_id, patient_id)
    if patient.archived:
        raise ContractError(409)
    return patient


def lock_patient_for_existing_appointment(
    connection: Connection, organization_id: UUID, patient_id: UUID
) -> PatientLifecycle:
    """Use only after the caller verifies a permitted pre-existing Appointment ID."""
    return lock_patient_lifecycle(connection, organization_id, patient_id)
