// Better Auth native session IDs remain authoritative; these fields are server-owned.
export const sessionFields = {
  securityRevision: { type: "number", required: true, input: false },
  absoluteDeadline: { type: "date", required: true, input: false },
  lastActivity: { type: "date", required: true, input: false },
} as const;

// The recovery coordinator is not a public Better Auth endpoint. Its mapped
// models allow a supported adapter transaction to share native auth storage.
const stringField = (column: string, required = true) => ({
  type: "string" as const, fieldName: column, required, input: false, returned: false,
});
const numberField = (column: string) => ({
  type: "number" as const, bigint: true, fieldName: column,
  required: true, input: false, returned: false,
});
const dateField = (column: string, required = true) => ({
  type: "date" as const, fieldName: column, required, input: false, returned: false,
});

export const lifecycleSchemaPlugin = {
  id: "carepulse-lifecycle",
  schema: {
    recoveryOperation: {
      modelName: "recovery_operations",
      fields: {
        organizationId: stringField("organization_id"),
        staffId: stringField("staff_id"),
        authUserId: stringField("auth_user_id"),
        purpose: stringField("purpose"),
        proofDigest: stringField("proof_digest"),
        proofReference: stringField("proof_reference"),
        proofExpiresAt: dateField("proof_expires_at"),
        requestFingerprint: stringField("request_fingerprint"),
        fingerprintKeyVersion: stringField("fingerprint_key_version"),
        capturedStaffRevision: numberField("captured_staff_revision"),
        capturedLifecycleRevision: numberField("captured_lifecycle_revision"),
        reservedSecurityRevision: numberField("reserved_security_revision"),
        state: stringField("state"),
        invalidated: { type: "boolean", fieldName: "invalidated", required: true, input: false, returned: false },
        authFence: numberField("auth_fence"),
        invalidationReference: stringField("invalidation_reference", false),
        createdAt: dateField("created_at"),
        authDoneAt: dateField("auth_done_at", false),
        updatedAt: dateField("updated_at"),
        closedAt: dateField("closed_at", false),
      },
    },
    authRecoveryCompletion: {
      modelName: "auth_recovery_completions",
      fields: {
        proofDigest: stringField("proof_digest"),
        authUserId: stringField("auth_user_id"),
        requestFingerprint: stringField("request_fingerprint"),
        reservedSecurityRevision: numberField("reserved_security_revision"),
        authFence: numberField("auth_fence"),
        completedAt: dateField("completed_at"),
      },
    },
  },
} as const;
