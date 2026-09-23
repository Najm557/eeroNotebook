import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { NotebookShareDialog } from './NotebookShareDialog'
import {
  useCreateShare,
  useNotebookShares,
  useRevokeShare,
} from '@/lib/hooks/use-shares'

// useTranslation is mocked globally in setup.ts (t returns the key string).
vi.mock('@/lib/hooks/use-shares', () => ({
  useNotebookShares: vi.fn(),
  useCreateShare: vi.fn(),
  useRevokeShare: vi.fn(),
}))

const mockShares = vi.mocked(useNotebookShares)
const mockCreate = vi.mocked(useCreateShare)
const mockRevoke = vi.mocked(useRevokeShare)

type SharesResult = ReturnType<typeof useNotebookShares>
type MutationResult = ReturnType<typeof useCreateShare>

const asShares = (value: Partial<SharesResult>) => value as SharesResult
const asMutation = (value: Partial<MutationResult>) => value as MutationResult

function renderDialog() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <NotebookShareDialog
        open
        onOpenChange={vi.fn()}
        notebookId="notebook:mine"
        notebookName="Biology"
      />
    </QueryClientProvider>
  )
}

describe('NotebookShareDialog', () => {
  let createMutate: ReturnType<typeof vi.fn>
  let revokeMutate: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    createMutate = vi.fn().mockResolvedValue(undefined)
    revokeMutate = vi.fn().mockResolvedValue(undefined)
    mockCreate.mockReturnValue(
      asMutation({
        mutateAsync: createMutate as unknown as MutationResult['mutateAsync'],
        isPending: false,
      })
    )
    mockRevoke.mockReturnValue(
      asMutation({
        mutateAsync: revokeMutate as unknown as MutationResult['mutateAsync'],
        isPending: false,
      })
    )
    mockShares.mockReturnValue(asShares({ data: [], isLoading: false }))
  })

  it('offers one address field and no way to name a second person', () => {
    // Requirement 6.6, held by the shape of the form rather than by a rule: no
    // multi-select, no "everyone", no role picker. A second recipient means
    // submitting twice, deliberately.
    renderDialog()

    const emails = screen.getAllByRole('textbox')
    expect(emails).toHaveLength(1)
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.getByText('notebooks.shareOnePersonHint')).toBeInTheDocument()
  })

  it('grants access to the typed address', async () => {
    renderDialog()

    fireEvent.change(screen.getByLabelText('notebooks.shareEmailLabel'), {
      target: { value: 'student@example.invalid' },
    })
    // The submit button is gated on validation, which resolves asynchronously.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /shareGrant/ })).toBeEnabled()
    )
    fireEvent.click(screen.getByRole('button', { name: /shareGrant/ }))

    await waitFor(() =>
      expect(createMutate).toHaveBeenCalledWith('student@example.invalid')
    )
  })

  it('does not submit something that cannot be an address', async () => {
    renderDialog()

    fireEvent.change(screen.getByLabelText('notebooks.shareEmailLabel'), {
      target: { value: 'everyone' },
    })
    fireEvent.click(screen.getByRole('button', { name: /shareGrant/ }))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /shareGrant/ })).toBeDisabled()
    )
    expect(createMutate).not.toHaveBeenCalled()
  })

  it('lists who has access, always as a viewer', () => {
    mockShares.mockReturnValue(
      asShares({
        data: [
          {
            notebook_id: 'notebook:mine',
            member_id: 'member:student',
            email: 'student@example.invalid',
            role: 'viewer',
            created: new Date().toISOString(),
          },
        ],
        isLoading: false,
      })
    )
    renderDialog()

    expect(screen.getByText('student@example.invalid')).toBeInTheDocument()
    expect(screen.getByText('notebooks.shareRoleViewer')).toBeInTheDocument()
  })

  it('labels a share whose member has no address rather than dropping it', () => {
    // An owner must still be able to revoke a grant we cannot name.
    mockShares.mockReturnValue(
      asShares({
        data: [
          {
            notebook_id: 'notebook:mine',
            member_id: 'member:operator',
            email: null,
            role: 'viewer',
            created: new Date().toISOString(),
          },
        ],
        isLoading: false,
      })
    )
    renderDialog()

    expect(screen.getByText('notebooks.shareUnnamedMember')).toBeInTheDocument()
  })

  it('confirms before revoking, and says nothing is deleted', async () => {
    mockShares.mockReturnValue(
      asShares({
        data: [
          {
            notebook_id: 'notebook:mine',
            member_id: 'member:student',
            email: 'student@example.invalid',
            role: 'viewer',
            created: new Date().toISOString(),
          },
        ],
        isLoading: false,
      })
    )
    renderDialog()

    fireEvent.click(screen.getByRole('button', { name: 'notebooks.shareRevoke' }))
    expect(revokeMutate).not.toHaveBeenCalled()

    // Requirement 6.5 is what the confirmation text has to convey: access ends,
    // content does not.
    await waitFor(() =>
      expect(screen.getByText('notebooks.shareRevokeConfirm')).toBeInTheDocument()
    )

    // Two buttons carry this label once the confirmation is open: the row's icon
    // button and the dialog's action. The last one is the confirmation.
    const confirmations = screen.getAllByRole('button', {
      name: 'notebooks.shareRevoke',
    })
    fireEvent.click(confirmations[confirmations.length - 1])
    await waitFor(() =>
      expect(revokeMutate).toHaveBeenCalledWith('member:student')
    )
  })

  it('says so when nobody else has access', () => {
    renderDialog()
    expect(screen.getByText('notebooks.shareNobodyYet')).toBeInTheDocument()
  })
})
