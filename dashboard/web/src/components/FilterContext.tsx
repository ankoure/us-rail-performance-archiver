export function FilterContext({
  agency,
  line,
  when,
}: {
  agency: string;
  line?: string;
  when: string;
}) {
  return (
    <p className="card-hint filter-context">
      {agency}
      {line ? ` · ${line}` : ""} · {when}
    </p>
  );
}
