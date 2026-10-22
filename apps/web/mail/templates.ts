export type TemplateKind = "invitation" | "reset";

export function renderMail(kind: TemplateKind) {
  if (kind !== "invitation" && kind !== "reset") throw new Error("Invalid mail template");
  const subject = `[SYNTHETIC ONLY] CarePulse ${kind}`;
  const link = `https://carepulse.invalid/non-operational/${kind}`;
  return {
    subject,
    text: `${subject}\nNon-operational test link: ${link}\nNo account or token is created.`,
    html: `<p>${subject}</p><p>Non-operational test link: <a href="${link}">${link}</a></p><p>No account or token is created.</p>`,
  };
}
