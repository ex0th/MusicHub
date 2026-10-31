// Typisierter Client für die MusicHub-API. Alle Aufrufe laufen über request(), das
// Problem-Details-Antworten in ApiError übersetzt und das Token anhängt.

import type { TokenProvider } from "./auth";
import type {
  ConnectorStatus,
  LiveResponse,
  Me,
  Page,
  Problem,
  ReadyResponse,
  Track,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: Problem | null,
  ) {
    super(problem?.detail ?? `HTTP ${status}`);
    this.name = "ApiError";
  }

  get requestId(): string | null {
    return this.problem?.request_id ?? null;
  }
}

/** Teilmenge von fetch, die der Client nutzt – erleichtert Tests. */
export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface ApiClientOptions {
  baseUrl?: string;
  tokens?: TokenProvider;
  fetch?: FetchLike;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly tokens: TokenProvider | undefined;
  private readonly fetchImpl: FetchLike;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
    this.tokens = options.tokens;
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  async request<T>(path: string, init: RequestInit & { auth?: boolean } = {}): Promise<T> {
    const { auth = true, ...rest } = init;
    const headers = new Headers(rest.headers);
    headers.set("Accept", "application/json");
    if (rest.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const token = auth ? this.tokens?.getToken() : null;
    if (token) headers.set("Authorization", `Bearer ${token}`);

    const response = await this.fetchImpl(`${this.baseUrl}${path}`, { ...rest, headers });

    if (response.status === 204) return undefined as T;
    const isJson = (response.headers.get("Content-Type") ?? "").includes("json");
    const body: unknown = isJson ? await response.json() : null;

    // /ready antwortet mit 503 und gültigem JSON-Body – das ist kein Transportfehler.
    if (!response.ok && !(response.status === 503 && path === "/api/health/ready")) {
      const problem = body && typeof body === "object" && "code" in body ? (body as Problem) : null;
      throw new ApiError(response.status, problem);
    }
    return body as T;
  }

  live(): Promise<LiveResponse> {
    return this.request("/api/health/live", { auth: false });
  }

  ready(): Promise<ReadyResponse> {
    return this.request("/api/health/ready", { auth: false });
  }

  me(): Promise<Me> {
    return this.request("/api/v1/me");
  }

  connectors(): Promise<ConnectorStatus[]> {
    return this.request("/api/v1/connectors");
  }

  listTracks(params: { q?: string; limit?: number; offset?: number } = {}): Promise<Page<Track>> {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const qs = query.toString();
    return this.request(`/api/v1/tracks${qs ? `?${qs}` : ""}`);
  }

  createTrack(input: {
    title: string;
    artist: string;
    album?: string | null;
    duration_ms?: number | null;
    isrc?: string | null;
  }): Promise<Track> {
    return this.request("/api/v1/tracks", { method: "POST", body: JSON.stringify(input) });
  }

  deleteTrack(id: string): Promise<void> {
    return this.request(`/api/v1/tracks/${encodeURIComponent(id)}`, { method: "DELETE" });
  }
}

export function formatDuration(ms: number | null): string {
  if (!ms) return "–";
  const total = Math.round(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** "3:45" oder "225" (Sekunden) -> Millisekunden. Leere Eingabe -> null. */
export function parseDuration(input: string): number | null {
  const value = input.trim();
  if (!value) return null;
  const match = /^(?:(\d+):)?(\d{1,2}|\d+)$/.exec(value);
  if (!match) throw new Error("Dauer bitte als m:ss oder Sekunden angeben");
  const minutes = match[1] ? Number(match[1]) : 0;
  const seconds = Number(match[2]);
  if (match[1] && seconds >= 60) throw new Error("Sekunden müssen unter 60 liegen");
  const ms = (minutes * 60 + seconds) * 1000;
  if (ms <= 0) throw new Error("Dauer muss größer als 0 sein");
  return ms;
}
