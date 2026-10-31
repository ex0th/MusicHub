// Reine Hilfsfunktionen für die Seite „Plattformen“ (ohne DOM, testbar).

import type { Connection, Job, Platform, RemotePlaylist } from "./types";

/** Ehrliche Hinweise zu Voraussetzungen und Grenzen der offiziellen APIs. */
export const PLATFORM_NOTES: Record<Platform, string[]> = {
  spotify: [
    "Offizielle Spotify Web API, nur lesend (Scopes: eigene und kollaborative Playlists lesen).",
    "Development Mode: Der Besitzer der Spotify-App braucht Premium, höchstens 5 Nutzer.",
    "Inhalte sind nur bei eigenen oder kollaborativen Playlists lesbar; bei fremden Playlists liefert Spotify nur Name und Anzahl.",
    "„Lieblingssongs“ werden nicht gelesen.",
  ],
  youtube: [
    "Offizielle YouTube Data API v3, nur lesend (Scope youtube.readonly).",
    "Gelesen werden die YouTube-Playlists deines Kanals. In YouTube Music angelegte Playlists erscheinen dort in der Regel auch – die YouTube-Music-Mediathek (Alben, Künstler, „Mag ich“-Musik, Uploads) stellt die offizielle API nicht bereit.",
    "Tageskontingent 10 000 Einheiten (1 Einheit je 50 Einträge). Ist es erschöpft, setzt der Import nach Mitternacht (pazifische Zeit) fort.",
    "Solange die Google-App im Status „Testing“ ist, läuft die Anmeldung nach 7 Tagen ab.",
  ],
  soundcloud: [
    "Eine eigene SoundCloud-API-App gibt es laut SoundCloud nur mit einem Artist-Pro-Abo.",
    "Ohne Zugangsdaten bleibt der Connector aus. SoundCloud-Links lassen sich in der Track-Ansicht eintragen oder per CSV importieren.",
  ],
};

export const CALLBACK_ERRORS: Record<string, string> = {
  invalid_state: "Die Anmeldung war ungültig oder wurde schon verwendet. Bitte erneut verbinden.",
  expired: "Die Anmeldung ist abgelaufen. Bitte erneut verbinden.",
  denied: "Die Anmeldung wurde bei der Plattform abgebrochen.",
  invalid_request: "Die Plattform hat keinen Autorisierungscode geliefert.",
  exchange_failed: "Die Plattform hat die Anmeldung abgelehnt. Zugangsdaten und Redirect-URI prüfen.",
};

export type ConnectionState = "unconfigured" | "disconnected" | "connected" | "reauth";

export function connectionState(c: Connection): ConnectionState {
  if (c.account?.status === "reauth_required") return "reauth";
  if (c.account) return "connected";
  return c.configured ? "disconnected" : "unconfigured";
}

export const CONNECTION_TEXT: Record<ConnectionState, string> = {
  unconfigured: "Nicht eingerichtet",
  disconnected: "Nicht verbunden",
  connected: "Verbunden",
  reauth: "Neu verbinden nötig",
};

export function isActive(job: Job): boolean {
  return job.status === "queued" || job.status === "running" || job.status === "failed";
}

/** Fortschritt in Prozent oder null, wenn die Gesamtzahl unbekannt ist. */
export function jobPercent(job: Job): number | null {
  if (job.status === "succeeded") return 100;
  const { current, total } = job.progress;
  if (!total || current === null || current === undefined) return null;
  return Math.max(0, Math.min(100, Math.round((current / total) * 100)));
}

export function jobStatusText(job: Job, now: Date = new Date()): string {
  switch (job.status) {
    case "queued":
      return "Wartet";
    case "running":
      return job.progress.message ?? "Läuft";
    case "succeeded":
      return job.result?.["unchanged"] ? "Unverändert – übersprungen" : "Fertig";
    case "dead":
      return "Fehlgeschlagen";
    case "failed": {
      const minutes = Math.max(1, Math.round((new Date(job.run_after).getTime() - now.getTime()) / 60000));
      const when = minutes >= 90 ? `in ${Math.round(minutes / 60)} h` : `in ${minutes} min`;
      return `Neuer Versuch ${when} (${job.attempts}/${job.max_attempts})`;
    }
  }
}

/** Kurzfassung eines Import-Ergebnisses: „12 neu · 3 verknüpft · 1 übersprungen“. */
export function jobResultText(job: Job): string {
  const r = job.result ?? {};
  const parts: string[] = [];
  const add = (key: string, label: string) => {
    const n = r[key];
    if (typeof n === "number" && n > 0) parts.push(`${n} ${label}`);
  };
  add("created", "neu");
  add("linked", "verknüpft");
  add("skipped", "übersprungen");
  add("failed", "fehlgeschlagen");
  add("playlists", "Playlists");
  add("filled", "ergänzt");
  add("unavailable", "nicht verfügbar");
  add("imports_queued", "Importe eingeplant");
  return parts.join(" · ");
}

export type PlaylistState = "blocked" | "removed" | "error" | "current" | "changed" | "new";

export function playlistState(p: RemotePlaylist): PlaylistState {
  if (p.removed) return "removed";
  if (!p.items_accessible) return "blocked";
  if (p.last_import_status === "error") return "error";
  if (p.local_playlist_id) return p.up_to_date ? "current" : "changed";
  return "new";
}

export const PLAYLIST_STATE_TEXT: Record<PlaylistState, string> = {
  blocked: "Inhalt nicht lesbar",
  removed: "Nicht mehr auf der Plattform",
  error: "Fehler beim Import",
  current: "Importiert, aktuell",
  changed: "Auf der Plattform geändert",
  new: "Noch nicht importiert",
};
