import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { API, server } from '../../test/server';
import { renderWithProviders } from '../../test/render';
import { NotesPage } from './NotesPage';

const note = (title: string) => ({
  id: crypto.randomUUID(),
  title,
  body: '',
  createdBy: 'user-1',
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
});

describe('NotesPage', () => {
  it('lists the notes from the API', async () => {
    server.use(
      http.get(`${API}/notes`, () =>
        HttpResponse.json({ data: [note('Shift handover')], page: 1, limit: 50, total: 1 }),
      ),
    );
    renderWithProviders(<NotesPage />);
    expect(await screen.findByRole('heading', { name: 'Shift handover' })).toBeInTheDocument();
  });

  it('shows the API error when a note cannot be saved', async () => {
    server.use(
      http.get(`${API}/notes`, () => HttpResponse.json({ data: [], page: 1, limit: 50, total: 0 })),
      http.post(`${API}/notes`, () =>
        HttpResponse.json(
          { error: { code: 'conflict', message: 'A note with this title already exists' } },
          { status: 409 },
        ),
      ),
    );
    renderWithProviders(<NotesPage />);
    await userEvent.type(screen.getByLabelText('Title'), 'Duplicate');
    await userEvent.click(screen.getByRole('button', { name: 'Add note' }));
    expect(await screen.findByText('A note with this title already exists')).toBeInTheDocument();
  });

  it('sends the signed-in user token', async () => {
    let auth: string | null = null;
    server.use(
      http.get(`${API}/notes`, ({ request }) => {
        auth = request.headers.get('authorization');
        return HttpResponse.json({ data: [], page: 1, limit: 50, total: 0 });
      }),
    );
    const { setTokenProvider } = await import('../../api/client');
    setTokenProvider(async () => 'token-abc');
    renderWithProviders(<NotesPage />);
    expect(await screen.findByText('No notes yet.')).toBeInTheDocument();
    expect(auth).toBe('Bearer token-abc');
  });
});
