export function FilterContext({
  scope,
  line,
  when,
}: {
  /** What the page is showing — the section label, not the agency (the
   *  agency is already named in the header above). */
  scope: string;
  line?: string;
  when: string;
}) {
  return (
    <p className="card-hint filter-context">
      {scope}
      {line ? ` · ${line}` : ""} · {when}
    </p>
  );
}
