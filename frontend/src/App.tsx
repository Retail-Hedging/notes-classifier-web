import { useEffect, useMemo, useState } from "react";
import {
  readToken,
  fetchUnknown,
  fetchConversation,
  classify,
  type UnknownItem,
  type ConversationPayload,
} from "./api";

type Status = "loading" | "ready" | "empty" | "error" | "unauthorized";

const STATE_BUTTONS: Array<{ value: string; label: string; color: string }> = [
  { value: "DISQUALIFIED", label: "❌ DQ",           color: "bg-red-600 active:bg-red-700" },
  { value: "UNINTERESTED", label: "👎 Uninterested", color: "bg-orange-600 active:bg-orange-700" },
  { value: "UNKNOWN",      label: "❓ Skip",          color: "bg-zinc-700 active:bg-zinc-800" },
  { value: "CLOSE",        label: "📎 Close",         color: "bg-sky-600 active:bg-sky-700" },
  { value: "CONFIRMED",    label: "✅ Confirmed",    color: "bg-emerald-600 active:bg-emerald-700" },
];

function relativeTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function ConvoCard({
  item,
  convo,
  busy,
  error,
  onClassify,
}: {
  item: UnknownItem;
  convo: ConversationPayload | null;
  busy: boolean;
  error: string | null;
  onClassify: (s: string) => void;
}) {
  return (
    <div className="flex flex-col h-full">
      <div className="p-4 border-b border-zinc-800 bg-zinc-900/60 backdrop-blur sticky top-0 z-10">
        <div className="flex items-baseline justify-between gap-2">
          <div className="text-sm text-zinc-400 truncate">{item.product_slug}</div>
          <div className="text-xs text-zinc-500 flex-none">{relativeTime(item.last_message_timestamp)}</div>
        </div>
        <div className="text-lg font-semibold truncate">{item.customer_name}</div>
        {convo?.strategy?.one_line_pitch && (
          <div className="text-xs text-zinc-500 mt-1 truncate">
            <span className="text-zinc-400">Pitch: </span>
            {convo.strategy.one_line_pitch}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        {item.note && (
          <div className="text-xs bg-yellow-900/40 text-yellow-100 rounded p-2 border border-yellow-700/50">
            <div className="font-semibold text-yellow-300 mb-1">existing note</div>
            <div className="whitespace-pre-wrap break-words">{item.note}</div>
          </div>
        )}
        {convo === null ? (
          <div className="text-zinc-500 text-sm animate-pulse">loading conversation…</div>
        ) : convo.messages.length === 0 ? (
          <div className="text-zinc-500 text-sm italic">no messages in this conversation</div>
        ) : (
          convo.messages.map((m, i) => {
            const outbound = m.type === "outbound" || m.type === "sent";
            return (
              <div
                key={i}
                className={
                  "max-w-[85%] rounded-lg p-2 text-sm leading-snug " +
                  (outbound
                    ? "ml-auto bg-sky-600/30 text-sky-50 border border-sky-700/50"
                    : "mr-auto bg-zinc-800 text-zinc-100 border border-zinc-700")
                }
              >
                <div className="text-[10px] uppercase tracking-wider text-zinc-400 mb-0.5">
                  {m.from}
                </div>
                <div className="whitespace-pre-wrap break-words">{m.message_text}</div>
              </div>
            );
          })
        )}
        {convo?.strategy?.icp && (
          <details className="text-xs text-zinc-500 pt-4">
            <summary className="cursor-pointer">ICP context</summary>
            <div className="pt-1 whitespace-pre-wrap">{convo.strategy.icp}</div>
          </details>
        )}
      </div>

      <div className="p-2 border-t border-zinc-800 bg-zinc-900/80 grid grid-cols-5 gap-1 pb-[max(env(safe-area-inset-bottom),0.5rem)]">
        {STATE_BUTTONS.map((b) => (
          <button
            key={b.value}
            disabled={busy}
            onClick={() => onClassify(b.value)}
            className={`${b.color} disabled:opacity-50 text-white font-semibold text-xs rounded-lg py-3 active:scale-95 transition`}
          >
            {b.label}
          </button>
        ))}
      </div>
      {error && <div className="text-xs text-red-400 text-center py-1 bg-red-900/40">{error}</div>}
    </div>
  );
}

export default function App() {
  const token = useMemo(readToken, []);
  const [status, setStatus] = useState<Status>("loading");
  const [items, setItems] = useState<UnknownItem[]>([]);
  const [index, setIndex] = useState(0);
  const [convo, setConvo] = useState<ConversationPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      setStatus("unauthorized");
      return;
    }
    (async () => {
      try {
        const list = await fetchUnknown(token);
        setItems(list);
        setStatus(list.length ? "ready" : "empty");
      } catch (e: any) {
        setStatus(e?.message === "unauthorized" ? "unauthorized" : "error");
      }
    })();
  }, [token]);

  const current = items[index];
  useEffect(() => {
    if (!token || !current) return;
    setConvo(null);
    fetchConversation(token, current.product_slug, current.conversation_id)
      .then(setConvo)
      .catch(() => setConvo({ messages: [], strategy: {} }));
  }, [current?.product_slug, current?.conversation_id, token]);

  async function handleClassify(newState: string) {
    if (!token || !current) return;
    setBusy(true);
    setError(null);
    try {
      await classify(token, current.product_slug, current.customer_name, newState);
      const nextIndex = index + 1;
      if (nextIndex >= items.length) {
        setStatus("empty");
      } else {
        setIndex(nextIndex);
      }
    } catch (e: any) {
      setError(e?.message || "classify failed");
    } finally {
      setBusy(false);
    }
  }

  if (status === "loading")
    return <div className="h-full flex items-center justify-center text-zinc-500">loading…</div>;

  if (status === "unauthorized")
    return (
      <div className="h-full flex items-center justify-center p-6 text-center">
        <div>
          <div className="text-xl font-semibold mb-2">Unauthorized</div>
          <div className="text-sm text-zinc-400">
            Open this page via the magic link with <code>?token=…</code>.
          </div>
        </div>
      </div>
    );

  if (status === "error")
    return (
      <div className="h-full flex items-center justify-center p-6 text-center text-red-400">
        Failed to load — check backend logs.
      </div>
    );

  if (status === "empty")
    return (
      <div className="h-full flex items-center justify-center p-6 text-center">
        <div>
          <div className="text-2xl font-semibold mb-2">🎉 All caught up</div>
          <div className="text-sm text-zinc-400 mb-4">
            No UNKNOWN conversations on active campaigns.
          </div>
          <button
            onClick={() => window.location.reload()}
            className="px-4 py-2 rounded-lg bg-zinc-800 text-sm"
          >
            Reload
          </button>
        </div>
      </div>
    );

  if (!current) return null;

  return (
    <div className="h-full flex flex-col">
      <div className="flex-none px-3 py-1 bg-zinc-950 border-b border-zinc-800 text-xs text-zinc-500 flex items-center justify-between">
        <span>UNKNOWN review</span>
        <span>
          {index + 1} / {items.length}
        </span>
      </div>
      <div className="flex-1 min-h-0">
        <ConvoCard
          item={current}
          convo={convo}
          busy={busy}
          error={error}
          onClassify={handleClassify}
        />
      </div>
    </div>
  );
}
