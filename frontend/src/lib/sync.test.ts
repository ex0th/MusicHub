import { describe, expect, it } from "vitest";

import { canSyncFromPreview, planSummary, runSummary, scheduleText } from "./sync";
import type { SyncPlan, SyncRun, SyncTarget } from "./types";

const plan = (o: Partial<SyncPlan> = {}): SyncPlan => ({
  create_playlist: false,
  remote_name: null,
  add: [],
  remove: [],
  reorder: false,
  unresolved: [],
  duplicates_skipped: 0,
  already_present: 0,
  external_added: [],
  external_removed: [],
  not_readded: [],
  requires_approval: false,
  noop: false,
  first_sync: false,
  quota_units: null,
  ...o,
});

const run = (o: Partial<SyncRun> = {}): SyncRun => ({
  id: "r",
  target_id: "t",
  playlist_id: "p",
  playlist_name: "P",
  platform: "spotify",
  remote_playlist_id: null,
  remote_name: null,
  trigger: "preview",
  status: "preview",
  dry_run: true,
  mode: "add_only",
  approved_external_removal: false,
  started_at: null,
  finished_at: "2026-11-01T10:00:00Z",
  created_at: "2026-11-01T10:00:00Z",
  added: 0,
  removed: 0,
  skipped: 0,
  unmatched: 0,
  errors: [],
  created_by: "dev",
  plan: null,
  ...o,
});

const target = { id: "t", mode: "add_only", write_ready: true } as SyncTarget;

describe("Sync-Texte", () => {
  it("fasst Pläne zusammen", () => {
    const add = { external_id: "a", title: "A", artist: "B", url: "u", track_id: "t", version: null, new_link: false };
    expect(planSummary(plan({ noop: true }))).toBe("Keine Änderungen nötig");
    expect(planSummary(plan({ create_playlist: true, add: [add], reorder: true }))).toBe(
      "Playlist anlegen · 1 hinzufügen · umsortieren",
    );
  });

  it("fasst Läufe zusammen", () => {
    expect(runSummary(run({ dry_run: false, status: "succeeded", added: 3, skipped: 1 }))).toBe("3 hinzugefügt · 1 übersprungen");
    expect(runSummary(run({ added: 2 }))).toBe("geplant: 2 hinzufügen");
  });

  it("benennt Zeitpläne", () => {
    expect(scheduleText(null)).toBe("Nur manuell");
    expect(scheduleText(1440)).toBe("Täglich");
  });
});

describe("Bestätigung", () => {
  it("erlaubt Sync nur mit frischer, passender Vorschau", () => {
    const now = new Date("2026-11-01T10:30:00Z");
    expect(canSyncFromPreview(target, run(), now)).toBe(true);
    expect(canSyncFromPreview(target, run({ mode: "mirror" }), now)).toBe(false);
    expect(canSyncFromPreview(target, run(), new Date("2026-11-01T11:30:00Z"))).toBe(false);
    expect(canSyncFromPreview({ ...target, write_ready: false }, run(), now)).toBe(false);
  });
});
