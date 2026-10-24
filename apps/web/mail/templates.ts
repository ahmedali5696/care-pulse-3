export type TemplateKind = "invitation" | "reset";

export function renderMail(kind: TemplateKind, destination?: string) {
  if (kind !== "invitation" && kind !== "reset") throw new Error("Invalid mail template");
  const subject = `[SYNTHETIC ONLY] CarePulse ${kind}`;
  const link = destination ?? `https://carepulse.invalid/non-operational/${kind}`;
  const note = destination ? "" : "\nNo account or token is created.";
  const description = destination ? "Synthetic account link" : "Non-operational test link";
  const safeLink = link.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
  return {
    subject,
    text: `${subject}\n${description}: ${link}${note}`,
    html: `<p>${subject}</p><p>${description}: <a href="${safeLink}">${safeLink}</a></p>${destination ? "" : "<p>No account or token is created.</p>"}`,
  };
}
