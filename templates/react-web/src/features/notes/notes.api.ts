import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, call } from '../../api/client';
import type { components } from '../../api/schema';

export type Note = components['schemas']['Note'];
export type CreateNote = components['schemas']['CreateNote'];

const keys = {
  all: ['notes'] as const,
  list: (search: string) => ['notes', 'list', search] as const,
};

export function useNotes(search = '') {
  return useQuery({
    queryKey: keys.list(search),
    queryFn: () =>
      call(
        api.GET('/notes', {
          params: { query: { search: search || undefined, page: 1, limit: 50 } },
        }),
      ),
  });
}

export function useCreateNote() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateNote) => call(api.POST('/notes', { body })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.all }),
  });
}
