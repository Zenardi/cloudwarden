import "./globals.css";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import AuthGate from "./components/AuthGate";
import Nav from "./components/Nav";

export const metadata: Metadata = {
  title: "CloudWarden",
  description: "Multi-cloud governance-as-code & FinOps: policy posture, cost analysis, right-sizing recommendations, and guarded remediation.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Nav />
        <main className="container">
          <AuthGate>{children}</AuthGate>
        </main>
      </body>
    </html>
  );
}
