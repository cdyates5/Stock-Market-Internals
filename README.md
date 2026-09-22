# S&P 500: market internals dashboard

A self-refreshing dashboard that asks whether market internals confirm the move in the S&P 500. Eleven internals
in four pillars (credit, equity rotation, volatility, safe haven) are screened, scaled by their own volatility and
combined into a composite. The composite is turned into an internals-implied 13-week return and compared with the
actual one. The page also carries the full candidate screen and five tests: regime stability, lead/lag, divergence,
robustness and a strength-versus-stability scatter. The methodology is in the dashboard's own Method section.

GitHub Actions rebuilds it every US trading day. It deploys only when three checks pass: the build, a data-freshness
check and a headless-browser render check.

```
.
├── rebuild.py                       # data fetch + model + single-file HTML (template embedded)
├── requirements.txt                 # pinned: numpy, pandas, playwright
├── tests/check_render.py            # headless Chromium: zero console errors, all charts, no mobile overflow
├── .github/workflows/refresh.yml    # schedule, build, render check, deploy
└── .github/dependabot.yml           # monthly PRs for GitHub Action version bumps
```

Each run writes to `site/`: `index.html` (the dashboard), `latest.json` (the headline readings) and `.nojekyll`.

This repo works exactly like the yield-internals repo. If that one is already running, sections 2b and 3 are the only
new steps, and you can reuse the same two keys.

---

## 1. Decide who should see it

**A GitHub Pages site is public on the internet even when the repository is private.** The exception is an
organisation on GitHub Enterprise Cloud, which can set Pages visibility to private. Pages from a private repository
also needs a paid plan: Pro for a personal account, Team or Enterprise for an organisation.

| Option | Who can see it | Set up |
|---|---|---|
| **A. Private Pages** (Enterprise Cloud organisation) | Only people with read access to the repo | Private repo, Pages source *GitHub Actions*, then Settings → Pages → Visibility: *Private* |
| **B. Artifact only** (any plan) | Only repo members, by downloading the run artifact | Private repo; add repository variable `DEPLOY_TARGET` = `artifact`; skip Pages |
| **C. Public Pages** | Anyone with the URL | Any repo; Pages source *GitHub Actions* |

## 2. Create the repository and add the files

1. On github.com, click **+** → **New repository**. Give it a name, e.g. `spx-internals`. Choose Private for
   options A and B. Leave "Add a README", .gitignore and licence unticked.
2. Add the files. Upload the **contents** of the unzipped folder, not the folder itself. `.github` must sit at the
   repository root or GitHub never finds the workflow.
   - **Command line:**
     ```bash
     cd spx-internals-github
     git init -b main && git add . && git commit -m "S&P 500 internals dashboard"
     git remote add origin https://github.com/<owner>/spx-internals.git
     git push -u origin main
     ```
     If the push is refused with a message about **workflow scope**, run `gh auth refresh -s workflow`, or use a
     token with the `workflow` scope, or push over SSH.
   - **Browser only:** on the empty repo page click **uploading an existing file**. Drag in `rebuild.py`,
     `requirements.txt`, `README.md`, `.gitignore` and the `tests` folder, then commit. Next create the workflow:
     **Add file → Create new file**, type the name `.github/workflows/refresh.yml`, paste the file's contents and
     commit. Repeat for `.github/dependabot.yml`. On a Mac, Finder hides dot-folders; press **Cmd + Shift + .** to
     show them.

## 2b. Add the two data keys (required on GitHub)

GitHub's shared runners reuse the same IP addresses across thousands of jobs, and data sites block them. Yahoo
Finance returns `HTTP 429`, and FRED's CSV endpoint resets the connection. The automated build therefore uses two
free, keyed APIs that work from anywhere:

- **Tiingo**, for the 26 ETFs.
- **FRED's JSON API**, for VIX, VIX3M, financial conditions (NFCI), credit spreads, the 10s2s curve and the
  broad dollar.

**Already running the yield-internals dashboard?** Use the same two keys. Secrets belong to one repository, so add
them again here. In an organisation, you can instead store them once as organisation secrets.

1. **Tiingo token:** sign up at <https://www.tiingo.com>, confirm your email, then copy the token from
   Account → API → Token.
2. **FRED API key:** request one at <https://fred.stlouisfed.org/docs/api/api_key.html>. It is free and instant with
   a St. Louis Fed account, and is a 32-character lower-case string.
3. **Add both:** Settings → **Secrets and variables** → **Actions** → **Secrets** tab →
   **New repository secret**, twice:
   - name `TIINGO_TOKEN`, value = your Tiingo token
   - name `FRED_API_KEY`, value = your FRED key

Before fetching anything, the build checks both keys. If either is missing or wrong, it stops within seconds with
a message naming the key to fix. It does not hang or fall back to a blocked source.

**What comes from where on the automated build:**

| Data | Source | Note |
|---|---|---|
| The target | Tiingo, **SPY** total return | The S&P 500 index is not on Tiingo's free tier, and FRED's `SP500` only starts in 2016. SPY tracks the index at 0.998 weekly-return correlation. The ~1.8%/yr dividend yield shows up only in compounding, not in the correlations the model uses. The page labels it "S&P 500 (SPY proxy)" |
| 25 sector, style, credit and safe-haven ETFs | Tiingo | total-return adjusted closes |
| VIX, VIX3M | FRED (`VIXCLS`, `VXVCLS`) | `VXVCLS` starts Dec 2007, earlier than CBOE's own VIX3M file (Sep 2009), so the term-structure internal gets the 2008 crisis as well |
| NFCI, HY/IG OAS, 10s2s curve, broad dollar | FRED JSON API | The OAS series are short-history and context-only; the broad dollar and NFCI publish with a lag of about a week |

**Locally, with no keys,** `rebuild.py` uses Yahoo (the actual `^GSPC` index), CBOE and FRED's CSV endpoint, all of
which work from a normal internet connection.

## 3. Turn on deployment and run it

1. **Pages (options A and C):** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
   **Artifact only (option B):** Settings → Secrets and variables → Actions → *Variables* → New repository
   variable, `DEPLOY_TARGET` = `artifact`.
2. Check that the default branch is `main` (Settings → General).
3. Open **Actions** → **Refresh S&P 500 internals dashboard** → **Run workflow** → branch `main` → Run. The first
   run takes about 5 minutes.
4. A good run looks like this:
   - The build log starts with `Tiingo token OK` and `FRED API key OK`, lists every series with its source, and
     ends with `status: ok`.
   - The render check shows `errors=0` for desktop, mobile and dark.
   - The run's **Summary** tab shows a readings table.
   - The `deploy` job shows the site URL, usually `https://<owner>.github.io/spx-internals/`. For artifact-only,
     download `spx-internals-<n>` from the run page and open `index.html`.

Committing the workflow triggers an automatic first run. If Pages was not enabled yet, that run fails at the deploy
step; this is expected. Enable Pages and re-run.

## 4. Schedule

```yaml
- cron: "30 0 * * 2-6"    # 00:30 UTC, Tuesday–Saturday
```

This covers every US trading day, after the close. The run lands at **10:30 AEST / 11:30 AEDT** in Melbourne. It is
deliberately one hour after the yield-internals dashboard (23:30 UTC). With a shared Tiingo token this keeps both
builds under the free tier's **50 requests/hour** cap, since each build uses 25–27. If the two dashboards use
separate tokens, or this is the only one, the offset does not matter.

A push to `main` that changes the code also rebuilds and redeploys. The manual **Run workflow** button has a
*clear cache* option that refetches all history.

## 5. What happens when something breaks

The last good page stays live unless a new run passes every check.

| Situation | Behaviour | Deploys? |
|---|---|---|
| `TIINGO_TOKEN` or `FRED_API_KEY` missing or wrong | Fails in seconds, naming the key to fix | **No** |
| One series fails to download | Tries the next source, then the previous run's cached copy; an orange banner lists stale inputs | Yes |
| A context-only series (OAS, curve, dollar) has no source and no cache | Dropped for that run; the rest builds normally | Yes |
| The whole first attempt fails | The workflow waits 2 minutes and retries once | – |
| Latest data older than 7 days | Exits with code 2, run fails | **No** |
| Model sanity checks fail (index out of range, fewer than 6 members, composite fit collapses, gaps) | Exits with code 3, run fails | **No** |
| Render check fails (console error, missing charts, mobile overflow) | Run fails | **No** |

GitHub emails the person who last edited the workflow file when a scheduled run fails. Check Settings (profile) →
Notifications → Actions.

## 6. Keeping it running

- **Public repositories only:** GitHub disables scheduled workflows after 60 days with no repository activity. This
  workflow does not commit anything, so push something occasionally. Private repositories are unaffected.
- **Membership is re-screened on every run.** Unlike the yield dashboard, this build re-runs the selection each
  time rather than freezing it. If the data shifts enough, the set of internals can change between runs, and the
  screen table on the page always shows the current reasons. To freeze membership, hard-code the chosen keys in
  `main()` in place of the `select()` result.
- **Tiingo limits (free tier):** 50 requests/hour, 1,000/day, 500 unique symbols/month. One daily build uses about
  27 requests and 26 symbols.
- **Action versions** are pinned to majors that run on Node 24. GitHub removed Node 20 from hosted runners in
  September 2026. Dependabot opens a PR when new majors ship; PRs build and render-check but never deploy.
- **Python packages** are pinned on purpose. A pandas or numpy upgrade can move the statistics, so bump them
  deliberately and compare `latest.json` before merging.
- **Actions minutes:** roughly 4 minutes × ~22 runs ≈ 90–110 minutes a month. Public repos are free; for private
  repos, check your plan's included minutes.

## 7. Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python rebuild.py                          # -> site/index.html, site/latest.json  (Yahoo, CBOE, fredgraph)
python tests/check_render.py site/index.html
# to mirror the automated build:  export TIINGO_TOKEN=... FRED_API_KEY=...  then  python rebuild.py
```

Options: `--out DIR`, `--cache DIR`, `--ttl-hours 10`, `--max-age-days 7`.
Exit codes: `0` ok, `2` stale data, `3` sanity failure, `1` fetch or runtime error.

## 8. `latest.json`

Published next to the page on every deploy, so other tools can read the numbers without parsing the HTML:

```json
{
 "asof": "2026-09-18", "target": "S&P 500 (SPY proxy)", "spx": 765.1,
 "return_13w_pct": 1.98, "implied_13w_pct": 4.05, "unconfirmed_13w_pct": -2.07, "unconfirmed_z": -0.4,
 "composite_13w_z": 0.35, "beta_pct_per_unit": 2.5,
 "pillar_contrib": {"CR": 0.62, "RISK": -0.31, "VOL": 0.02, "SAFE": 0.48},
 "members": {"HYG_IEI": {"change_13w": 0.011, "unit": "pct", "contrib": 0.12}, "...": {}},
 "stale": {}, "composite_weekly_rho": 0.78, "run_url": "https://github.com/..."
}
```

`unit` explains each member's `change_13w`:

- `pct`: a fractional log return (0.011 = +1.1%)
- `bp`: basis points
- `pts`: index points
- `ppt`: percentage points

On the automated build, `spx` is SPY's price, not the index level.
