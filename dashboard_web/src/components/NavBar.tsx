"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/otp", label: "OTP" },
  { href: "/headways-dwells", label: "Headways & Dwells" },
  { href: "/alerts", label: "Alerts" },
  { href: "/line-delays", label: "Line Delays" },
];

export function NavBar() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency");

  return (
    <nav className="navbar">
      <span className="navbar-brand">Transit Dashboard</span>
      {LINKS.map(({ href, label }) => {
        const target = agency ? `${href}?agency=${agency}` : href;
        const isActive = pathname === href;
        return (
          <Link
            key={href}
            href={target}
            className={`navbar-link${isActive ? " active" : ""}`}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
