import Link from "next/link";

export default function Nav() {
  return (
    <nav className="nav">
      <span className="brand">⚡ Azure FinOps</span>
      <Link href="/">Overview</Link>
      <Link href="/costs">Costs</Link>
      <Link href="/recommendations">Recommendations</Link>
      <Link href="/policies">Policies</Link>
      <Link href="/runs">Runs</Link>
      <Link href="/remediation">Remediation</Link>
      <Link href="/subscriptions">Subscriptions</Link>
    </nav>
  );
}
