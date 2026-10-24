// Synthetic Mailpit only. Production delivery/lifecycle remains later-owner work.
import nodemailer from "nodemailer";
import { loadMailSettings } from "./config.ts";
import { renderMail, type TemplateKind } from "./templates.ts";

export async function sendSyntheticMail(kind: TemplateKind, recipient: string, link?: string) {
  const config = loadMailSettings();
  if (!config.smtp ||
      !/^[a-z0-9-]+@example\.invalid$/.test(recipient)) {
    throw new Error("Synthetic mail unavailable");
  }
  const transport = nodemailer.createTransport({
    host: "mail", port: 1025, secure: false, ignoreTLS: true,
    connectionTimeout: 2000, greetingTimeout: 2000, socketTimeout: 2000, dnsTimeout: 2000,
    disableFileAccess: true, disableUrlAccess: true,
    logger: false, debug: false,
  });
  try {
    const result = await transport.sendMail({
      from: "CarePulse Synthetic <no-reply@example.invalid>", to: recipient,
      ...renderMail(kind, link),
    });
    if (result.accepted.length !== 1 || result.rejected.length) throw new Error("Mail rejected");
  } catch {
    throw new Error("Synthetic mail unavailable");
  } finally {
    transport.close();
  }
}
