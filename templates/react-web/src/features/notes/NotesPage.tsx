import { useState, type FormEvent } from 'react';
import { ApiError } from '../../api/client';
import { Button } from '../../components/Button';
import { Field } from '../../components/Field';
import { useCreateNote, useNotes } from './notes.api';

/** Reference page: a list from the API, a search box and a create form. */
export function NotesPage() {
  const [search, setSearch] = useState('');
  const notes = useNotes(search);
  const create = useCreateNote();
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');

  function submit(event: FormEvent) {
    event.preventDefault();
    // mutate (not mutateAsync): a failed save shows its error below instead of throwing.
    create.mutate(
      { title, body },
      {
        onSuccess: () => {
          setTitle('');
          setBody('');
        },
      },
    );
  }

  const createError =
    create.error instanceof ApiError
      ? create.error.message
      : create.error
        ? 'Could not save the note'
        : undefined;

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-semibold">Notes</h1>

      <form
        onSubmit={submit}
        aria-label="New note"
        className="space-y-3 rounded-lg bg-white p-4 shadow-sm"
      >
        <Field
          label="Title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          error={createError}
        />
        <Field label="Body" value={body} onChange={(e) => setBody(e.target.value)} />
        <Button type="submit" disabled={create.isPending}>
          Add note
        </Button>
      </form>

      <section aria-label="Notes" className="space-y-3">
        <Field
          label="Search notes"
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {notes.isPending && <p>Loading notes…</p>}
        {notes.isError && <p role="alert">Notes could not be loaded.</p>}
        {notes.data && notes.data.data.length === 0 && <p>No notes yet.</p>}
        <ul className="divide-y divide-gray-200 rounded-lg bg-white shadow-sm">
          {notes.data?.data.map((note) => (
            <li key={note.id} className="p-4">
              <h2 className="font-medium">{note.title}</h2>
              {note.body && <p className="text-sm text-gray-600">{note.body}</p>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
