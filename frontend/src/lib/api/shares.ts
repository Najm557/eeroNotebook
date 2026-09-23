import apiClient from './client'
import { CreateShareRequest, ShareResponse } from '@/lib/types/api'

/**
 * Share management. Owner-only on the server; the UI only offers these to an
 * owner (spec task 6.3).
 *
 * There is no `update`: a share carries one role and revoking is the only edit,
 * so an update call would exist only to send a role the schema refuses. And
 * `create` takes one address - no array, no group, no wildcard (Requirement 6.6).
 */
export const sharesApi = {
  list: async (notebookId: string) => {
    const response = await apiClient.get<ShareResponse[]>(
      `/notebooks/${notebookId}/shares`
    )
    return response.data
  },

  create: async (notebookId: string, data: CreateShareRequest) => {
    const response = await apiClient.post<ShareResponse>(
      `/notebooks/${notebookId}/shares`,
      data
    )
    return response.data
  },

  revoke: async (notebookId: string, memberId: string) => {
    const response = await apiClient.delete(
      `/notebooks/${notebookId}/shares/${memberId}`
    )
    return response.data
  },
}
