import { SectionNav } from "@/components/SectionNav";
import { sectionSlugs } from "@/lib/sections";

/**
 * Runs once per agency produced by the parent segment's generateStaticParams,
 * so the export only contains sections that actually exist for that agency.
 */
export function generateStaticParams({ params }: { params: { agencyId: string } }) {
  return sectionSlugs(params.agencyId).map((sectionId) => ({ sectionId }));
}

export default async function SectionLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ agencyId: string; sectionId: string }>;
}) {
  const { agencyId, sectionId } = await params;

  return (
    <>
      <SectionNav agencyId={agencyId} sectionId={sectionId} />
      {children}
    </>
  );
}
