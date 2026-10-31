// Spiegel der Backend-Schemas (backend/src/musichub/api/schemas.py).
// Bei Änderungen an der API hier nachziehen; perspektivisch aus /api/openapi.json generieren.

export type Platform = "spotify" | "soundcloud" | "youtube";
export type ConnectorState = "not_configured" | "ready" | "degraded" | "suspended";

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

export interface PlatformLink {
  id: string;
  platform: Platform;
  external_id: string;
  url: string | null;
  status: "linked" | "candidate" | "rejected" | "unavailable";
  match_method: "manual" | "isrc" | "fuzzy" | "import";
  match_score: number | null;
}

export interface Track {
  id: string;
  title: string;
  artist: string;
  album: string | null;
  duration_ms: number | null;
  isrc: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  links: PlatformLink[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
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
}
