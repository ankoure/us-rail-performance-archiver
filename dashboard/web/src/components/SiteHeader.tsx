import Link from "next/link";

/**
 * Global chrome. Only holds links that aren't scoped to a single agency —
 * metric tabs live in AgencyNav, inside /agency/[agencyId].
 */
export function SiteHeader() {
  return (
    <nav className="navbar">
      <Link href="/" className="navbar-brand">
        Transit Dashboard
      </Link>
      <Link href="/compare" className="navbar-link">
        Compare
      </Link>
      <Link href="/support" className="navbar-link">
        Support
      </Link>
    </nav>
  );
}
