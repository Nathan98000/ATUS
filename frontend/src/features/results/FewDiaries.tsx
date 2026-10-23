/**
 * A figure resting on few diaries (`fewDiariesNote`), and the way to more:
 * the same analysis over the latest years combined (`combineYearsLink`;
 * review round 6, the owner's decision D7).
 */
import { Link } from 'react-router'

export function FewDiaries({
  text,
  combine,
}: {
  text: string
  combine?: { path: string; years: string } | null
}) {
  return (
    <>
      {text}
      {combine ? (
        <>
          {' '}
          <Link to={combine.path}>Combine {combine.years} for more diaries</Link>
        </>
      ) : null}
    </>
  )
}
