// Typisierter Client für die MusicHub-API. Alle Aufrufe laufen über request(), das
// Problem-Details-Antworten in ApiError übersetzt und das Token anhängt.

import type { TokenProvider } from "./auth";
import type {
  Availability,
  ConnectorStatus,
  LibraryStats,
  LinkStatus,
  LiveResponse,
  Me,
  Page,
  PlaylistDetail,
  PlaylistSummary,
  Problem,
  ReadyResponse,
  SourceInput,
  Tag,
  TagWithCount,
  Track,
  TrackDetail,
  TrackInput,
  TrackQuery,
  TrackSource,
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

  // --- Statistik ------------------------------------------------------------

  stats(): Promise<LibraryStats> {
    return this.request("/api/v1/stats");
  }

  // --- Tracks ----------------------------------------------------------------

  listTracks(params: TrackQuery = {}): Promise<Page<Track>> {
    return this.request(`/api/v1/tracks${trackQueryString(params)}`);
  }

  getTrack(id: string): Promise<TrackDetail> {
    return this.request(`/api/v1/tracks/${enc(id)}`);
  }

  createTrack(input: TrackInput & { sources?: SourceInput[] }): Promise<TrackDetail> {
    return this.request("/api/v1/tracks", { method: "POST", body: JSON.stringify(input) });
  }

  updateTrack(id: string, changes: Partial<TrackInput>): Promise<TrackDetail> {
    return this.request(`/api/v1/tracks/${enc(id)}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    });
  }

  deleteTrack(id: string, options: { force?: boolean } = {}): Promise<void> {
    const qs = options.force ? "?force=true" : "";
    return this.request(`/api/v1/tracks/${enc(id)}${qs}`, { method: "DELETE" });
  }

  setTrackTags(id: string, tagIds: string[]): Promise<TrackDetail> {
    return this.request(`/api/v1/tracks/${enc(id)}/tags`, {
      method: "PUT",
      body: JSON.stringify({ tag_ids: tagIds }),
    });
  }

  addSource(trackId: string, input: SourceInput): Promise<TrackSource> {
    return this.request(`/api/v1/tracks/${enc(trackId)}/sources`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  updateSource(
    trackId: string,
    sourceId: string,
    changes: { url?: string | null; availability?: Availability; link_status?: LinkStatus },
  ): Promise<TrackSource> {
    return this.request(`/api/v1/tracks/${enc(trackId)}/sources/${enc(sourceId)}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    });
  }

  deleteSource(trackId: string, sourceId: string): Promise<void> {
    return this.request(`/api/v1/tracks/${enc(trackId)}/sources/${enc(sourceId)}`, {
      method: "DELETE",
    });
  }

  // --- Tags ------------------------------------------------------------------

  listTags(): Promise<TagWithCount[]> {
    return this.request("/api/v1/tags");
  }

  createTag(input: { name: string; category?: string }): Promise<Tag> {
    return this.request("/api/v1/tags", { method: "POST", body: JSON.stringify(input) });
  }

  updateTag(id: string, changes: { name?: string; category?: string }): Promise<Tag> {
    return this.request(`/api/v1/tags/${enc(id)}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    });
  }

  deleteTag(id: string): Promise<void> {
    return this.request(`/api/v1/tags/${enc(id)}`, { method: "DELETE" });
  }

  // --- Playlists -------------------------------------------------------------

  listPlaylists(params: { q?: string; limit?: number; offset?: number } = {}): Promise<
    Page<PlaylistSummary>
  > {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    query.set("limit", String(params.limit ?? 200));
    if (params.offset) query.set("offset", String(params.offset));
    return this.request(`/api/v1/playlists?${query.toString()}`);
  }

  getPlaylist(id: string): Promise<PlaylistDetail> {
    return this.request(`/api/v1/playlists/${enc(id)}`);
  }

  createPlaylist(input: {
    name: string;
    description?: string | null;
    track_ids?: string[];
  }): Promise<PlaylistDetail> {
    return this.request("/api/v1/playlists", { method: "POST", body: JSON.stringify(input) });
  }

  updatePlaylist(
    id: string,
    changes: { name?: string; description?: string | null },
  ): Promise<PlaylistDetail> {
    return this.request(`/api/v1/playlists/${enc(id)}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    });
  }

  deletePlaylist(id: string): Promise<void> {
    return this.request(`/api/v1/playlists/${enc(id)}`, { method: "DELETE" });
  }

  addToPlaylist(
    id: string,
    trackIds: string[],
    options: { position?: number; skip_existing?: boolean } = {},
  ): Promise<PlaylistDetail> {
    return this.request(`/api/v1/playlists/${enc(id)}/items`, {
      method: "POST",
      body: JSON.stringify({ track_ids: trackIds, ...options }),
    });
  }

  removeFromPlaylist(id: string, itemId: string): Promise<PlaylistDetail> {
    return this.request(`/api/v1/playlists/${enc(id)}/items/${enc(itemId)}`, {
      method: "DELETE",
    });
  }

  moveInPlaylist(id: string, itemId: string, position: number): Promise<PlaylistDetail> {
    return this.request(`/api/v1/playlists/${enc(id)}/items/${enc(itemId)}/move`, {
      method: "POST",
      body: JSON.stringify({ position }),
    });
  }

  reorderPlaylist(id: string, itemIds: string[]): Promise<PlaylistDetail> {
    return this.request(`/api/v1/playlists/${enc(id)}/items/order`, {
      method: "PUT",
      body: JSON.stringify({ item_ids: itemIds }),
    });
  }
}

const enc = encodeURIComponent;

/** Query-String für /tracks; Listen werden als wiederholte Parameter übertragen. */
export function trackQueryString(params: TrackQuery): string {
  const query = new URLSearchParams();
  if (params.q) query.set("q", params.q);
  for (const p of params.platform ?? []) query.append("platform", p);
  for (const p of params.missing_platform ?? []) query.append("missing_platform", p);
  if (params.platform_count !== undefined) query.set("platform_count", String(params.platform_count));
  for (const t of params.tag ?? []) query.append("tag", t);
  if (params.has_isrc !== undefined) query.set("has_isrc", String(params.has_isrc));
  if (params.sort) query.set("sort", params.sort);
  if (params.order) query.set("order", params.order);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return qs ? `?${qs}` : "";
}

/** Gesamtdauer lesbar: "3:45", "1:02:10" bzw. "12 Std. 4 Min." ab 10 Stunden. */
export function formatTotalDuration(ms: number): string {
  const total = Math.round(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours >= 10) return `${hours} Std. ${minutes} Min.`;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  return formatDuration(ms);
}

export function formatDuration(ms: number | null | undefined): string {
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
