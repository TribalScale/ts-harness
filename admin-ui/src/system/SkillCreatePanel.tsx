import { useState } from "react";
import { ApiError, apiFetch } from "../api/client";
import type { SkillCreateResult, UploadFile } from "../api/types";
import { Button } from "../components/Button";

export type SkillCreateMode = "paste" | "folder";

export const ALLOWED_EXTENSIONS = [".md", ".txt", ".json", ".yaml", ".yml", ".csv"];
export const MAX_FILES = 200;
export const MAX_FILE_BYTES = 256 * 1024;
export const MAX_TOTAL_BYTES = 2 * 1024 * 1024;
export const MAX_DEPTH = 6;

const JUNK_NAMES = new Set([".DS_Store", "Thumbs.db", "__MACOSX"]);
const SEGMENT_RE = /^[A-Za-z0-9._-]+$/;

export type PickedFile = { path: string; file: Blob };

export type PreparedFile = {
  path: string;
  size: number;
  content?: string;
  problem?: string;
};

export type PreparedUpload = {
  files: PreparedFile[];
  skipped: string[];
  problems: string[];
};

export function parseFrontmatter(text: string): Record<string, string> {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
  if (!match) return {};
  const out: Record<string, string> = {};
  for (const line of match[1].split(/\r?\n/)) {
    const kv = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(line);
    if (!kv) continue;
    const value = kv[2].trim().replace(/^["']|["']$/g, "");
    if (value) out[kv[1]] = value;
  }
  return out;
}

function isJunk(path: string): boolean {
  return path.split("/").some((part) => JUNK_NAMES.has(part) || part.startsWith("."));
}

function stripSharedTop(paths: string[]): string[] {
  const split = paths.map((p) => p.split("/"));
  const first = split[0]?.[0];
  if (first && first !== "SKILL.md" && split.every((parts) => parts.length > 1 && parts[0] === first)) {
    return split.map((parts) => parts.slice(1).join("/"));
  }
  return paths;
}

function pathProblem(path: string): string | undefined {
  const parts = path.split("/");
  if (parts.some((part) => !part || part === "." || part === ".." || !SEGMENT_RE.test(part))) {
    return "invalid path";
  }
  if (parts.length > MAX_DEPTH) return `nested more than ${MAX_DEPTH} levels`;
  const name = parts[parts.length - 1];
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  if (!ALLOWED_EXTENSIONS.includes(ext)) return "file type not allowed";
  if (name === "SKILL.md" && parts.length > 1) return "nested SKILL.md";
  return undefined;
}

async function readBytes(file: Blob): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === "function") return file.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(file);
  });
}

export async function prepareUpload(picked: PickedFile[]): Promise<PreparedUpload> {
  const skipped = picked.filter((item) => isJunk(item.path)).map((item) => item.path);
  const kept = picked.filter((item) => !isJunk(item.path));
  const paths = stripSharedTop(kept.map((item) => item.path));
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const files: PreparedFile[] = [];
  const seen = new Map<string, string>();
  for (let i = 0; i < kept.length; i++) {
    const path = paths[i];
    const size = kept[i].file.size;
    let problem = pathProblem(path);
    const lower = path.toLowerCase();
    if (!problem && seen.has(lower)) problem = `duplicate of ${seen.get(lower)}`;
    seen.set(lower, path);
    if (!problem && size > MAX_FILE_BYTES) problem = `larger than ${MAX_FILE_BYTES / 1024} KB`;
    let content: string | undefined;
    if (!problem) {
      try {
        content = decoder.decode(await readBytes(kept[i].file));
        if (content.includes("\u0000")) problem = "not a text file";
      } catch {
        problem = "not valid UTF-8 text";
      }
    }
    files.push({ path, size, content, problem });
  }
  files.sort((a, b) => a.path.localeCompare(b.path));
  return { files, skipped, problems: uploadProblems(files) };
}

export function uploadProblems(files: PreparedFile[]): string[] {
  const problems: string[] = [];
  if (!files.length) problems.push("No files to upload.");
  if (files.length > MAX_FILES) problems.push(`Too many files: ${files.length} (max ${MAX_FILES}).`);
  const total = files.reduce((sum, item) => sum + item.size, 0);
  if (total > MAX_TOTAL_BYTES) problems.push(`Upload is larger than ${MAX_TOTAL_BYTES / (1024 * 1024)} MB.`);
  if (files.length && !files.some((item) => item.path === "SKILL.md")) {
    problems.push("The folder needs SKILL.md at its top level.");
  }
  return problems;
}

type EntryLike = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  fullPath?: string;
  file?: (ok: (file: File) => void, fail: (err: unknown) => void) => void;
  createReader?: () => {
    readEntries: (ok: (entries: EntryLike[]) => void, fail: (err: unknown) => void) => void;
  };
};

async function walkEntry(entry: EntryLike, prefix: string, out: PickedFile[]): Promise<void> {
  const path = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile && entry.file) {
    const file = await new Promise<File>((resolve, reject) => entry.file!(resolve, reject));
    out.push({ path, file });
    return;
  }
  if (entry.isDirectory && entry.createReader) {
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise<EntryLike[]>((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) await walkEntry(child, path, out);
    }
  }
}

export async function pickedFromDrop(items: DataTransferItemList): Promise<PickedFile[]> {
  const entries: EntryLike[] = [];
  for (let i = 0; i < items.length; i++) {
    const item = items[i] as DataTransferItem & { webkitGetAsEntry?: () => EntryLike | null };
    const entry = item.webkitGetAsEntry?.();
    if (entry) entries.push(entry);
  }
  const out: PickedFile[] = [];
  for (const entry of entries) await walkEntry(entry, "", out);
  return out;
}

export function pickedFromInput(list: FileList | File[]): PickedFile[] {
  return Array.from(list).map((file) => ({
    path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
    file,
  }));
}

function formatSize(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError && err.path && !err.message.includes(err.path)) {
    return `${err.message} (${err.path})`;
  }
  return String((err as Error)?.message || err);
}

export function SkillCreatePanel({ mode, onClose }: { mode: SkillCreateMode; onClose: () => void }) {
  const [skillId, setSkillId] = useState("");
  const [category, setCategory] = useState("");
  const [description, setDescription] = useState("");
  const [version, setVersion] = useState("1.0.0");
  const [revisionOf, setRevisionOf] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [upload, setUpload] = useState<PreparedUpload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SkillCreateResult | null>(null);
  const [dragging, setDragging] = useState(false);

  function onPaste(text: string) {
    setMarkdown(text);
    if (!text.startsWith("---")) return;
    const fm = parseFrontmatter(text);
    if (fm.id || fm.name) setSkillId(fm.id || fm.name);
    if (fm.category) setCategory(fm.category);
    if (fm.description) setDescription(fm.description);
    if (fm.version) setVersion(fm.version);
  }

  async function loadPicked(picked: PickedFile[]) {
    setError(null);
    setResult(null);
    setUpload(await prepareUpload(picked));
  }

  function removeFile(path: string) {
    if (!upload) return;
    const files = upload.files.filter((item) => item.path !== path);
    setUpload({ ...upload, files, problems: uploadProblems(files) });
  }

  const uploadBlocked =
    !upload || upload.problems.length > 0 || upload.files.some((item) => Boolean(item.problem));
  const canSubmit = !busy && (mode === "paste" ? markdown.trim().length > 0 : !uploadBlocked);

  async function submit() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const revision = revisionOf.trim() || undefined;
      const body =
        mode === "paste"
          ? {
              mode: "text",
              markdown,
              id: skillId.trim() || undefined,
              category: category.trim() || undefined,
              description: description.trim() || undefined,
              version: version.trim() || undefined,
              revision_of: revision,
            }
          : {
              mode: "folder",
              files: (upload?.files || []).map((item): UploadFile => ({ path: item.path, content: item.content || "" })),
              revision_of: revision,
            };
      setResult(
        await apiFetch<SkillCreateResult>("/admin/skills/drafts", {
          method: "POST",
          body: JSON.stringify(body),
        })
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mcp-form" aria-label={mode === "paste" ? "Paste skill" : "Upload skill folder"}>
      <h4>{mode === "paste" ? "Paste skill" : "Upload folder"}</h4>
      {mode === "paste" ? (
        <>
          <label className="form-field">
            <span>Id</span>
            <input aria-label="Skill id" value={skillId} onChange={(e) => setSkillId(e.target.value)} />
          </label>
          <label className="form-field">
            <span>Category</span>
            <input aria-label="Skill category" value={category} onChange={(e) => setCategory(e.target.value)} />
          </label>
          <label className="form-field">
            <span>Description</span>
            <input
              aria-label="Skill description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label className="form-field">
            <span>Version</span>
            <input aria-label="Skill version" value={version} onChange={(e) => setVersion(e.target.value)} />
          </label>
          <label className="form-field">
            <span>Skill markdown</span>
            <textarea
              aria-label="Skill markdown"
              rows={16}
              value={markdown}
              onChange={(e) => onPaste(e.target.value)}
            />
          </label>
        </>
      ) : (
        <>
          <label className="form-field">
            <span>Folder</span>
            <input
              aria-label="Skill folder"
              type="file"
              multiple
              {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
              onChange={(e) => {
                if (e.target.files) void loadPicked(pickedFromInput(e.target.files));
              }}
            />
          </label>
          <div
            className={`skill-dropzone${dragging ? " active" : ""}`}
            aria-label="Drop skill folder"
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              void pickedFromDrop(e.dataTransfer.items).then(loadPicked);
            }}
          >
            Or drop a folder here. It needs SKILL.md at the top; other files may be nested (
            {ALLOWED_EXTENSIONS.join(", ")}).
          </div>
          {upload && (
            <>
              {upload.skipped.length > 0 && (
                <p className="muted">Skipped: {upload.skipped.join(", ")}</p>
              )}
              {upload.problems.map((problem) => (
                <p key={problem} className="error" role="alert">
                  {problem}
                </p>
              ))}
              <ul aria-label="Upload files" className="mono">
                {upload.files.map((item) => (
                  <li key={item.path}>
                    {item.path} · {formatSize(item.size)}
                    {item.problem && (
                      <>
                        {" "}
                        <span className="error">{item.problem}</span>{" "}
                        <Button type="button" variant="ghost" onClick={() => removeFile(item.path)}>
                          Remove
                        </Button>
                      </>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
      <label className="form-field">
        <span>Revision of (optional)</span>
        <input aria-label="Revision of" value={revisionOf} onChange={(e) => setRevisionOf(e.target.value)} />
      </label>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {result && (
        <p role="status">
          Draft {result.skill_id} created. <a href={result.inbox_href}>Open in Inbox</a>
        </p>
      )}
      <Button type="button" variant="primary" disabled={!canSubmit} onClick={() => void submit()}>
        {busy ? "Submitting…" : "Submit for review"}
      </Button>
      <Button type="button" variant="ghost" onClick={onClose}>
        Close
      </Button>
    </section>
  );
}
