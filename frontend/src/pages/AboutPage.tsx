import { useEffect } from 'react'

import { useMeta } from '../api/queries'

export function AboutPage() {
  const meta = useMeta()

  useEffect(() => {
    document.title = 'About & methodology · ATUS Explorer'
    return () => {
      document.title = 'ATUS Explorer'
    }
  }, [])

  return (
    <div className="about-page" style={{ maxWidth: '46rem' }}>
      <h1 tabIndex={-1}>About ATUS Explorer</h1>
      <p>
        ATUS Explorer is an independent, open-source tool for exploring the{' '}
        <a href="https://www.bls.gov/tus/" rel="noreferrer">
          American Time Use Survey (ATUS)
        </a>
        , conducted by the U.S. Census Bureau for the U.S. Bureau of Labor Statistics. It is not
        an official BLS product and is not endorsed by BLS.
      </p>

      <h2>What the survey measures</h2>
      <p>
        ATUS is the only federal survey measuring how people divide their time. One person per
        sampled household reports a complete 24-hour diary; each reported activity is coded
        against a hierarchical activity lexicon. The survey covers the civilian noninstitutional
        population age 15 and over, continuously since 2003.
      </p>

      <h2>Why estimates are weighted</h2>
      <p>
        Diaries are not a simple average of the population: some groups and some days are
        sampled more than others. Every estimate here uses the official BLS survey weights,
        which make each diary represent the person-days it stands for — so results are estimates
        for the population, balanced across days of the week and the year.
      </p>

      <h2>Why uncertainty is shown</h2>
      <p>
        Every number here is an estimate from a sample, so it carries sampling uncertainty.
        Standard errors are computed with the survey's official 160 replicate weights and shown
        together with confidence intervals. When two groups are compared, the difference's
        uncertainty accounts for the correlation between the groups — it is computed by the
        analytical engine, never in this interface.
      </p>

      <h2>Why 2020 is special</h2>
      <p>
        ATUS data collection was suspended from March 18 to May 9, 2020, at the start of the
        COVID-19 pandemic. The standard multi-year weights are therefore not defined for 2020.
        BLS published 2020 under a dedicated pandemic weighting that represents the weeks that
        were collected (January 1 – March 17 and May 10 – December 31). Trends show 2020 from
        that weighting as a hollow point, with a note that it is a partial-year estimate that
        may not compare exactly with other years; choosing 2020 alone uses it too (it is also an
        "Advanced options" choice), and the result always carries its caveat.
      </p>

      <h2>Interpreting results</h2>
      <ul>
        <li>
          Estimates are <b>descriptive</b>: they say how much time groups spent, not why.
        </li>
        <li>
          "Participation" is a <b>daily</b> rate — the share doing an activity on an average
          day, not the share who ever do it.
        </li>
        <li>
          "Children in household" means any household child under 18 — close to, but not the
          same as, being a parent.
        </li>
        <li>
          Sample sizes are unweighted respondent counts; the "represents" figure is the weighted
          population — the two are always shown separately.
        </li>
      </ul>

      <h2>Data and versions</h2>
      <p>
        {meta.data ? (
          <>
            This instance serves BLS multi-year release <code>{meta.data.data.release}</code> (
            {meta.data.data.years[0]}–{meta.data.data.years[meta.data.data.years.length - 1]},{' '}
            {meta.data.data.respondents.toLocaleString('en-US')} respondents), analytics version{' '}
            <code>{meta.data.analytics_version}</code>, API <code>{meta.data.api_version}</code>
            .
          </>
        ) : (
          'Version information appears here when the analysis service is reachable.'
        )}{' '}
        Results are reproducible: the same analysis specification against the same data release
        and analytics version always yields the same numbers, and every result page's link
        encodes its full specification.
      </p>

      <h2 id="privacy">Privacy and logs</h2>
      <p>
        ATUS Explorer has no accounts, sets no cookies, keeps nothing in your browser's storage,
        and uses no third-party analytics, trackers, fonts or scripts. The pages, the survey
        metadata and the most common analyses (including the starter examples) are served as
        static files that do not involve the analysis service.
      </p>
      <p>
        A new analysis sends only its specification (activity, population, years, statistic) to
        the analysis service. Opening the analysis builder, or an analysis that is not among the
        pre-computed ones, also sends one empty request (at most once every 10 minutes) that
        wakes the service up. For each request that reaches the service, it logs:
      </p>
      <ul>
        <li>a random request ID, the address requested, the response status and its timing;</li>
        <li>
          whether the result came from a cache, and the analysis key (a fingerprint of the
          specification, not the specification itself);
        </li>
        <li>
          your IP address <b>shortened to its network</b> (the first three parts of an IPv4
          address, the first 48 bits of an IPv6 address).
        </li>
      </ul>
      <p>
        Request bodies are never logged. The service uses your full IP address only in memory,
        to limit how many new analyses one visitor can start, and never writes it into its log
        lines; the edge's daily usage counter keeps only a keyed hash of the shortened address,
        with a key derived anew each day from a secret. It deletes those counts after each day,
        although its storage can restore deleted data for up to 30 days. The database receives
        nothing about visitors.
      </p>
      <p>
        Logs are kept by the hosting providers' free plans and then deleted: the API host
        (Render) keeps them for 7 days, the edge (Cloudflare Workers Logs) for 3 days (7 days
        from 1 December 2026). To deliver the site, Cloudflare and Render also handle standard
        connection data, such as your full IP address, in their own systems and under their own
        privacy policies.
      </p>
      <p>
        A shared analysis link contains the analysis specification, so anyone you send it to can
        see what you analyzed.
      </p>

      <h2>Learn more</h2>
      <ul>
        <li>
          <a href="https://www.bls.gov/tus/atususersguide.pdf" rel="noreferrer">
            ATUS User's Guide
          </a>{' '}
          — the survey's official methodology reference.
        </li>
        <li>
          <a href="https://github.com/Nathan98000/ATUS" rel="noreferrer">
            Project repository
          </a>{' '}
          — the full pipeline, statistical engine, API, and this frontend, with documentation of
          every methodological decision.
        </li>
      </ul>

      <p className="field-hint">
        Estimates should not be interpreted beyond their population, period, and methodology.
        Data source: U.S. Bureau of Labor Statistics, American Time Use Survey.
      </p>
    </div>
  )
}
