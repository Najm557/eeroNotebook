import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { sharesApi } from '@/lib/api/shares'
import { QUERY_KEYS } from '@/lib/api/query-client'
import { useToast } from '@/lib/hooks/use-toast'
import { useTranslation } from '@/lib/hooks/use-translation'
import { getApiErrorMessage } from '@/lib/utils/error-handler'

/**
 * Who a notebook is shared with (spec task 6.3).
 *
 * `enabled` is a parameter because the route is owner-only: fetching it for a
 * Viewer would be a guaranteed 403 on every render of a notebook they can
 * legitimately read. The caller passes the dialog's open state and the owner
 * check.
 */
export function useNotebookShares(notebookId: string, enabled: boolean = false) {
  return useQuery({
    queryKey: QUERY_KEYS.notebookShares(notebookId),
    queryFn: () => sharesApi.list(notebookId),
    enabled: !!notebookId && enabled,
  })
}

export function useCreateShare(notebookId: string) {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (email: string) => sharesApi.create(notebookId, { email }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: QUERY_KEYS.notebookShares(notebookId),
      })
      toast({
        title: t('common.success'),
        description: t('notebooks.shareGrantSuccess'),
      })
    },
    onError: (error: unknown) => {
      // The backend's own message is shown rather than a generic one. "Ask them
      // to sign in once and then share again" is the whole value of that
      // response, and a mapped generic error would throw it away.
      toast({
        title: t('common.error'),
        description: getApiErrorMessage(error, t, 'notebooks.shareGrantFailed'),
        variant: 'destructive',
      })
    },
  })
}

export function useRevokeShare(notebookId: string) {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (memberId: string) => sharesApi.revoke(notebookId, memberId),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: QUERY_KEYS.notebookShares(notebookId),
      })
      // The revoked member's own notebook list changes, and an owner may be
      // looking at both in two tabs; broad invalidation is the house style.
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.notebooks })
      toast({
        title: t('common.success'),
        description: t('notebooks.shareRevokeSuccess'),
      })
    },
    onError: (error: unknown) => {
      toast({
        title: t('common.error'),
        description: getApiErrorMessage(error, t, 'notebooks.shareRevokeFailed'),
        variant: 'destructive',
      })
    },
  })
}
