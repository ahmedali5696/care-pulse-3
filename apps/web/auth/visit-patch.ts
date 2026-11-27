// Validate the original signed bytes, including keys JSON.parse would otherwise collapse.
function strictObject(body: Uint8Array): Record<string, unknown> | null {
  try {
    const source = new TextDecoder("utf-8", { fatal: true }).decode(body);
    const parsed: unknown = JSON.parse(source);
    const scopes: (Set<string> | null)[] = [];
    const tokens = /("(?:[^"\\]|\\.)*")(\s*:)?|[{}\[\]]|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
    for (const token of source.matchAll(tokens)) {
      if (token[1]) {
        if (token[2]) {
          const key: string = JSON.parse(token[1]);
          const scope = scopes.at(-1);
          if (!scope || scope.has(key)) return null;
          scope.add(key);
        }
      } else if (token[0] === "{") scopes.push(new Set());
      else if (token[0] === "[") scopes.push(null);
      else if (token[0] === "}" || token[0] === "]") scopes.pop();
      else if (token[3] && !/^-?\d+$/.test(token[3])) return null;
    }
    return object(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function object(input: unknown): input is Record<string, unknown> {
  return input !== null && typeof input === "object" && !Array.isArray(input);
}

const revision = (input: unknown) => typeof input === "number" && Number.isInteger(input) && input >= 0;
const clinicalFields = ["examination", "findings", "diagnosis", "medical_notes", "treatment", "tests", "recommendation"];
const sourceFields = ["followup_decision", "expected_visit_revision"];
const sourceV2Fields = ["expected_source_decision_revision", "expected_followup_revision"];

function canonicalDate(input: unknown): boolean {
  if (typeof input !== "string" || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(input) || input.startsWith("0000")) return false;
  const day = new Date(`${input}T00:00:00Z`);
  return Number.isFinite(day.getTime()) && day.toISOString().slice(0, 10) === input;
}

function validReason(input: unknown): boolean {
  // Match the Python source reason validator's whitespace and Unicode rules.
  return typeof input === "string" && !/[\x00-\x1f\uD800-\uDFFF]/u.test(input) &&
    /[^\x20\x85\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u.test(input);
}

function validDecision(input: unknown): boolean {
  if (!object(input) || Object.keys(input).some(key => !["kind", "deadline_date", "reason"].includes(key))) return false;
  const { kind, deadline_date: deadline, reason } = input;
  if (reason != null && !validReason(reason)) return false;
  if (kind === "DEFAULT") return deadline == null;
  if (!validReason(reason)) return false;
  if (kind === "OPTED_OUT") return deadline == null;
  return kind === "DEADLINE" && canonicalDate(deadline);
}

function validSource(body: Record<string, unknown>): boolean {
  if (!validDecision(body.followup_decision) || !revision(body.expected_visit_revision)) return false;
  const keys = Object.keys(body);
  if (keys.some(key => !sourceFields.includes(key) && !sourceV2Fields.includes(key))) return false;
  return !sourceV2Fields.some(key => Object.hasOwn(body, key)) || sourceV2Fields.every(key => revision(body[key]));
}

function validClinical(body: Record<string, unknown>): boolean {
  if (!revision(body.expected_visit_revision)) return false;
  const fields = Object.keys(body).filter(key => key !== "expected_visit_revision");
  return fields.length > 0 && fields.every(key => clinicalFields.includes(key) &&
    (body[key] === null || (typeof body[key] === "string" && !/[\x00\uD800-\uDFFF]/u.test(body[key]))));
}

export function visitPatchPurpose(bytes: Uint8Array): string | null {
  const body = strictObject(bytes);
  if (!body) return null;
  if (Object.hasOwn(body, "followup_decision")) return validSource(body) ? "VISIT_FOLLOWUP" : null;
  return validClinical(body) ? "VISIT_CLINICAL" : null;
}
