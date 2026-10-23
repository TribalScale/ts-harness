import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  unstable_useComposerInput,
  useExternalStoreRuntime,
  type AppendMessage,
  type MessageState,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import React, { useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { chatReplyText, messageDisplayText } from "../api/normalizers";
import type { ChatMessage, ProgramStatus, SessionMessageResponse, SessionSummary } from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { isWorkingActivity } from "./working";

interface DisplayMessage extends ChatMessage {
  id: string;
  failed?: boolean;
}

const SAMPLE_PROMPTS = [
  "Show blocked work",
  "What approvals need attention?",
  "Summarize delivery status",
];

type SkillHint = { id: string; agents?: string[] };

/** Same stems `resolve_for_channel_chat` drops; listing them would pin-then-block. */
const CHANNEL_CHAT_EXCLUDED_IDS = new Set(["workflow-state", "governed-delivery", "fan-out"]);

/** Resending a failed message replaces its bubble instead of stacking a duplicate. */
function dropTrailingFailure(messages: DisplayMessage[], text: string): DisplayMessage[] {
  const last = messages[messages.length - 1];
  if (last?.failed && last.role === "user" && last.content === text) {
    return messages.slice(0, -1);
  }
  return messages;
}

function nextMessageId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function currentAgentId(sessionId: string | undefined, sessions: SessionSummary[]): string {
  const match = sessions.find((session) => session.session_id === sessionId);
  return match?.agent_id || "orchestrator";
}

function leadingSlashQuery(draft: string, caret: number): string | null {
  if (!draft.startsWith("/")) return null;
  const first = draft.split(/\s/, 1)[0];
  if (caret > first.length) return null;
  return first.slice(1);
}

function applySkillPin(draft: string, skillId: string): string {
  if (draft.startsWith("/")) {
    const rest = draft.replace(/^\/\S*/, "").replace(/^\s+/, "");
    return rest ? `/${skillId} ${rest}` : `/${skillId}`;
  }
  return draft ? `/${skillId} ${draft}` : `/${skillId}`;
}

function appendText(message: AppendMessage): string {
  for (const part of message.content) {
    if (part.type === "text" && "text" in part && typeof part.text === "string") return part.text;
  }
  return "";
}

function convertMessage(message: DisplayMessage): ThreadMessageLike {
  const role = message.role === "user" ? "user" : "assistant";
  return {
    id: message.id,
    role,
    content: messageDisplayText(message),
    status: role === "assistant" ? { type: "complete", reason: "stop" } : undefined,
    metadata: message.failed ? { custom: { failed: true } } : undefined,
  };
}

/** Plain text only. Streaming polish stays off so a finished reply is in the DOM at once. */
function PlainText() {
  return <MessagePartPrimitive.Text smooth={false} />;
}

function ChatBubble({ message }: { message: MessageState }) {
  const failed = Boolean(message.metadata.custom.failed);
  const sender = message.role === "user" ? "user" : "assistant";
  return (
    <MessagePrimitive.Root
      className={`bubble ${sender}${failed ? " failed" : ""}`}
      aria-label={failed ? "Failed message" : undefined}
    >
      <MessagePrimitive.Parts components={{ Text: PlainText }} />
      {failed ? <span className="bubble-status">Not sent</span> : null}
    </MessagePrimitive.Root>
  );
}

function ConsoleComposer({
  agentId,
  busy,
  prefill,
  samplePrompts,
  placeholder,
  restoreDraft,
  onText,
  onRejectEmpty,
}: {
  agentId: string;
  busy: boolean;
  prefill?: string;
  samplePrompts: string[];
  placeholder: string;
  restoreDraft: React.MutableRefObject<(text: string) => void>;
  onText: (text: string) => void;
  onRejectEmpty: () => void;
}) {
  const { value, setText, send } = unstable_useComposerInput();
  const [caret, setCaret] = useState(0);
  const [skillHints, setSkillHints] = useState<SkillHint[]>([]);
  restoreDraft.current = setText;

  useEffect(() => {
    onText(value);
  }, [onText, value]);

  useEffect(() => {
    if (prefill) {
      setText(prefill);
      setCaret(prefill.length);
    }
  }, [prefill, setText]);

  const slashQuery = leadingSlashQuery(value, caret);
  const pinOptions = skillHints.filter((skill) => {
    if (CHANNEL_CHAT_EXCLUDED_IDS.has(skill.id)) return false;
    if (!(skill.agents || []).includes(agentId)) return false;
    if (!slashQuery) return true;
    return skill.id.toLowerCase().includes(slashQuery.toLowerCase());
  });

  useEffect(() => {
    if (slashQuery === null) {
      setSkillHints([]);
      return;
    }
    let cancelled = false;
    apiFetch<{ skills?: SkillHint[] }>("/admin/skills")
      .then((data) => {
        if (!cancelled) setSkillHints(data.skills || []);
      })
      .catch(() => {
        if (!cancelled) setSkillHints([]);
      });
    return () => {
      cancelled = true;
    };
  }, [slashQuery === null ? null : "open"]);

  function submit() {
    if (busy) return;
    if (!value.trim()) {
      onRejectEmpty();
      return;
    }
    send();
  }

  function trackCaret(target: HTMLTextAreaElement) {
    setCaret(target.selectionStart ?? target.value.length);
  }

  return (
    <div className="chat-composer-dock">
      <div className="sample-prompts" aria-label="Sample prompts">
        {samplePrompts.map((prompt) => (
          <Button key={prompt} type="button" variant="secondary" size="sm" onClick={() => setText(prompt)}>
            {prompt}
          </Button>
        ))}
      </div>
      <ComposerPrimitive.Root className="composer">
        <div className="composer-input">
          <ComposerPrimitive.Input
            aria-label="Message"
            placeholder={placeholder}
            submitMode="none"
            onClick={(event) => trackCaret(event.currentTarget)}
            onKeyUp={(event) => trackCaret(event.currentTarget)}
            onSelect={(event) => trackCaret(event.currentTarget)}
            onKeyDown={(event) => {
              trackCaret(event.currentTarget);
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
          />
          {slashQuery !== null && pinOptions.length > 0 && (
            <ul className="skill-pin-list" role="listbox" aria-label="Skill pins">
              {pinOptions.map((skill) => (
                <li key={skill.id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected="false"
                    onClick={() => {
                      const next = applySkillPin(value, skill.id);
                      setText(next);
                      setCaret(next.split(/\s/, 1)[0].length);
                    }}
                  >
                    {skill.id}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <Button variant="primary" type="button" busy={busy} onClick={submit}>
          Send
        </Button>
      </ComposerPrimitive.Root>
    </div>
  );
}

/**
 * assistant-ui thread for the web channel. Primitives are styled with the console
 * classes in styles.css. Do not replace this with the library's Tailwind thread
 * starter; a later design system should restyle these same primitives.
 */
export function ChatThread({
  sessionId,
  prefill,
  sessions,
  bindingKey,
  error,
  endpoint,
  extraBody,
  agentId: agentIdOverride,
  samplePrompts = SAMPLE_PROMPTS,
  placeholder = "Message the orchestrator…",
  emptyTitle = "Start a conversation",
  emptyDetail = "Message the orchestrator to inspect work, approvals, and delivery status.",
  onSelectSession,
  onSessionsChanged,
  onDetailHeld,
  onError,
}: {
  sessionId?: string;
  prefill?: string;
  sessions: SessionSummary[];
  bindingKey: string;
  error: string | null;
  /** Posts `{text, session_id, ...extraBody}` here instead of the web `/session/message` body. */
  endpoint?: string;
  extraBody?: Record<string, unknown>;
  agentId?: string;
  samplePrompts?: string[];
  placeholder?: string;
  emptyTitle?: string;
  emptyDetail?: string;
  onSelectSession: (id: string) => void;
  onSessionsChanged: () => Promise<void> | void;
  onDetailHeld: (held: boolean) => void;
  onError: (message: string | null) => void;
}) {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [programId, setProgramId] = useState("");
  const [activity, setActivity] = useState("");
  const [composerText, setComposerText] = useState("");
  const activeSession = useRef(sessionId || "");
  const pendingSendSession = useRef<string | null>(null);
  const restoreDraft = useRef<(text: string) => void>(() => undefined);

  useEffect(() => {
    onDetailHeld(messages.length > 0 || composerText.length > 0);
    return () => onDetailHeld(false);
  }, [composerText.length, messages.length, onDetailHeld]);

  async function loadMessages(id: string, { preserveLocal = false } = {}) {
    try {
      const data = await apiFetch<{ messages?: ChatMessage[]; program_id?: string }>(
        `/admin/sessions/${encodeURIComponent(id)}/messages`
      );
      if (activeSession.current !== id) return;
      setProgramId(String(data.program_id || ""));
      if (!Array.isArray(data.messages)) return;
      const displayable = data.messages
        .filter((message) => messageDisplayText(message).trim())
        .map((message, index) => ({
          ...message,
          id: `transcript-${index}-${message.role || "note"}`,
        }));
      if (!displayable.length && preserveLocal) return;
      setMessages(displayable);
    } catch (e: any) {
      if (activeSession.current !== id) return;
      onError(String(e.message || e));
    }
  }

  useEffect(() => {
    const id = sessionId || "";
    activeSession.current = id;
    if (!id) {
      setMessages([]);
      setProgramId("");
      setActivity("");
      return;
    }
    const adoptedFromSend = pendingSendSession.current === id;
    pendingSendSession.current = null;
    if (!adoptedFromSend) {
      setMessages([]);
      setProgramId("");
      setActivity("");
    }
    void loadMessages(id, { preserveLocal: adoptedFromSend });
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    const timer = window.setInterval(() => {
      void loadMessages(sessionId, { preserveLocal: true });
    }, 5000);
    return () => window.clearInterval(timer);
  }, [sessionId]);

  useEffect(() => {
    if (!programId) {
      setActivity("");
      return;
    }
    const watchedSession = activeSession.current;
    let cancelled = false;
    async function loadActivity() {
      try {
        const status = await apiFetch<ProgramStatus>(
          `/admin/programs/${encodeURIComponent(programId)}/status`
        );
        if (cancelled || activeSession.current !== watchedSession) return;
        setActivity(String(status.status || ""));
      } catch {
        if (!cancelled && activeSession.current === watchedSession) setActivity("");
      }
    }
    void loadActivity();
    const timer = window.setInterval(() => {
      void loadActivity();
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [programId]);

  async function onNew(message: AppendMessage) {
    const text = appendText(message).trim();
    if (!text) {
      onError("Enter a message before sending.");
      return;
    }
    setBusy(true);
    onError(null);
    const userMessage: DisplayMessage = { id: nextMessageId(), role: "user", content: text };
    setMessages((prev) => [...dropTrailingFailure(prev, text), userMessage]);
    try {
      const body = endpoint
        ? { ...extraBody, text, session_id: sessionId || undefined }
        : {
            text,
            channel: "web",
            source: "web",
            thread_id: bindingKey,
            binding_key: bindingKey,
            session_id: sessionId || undefined,
          };
      const result = await apiFetch<SessionMessageResponse>(endpoint || "/session/message", {
        method: "POST",
        body: JSON.stringify(body),
      });
      const newSession = String(result.session_id || sessionId || "");
      if (newSession && newSession !== sessionId) {
        pendingSendSession.current = newSession;
        activeSession.current = newSession;
        onSelectSession(newSession);
      }
      if (result.status === "blocked") {
        const summary =
          typeof result.output === "object" && result.output && "summary" in result.output
            ? String(result.output.summary || "")
            : "";
        onError(summary || result.error || "Request blocked");
        setMessages((prev) =>
          prev.map((item) => (item.id === userMessage.id ? { ...item, failed: true } : item))
        );
        return;
      }
      const reply = chatReplyText(result).trim();
      if (reply) {
        setMessages((prev) => [
          ...prev,
          { id: nextMessageId(), role: "assistant", content: reply },
        ]);
      }
      await onSessionsChanged();
      if (newSession) await loadMessages(newSession, { preserveLocal: true });
    } catch (e: any) {
      setMessages((prev) =>
        prev.map((item) => (item.id === userMessage.id ? { ...item, failed: true } : item))
      );
      restoreDraft.current(text);
      onError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  const runtime = useExternalStoreRuntime({
    messages,
    convertMessage,
    onNew,
    isSendDisabled: busy,
  });

  const agentId = agentIdOverride || currentAgentId(sessionId, sessions);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className={`chat-detail${sessionId || messages.length > 0 ? "" : " is-empty"}`}>
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        <div className="chat-stage">
          {!sessionId && messages.length === 0 && (
            <div className="chat-empty">
              <PageState kind="empty" title={emptyTitle} detail={emptyDetail} />
            </div>
          )}
          <ThreadPrimitive.Root>
            <ThreadPrimitive.Viewport
              className="chat-thread"
              aria-live="polite"
              aria-relevant="additions text"
              autoScroll
            >
              <ThreadPrimitive.Messages>
                {({ message }) => <ChatBubble message={message} />}
              </ThreadPrimitive.Messages>
              {(busy || isWorkingActivity(activity)) && (
                <div className="chat-working" role="status">
                  <span className="chat-working-dots" aria-hidden="true">
                    <span />
                    <span />
                    <span />
                  </span>
                  Working
                </div>
              )}
            </ThreadPrimitive.Viewport>
          </ThreadPrimitive.Root>
        </div>
        <ConsoleComposer
          agentId={agentId}
          busy={busy}
          prefill={prefill}
          samplePrompts={samplePrompts}
          placeholder={placeholder}
          restoreDraft={restoreDraft}
          onText={setComposerText}
          onRejectEmpty={() => onError("Enter a message before sending.")}
        />
      </div>
    </AssistantRuntimeProvider>
  );
}
