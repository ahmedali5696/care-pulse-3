export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    try {
      const { loadWebConfig } = await import("./config");
      loadWebConfig();
    } catch (error) {
      // Next.js may otherwise log the failure but keep accepting connections.
      const message = error instanceof Error && /^Invalid CP_[A-Z_]+(?:\.[a-z_]+)*$/.test(error.message)
        ? error.message
        : "Invalid web configuration";
      process.stderr.write(`${message}\n`);
      process.exit(1);
    }
  }
}
