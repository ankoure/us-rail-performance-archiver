import { AgencyHeader } from "@/components/AgencyHeader";

// One generateStaticParams on the layout covers every page nested under it,
// so individual metric pages stay "use client" and don't repeat it.
export { generateStaticParams } from "@/lib/agencyParams";

export default async function AgencyLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ agencyId: string }>;
}) {
  const { agencyId } = await params;

  return (
    <>
      <AgencyHeader agencyId={agencyId} />
      {children}
    </>
  );
}
