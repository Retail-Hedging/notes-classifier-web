const TOKEN_KEY = "classifier_token";

export function readToken(): string | null {
  const url = new URL(window.location.href);
  const fromQuery = url.searchParams.get("token");
  if (fromQuery) {
    localStorage.setItem(TOKEN_KEY, fromQuery);
    url.searchParams.delete("token");
    window.history.replaceState({}, "", url.toString());
    return fromQuery;
  }
  return localStorage.getItem(TOKEN_KEY);
}

function authHeaders(token: string) {
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

export type UnknownItem = {
  product_slug: string;
  conversation_id: string;
  customer_name: string;
  note: string;
  last_message_timestamp: string;
};

export type ConversationPayload = {
  messages: Array<{ type: string; from: string; message_text: string; timestamp?: string }>;
  strategy: { market_category?: string; one_line_pitch?: string; icp?: string };
};

export async function fetchUnknown(token: string): Promise<UnknownItem[]> {
  const r = await fetch("/api/unknown", { headers: authHeaders(token) });
  if (r.status === 401) throw new Error("unauthorized");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const data = await r.json();
  return data.items ?? [];
}

export async function fetchConversation(
  token: string,
  product_slug: string,
  conversation_id: string,
): Promise<ConversationPayload> {
  const u = new URL("/api/conversation", window.location.origin);
  u.searchParams.set("product_slug", product_slug);
  u.searchParams.set("conversation_id", conversation_id);
  const r = await fetch(u.toString(), { headers: authHeaders(token) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

export async function classify(
  token: string,
  product_slug: string,
  customer_name: string,
  new_state: string,
): Promise<void> {
  const r = await fetch("/api/classify", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ product_slug, customer_name, new_state }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
}
