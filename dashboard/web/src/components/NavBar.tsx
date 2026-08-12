"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/otp", label: "OTP" },
  { href: "/headways-dwells", label: "Headways & Dwells" },
  { href: "/speed", label: "Speed" },
  { href: "/adherence", label: "Adherence" },
  { href: "/alerts", label: "Alerts" },
  { href: "/line-delays", label: "Line Delays" },
  { href: "/compare", label: "Compare" },
];

const DAY_PAGES = new Set(["/alerts", "/line-delays", "/adherence"]);
const RANGE_PAGES = new Set(["/otp", "/headways-dwells", "/speed", "/compare"]);

function pageKind(pathname: string): "day" | "range" | null {
  if (DAY_PAGES.has(pathname)) return "day";
  if (RANGE_PAGES.has(pathname)) return "range";
  return null;
}

/**
 * Forwards the current filter state (agency, date range/day, line, ...) to
 * `target`, translating between the day-picker and range-picker vocabularies
 * so switching tabs doesn't silently drop the selected date(s).
 */
function targetHref(fromPathname: string, target: string, currentParams: URLSearchParams): string {
  const next = new URLSearchParams(currentParams.toString());
  const fromKind = pageKind(fromPathname);
  const toKind = pageKind(target);

  if (fromKind === "day" && toKind === "range") {
    const day = next.get("day");
    if (day) {
      next.set("start", day);
      next.set("end", day);
    }
    next.delete("day");
  } else if (fromKind === "range" && toKind === "day") {
    const end = next.get("end");
    if (end) next.set("day", end);
    next.delete("start");
    next.delete("end");
  }

  const qs = next.toString();
  return qs ? `${target}?${qs}` : target;
}

export function NavBar() {
  const pathname = usePathname();
  const searchParams = useSearchParams();

  return (
    <nav className="navbar">
      <span className="navbar-brand">Transit Dashboard</span>
      {LINKS.map(({ href, label }) => {
        const isActive = pathname === href;
        const target = targetHref(pathname, href, searchParams);
        return (
          <Link key={href} href={target} className={`navbar-link${isActive ? " active" : ""}`}>
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
