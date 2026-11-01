import type { Metadata } from "next";
import { LoginForm } from "@/components/LoginForm";

export const metadata: Metadata = { title: "Log in · Iso-Vault" };

export default function LoginPage() {
  return (
    <main className="login">
      <LoginForm />
    </main>
  );
}
