import { useEffect } from 'react'

import { useMeta } from '../api/queries'

/**
 * About ATUS Explorer, in the order a reader asks (review round 6): what it
 * is, what the survey measures, how sure the numbers are (the results'
 * "What does 95% mean?" links here), why 2020 is different, reading the
 * results, privacy — a plain summary, the full account of what is logged
 * one click away — then the data release and further reading.
 */
export function AboutPage() {
  const meta = useMeta()

  useEffect(() => {
    document.title = 'About & methodology · ATUS Explorer'
    return () => {
      document.title = 'ATUS Explorer'
    }
  }, [])

  return (
    <div className="about-page">
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
        sampled household reports a complete 24-hour diary, and each activity in it is coded
        against a detailed list of activities (sleeping, housework, watching TV, and hundreds
        more, grouped into categories). The survey covers people age 15 and over living in
        households (not, for example, in prisons or nursing homes), continuously since 2003.
      </p>
      <p>
        Diaries are not a simple average of the population: some groups and some days are
        sampled more than others. Every estimate here uses the official BLS survey weights,
        which make each diary represent the person-days it stands for — so results are estimates
        for the population, balanced across days of the week and the year.
      </p>

      <h2 id="how-sure" tabIndex={-1}>
        How sure are the numbers?
      </h2>
      <p>
        Every figure here is an estimate from a sample of people, so it has sampling error. Each
        comes with a 95% confidence interval. The method that produces it is built so that, if
        the survey were repeated many times with new samples, about 95% of the intervals it
        produced would contain the true value for the whole population. The interval reflects
        sampling error only, not other errors, such as people misremembering their day. It is
        computed from the survey's 160 official replicate weights.
      </p>
      <p>
        A figure based on few diaries is less dependable than its interval suggests: with fewer
        than 100 people's diaries behind it, the result says so and, for a single year, offers
        to combine several years for more.
      </p>
      <p>
        When two groups are compared, the confidence interval for their difference allows for
        both groups coming from the same survey; the analysis service computes it, never this
        page. A trend split into groups draws each group's estimates without intervals; it links
        to a comparison of two of its groups in one year, which shows the confidence interval
        for their difference.
      </p>

      <h2>Why 2020 is different</h2>
      <p>
        ATUS data collection was suspended from March 18 to May 9, 2020, at the start of the
        COVID-19 pandemic, so the survey's usual yearly weighting does not cover 2020. BLS
        published 2020 under a separate weighting that represents the weeks that were collected
        (January 1 – March 17 and May 10 – December 31). Trends show 2020 from that weighting as
        a hollow point, reached by a dotted line, with a note that it is a partial year that may
        not compare exactly with other years. Choosing 2020 alone uses it too, and the result
        always says so. The site picks the weighting itself: the 2020 one for 2020, the usual
        one for every other year.
      </p>

      <h2>Reading the results</h2>
      <ul>
        <li>
          Estimates are <b>descriptive</b>: they say how much time groups spent, not why.
        </li>
        <li>
          "Share of people who do it" is a <b>daily</b> share — the people doing an activity on
          an average day, not the share who ever do it.
        </li>
        <li>
          "Children at home" means any child under 18 in the household — close to, but not the
          same as, being a parent.
        </li>
        <li>
          The number of diaries behind a figure, in its small print, counts the people surveyed,
          not the population they stand for.
        </li>
      </ul>

      <h2 id="privacy" tabIndex={-1}>
        Your privacy
      </h2>
      <ul>
        <li>No accounts, cookies, trackers or third-party scripts.</li>
        <li>
          The starter charts and the most common charts come from files on this site and send
          nothing to the analysis service.
        </li>
        <li>
          A new chart sends only what you asked for (activity, people, years). The service logs
          each request without its contents, with your IP address shortened, and the hosts
          delete those logs within 7 days.
        </li>
      </ul>
      <details className="about-details">
        <summary>Everything that is logged, and for how long</summary>
        <p>
          ATUS Explorer has no accounts, sets no cookies, keeps nothing in your browser's
          storage, and uses no third-party analytics, trackers, fonts or scripts. The pages, the
          survey metadata and the most common analyses (including the starter examples) are
          served as static files that do not involve the analysis service.
        </p>
        <p>
          A new analysis sends only its specification (activity, population, years, statistic)
          to the analysis service. Opening the analysis builder, or an analysis that is not
          among the pre-computed ones, also sends one empty request (at most once every 10
          minutes) that wakes the service up. For each request that reaches the service, it
          logs:
        </p>
        <ul>
          <li>
            a random request ID, the address requested, the response status and its timing;
          </li>
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
          lines; the edge's daily usage counter keeps only a keyed hash of the shortened
          address, with a key derived anew each day from a secret. It deletes those counts after
          each day, although its storage can restore deleted data for up to 30 days. The
          database receives nothing about visitors.
        </p>
        <p>
          Logs are kept by the hosting providers' free plans and then deleted: the API host
          (Render) keeps them for 7 days, the edge (Cloudflare Workers Logs) for 3 days (7 days
          from 1 December 2026). To deliver the site, Cloudflare and Render also handle standard
          connection data, such as your full IP address, in their own systems and under their
          own privacy policies.
        </p>
        <p>
          A shared analysis link contains the analysis specification, so anyone you send it to
          can see what you analyzed.
        </p>
      </details>

      <h2 id="data" tabIndex={-1}>
        Data and versions
      </h2>
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
