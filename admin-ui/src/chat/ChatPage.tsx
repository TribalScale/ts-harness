import React, { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type { SessionSummary, TranscriptHit } from "../api/types";
import { Button } from "../components/Button";
import { PlusIcon } from "../components/PlusIcon";
import { PageState } from "../components/PageState";
import { useMediaQuery } from "../layout/useMediaQuery";
import { ChatThread } from "./ChatThread";

function createWebBinding(): string {
  const id =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `web:${id}`;
}

export function ChatPage({
  sessionId,
  prefill,
  onSelectSession,
}: {
  sessionId?: string;
  prefill?: string;
  onSelectSession: (id: string) => void;
}) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [listOpen, setListOpen] = useState(false);
  const desktop = useMediaQuery("(min-width: 1024px)");
  const [transcriptQuery, setTranscriptQuery] = useState("");
  const [transcriptHits, setTranscriptHits] = useState<TranscriptHit[]>([]);
  const [transcriptSearching, setTranscriptSearching] = useState(false);
  const [bindingKey, setBindingKey] = useState(() => {
    const existing = sessionStorage.getItem("ec_web_binding");
    if (existing) return existing;
    const key = createWebBinding();
    sessionStorage.setItem("ec_web_binding", key);
    return key;
  });

  const loadSessions = useCallback(async () => {
    try {
      const data = await apiFetch<{ sessions: SessionSummary[] }>("/admin/sessions?channel=web");
      setSessions(data.sessions || []);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  async function searchTranscripts(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTranscriptSearching(true);
    setError(null);
    try {
      const data = await apiFetch<{ hits?: TranscriptHit[] }>(
        `/admin/sessions/search?q=${encodeURIComponent(transcriptQuery)}`
      );
      setTranscriptHits(data.hits || []);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setTranscriptSearching(false);
    }
  }

  function startNewConversation() {
    const key = createWebBinding();
    sessionStorage.setItem("ec_web_binding", key);
    setBindingKey(key);
    setListOpen(false);
    onSelectSession("");
  }

  function openSession(id: string) {
    const session = sessions.find((item) => item.session_id === id);
    const key = String(session?.binding_key || "").trim();
    if (key) {
      sessionStorage.setItem("ec_web_binding", key);
      setBindingKey(key);
    }
    setListOpen(false);
    onSelectSession(id);
  }

  const list = (
    <div className="panel-pad chat-list">
      {!desktop && (
        <Button variant="secondary" size="sm" type="button" onClick={() => setListOpen(false)}>
          Back
        </Button>
      )}
      <div className="chat-list-header">
        <h2>Chat</h2>
        <p className="muted">Web channel (Slack remains available)</p>
      </div>
      <form
        className="transcript-search"
        aria-label="Transcript search"
        onSubmit={searchTranscripts}
      >
        <label>
          <span>Search transcripts</span>
          <input
            aria-label="Search transcripts"
            value={transcriptQuery}
            onChange={(e) => setTranscriptQuery(e.target.value)}
          />
        </label>
        <Button type="submit" busy={transcriptSearching}>
          Search
        </Button>
      </form>
      {transcriptHits.length > 0 && (
        <div className="transcript-hits" aria-label="Transcript search results">
          {transcriptHits.map((hit) => (
            <button
              key={`${hit.session_id}-${hit.seq}`}
              className="row-card"
              onClick={() => openSession(hit.session_id)}
            >
              <strong>{hit.snippet}</strong>
              <div className="muted mono">{hit.session_id}</div>
              <div className="muted">
                {hit.role}
                {hit.created_at ? ` · ${hit.created_at}` : ""}
              </div>
            </button>
          ))}
        </div>
      )}
      <div className="thread-list" aria-label="Recent threads">
        {sessions.map((s) => (
          <button
            key={s.session_id}
            className={`row-card ${sessionId === s.session_id ? "selected" : ""}`}
            onClick={() => openSession(s.session_id)}
          >
            <strong className="mono">{s.session_id.slice(0, 8)}</strong>
            <div className="muted">{s.agent_id || "Agent"}</div>
            <div className="muted">{s.started_at || "Start time unavailable"}</div>
          </button>
        ))}
      </div>
      {!sessions.length && (
        <PageState
          kind="empty"
          title="No conversation history"
          detail="New web conversations will appear here after the first message."
        />
      )}
    </div>
  );

  const showList = desktop || listOpen;
  const showThread = desktop || !listOpen;

  return (
    <div className="split chat-layout">
      <aside
        className="split-list"
        aria-label="Conversations"
        hidden={!showList}
        style={showList ? undefined : { display: "none" }}
      >
        {list}
      </aside>
      <section
        className="split-detail"
        hidden={!showThread}
        style={showThread ? undefined : { display: "none" }}
      >
        <div className="chat-toolbar">
          {!desktop && (
            <Button variant="secondary" size="sm" type="button" onClick={() => setListOpen(true)}>
              History
            </Button>
          )}
          <Button
            variant="primary"
            size="sm"
            type="button"
            className="chat-new-button"
            icon={<PlusIcon />}
            onClick={startNewConversation}
          >
            New chat
          </Button>
        </div>
        <ChatThread
          sessionId={sessionId}
          prefill={prefill}
          sessions={sessions}
          bindingKey={bindingKey}
          error={error}
          onSelectSession={onSelectSession}
          onSessionsChanged={loadSessions}
          onDetailHeld={() => undefined}
          onError={setError}
        />
      </section>
    </div>
  );
}
