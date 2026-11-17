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
import {
  apiFetch,
  INTAKE_MAX_FILE_BYTES,
  INTAKE_MAX_FILES,
  INTAKE_MAX_TOTAL_BYTES,
  uploadIntakeFile,
  type IntakePointer,
} from "../api/client";
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

/** A file picked in the composer: uploading, uploaded (with its pointer), or refused. */
interface ComposerAttachment {
  id: string;
  name: string;
  size: number;
  status: "uploading" | "ready" | "error";
  pointer?: IntakePointer;
  error?: string;
}

const EMPTY_SEND = "Enter a message before sending.";
const EMPTY_SEND_WITH_FILES = "Enter a message or attach a file.";
const SIZE_LIMIT_MESSAGE = "Files are limited to 20 MB each and 80 MB per message.";
const COUNT_LIMIT_MESSAGE = `Attach up to ${INTAKE_MAX_FILES} files per message.`;

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/** What the user bubble shows: their words, then the attached file names. */
function sentText(text: string, names: string[]): string {
  if (names.length === 0) return text;
  const line = `Attached: ${names.join(", ")}`;
  return text ? `${text}\n\n${line}` : line;
}

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
  attachments,
  onPickFiles,
  onRemoveAttachment,
  onSendFilesOnly,
}: {
  agentId: string;
  busy: boolean;
  prefill?: string;
  samplePrompts: string[];
  placeholder: string;
  restoreDraft: React.MutableRefObject<(text: string) => void>;
  onText: (text: string) => void;
  onRejectEmpty: () => void;
  /** Omitted where the chat posts to a custom endpoint: those chats take text only. */
  attachments?: ComposerAttachment[];
  onPickFiles?: (files: File[]) => void;
  onRemoveAttachment?: (id: string) => void;
  onSendFilesOnly?: () => void;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const uploading = (attachments || []).some((item) => item.status === "uploading");
  const readyFiles = (attachments || []).filter((item) => item.status === "ready").length;
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
    if (busy || uploading) return;
    if (!value.trim()) {
      if (readyFiles > 0 && onSendFilesOnly) {
        onSendFilesOnly();
        return;
      }
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
        {onPickFiles && (
          <>
            <input
              ref={fileInput}
              type="file"
              multiple
              hidden
              aria-label="Attach files"
              onChange={(event) => {
                const files = Array.from(event.currentTarget.files || []);
                event.currentTarget.value = "";
                if (files.length > 0) onPickFiles(files);
              }}
            />
            <Button
              variant="secondary"
              type="button"
              disabled={busy}
              onClick={() => fileInput.current?.click()}
            >
              Attach
            </Button>
          </>
        )}
        <Button
          variant="primary"
          type="button"
          busy={busy}
          disabled={uploading}
          onClick={submit}
        >
          Send
        </Button>
      </ComposerPrimitive.Root>
      {attachments && attachments.length > 0 && (
        <ul className="composer-attachments" aria-label="Attachments">
          {attachments.map((item) => (
            <li key={item.id} className={`composer-attachment is-${item.status}`}>
              <span className="composer-attachment-name">{item.name}</span>
              <span className="composer-attachment-meta">
                {item.status === "uploading"
                  ? "Uploading…"
                  : item.status === "error"
                    ? item.error || "Not uploaded"
                    : formatBytes(item.size)}
              </span>
              <Button
                variant="ghost"
                size="sm"
                type="button"
                aria-label={`Remove ${item.name}`}
                disabled={busy}
                onClick={() => onRemoveAttachment?.(item.id)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}
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
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const attachmentsRef = useRef<ComposerAttachment[]>([]);
  attachmentsRef.current = attachments;
  const allowAttachments = !endpoint;
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

  function pickFiles(files: File[]) {
    const current = attachmentsRef.current.filter((item) => item.status !== "error");
    if (current.length + files.length > INTAKE_MAX_FILES) {
      onError(COUNT_LIMIT_MESSAGE);
      return;
    }
    let total = current.reduce((sum, item) => sum + item.size, 0);
    const added: ComposerAttachment[] = [];
    const toUpload: Array<[string, File]> = [];
    for (const file of files) {
      const id = nextMessageId();
      if (file.size > INTAKE_MAX_FILE_BYTES || total + file.size > INTAKE_MAX_TOTAL_BYTES) {
        added.push({ id, name: file.name, size: file.size, status: "error", error: "Too large" });
        onError(SIZE_LIMIT_MESSAGE);
        continue;
      }
      total += file.size;
      added.push({ id, name: file.name, size: file.size, status: "uploading" });
      toUpload.push([id, file]);
    }
    setAttachments((prev) => [...prev, ...added]);
    for (const [id, file] of toUpload) {
      uploadIntakeFile(file)
        .then((pointer) =>
          setAttachments((prev) =>
            prev.map((item) => (item.id === id ? { ...item, status: "ready", pointer } : item))
          )
        )
        .catch((e: any) =>
          setAttachments((prev) =>
            prev.map((item) =>
              item.id === id
                ? { ...item, status: "error", error: String(e?.message || "Upload failed") }
                : item
            )
          )
        );
    }
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => prev.filter((item) => item.id !== id));
  }

  async function onNew(message: AppendMessage) {
    await sendMessage(appendText(message).trim());
  }

  async function sendMessage(text: string) {
    const ready = allowAttachments
      ? attachmentsRef.current.filter((item) => item.status === "ready" && item.pointer)
      : [];
    if (!text && ready.length === 0) {
      onError(allowAttachments ? EMPTY_SEND_WITH_FILES : EMPTY_SEND);
      return;
    }
    setBusy(true);
    onError(null);
    const display = sentText(text, ready.map((item) => item.name));
    const userMessage: DisplayMessage = { id: nextMessageId(), role: "user", content: display };
    setMessages((prev) => [...dropTrailingFailure(prev, display), userMessage]);
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
            ...(ready.length > 0 ? { attachments: ready.map((item) => item.pointer) } : {}),
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
      if (ready.length > 0) {
        const sentIds = new Set(ready.map((item) => item.id));
        setAttachments((prev) => prev.filter((item) => !sentIds.has(item.id)));
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
      if (text) restoreDraft.current(text);
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
          onRejectEmpty={() => onError(allowAttachments ? EMPTY_SEND_WITH_FILES : EMPTY_SEND)}
          attachments={allowAttachments ? attachments : undefined}
          onPickFiles={allowAttachments ? pickFiles : undefined}
          onRemoveAttachment={allowAttachments ? removeAttachment : undefined}
          onSendFilesOnly={allowAttachments ? () => void sendMessage("") : undefined}
        />
      </div>
    </AssistantRuntimeProvider>
  );
}
