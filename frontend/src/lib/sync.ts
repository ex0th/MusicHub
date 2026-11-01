// Reine Hilfsfunktionen für die Synchronisationsseite (ohne DOM, testbar).

import type { SyncPlan, SyncRun, SyncRunStatus, SyncTarget, UnresolvedReason } from "./types";

export const STATUS_TEXT: Record<SyncRunStatus, string> = {
  queued: "Wartet",
  running: "Läuft",
  preview: "Vorschau",
  succeeded: "Erfolgreich",
  no_changes: "Keine Änderungen",
  partial: "Teilweise",
  conflict: "Konflikt – Zustimmung nötig",
  blocked: "Blockiert",
  failed: "Fehlgeschlagen",
};

export const REASON_TEXT: Record<UnresolvedReason, string> = {
  unchecked: "Noch nicht geprüft",
  not_found: "Nicht gefunden",
  api_error: "API-Fehler bei der Suche",
  candidate: "Nur ähnlicher Treffer",
  unavailable: "Auf der Plattform nicht verfügbar",
};

export const MODE_TEXT = {
  add_only: "Nur neue Tracks hinzufügen",
  mirror: "Änderungen vollständig abgleichen",
} as const;

export const SCHEDULES: { minutes: number | null; label: string }[] = [
  { minutes: null, label: "Nur manuell" },
  { minutes: 60, label: "Stündlich" },
  { minutes: 360, label: "Alle 6 Stunden" },
  { minutes: 1440, label: "Täglich" },
  { minutes: 10080, label: "Wöchentlich" },
];

export function scheduleText(minutes: number | null): string {
  return SCHEDULES.find((s) => s.minutes === minutes)?.label ?? `Alle ${minutes} Minuten`;
}

export function isActive(run: SyncRun): boolean {
  return run.status === "queued" || run.status === "running";
}

export function hasPlan(run: SyncRun): run is SyncRun & { plan: SyncPlan } {
  return !!run.plan && "add" in run.plan;
}

/** Kurzfassung eines Plans: „3 hinzufügen · 1 entfernen · umsortieren“. */
export function planSummary(plan: SyncPlan): string {
  if (plan.noop) return "Keine Änderungen nötig";
  const parts: string[] = [];
  if (plan.create_playlist) parts.push("Playlist anlegen");
  if (plan.add.length) parts.push(`${plan.add.length} hinzufügen`);
  if (plan.remove.length) parts.push(`${plan.remove.length} entfernen`);
  if (plan.reorder) parts.push("umsortieren");
  return parts.join(" · ");
}

/** Kurzfassung eines Laufs für die Historie. */
export function runSummary(run: SyncRun): string {
  const parts: string[] = [];
  if (run.added) parts.push(`${run.added} hinzugefügt`);
  if (run.removed) parts.push(`${run.removed} entfernt`);
  if (run.skipped) parts.push(`${run.skipped} übersprungen`);
  if (run.dry_run && run.status === "preview") {
    return parts.length ? `geplant: ${parts.join(" · ").replaceAll("hinzugefügt", "hinzufügen").replaceAll("entfernt", "entfernen")}` : "keine Änderungen geplant";
  }
  return parts.join(" · ") || "–";
}

/** Darf mit dieser Vorschau (sofort) synchronisiert werden? */
export function canSyncFromPreview(target: SyncTarget, run: SyncRun, now: Date = new Date()): boolean {
  if (!target.write_ready || run.status !== "preview" || run.target_id !== target.id) return false;
  if (run.mode !== target.mode) return false;
  const finished = new Date(run.finished_at ?? run.created_at).getTime();
  return now.getTime() - finished < 60 * 60 * 1000;
}
