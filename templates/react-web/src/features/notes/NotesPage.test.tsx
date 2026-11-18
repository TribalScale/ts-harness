import { screen, waitFor } from '@testing-library/react';
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

  it('adds a note and clears the form', async () => {
    const notes: ReturnType<typeof note>[] = [];
    server.use(
      http.get(`${API}/notes`, () =>
        HttpResponse.json({ data: notes, page: 1, limit: 50, total: notes.length }),
      ),
      http.post(`${API}/notes`, async ({ request }) => {
        const body = (await request.json()) as { title: string; body: string };
        const created = { ...note(body.title), body: body.body };
        notes.push(created);
        return HttpResponse.json(created, { status: 201 });
      }),
    );
    renderWithProviders(<NotesPage />);
    await userEvent.type(screen.getByLabelText('Title'), 'Weekly plan');
    await userEvent.type(screen.getByLabelText('Body'), 'Line 3 first');
    await userEvent.click(screen.getByRole('button', { name: 'Add note' }));
    expect(await screen.findByRole('heading', { name: 'Weekly plan' })).toBeInTheDocument();
    expect(screen.getByText('Line 3 first')).toBeInTheDocument();
    expect(screen.getByLabelText('Title')).toHaveValue('');
  });

  it('searches notes by title', async () => {
    let search: string | null = null;
    server.use(
      http.get(`${API}/notes`, ({ request }) => {
        search = new URL(request.url).searchParams.get('search');
        return HttpResponse.json({ data: [], page: 1, limit: 50, total: 0 });
      }),
    );
    renderWithProviders(<NotesPage />);
    await userEvent.type(screen.getByLabelText('Search notes'), 'plan');
    await waitFor(() => expect(search).toBe('plan'));
  });

  it('says when notes cannot be loaded', async () => {
    server.use(
      http.get(`${API}/notes`, () =>
        HttpResponse.json({ error: { code: 'internal', message: 'x' } }, { status: 500 }),
      ),
    );
    renderWithProviders(<NotesPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Notes could not be loaded.');
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
