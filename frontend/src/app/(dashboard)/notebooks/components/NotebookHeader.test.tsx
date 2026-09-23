import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi } from 'vitest'
import { NotebookHeader } from './NotebookHeader'
import { NotebookResponse } from '@/lib/types/api'

// useTranslation is mocked globally in setup.ts (t returns the key string).
vi.mock('@/lib/hooks/use-notebooks', () => ({
  useUpdateNotebook: () => ({ mutate: vi.fn(), mutateAsync: vi.fn() }),
  useNotebookDeletePreview: () => ({ data: undefined, isLoading: false }),
  useDeleteNotebook: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))

vi.mock('@/lib/hooks/use-shares', () => ({
  useNotebookShares: () => ({ data: [], isLoading: false }),
  useCreateShare: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRevokeShare: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

const notebook = (role: 'owner' | 'viewer'): NotebookResponse => ({
  id: 'notebook:mine',
  name: 'Biology',
  description: 'Cells',
  archived: false,
  created: new Date().toISOString(),
  updated: new Date().toISOString(),
  source_count: 2,
  note_count: 1,
  role,
})

function renderHeader(role: 'owner' | 'viewer') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <NotebookHeader notebook={notebook(role)} />
    </QueryClientProvider>
  )
}

describe('NotebookHeader', () => {
  it('offers sharing, archiving and deleting to the owner', () => {
    renderHeader('owner')
    expect(screen.getByText('notebooks.share')).toBeInTheDocument()
    expect(screen.getByText('notebooks.archive')).toBeInTheDocument()
    expect(screen.getByText('common.delete')).toBeInTheDocument()
  })

  it('offers a Viewer none of them', () => {
    // Every one of these is owner-only on the API (Requirements 5.2, 6.4), so
    // offering them would guarantee a 403 per click - and the delete dialog's
    // preview is itself owner-only, so it would fail before showing anything.
    renderHeader('viewer')
    expect(screen.queryByText('notebooks.share')).not.toBeInTheDocument()
    expect(screen.queryByText('notebooks.archive')).not.toBeInTheDocument()
    expect(screen.queryByText('common.delete')).not.toBeInTheDocument()
  })

  it('tells a Viewer their access is read-only', () => {
    renderHeader('viewer')
    expect(screen.getByText('notebooks.sharedWithYou')).toBeInTheDocument()
    expect(
      screen.getByText('notebooks.viewerReadOnlyNotice')
    ).toBeInTheDocument()
  })

  it('does not let a Viewer edit the name or description in place', () => {
    renderHeader('viewer')
    // The owner's name and description are click-to-edit buttons; a Viewer gets
    // plain text, so there is nothing to click that would then 403 on save.
    expect(screen.getByRole('heading', { name: 'Biology' })).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Biology' })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Cells' })
    ).not.toBeInTheDocument()
  })

  it('keeps the name editable for the owner', () => {
    renderHeader('owner')
    expect(screen.getByRole('button', { name: 'Biology' })).toBeInTheDocument()
  })
})
