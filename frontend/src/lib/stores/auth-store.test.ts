import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { queryClient, QUERY_KEYS } from '@/lib/api/query-client'
import { useAuthStore } from './auth-store'

/**
 * Cached query data must not survive a change of member.
 *
 * Found in a browser against the live deployment: after signing out as one
 * member and in as another in the same tab, the notebook list still rendered the
 * FIRST member's notebooks - titles and descriptions included - until a manual
 * reload. The API was correct throughout; the disclosure was entirely in
 * TanStack Query's cache, which holds entries under keys like ['notebooks'] that
 * carry no member id and so cannot be told apart by member.
 *
 * staleTime is 5 minutes and gcTime 10, so the window is long enough to matter
 * on any shared machine - which is exactly how per-member access gets
 * demonstrated.
 */
describe('auth store clears cached data across a member switch', () => {
  const originalFetch = global.fetch

  beforeEach(() => {
    queryClient.clear()
    useAuthStore.setState({
      isAuthenticated: false,
      token: null,
      refreshToken: null,
      expiresAt: null,
      email: null,
      isAdmin: false,
    })
  })

  afterEach(() => {
    global.fetch = originalFetch
    vi.restoreAllMocks()
  })

  function seedAnotherMembersCache() {
    queryClient.setQueryData(QUERY_KEYS.notebooks, [
      { id: 'notebook:ada', name: 'Cell Biology 210', description: "Ada's private notebook" },
    ])
    queryClient.setQueryData(QUERY_KEYS.sources('notebook:ada'), [
      { id: 'source:ada1', title: 'Mitochondrial transport (lecture 4)' },
    ])
  }

  it('drops the previous member data on logout', () => {
    seedAnotherMembersCache()
    expect(queryClient.getQueryData(QUERY_KEYS.notebooks)).toBeDefined()

    useAuthStore.getState().logout()

    expect(queryClient.getQueryData(QUERY_KEYS.notebooks)).toBeUndefined()
    expect(queryClient.getQueryData(QUERY_KEYS.sources('notebook:ada'))).toBeUndefined()
  })

  it('drops it on a successful sign-in too, for a switch that never logged out', async () => {
    // The logout path alone is not enough: a restored tab, or the api client's
    // 401 interceptor dropping the session, both reach the login form with
    // another member's cache still populated.
    seedAnotherMembersCache()

    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        access_token: 'grace-token',
        refresh_token: 'grace-refresh',
        expires_in: 3600,
        email: 'grace@eeronotebook.invalid',
        is_admin: false,
      }),
    } as Response)

    const ok = await useAuthStore.getState().login('pw', 'grace@eeronotebook.invalid')

    expect(ok).toBe(true)
    expect(useAuthStore.getState().email).toBe('grace@eeronotebook.invalid')
    expect(queryClient.getQueryData(QUERY_KEYS.notebooks)).toBeUndefined()
    expect(queryClient.getQueryData(QUERY_KEYS.sources('notebook:ada'))).toBeUndefined()
  })

  it('keeps the new session after clearing, so sign-in is not self-defeating', async () => {
    // queryClient.clear() runs before the session is set. If the order were
    // reversed for convenience one day, the token would survive but this asserts
    // the session is actually live afterwards rather than cleared with the cache.
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: 'tok', expires_in: 3600, email: 'a@b.invalid' }),
    } as Response)

    await useAuthStore.getState().login('pw', 'a@b.invalid')

    expect(useAuthStore.getState().isAuthenticated).toBe(true)
    expect(useAuthStore.getState().token).toBe('tok')
  })

  it('does not clear the cache when sign-in fails', async () => {
    // A wrong password must not wipe the working session's data: the member is
    // still signed in as themselves.
    seedAnotherMembersCache()

    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: 'Invalid credentials' }),
    } as Response)

    const ok = await useAuthStore.getState().login('wrong', 'grace@eeronotebook.invalid')

    expect(ok).toBe(false)
    expect(queryClient.getQueryData(QUERY_KEYS.notebooks)).toBeDefined()
  })
})
