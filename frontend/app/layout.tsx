import "./globals.css";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import Nav from "./components/Nav";

export const metadata: Metadata = {
  title: "Azure FinOps Optimizer",
  description: "Azure cost analysis, right-sizing recommendations, and remediation.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Nav />
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
