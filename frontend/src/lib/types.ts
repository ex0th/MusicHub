// Spiegel der Backend-Schemas (backend/src/musichub/api/schemas.py).
// Bei Änderungen an der API hier nachziehen; perspektivisch aus /api/openapi.json generieren.

export type Platform = "spotify" | "soundcloud" | "youtube";
export type ConnectorState = "not_configured" | "ready" | "degraded" | "suspended";
export type LinkStatus = "linked" | "candidate" | "rejected";
export type Availability = "unknown" | "available" | "unavailable";
export type MatchMethod = "manual" | "isrc" | "fuzzy" | "import";

export interface LiveResponse {
  status: "ok";
  version: string;
  build: string;
}

export interface CheckResult {
  status: "ok" | "error";
  detail: string | null;
}

export interface ReadyResponse {
  status: "ok" | "error";
  checks: Record<string, CheckResult>;
}

export interface Me {
  subject: string;
  display_name: string;
  groups: string[];
  can_write: boolean;
  auth_method: string;
}

export interface ConnectorStatus {
  platform: Platform;
  state: ConnectorState;
  capabilities: string[];
  consecutive_failures: number;
  last_error: string | null;
  suspended_for_seconds: number | null;
}

export interface Tag {
  id: string;
  name: string;
  category: string;
}

export interface TagWithCount extends Tag {
  track_count: number;
}

export interface TrackSource {
  id: string;
  track_id: string;
  platform: Platform;
  external_id: string;
  url: string | null;
  link_status: LinkStatus;
  availability: Availability;
  match_method: MatchMethod;
  match_score: number | null;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Track {
  id: string;
  title: string;
  artist: string;
  album: string | null;
  version: string | null;
  duration_ms: number | null;
  isrc: string | null;
  release_date: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  sources: TrackSource[];
  tags: Tag[];
  /** Plattformen mit bestätigter, nicht unverfügbarer Quelle */
  platforms: Platform[];
}

export interface PlaylistRef {
  id: string;
  name: string;
  occurrences: number;
}

export interface TrackDetail extends Track {
  playlists: PlaylistRef[];
}

export interface TrackInput {
  title: string;
  artist: string;
  album?: string | null;
  version?: string | null;
  duration_ms?: number | null;
  isrc?: string | null;
  release_date?: string | null;
  notes?: string | null;
  tag_ids?: string[];
}

export interface SourceInput {
  url?: string | null;
  platform?: Platform;
  external_id?: string | null;
  availability?: Availability;
  link_status?: LinkStatus;
}

export type TrackSort =
  | "title"
  | "artist"
  | "album"
  | "version"
  | "duration"
  | "release_date"
  | "created_at"
  | "updated_at";

export interface TrackQuery {
  q?: string;
  platform?: Platform[];
  missing_platform?: Platform[];
  platform_count?: number;
  tag?: string[];
  has_isrc?: boolean;
  sort?: TrackSort;
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface PlaylistSummary {
  id: string;
  name: string;
  description: string | null;
  item_count: number;
  total_duration_ms: number;
  created_at: string;
  updated_at: string;
}

export interface PlaylistItem {
  id: string;
  position: number;
  added_at: string;
  track: Track;
}

export interface PlaylistDetail extends PlaylistSummary {
  items: PlaylistItem[];
}

export interface PlatformStats {
  platform: Platform;
  tracks: number;
  exclusive: number;
  unavailable: number;
}

export interface LibraryStats {
  tracks: number;
  playlists: number;
  tags: number;
  local_only: number;
  single_platform: number;
  platforms: PlatformStats[];
}

/** RFC 9457 Problem Details, wie sie das Backend liefert. */
export interface Problem {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  request_id: string | null;
  errors?: { loc: (string | number)[]; msg: string; type: string }[];
  [extra: string]: unknown;
}

// --- Import / Export ---------------------------------------------------------------

export type ImportStatus = "analyzed" | "committed" | "discarded" | "failed";
export type ImportItemStatus = "new" | "existing" | "potential_duplicate" | "file_duplicate" | "invalid";
export type ImportDecision = "create" | "use_existing" | "skip";
export type ImportResult = "created" | "linked" | "skipped" | "failed";
export type PlaylistImportMode = "create" | "merge" | "replace" | "skip";

export interface ImportFormat {
  id: string;
  label: string;
  kind: "csv" | "json" | "txt";
}

export interface ImportFormats {
  formats: ImportFormat[];
  max_bytes: number;
  max_rows: number;
  extensions: string[];
}

export interface ImportPlaylistPlan {
  name: string;
  description: string | null;
  track_count: number;
  existing_playlist_id: string | null;
  mode: PlaylistImportMode;
}

export interface ImportPlaylistResult {
  name: string;
  mode: PlaylistImportMode;
  added: number;
  playlist_id: string | null;
  error?: string;
}

export interface ImportSummary {
  new?: number;
  existing?: number;
  potential_duplicate?: number;
  file_duplicate?: number;
  invalid?: number;
  total?: number;
  created?: number;
  linked?: number;
  skipped?: number;
  failed?: number;
  tags_created?: number;
  playlists?: ImportPlaylistResult[];
}

export interface ImportRun {
  id: string;
  filename: string;
  size_bytes: number;
  format: string | null;
  format_label: string | null;
  status: ImportStatus;
  created_by: string;
  created_at: string;
  committed_at: string | null;
  warnings: string[];
  error: string | null;
  summary: ImportSummary;
  playlists: ImportPlaylistPlan[];
  unassigned: { count: number; enabled: boolean; name: string } | null;
}

export interface ImportItem {
  id: string;
  row: number;
  status: ImportItemStatus;
  decision: ImportDecision;
  title: string | null;
  artist: string | null;
  version: string | null;
  album: string | null;
  duration_ms: number | null;
  isrc: string | null;
  sources: { platform: Platform; external_id: string; url: string }[];
  playlists: string[];
  errors: string[];
  warnings: string[];
  match: { id: string; title: string; artist: string; version: string | null } | null;
  match_reason: string | null;
  match_score: number | null;
  duplicate_of_row: number | null;
  result: ImportResult | null;
  result_track_id: string | null;
  result_message: string | null;
}

// --- Matching --------------------------------------------------------------------

export type MatchLevel = "certain" | "likely" | "review" | "different";
export type CandidateStatus = "open" | "deferred" | "rejected";

export interface MatchReason {
  code: string;
  text: string;
  /** Punkte (plus/minus), bei „cap“ die Obergrenze, sonst 0 */
  effect: number;
  kind: "plus" | "minus" | "info" | "cap" | "block";
}

export interface MatchTrack extends Track {
  playlist_count: number;
  /** von der Engine erkannte Version, z. B. „Original“ oder „Remix (kaskade)“ */
  detected_version: string;
}

export interface MatchCandidate {
  id: string;
  status: CandidateStatus;
  confidence: number;
  level: MatchLevel;
  reasons: MatchReason[];
  blockers: string[];
  auto_merge_allowed: boolean;
  engine_version: string;
  decided_at: string | null;
  decided_by: string | null;
  created_at: string;
  updated_at: string;
  /** neuerer Track */
  source: MatchTrack;
  /** älterer, etablierter Track */
  suggested: MatchTrack;
}

export interface MatchSummary {
  statuses: Record<CandidateStatus, number>;
  open_levels: Record<Exclude<MatchLevel, "different">, number>;
  auto_merge_ready: number;
  merges: number;
  last_change: string | null;
}

export interface MatchScanResult {
  tracks: number;
  unmatchable: number;
  compared: number;
  created: number;
  updated: number;
  removed: number;
  levels: Partial<Record<MatchLevel, number>>;
}

export interface MatchComparison {
  confidence: number;
  level: MatchLevel;
  reasons: MatchReason[];
  blockers: string[];
  auto_merge_allowed: boolean;
  version_a: string;
  version_b: string;
  duration_diff_ms: number | null;
  isrc_match: boolean | null;
  candidate_id: string | null;
  candidate_status: CandidateStatus | null;
  track_a: MatchTrack;
  track_b: MatchTrack;
}

export interface TrackMerge {
  id: string;
  kept_track_id: string;
  removed_track_id: string;
  kept_title: string;
  kept_artist: string;
  removed_title: string;
  removed_artist: string;
  method: "manual" | "auto";
  confidence: number | null;
  level: MatchLevel | null;
  status: "merged" | "undone";
  created_by: string;
  created_at: string;
  undone_at: string | null;
  undone_by: string | null;
  changes: {
    sources: number;
    sources_demoted: number;
    playlist_items: number;
    tags_added: number;
    import_items: number;
  };
  filled_fields: string[];
}

// --- Plattform-Konten --------------------------------------------------------------

export type AccountStatus = "connected" | "reauth_required";
export type RemoteImportStatus = "ok" | "unchanged" | "error";
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "dead";

export interface ConnectionAccount {
  display_name: string;
  external_user_id: string;
  status: AccountStatus;
  scopes: string[];
  connected_at: string;
  connected_by: string;
  last_error: string | null;
  playlists_synced_at: string | null;
  last_import_at: string | null;
}

export interface Connection {
  platform: Platform;
  label: string;
  configured: boolean;
  missing: string[];
  connector_state: string;
  capabilities: string[];
  read_only: boolean;
  sync_supported: boolean;
  write_enabled: boolean;
  write_granted: boolean;
  redirect_uri: string | null;
  account: ConnectionAccount | null;
  playlists: number;
  imported: number;
  changed: number;
  failed: number;
  active_jobs: number;
}

export interface RemotePlaylist {
  id: string;
  platform: Platform;
  external_id: string;
  name: string;
  owner_name: string | null;
  description: string | null;
  track_count: number | null;
  url: string | null;
  owned: boolean;
  items_accessible: boolean;
  removed: boolean;
  local_playlist_id: string | null;
  local_playlist_name: string | null;
  last_imported_at: string | null;
  last_import_status: RemoteImportStatus | null;
  last_import_run_id: string | null;
  last_error: string | null;
  up_to_date: boolean;
}

export interface Job {
  id: string;
  kind: string;
  label: string;
  platform: Platform | null;
  remote_playlist_id: string | null;
  status: JobStatus;
  attempts: number;
  max_attempts: number;
  progress: { current?: number | null; total?: number | null; message?: string | null };
  result: Record<string, unknown> | null;
  last_error: string | null;
  run_after: string;
  created_at: string;
  finished_at: string | null;
}

// --- Synchronisierung --------------------------------------------------------------

export type SyncMode = "add_only" | "mirror";
export type SyncTrigger = "preview" | "manual" | "scheduled";
export type SyncRunStatus =
  | "queued"
  | "running"
  | "preview"
  | "succeeded"
  | "no_changes"
  | "partial"
  | "conflict"
  | "blocked"
  | "failed";
export type UnresolvedReason = "unchecked" | "not_found" | "api_error" | "candidate" | "unavailable";

export interface SyncTarget {
  id: string;
  playlist_id: string;
  platform: Platform;
  remote_playlist_id: string | null;
  remote_name: string | null;
  remote_url: string | null;
  enabled: boolean;
  mode: SyncMode;
  managed: boolean;
  mirror_allowed: boolean;
  schedule_minutes: number | null;
  next_run_at: string | null;
  confirmed_at: string | null;
  last_synced_at: string | null;
  last_status: SyncRunStatus | null;
  last_run_id: string | null;
  last_error: string | null;
  write_ready: boolean;
  write_blockers: string[];
  needs_confirmation: boolean;
  active_run_id: string | null;
}

export interface SyncTargetInput {
  platform: Platform;
  remote_playlist_id?: string;
  create_new?: boolean;
  remote_name?: string;
  mode?: SyncMode;
  mirror_allowed?: boolean;
  schedule_minutes?: number | null;
  enabled?: boolean;
}

export interface PlanRemote {
  external_id: string;
  title: string | null;
  artist: string | null;
  url: string;
}

export interface PlanAdd extends PlanRemote {
  track_id: string;
  version: string | null;
  new_link: boolean;
}

export interface PlanUnresolved {
  track_id: string;
  title: string;
  artist: string;
  version: string | null;
  reason: UnresolvedReason;
  candidate_url: string | null;
  message: string | null;
  alternatives: { platform: Platform; url: string }[];
}

export interface SyncPlan {
  create_playlist: boolean;
  remote_name: string | null;
  add: PlanAdd[];
  remove: (PlanRemote & { external: boolean })[];
  reorder: boolean;
  unresolved: PlanUnresolved[];
  duplicates_skipped: number;
  already_present: number;
  external_added: PlanRemote[];
  external_removed: PlanRemote[];
  not_readded: PlanRemote[];
  requires_approval: boolean;
  noop: boolean;
  first_sync: boolean;
  quota_units: number | null;
}

export interface SyncRun {
  id: string;
  target_id: string | null;
  playlist_id: string | null;
  playlist_name: string;
  platform: Platform;
  remote_playlist_id: string | null;
  remote_name: string | null;
  trigger: SyncTrigger;
  status: SyncRunStatus;
  dry_run: boolean;
  mode: SyncMode;
  approved_external_removal: boolean;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  added: number;
  removed: number;
  skipped: number;
  unmatched: number;
  errors: string[];
  created_by: string;
  plan: SyncPlan | Record<string, never> | null;
}

export interface UnresolvedTrack {
  track_id: string;
  title: string;
  artist: string;
  version: string | null;
  reason: UnresolvedReason;
  candidate_url: string | null;
  checked_at: string | null;
  message: string | null;
  alternatives: TrackSource[];
}
