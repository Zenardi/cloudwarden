"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

type IconName =
  | "overview" | "costs" | "recommendations" | "budgets" | "policies" | "collections"
  | "bindings" | "compliance" | "waivers" | "remediation" | "runs" | "executions"
  | "events" | "assets" | "subscriptions" | "accountGroups" | "notifications"
  | "audit" | "chevronLeft" | "close" | "menu";

// Monochrome 24px line icons (stroke = currentColor). Standard operator-tool
// affordances — deliberately not illustrated/playful.
const PATHS: Record<IconName, ReactNode> = {
  overview: (
    <>
      <rect x="3" y="3" width="8" height="8" rx="1.5" />
      <rect x="13" y="3" width="8" height="8" rx="1.5" />
      <rect x="3" y="13" width="8" height="8" rx="1.5" />
      <rect x="13" y="13" width="8" height="8" rx="1.5" />
    </>
  ),
  costs: (
    <>
      <rect x="2" y="6" width="20" height="12" rx="2" />
      <circle cx="12" cy="12" r="2.5" />
      <path d="M5 9v6M19 9v6" />
    </>
  ),
  recommendations: (
    <>
      <path d="M9 18h6" />
      <path d="M10 21h4" />
      <path d="M12 3a6 6 0 00-4 10.5c.7.6 1 1 1 2h6c0-1 .3-1.4 1-2A6 6 0 0012 3z" />
    </>
  ),
  budgets: (
    <>
      <path d="M3 7h15a2 2 0 012 2v7a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
      <path d="M3 7V6a2 2 0 012-2h11" />
      <circle cx="16.5" cy="12.5" r="1.5" />
    </>
  ),
  policies: <path d="M12 3l7 3v5c0 4.4-3 7.6-7 9-4-1.4-7-4.6-7-9V6z" />,
  collections: (
    <>
      <path d="M12 3l9 5-9 5-9-5 9-5z" />
      <path d="M3 12l9 5 9-5" />
      <path d="M3 16l9 5 9-5" />
    </>
  ),
  bindings: (
    <>
      <path d="M9 15l6-6" />
      <path d="M11 6l1-1a3.5 3.5 0 015 5l-2 2" />
      <path d="M13 18l-1 1a3.5 3.5 0 01-5-5l2-2" />
    </>
  ),
  compliance: (
    <>
      <rect x="6" y="4" width="12" height="17" rx="2" />
      <path d="M9 4a3 3 0 016 0" />
      <path d="M9 13l2 2 4-4" />
    </>
  ),
  remediation: (
    <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.1-3.1a6 6 0 01-7.9 7.9l-6.3 6.3a2.1 2.1 0 01-3-3l6.3-6.3a6 6 0 017.9-7.9z" />
  ),
  waivers: (
    <>
      <path d="M4 8a1 1 0 011-1h14a1 1 0 011 1v2a2 2 0 000 4v2a1 1 0 01-1 1H5a1 1 0 01-1-1v-2a2 2 0 000-4z" />
      <path d="M12 8v8" />
    </>
  ),
  runs: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M10 8.5l5 3.5-5 3.5z" />
    </>
  ),
  executions: <path d="M3 12h4l2.5-7 4 14 2.5-7H21" />,
  events: (
    <>
      <path d="M8 6h13M8 12h13M8 18h13" />
      <path d="M3.6 6h.01M3.6 12h.01M3.6 18h.01" />
    </>
  ),
  assets: (
    <>
      <rect x="3" y="4" width="18" height="7" rx="1.5" />
      <rect x="3" y="13" width="18" height="7" rx="1.5" />
      <path d="M7 7.5h.01M7 16.5h.01" />
    </>
  ),
  subscriptions: <path d="M7 18a4 4 0 01-.5-7.97 5.5 5.5 0 0110.6-1.5A3.5 3.5 0 0116.5 18H7z" />,
  accountGroups: (
    <>
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 20a5.5 5.5 0 0111 0" />
      <path d="M16 5.3a3 3 0 010 5.4" />
      <path d="M18.5 20a5.5 5.5 0 00-3-4.9" />
    </>
  ),
  notifications: (
    <>
      <path d="M6 9a6 6 0 1112 0c0 4 1.5 5.5 2 6H4c.5-.5 2-2 2-6z" />
      <path d="M10 20a2 2 0 004 0" />
    </>
  ),
  audit: (
    <>
      <path d="M3 12a9 9 0 109-9 9 9 0 00-8 5" />
      <path d="M3 4v4h4" />
      <path d="M12 8v4l3 2" />
    </>
  ),
  chevronLeft: <path d="M15 6l-6 6 6 6" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
};

function Icon({ name, className = "nav-ico" }: { name: IconName; className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  );
}

/** Hamburger glyph reused by the mobile top bar in AppShell. */
export function MenuIcon() {
  return <Icon name="menu" />;
}

interface NavItem {
  href: string;
  label: string;
  icon: IconName;
}
interface NavGroup {
  label?: string;
  items: NavItem[];
}

const GROUPS: NavGroup[] = [
  { items: [{ href: "/", label: "Overview", icon: "overview" }] },
  {
    label: "FinOps",
    items: [
      { href: "/costs", label: "Costs", icon: "costs" },
      { href: "/showback", label: "Showback", icon: "budgets" },
      { href: "/recommendations", label: "Recommendations", icon: "recommendations" },
      { href: "/budgets", label: "Budgets", icon: "budgets" },
    ],
  },
  {
    label: "Governance",
    items: [
      { href: "/policies", label: "Policies", icon: "policies" },
      { href: "/collections", label: "Collections", icon: "collections" },
      { href: "/bindings", label: "Bindings", icon: "bindings" },
      { href: "/compliance", label: "Compliance", icon: "compliance" },
      { href: "/waivers", label: "Waivers", icon: "waivers" },
      { href: "/remediation", label: "Remediation", icon: "remediation" },
    ],
  },
  {
    label: "Operations",
    items: [
      { href: "/runs", label: "Runs", icon: "runs" },
      { href: "/executions", label: "Executions", icon: "executions" },
      { href: "/events", label: "Events", icon: "events" },
    ],
  },
  {
    label: "Inventory",
    items: [{ href: "/assets", label: "Assets", icon: "assets" }],
  },
  {
    label: "Accounts",
    items: [
      { href: "/subscriptions", label: "Subscriptions", icon: "subscriptions" },
      { href: "/account-groups", label: "Account groups", icon: "accountGroups" },
    ],
  },
  {
    label: "System",
    items: [
      { href: "/notifications", label: "Notifications", icon: "notifications" },
      { href: "/audit", label: "Audit", icon: "audit" },
    ],
  },
];

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(href + "/");
}

interface SidebarProps {
  collapsed: boolean;
  onToggleCollapse: () => void;
  onCloseMobile: () => void;
}

export default function Sidebar({ collapsed, onToggleCollapse, onCloseMobile }: SidebarProps) {
  const pathname = usePathname() || "/";
  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <Link href="/" className="sidebar-brand" onClick={onCloseMobile}>
          <span className="brand-mark" aria-hidden>
            🛡️
          </span>
          <span className="brand-name">CloudWarden</span>
        </Link>
        <button className="drawer-close" aria-label="Close navigation" onClick={onCloseMobile}>
          <Icon name="close" />
        </button>
      </div>

      <nav className="sidebar-nav" aria-label="Primary">
        {GROUPS.map((group, gi) => (
          <div
            className="nav-group"
            key={group.label ?? `group-${gi}`}
            role="group"
            aria-label={group.label}
          >
            {group.label && <div className="nav-group-label">{group.label}</div>}
            {group.items.map((item) => {
              const active = isActive(pathname, item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className="nav-link"
                  title={collapsed ? item.label : undefined}
                  aria-current={active ? "page" : undefined}
                  onClick={onCloseMobile}
                >
                  <Icon name={item.icon} />
                  <span className="nav-label">{item.label}</span>
                </Link>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="sidebar-foot">
        <button
          className="collapse-btn"
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand" : "Collapse"}
        >
          <Icon name="chevronLeft" />
          <span className="nav-label">Collapse</span>
        </button>
      </div>
    </aside>
  );
}
