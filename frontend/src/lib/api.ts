// Typisierter Client für die MusicHub-API. Alle Aufrufe laufen über request(), das
// Problem-Details-Antworten in ApiError übersetzt und das Token anhängt.

import type { TokenProvider } from "./auth";
import type {
  Availability,
  CandidateStatus,
  Connection,
  ImportDecision,
  ImportFormats,
  ImportItem,
  ImportItemStatus,
  ImportRun,
  Job,
  PlaylistImportMode,
  ConnectorStatus,
  LibraryStats,
  LinkStatus,
  LiveResponse,
  MatchCandidate,
  MatchComparison,
  MatchLevel,
  MatchScanResult,
  MatchSummary,
  Me,
  Page,
  PlaylistDetail,
  Platform,
  PlaylistSummary,
  Problem,
  ReadyResponse,
  RemotePlaylist,
  SourceInput,
  Tag,
  TagWithCount,
  Track,
  TrackDetail,
  TrackInput,
  TrackMerge,
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
    if (rest.body !== undefined && !(rest.body instanceof FormData) && !headers.has("Content-Type")) {
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

  // --- Import ----------------------------------------------------------------

  importFormats(): Promise<ImportFormats> {
    return this.request("/api/v1/imports/formats");
  }

  uploadImport(file: File, format?: string): Promise<ImportRun> {
    const body = new FormData();
    body.append("file", file, file.name);
    if (format) body.append("format", format);
    return this.request("/api/v1/imports", { method: "POST", body });
  }

  listImports(limit = 20, offset = 0): Promise<Page<ImportRun>> {
    return this.request(`/api/v1/imports?limit=${limit}&offset=${offset}`);
  }

  getImport(id: string): Promise<ImportRun> {
    return this.request(`/api/v1/imports/${enc(id)}`);
  }

  importItems(
    id: string,
    params: { status?: ImportItemStatus[]; limit?: number; offset?: number } = {},
  ): Promise<Page<ImportItem>> {
    const query = new URLSearchParams();
    for (const s of params.status ?? []) query.append("status", s);
    query.set("limit", String(params.limit ?? 200));
    query.set("offset", String(params.offset ?? 0));
    return this.request(`/api/v1/imports/${enc(id)}/items?${query.toString()}`);
  }

  setImportDecision(id: string, itemId: string, decision: ImportDecision): Promise<ImportItem> {
    return this.request(`/api/v1/imports/${enc(id)}/items/${enc(itemId)}`, {
      method: "PATCH",
      body: JSON.stringify({ decision }),
    });
  }

  setImportOptions(
    id: string,
    options: {
      playlists?: { name: string; mode: PlaylistImportMode }[];
      unassigned?: { enabled?: boolean; name?: string };
    },
  ): Promise<ImportRun> {
    return this.request(`/api/v1/imports/${enc(id)}`, {
      method: "PATCH",
      body: JSON.stringify(options),
    });
  }

  commitImport(id: string): Promise<ImportRun> {
    return this.request(`/api/v1/imports/${enc(id)}/commit`, { method: "POST" });
  }

  discardImport(id: string): Promise<ImportRun> {
    return this.request(`/api/v1/imports/${enc(id)}/discard`, { method: "POST" });
  }

  // --- Plattform-Konten --------------------------------------------------------

  connections(): Promise<Connection[]> {
    return this.request("/api/v1/connections");
  }

  authorizeConnection(platform: Platform): Promise<{ authorize_url: string }> {
    return this.request(`/api/v1/connections/${platform}/authorize`, { method: "POST" });
  }

  disconnect(platform: Platform): Promise<{ revoked: boolean }> {
    return this.request(`/api/v1/connections/${platform}`, { method: "DELETE" });
  }

  remotePlaylists(platform: Platform): Promise<RemotePlaylist[]> {
    return this.request(`/api/v1/connections/${platform}/playlists`);
  }

  refreshRemotePlaylists(platform: Platform, importChanged = false): Promise<Job> {
    return this.request(`/api/v1/connections/${platform}/playlists/refresh`, {
      method: "POST",
      body: JSON.stringify({ import_changed: importChanged }),
    });
  }

  importRemotePlaylists(platform: Platform, playlistIds: string[], force = false): Promise<Job[]> {
    return this.request(`/api/v1/connections/${platform}/import`, {
      method: "POST",
      body: JSON.stringify({ playlist_ids: playlistIds, force }),
    });
  }

  refreshMetadata(platform: Platform): Promise<Job> {
    return this.request(`/api/v1/connections/${platform}/refresh-metadata`, { method: "POST" });
  }

  jobs(platform?: Platform, limit = 30): Promise<Job[]> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (platform) query.set("platform", platform);
    return this.request(`/api/v1/jobs?${query.toString()}`);
  }

  // --- Matching --------------------------------------------------------------

  matchingSummary(): Promise<MatchSummary> {
    return this.request("/api/v1/matching/summary");
  }

  scanMatching(): Promise<MatchScanResult> {
    return this.request("/api/v1/matching/scan", { method: "POST" });
  }

  matchCandidates(
    params: { status?: CandidateStatus[]; level?: MatchLevel[]; limit?: number; offset?: number } = {},
  ): Promise<Page<MatchCandidate>> {
    const query = new URLSearchParams();
    for (const s of params.status ?? []) query.append("status", s);
    for (const l of params.level ?? []) query.append("level", l);
    query.set("limit", String(params.limit ?? 20));
    query.set("offset", String(params.offset ?? 0));
    return this.request(`/api/v1/matching/candidates?${query.toString()}`);
  }

  confirmMatch(id: string, keep: "suggested" | "source" = "suggested"): Promise<TrackMerge> {
    return this.request(`/api/v1/matching/candidates/${enc(id)}/confirm`, {
      method: "POST",
      body: JSON.stringify({ keep }),
    });
  }

  decideMatch(id: string, action: "reject" | "defer" | "reopen"): Promise<MatchCandidate> {
    return this.request(`/api/v1/matching/candidates/${enc(id)}/${action}`, { method: "POST" });
  }

  compareTracks(trackA: string, trackB: string): Promise<MatchComparison> {
    return this.request(`/api/v1/matching/compare?track_a=${enc(trackA)}&track_b=${enc(trackB)}`);
  }

  rejectPair(trackA: string, trackB: string): Promise<MatchCandidate> {
    return this.request("/api/v1/matching/reject-pair", {
      method: "POST",
      body: JSON.stringify({ track_a_id: trackA, track_b_id: trackB }),
    });
  }

  mergeTracks(keepTrackId: string, mergeTrackId: string): Promise<TrackMerge> {
    return this.request("/api/v1/matching/merge", {
      method: "POST",
      body: JSON.stringify({ keep_track_id: keepTrackId, merge_track_id: mergeTrackId }),
    });
  }

  autoMerge(): Promise<{ merged: TrackMerge[]; skipped: number }> {
    return this.request("/api/v1/matching/auto-merge", { method: "POST" });
  }

  listMerges(limit = 20, offset = 0): Promise<Page<TrackMerge>> {
    return this.request(`/api/v1/matching/merges?limit=${limit}&offset=${offset}`);
  }

  undoMerge(id: string): Promise<TrackMerge> {
    return this.request(`/api/v1/matching/merges/${enc(id)}/undo`, { method: "POST" });
  }

  // --- Downloads (Export, Bericht) --------------------------------------------

  /** Lädt eine Datei mit Token und liefert Inhalt und Dateinamen aus Content-Disposition. */
  async download(path: string): Promise<{ blob: Blob; filename: string }> {
    const headers = new Headers();
    const token = this.tokens?.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, { headers });
    if (!response.ok) {
      const isJson = (response.headers.get("Content-Type") ?? "").includes("json");
      throw new ApiError(response.status, isJson ? ((await response.json()) as Problem) : null);
    }
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = /filename="([^"]+)"/.exec(disposition);
    return { blob: await response.blob(), filename: match?.[1] ?? "download" };
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
