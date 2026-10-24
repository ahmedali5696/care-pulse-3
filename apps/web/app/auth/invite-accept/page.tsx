"use client";

import { useEffect, useRef, useState } from "react";

type InvitationLink = {
  organization_id: string;
  invitation_id: string;
  proof: string;
};

export default function AcceptInvitationPage() {
  const invitation = useRef<InvitationLink | null>(null);
  const [ready, setReady] = useState(false);
  const [state, setState] = useState<"form" | "submitting" | "active" | "failed">("form");

  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const organization_id = params.get("organization_id");
    const invitation_id = params.get("proof_reference");
    const proof = params.get("proof");
    if (organization_id && invitation_id && proof) {
      invitation.current = { organization_id, invitation_id, proof };
    }
    window.history.replaceState(null, "", "/auth/invite-accept");
    setReady(true);
  }, []);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!invitation.current || state === "submitting") return;
    const form = new FormData(event.currentTarget);
    setState("submitting");
    try {
      const response = await fetch("/api/auth/invite-accept", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify({
          ...invitation.current,
          name: form.get("name"), password: form.get("password"),
        }),
      });
      setState(response.ok ? "active" : "failed");
      if (response.ok) invitation.current = null;
    } catch {
      setState("failed");
    }
  }

  return (
    <main style={{ maxWidth: 440, margin: "4rem auto", padding: "1.5rem" }}>
      <h1>Activate your CarePulse clinic</h1>
      {!ready ? <p>Checking invitation…</p> : !invitation.current && state !== "active" ?
        <p role="alert">This invitation link is unavailable.</p> : state === "active" ?
        <p role="status">The clinic Owner account is active.</p> :
        <form onSubmit={submit}>
          <p>Use the invitation sent to your registered email.</p>
          <label htmlFor="owner-name">Full name</label>
          <input id="owner-name" name="name" autoComplete="name" required style={{ display: "block", width: "100%", marginBottom: 16 }} />
          <label htmlFor="owner-password">Password</label>
          <input id="owner-password" name="password" type="password" autoComplete="new-password" required style={{ display: "block", width: "100%", marginBottom: 16 }} />
          <button disabled={state === "submitting"} type="submit">
            {state === "submitting" ? "Activating…" : "Activate clinic"}
          </button>
          {state === "failed" && <p role="alert">Unable to activate this invitation.</p>}
        </form>}
    </main>
  );
}
