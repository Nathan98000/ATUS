/** A small picture of each kind of result: a number, a line, two bars. */
export function AnalysisTypeIcon({ kind }: { kind: 'number' | 'line' | 'bars' }) {
  return (
    <svg className="analysis-type__icon" viewBox="0 0 40 28" aria-hidden="true">
      {kind === 'number' ? (
        <>
          <rect x="2" y="17" width="36" height="7" rx="2" className="analysis-type__track" />
          <rect x="2" y="17" width="15" height="7" rx="2" className="analysis-type__mark" />
          <rect x="2" y="4" width="20" height="7" rx="2" className="analysis-type__ink" />
        </>
      ) : kind === 'line' ? (
        <>
          <line x1="2" y1="25" x2="38" y2="25" className="analysis-type__axis" />
          <polyline points="4,18 11,13 18,16 25,8 32,10 36,5" className="analysis-type__line" />
          <circle cx="36" cy="5" r="2.6" className="analysis-type__mark" />
        </>
      ) : (
        <>
          <line x1="3" y1="2" x2="3" y2="26" className="analysis-type__axis" />
          <rect x="3" y="5" width="32" height="7" rx="2" className="analysis-type__mark" />
          <rect x="3" y="16" width="22" height="7" rx="2" className="analysis-type__mark-b" />
        </>
      )}
    </svg>
  )
}
