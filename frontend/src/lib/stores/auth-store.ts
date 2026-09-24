import axios from 'axios'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import apiClient from '@/lib/api/client'
import { getApiUrl } from '@/lib/config'
import { queryClient } from '@/lib/api/query-client'

/**
 * Member sessions (spec task 5.4).
 *
 * Replaces upstream's shared-password flow, which probed `/api/notebooks` with a
 * candidate password and, if it got a 200, stored the password itself as the
 * token. Two things changed and both matter here:
 *
 * - Credentials are exchanged for a session at `/api/auth/login`, because the
 *   identity provider is unpublished and the browser cannot reach it.
 * - Access tokens expire after an hour, where the shared password never did. So
 *   the session has to be refreshed, or a member is signed out mid-sentence.
 *
 * The persisted key stays `auth-storage` and the access token stays at
 * `state.token`, so `getAuthToken()` and the api client's interceptor keep working
 * untouched.
 */

/** Refresh this far ahead of expiry, so a request in flight never carries a token
 * that expires on the way. */
const REFRESH_MARGIN_MS = 60_000

interface AuthState {
  isAuthenticated: boolean
  /** Access token. Named `token` because the interceptor and `getAuthToken` read it. */
  token: string | null
  refreshToken: string | null
  /** Epoch ms at which `token` stops being accepted. Null when it does not expire
   * (the operator's credential) or is unknown. */
  expiresAt: number | null
  email: string | null
  isAdmin: boolean
  isLoading: boolean
  error: string | null
  /** Translation key under `auth.` for a known failure, so the UI can render it
   * in the member's language. The store cannot call `t()` — it lives outside
   * React — so it names the reason and lets the component translate it. */
  errorCode: 'invalidCredentials' | 'sessionExpired' | null
  lastAuthCheck: number | null
  isCheckingAuth: boolean
  hasHydrated: boolean
  authRequired: boolean | null
  setHasHydrated: (state: boolean) => void
  checkAuthRequired: () => Promise<boolean>
  login: (password: string, email?: string) => Promise<boolean>
  logout: () => void
  checkAuth: () => Promise<boolean>
  /** Exchange the refresh token for a new session. Safe to call when there is
   * nothing to refresh — it reports false rather than throwing. */
  refreshSession: () => Promise<boolean>
  /** Refresh only when the current token is near expiry. */
  ensureFreshSession: () => Promise<boolean>
}

interface SessionPayload {
  access_token: string
  refresh_token?: string | null
  expires_in?: number | null
  email?: string | null
  is_admin?: boolean
}

function expiryFrom(expiresIn?: number | null): number | null {
  if (!expiresIn || expiresIn <= 0) {
    return null
  }
  return Date.now() + expiresIn * 1000
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      isAuthenticated: false,
      token: null,
      refreshToken: null,
      expiresAt: null,
      email: null,
      isAdmin: false,
      isLoading: false,
      error: null,
      errorCode: null,
      lastAuthCheck: null,
      isCheckingAuth: false,
      hasHydrated: false,
      authRequired: null,

      setHasHydrated: (state: boolean) => {
        set({ hasHydrated: state })
      },

      checkAuthRequired: async () => {
        try {
          const response = await apiClient.get<{ auth_enabled?: boolean }>('/auth/status', {
            headers: { 'Cache-Control': 'no-store' },
          })

          // Authentication is no longer optional, so this is always true. Kept
          // because the UI branches on it and a server that says otherwise should
          // be believed rather than second-guessed.
          const required = response.data.auth_enabled !== false
          set({ authRequired: required })
          return required
        } catch (error) {
          console.error('Failed to check auth status:', error)

          if (axios.isAxiosError(error) && !error.response) {
            set({
              error: 'Unable to connect to server. Please check if the API is running.',
              authRequired: null,
            })
          } else {
            // Default to requiring authentication. Assuming otherwise on an error
            // would hand out an unauthenticated session.
            set({ authRequired: true })
          }

          throw error
        }
      },

      login: async (password: string, email?: string) => {
        set({ isLoading: true, error: null, errorCode: null })
        try {
          const apiUrl = await getApiUrl()

          // Deliberately raw fetch, as upstream did here: this call carries no
          // session, so the interceptor must not attach a stale token or
          // hard-redirect on the 401 that a wrong password legitimately returns.
          const response = await fetch(`${apiUrl}/api/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(
              email && email.trim() ? { email: email.trim(), password } : { password }
            ),
          })

          if (response.ok) {
            const session = (await response.json()) as SessionPayload
            // Discard every cached query before the new session is live. Now
            // that notebooks are per-member, a cache entry is another member's
            // data: signing in after somebody else on the same browser would
            // otherwise render THEIR notebook list, titles and descriptions
            // included, for as long as staleTime lasts. The API never served it
            // to this member - it is a display-level disclosure, and indexed
            // caches cannot be told apart by member because the query keys
            // carry no member id.
            queryClient.clear()
            set({
              isAuthenticated: true,
              token: session.access_token,
              refreshToken: session.refresh_token ?? null,
              expiresAt: expiryFrom(session.expires_in),
              email: session.email ?? null,
              isAdmin: session.is_admin ?? false,
              isLoading: false,
              lastAuthCheck: Date.now(),
              error: null,
              errorCode: null,
            })
            return true
          }

          // 401 means the credentials were wrong. A 5xx means the identity
          // provider is unreachable, which is deliberately *not* reported as a bad
          // password — that sends people resetting a password that was fine.
          let errorMessage: string
          let errorCode: AuthState['errorCode'] = null
          if (response.status === 401) {
            errorMessage = 'Invalid email or password.'
            errorCode = 'invalidCredentials'
          } else if (response.status >= 500) {
            errorMessage = 'Sign-in is unavailable right now. Please try again shortly.'
          } else if (response.status === 422) {
            errorMessage = 'Enter a password to sign in.'
          } else {
            errorMessage = `Sign-in failed (${response.status})`
          }

          set({
            error: errorMessage,
            errorCode,
            isLoading: false,
            isAuthenticated: false,
            token: null,
            refreshToken: null,
            expiresAt: null,
          })
          return false
        } catch (error) {
          console.error('Network error during sign-in:', error)
          const errorMessage =
            error instanceof TypeError && error.message.includes('Failed to fetch')
              ? 'Unable to connect to server. Please check if the API is running.'
              : error instanceof Error
                ? `Network error: ${error.message}`
                : 'An unexpected error occurred during sign-in'

          set({
            error: errorMessage,
            isLoading: false,
            isAuthenticated: false,
            token: null,
            refreshToken: null,
            expiresAt: null,
          })
          return false
        }
      },

      refreshSession: async () => {
        const { refreshToken } = get()
        if (!refreshToken) {
          // The operator's credential has no refresh token and does not expire.
          return false
        }

        try {
          const apiUrl = await getApiUrl()
          const response = await fetch(`${apiUrl}/api/auth/refresh`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_token: refreshToken }),
          })

          if (!response.ok) {
            // A refused refresh token is terminal: the session is over, and
            // holding a dead one would only produce confusing 401s later.
            set({
              isAuthenticated: false,
              token: null,
              refreshToken: null,
              expiresAt: null,
              error: 'Your session has expired. Please sign in again.',
              errorCode: 'sessionExpired',
            })
            return false
          }

          const session = (await response.json()) as SessionPayload
          set({
            isAuthenticated: true,
            token: session.access_token,
            refreshToken: session.refresh_token ?? refreshToken,
            expiresAt: expiryFrom(session.expires_in),
            email: session.email ?? get().email,
            lastAuthCheck: Date.now(),
            error: null,
          })
          return true
        } catch (error) {
          // A network failure is not a dead session: keep the tokens so the next
          // attempt can succeed rather than signing the member out over a blip.
          console.error('Session refresh failed:', error)
          return false
        }
      },

      ensureFreshSession: async () => {
        const { token, expiresAt } = get()
        if (!token) {
          return false
        }
        if (expiresAt === null) {
          return true
        }
        if (Date.now() < expiresAt - REFRESH_MARGIN_MS) {
          return true
        }
        return get().refreshSession()
      },

      logout: () => {
        // Cleared here as well as on login, because the two cover different
        // failures: this one stops a signed-out member's data sitting in memory
        // for the next person at the keyboard, and the login side covers a
        // switch that never went through logout at all (a restored tab, or the
        // 401 interceptor dropping the session).
        queryClient.clear()
        set({
          isAuthenticated: false,
          token: null,
          refreshToken: null,
          expiresAt: null,
          email: null,
          isAdmin: false,
          error: null,
          errorCode: null,
          lastAuthCheck: null,
        })
      },

      checkAuth: async () => {
        const state = get()
        const { token, lastAuthCheck, isCheckingAuth, isAuthenticated } = state

        if (isCheckingAuth) {
          return isAuthenticated
        }
        if (!token) {
          return false
        }

        // An expiring token is renewed before it is tested, so a member is not
        // signed out simply for leaving a tab open.
        if (state.expiresAt !== null && Date.now() >= state.expiresAt - REFRESH_MARGIN_MS) {
          const refreshed = await get().refreshSession()
          if (!refreshed) {
            return false
          }
        }

        const now = Date.now()
        if (isAuthenticated && lastAuthCheck && now - lastAuthCheck < 30000) {
          return true
        }

        set({ isCheckingAuth: true })

        try {
          const apiUrl = await getApiUrl()
          // Raw fetch again: a 401 here must update store state rather than
          // trigger the interceptor's clear-and-redirect.
          const response = await fetch(`${apiUrl}/api/auth/me`, {
            method: 'GET',
            headers: {
              Authorization: `Bearer ${get().token}`,
              'Content-Type': 'application/json',
            },
          })

          if (response.ok) {
            const me = (await response.json()) as {
              email?: string | null
              is_admin?: boolean
            }
            set({
              isAuthenticated: true,
              email: me.email ?? get().email,
              isAdmin: me.is_admin ?? get().isAdmin,
              lastAuthCheck: now,
              isCheckingAuth: false,
            })
            return true
          }

          set({
            isAuthenticated: false,
            token: null,
            refreshToken: null,
            expiresAt: null,
            lastAuthCheck: null,
            isCheckingAuth: false,
          })
          return false
        } catch (error) {
          console.error('checkAuth error:', error)
          // Network failure, not a rejected session: drop the authenticated flag
          // but keep the tokens, so a blip does not force a fresh sign-in.
          set({ isAuthenticated: false, lastAuthCheck: null, isCheckingAuth: false })
          return false
        }
      },
    }),
    {
      name: 'auth-storage',
      partialize: (state) => ({
        token: state.token,
        refreshToken: state.refreshToken,
        expiresAt: state.expiresAt,
        email: state.email,
        isAdmin: state.isAdmin,
        isAuthenticated: state.isAuthenticated,
      }),
      onRehydrateStorage: () => (state) => {
        state?.setHasHydrated(true)
      },
    }
  )
)
