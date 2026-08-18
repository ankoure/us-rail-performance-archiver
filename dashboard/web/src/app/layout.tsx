import type { Metadata } from "next";
import { Suspense } from "react";
import { Analytics } from "@vercel/analytics/next";
import "./globals.css";
import { NavBar } from "@/components/NavBar";

export const metadata: Metadata = {
  title: "Transit Dashboard",
  description: "OTP, headway/dwell, alert, and line-delay metrics for archived GTFS-RT feeds.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <Suspense fallback={null}>
          <NavBar />
          {children}
        </Suspense>
        <Analytics />
      </body>
    </html>
  );
}
