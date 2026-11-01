"use client";

import { useState, type FormEvent } from "react";
import { Logo } from "./Logo";

export function LoginForm() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (response.ok) {
        window.location.assign("/");
        return;
      }
      const body = (await response.json().catch(() => ({}))) as { error?: string };
      setError(body.error ?? "Couldn't log in.");
    } catch {
      setError("Can't reach the server.");
    }
    setBusy(false);
  }

  return (
    <form className="login-card" onSubmit={submit}>
      <div className="brand">
        <Logo />
        Iso-Vault
      </div>
      <label className="field">
        Password
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          autoFocus
          required
          onChange={(event) => setPassword(event.target.value)}
        />
      </label>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <button className="primary" type="submit" disabled={busy}>
        {busy ? "Logging in…" : "Log in"}
      </button>
      <p className="muted" style={{ fontSize: 13 }}>
        The installer printed this password. It&apos;s also in <span className="mono">data/secrets.json</span> on the
        server.
      </p>
    </form>
  );
}
