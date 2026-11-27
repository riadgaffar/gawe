import type { RecordCardModel } from "@/lib/record-evidence";

export function RecordEvidenceCard({ card }: { card: RecordCardModel | null }) {
  if (!card) return null;
  return <section className="card stack" aria-label={card.title}>
    <h3>{card.title}</h3>
    <p>{card.notice}</p>
    {card.coverage && <p className="muted">{card.coverage}</p>}
    {card.records.map(record => <section key={record.key} className="stack">
      <h4>{record.subject} <span className="muted">({record.resource})</span></h4>
      <p>{record.status}</p>
      <p>{record.capture}</p>
      {record.timing && <p>{record.timing}</p>}
      {record.rows.length > 0 && <div className="record-table"><table>
        <thead><tr><th scope="col">Field</th><th scope="col">Captured value</th>{card.refused && <><th scope="col">Observed value</th><th scope="col">Comparison</th></>}</tr></thead>
        <tbody>{record.rows.map(row => <tr key={row.field}>
          <th scope="row">{row.field}</th><td>{row.before}</td>
          {card.refused && <><td>{row.observed}</td><td>{row.change}</td></>}
        </tr>)}</tbody>
      </table></div>}
      <details><summary>Record revisions and provenance</summary>
        <dl>{record.metadata.map(item => <div key={item.label}><dt>{item.label}</dt><dd><code>{item.value}</code></dd></div>)}</dl>
      </details>
    </section>)}
  </section>;
}
