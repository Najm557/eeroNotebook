import { describe, it, expect } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { resources } from './index'

/**
 * The fork's name, and the two places upstream's name is kept on purpose.
 *
 * This is a fork of lfnovo/open-notebook, so both names legitimately appear in
 * the UI and the difference matters:
 *
 *   - the product a member is using is eeroNotebook
 *   - `docLink` labels an anchor whose href is github.com/lfnovo/open-notebook,
 *     and `updateAvailableDesc` reports the version check, which queries
 *     upstream's GitHub releases
 *
 * Renaming those two would misattribute upstream's documentation and releases to
 * this fork. Renaming anything else back would leave a member looking at a
 * product name that is not the one they were given. Both directions are pinned
 * here because an upstream merge touches these files and will reintroduce the
 * old name silently.
 */

const LOCALES_DIR = path.join(process.cwd(), 'src/lib/locales')
const UPSTREAM_ONLY_KEYS = ['docLink', 'updateAvailableDesc']

/** Each locale's source text, so a check can see which key a line belongs to. */
function localeSources(): [string, string][] {
  const out: [string, string][] = []
  for (const dir of fs.readdirSync(LOCALES_DIR, { withFileTypes: true })) {
    if (!dir.isDirectory()) continue
    const file = path.join(LOCALES_DIR, dir.name, 'index.ts')
    if (fs.existsSync(file)) out.push([dir.name, fs.readFileSync(file, 'utf8')])
  }
  return out
}

function keyOf(line: string): string {
  return /^\s*([A-Za-z0-9_]+)\s*:/.exec(line)?.[1] ?? '?'
}

describe('branding', () => {
  it('names every locale after the fork', () => {
    const entries = Object.entries(resources)
    for (const [tag, { translation }] of entries) {
      expect(translation.common.appName, `${tag} appName`).toBe('eeroNotebook')
    }
    // Guard against the list being empty and the loop above passing vacuously.
    expect(entries.length).toBeGreaterThanOrEqual(14)
  })

  it('uses the fork name on the sign-in screen in every locale', () => {
    for (const [tag, { translation }] of Object.entries(resources)) {
      expect(translation.auth.loginTitle, `${tag} auth.loginTitle`).toBe('eeroNotebook')
    }
  })

  it("keeps upstream's name only where it points at upstream", () => {
    // Read the source rather than the imported object: this asserts which KEY a
    // surviving "Open Notebook" belongs to, which the values alone cannot show.
    const staleBrand: string[] = []
    let upstreamRefs = 0

    for (const [name, source] of localeSources()) {
      for (const line of source.split('\n')) {
        if (!line.includes('Open Notebook')) continue
        const key = keyOf(line)
        if (UPSTREAM_ONLY_KEYS.includes(key)) {
          upstreamRefs += 1
        } else {
          staleBrand.push(`${name}: ${key}`)
        }
      }
    }

    expect(
      staleBrand,
      'these strings still say "Open Notebook" but describe this fork, not upstream'
    ).toEqual([])
    // And the exemptions are real rather than the search having found nothing.
    expect(upstreamRefs).toBeGreaterThan(0)
  })

  it('does not rebrand the two keys that describe upstream', () => {
    // The opposite direction, and the more likely one: a blanket find-replace
    // over the locale files would rename these two along with everything else,
    // leaving the UI crediting this fork for upstream's documentation and
    // announcing upstream's releases as its own.
    const wronglyRebranded: string[] = []

    for (const [name, source] of localeSources()) {
      for (const line of source.split('\n')) {
        if (!line.includes('eeroNotebook')) continue
        const key = keyOf(line)
        if (UPSTREAM_ONLY_KEYS.includes(key)) {
          wronglyRebranded.push(`${name}: ${key}`)
        }
      }
    }

    expect(
      wronglyRebranded,
      'these keys label upstream\'s docs and releases and must keep upstream\'s name'
    ).toEqual([])
  })

  it('has no stale brand left in the served metadata or the logo', () => {
    const layout = fs.readFileSync(path.join(process.cwd(), 'src/app/layout.tsx'), 'utf8')
    expect(layout).toContain('title: "eeroNotebook"')

    // The browser tab title is Next.js metadata, evaluated outside React where
    // t() does not exist, so it duplicates the appName value and can drift.
    expect(layout).not.toContain('title: "Open Notebook"')

    const logo = fs.readFileSync(path.join(process.cwd(), 'public/logo.svg'), 'utf8')
    expect(logo).toContain('eeroNotebook')
  })
})
