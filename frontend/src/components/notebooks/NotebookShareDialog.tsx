'use client'

import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Trash2, UserPlus } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { ConfirmDialog } from '@/components/common/ConfirmDialog'
import { LoadingSpinner } from '@/components/common/LoadingSpinner'
import {
  useCreateShare,
  useNotebookShares,
  useRevokeShare,
} from '@/lib/hooks/use-shares'
import { useTranslation } from '@/lib/hooks/use-translation'
import { getDateLocale } from '@/lib/utils/date-locale'
import { ShareResponse } from '@/lib/types/api'

/**
 * Share management for one notebook (spec task 6.3).
 *
 * **One address per grant, and that is the whole shape of the form.** There is no
 * multi-select, no "everyone", and no role picker, which is how Requirement 6.6 -
 * no single action makes a notebook visible to the team at large - is met here as
 * it is in the schema and in the API. Adding a second recipient means submitting
 * the form twice, deliberately.
 *
 * Only ever rendered for an owner. The routes behind it are owner-only, so
 * `useNotebookShares` is not enabled until the dialog is open.
 */
const shareSchema = z.object({
  // Shallow on purpose: the server matches the address against members of this
  // instance, so the only thing worth catching here is a value that obviously is
  // not an address. Anything stricter would reject a valid address the provider
  // accepts and leave the owner with no way through.
  email: z.string().min(3).regex(/^[^\s@]+@[^\s@]+$/),
})

type ShareFormData = z.infer<typeof shareSchema>

interface NotebookShareDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  notebookId: string
  notebookName: string
}

export function NotebookShareDialog({
  open,
  onOpenChange,
  notebookId,
  notebookName,
}: NotebookShareDialogProps) {
  const { t, language } = useTranslation()
  const [pendingRevoke, setPendingRevoke] = useState<ShareResponse | null>(null)

  const { data: shares, isLoading } = useNotebookShares(notebookId, open)
  const createShare = useCreateShare(notebookId)
  const revokeShare = useRevokeShare(notebookId)

  const {
    register,
    handleSubmit,
    reset,
    formState: { isValid },
  } = useForm<ShareFormData>({
    resolver: zodResolver(shareSchema),
    mode: 'onChange',
    defaultValues: { email: '' },
  })

  useEffect(() => {
    if (!open) {
      reset()
      setPendingRevoke(null)
    }
  }, [open, reset])

  const onSubmit = async (data: ShareFormData) => {
    try {
      await createShare.mutateAsync(data.email)
      reset()
    } catch {
      // The hook has already shown the reason, and it is the useful one: an
      // address with no member here is told to sign in once first. Keeping the
      // typed address in the field lets the owner retry without retyping.
    }
  }

  const confirmRevoke = async () => {
    if (!pendingRevoke) return
    try {
      await revokeShare.mutateAsync(pendingRevoke.member_id)
    } finally {
      setPendingRevoke(null)
    }
  }

  const label = (share: ShareResponse) =>
    share.email || t('notebooks.shareUnnamedMember')

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{t('notebooks.shareNotebook')}</DialogTitle>
            <DialogDescription>
              {t('notebooks.shareNotebookDesc', { name: notebookName })}
            </DialogDescription>
          </DialogHeader>

          <form onSubmit={handleSubmit(onSubmit)} className="space-y-2">
            <Label htmlFor="share-email">{t('notebooks.shareEmailLabel')}</Label>
            <div className="flex gap-2">
              <Input
                id="share-email"
                type="email"
                autoComplete="off"
                placeholder={t('notebooks.shareEmailPlaceholder')}
                {...register('email')}
              />
              <Button type="submit" disabled={!isValid || createShare.isPending}>
                <UserPlus className="h-4 w-4 mr-2" />
                {t('notebooks.shareGrant')}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {t('notebooks.shareOnePersonHint')}
            </p>
          </form>

          <div className="border-t pt-4">
            {isLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <LoadingSpinner size="sm" />
                <span>{t('notebooks.shareLoading')}</span>
              </div>
            ) : !shares || shares.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {t('notebooks.shareNobodyYet')}
              </p>
            ) : (
              <ul className="space-y-2">
                {shares.map((share) => (
                  <li
                    key={share.member_id}
                    className="flex items-center justify-between gap-3 rounded-md border px-3 py-2"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-medium truncate">{label(share)}</p>
                      <p className="text-xs text-muted-foreground">
                        {t('notebooks.shareGrantedAgo', {
                          time: formatDistanceToNow(new Date(share.created), {
                            addSuffix: true,
                            locale: getDateLocale(language),
                          }),
                        })}
                      </p>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <Badge variant="secondary">{t('notebooks.shareRoleViewer')}</Badge>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={t('notebooks.shareRevoke')}
                        className="text-red-600 hover:text-red-700"
                        onClick={() => setPendingRevoke(share)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={Boolean(pendingRevoke)}
        onOpenChange={(next) => {
          if (!next) setPendingRevoke(null)
        }}
        title={t('notebooks.shareRevoke')}
        description={t('notebooks.shareRevokeConfirm', {
          name: pendingRevoke ? label(pendingRevoke) : '',
        })}
        confirmText={t('notebooks.shareRevoke')}
        onConfirm={confirmRevoke}
        isLoading={revokeShare.isPending}
        confirmVariant="destructive"
      />
    </>
  )
}
