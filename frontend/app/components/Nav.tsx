import Link from "next/link";

export default function Nav() {
  return (
    <nav className="nav">
      <span className="brand">⚡ Azure FinOps</span>
      <Link href="/">Overview</Link>
      <Link href="/costs">Costs</Link>
      <Link href="/recommendations">Recommendations</Link>
      <Link href="/policies">Policies</Link>
      <Link href="/compliance">Compliance</Link>
      <Link href="/collections">Collections</Link>
      <Link href="/runs">Runs</Link>
      <Link href="/executions">Executions</Link>
      <Link href="/events">Events</Link>
      <Link href="/assets">Assets</Link>
      <Link href="/remediation">Remediation</Link>
      <Link href="/subscriptions">Subscriptions</Link>
      <Link href="/account-groups">Account Groups</Link>
      <Link href="/bindings">Bindings</Link>
      <Link href="/notifications">Notifications</Link>
    </nav>
  );
}
