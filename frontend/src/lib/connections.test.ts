import { describe, expect, it } from "vitest";

import { connectionState, jobPercent, jobResultText, jobStatusText, playlistState } from "./connections";
import type { Connection, Job, RemotePlaylist } from "./types";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "j",
    kind: "platform.import_playlist",
    label: "Playlist importieren",
    platform: "spotify",
    remote_playlist_id: null,
    status: "running",
    attempts: 1,
    max_attempts: 6,
    progress: {},
    result: null,
    last_error: null,
    run_after: "2026-10-31T12:00:00Z",
    created_at: "2026-10-31T12:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

function playlist(overrides: Partial<RemotePlaylist> = {}): RemotePlaylist {
  return {
    id: "p",
    platform: "spotify",
    external_id: "x",
    name: "P",
    owner_name: null,
    description: null,
    track_count: 3,
    url: null,
    owned: true,
    items_accessible: true,
    removed: false,
    local_playlist_id: null,
    local_playlist_name: null,
    last_imported_at: null,
    last_import_status: null,
    last_import_run_id: null,
    last_error: null,
    up_to_date: false,
    ...overrides,
  };
}

describe("Jobs", () => {
  it("berechnet Fortschritt", () => {
    expect(jobPercent(job({ progress: { current: 25, total: 100 } }))).toBe(25);
    expect(jobPercent(job({ progress: { current: 5 } }))).toBeNull();
    expect(jobPercent(job({ status: "succeeded" }))).toBe(100);
  });

  it("beschreibt Wiederholungen nach Rate-Limit", () => {
    const now = new Date("2026-10-31T12:00:00Z");
    const text = jobStatusText(job({ status: "failed", run_after: "2026-10-31T12:02:00Z", attempts: 2 }), now);
    expect(text).toBe("Neuer Versuch in 2 min (2/6)");
    expect(jobStatusText(job({ status: "failed", run_after: "2026-11-01T08:00:00Z" }), now)).toContain("in 20 h");
  });

  it("fasst Ergebnisse zusammen", () => {
    expect(jobResultText(job({ result: { created: 2, linked: 1, skipped: 0 } }))).toBe("2 neu · 1 verknüpft");
  });
});

describe("Zustände", () => {
  it("erkennt Playlist-Zustände", () => {
    expect(playlistState(playlist({ items_accessible: false }))).toBe("blocked");
    expect(playlistState(playlist({ local_playlist_id: "l", up_to_date: true }))).toBe("current");
    expect(playlistState(playlist({ local_playlist_id: "l" }))).toBe("changed");
    expect(playlistState(playlist({ last_import_status: "error" }))).toBe("error");
    expect(playlistState(playlist())).toBe("new");
  });

  it("erkennt Verbindungszustände", () => {
    const base = { configured: false, account: null } as unknown as Connection;
    expect(connectionState(base)).toBe("unconfigured");
    expect(connectionState({ ...base, configured: true })).toBe("disconnected");
    expect(
      connectionState({ ...base, configured: true, account: { status: "reauth_required" } } as unknown as Connection),
    ).toBe("reauth");
  });
});
