// Reine Hilfsfunktionen für die Matching-Seite (ohne DOM, testbar).

import { formatDuration } from "./api";
import type { MatchLevel, MatchReason, MatchTrack, TrackMerge } from "./types";

export const LEVEL_LABEL: Record<MatchLevel, string> = {
  certain: "Sicher identifiziert",
  likely: "Wahrscheinlich identisch",
  review: "Manuelle Prüfung notwendig",
  different: "Unterschiedliche Tracks",
};

export const LEVEL_SHORT: Record<MatchLevel, string> = {
  certain: "Sicher",
  likely: "Wahrscheinlich",
  review: "Prüfen",
  different: "Unterschiedlich",
};

/** Kurzform des Effekts eines Grundes: „+55“, „−25“, „max. 40“ oder leer. */
export function reasonEffect(reason: MatchReason): string {
  switch (reason.kind) {
    case "plus":
      return `+${reason.effect}`;
    case "minus":
      return `−${Math.abs(reason.effect)}`;
    case "cap":
      return `max. ${reason.effect}`;
    case "block":
      return "gesperrt";
    default:
      return "";
  }
}

/** Gründe sortiert: Sperren und Obergrenzen zuerst, dann Abzüge, Zuschläge, Hinweise. */
export function sortReasons(reasons: MatchReason[]): MatchReason[] {
  const order: Record<MatchReason["kind"], number> = { block: 0, cap: 1, minus: 2, plus: 3, info: 4 };
  return [...reasons].sort((a, b) => order[a.kind] - order[b.kind]);
}

/** Texte der Gründe, die eine automatische Zusammenführung verhindern. */
export function blockerTexts(reasons: MatchReason[]): string[] {
  return reasons.filter((r) => r.kind === "block").map((r) => r.text);
}

/** Anzeige der Version: Versionsfeld bzw. aus dem Titel erkannte Version. */
export function versionText(track: MatchTrack): string {
  if (track.version) return track.version;
  return track.detected_version === "Original"
    ? "Original (keine Versionsangabe)"
    : `${track.detected_version} (aus dem Titel)`;
}

export interface FactRow {
  key: string;
  label: string;
  value: string;
}

const dateFormat = new Intl.DateTimeFormat("de-DE", { dateStyle: "medium" });

/** Vergleichbare Angaben eines Tracks für die Gegenüberstellung. */
export function trackFacts(track: MatchTrack): FactRow[] {
  return [
    { key: "version", label: "Version", value: versionText(track) },
    { key: "duration", label: "Dauer", value: formatDuration(track.duration_ms) },
    { key: "isrc", label: "ISRC", value: track.isrc ?? "–" },
    { key: "album", label: "Album", value: track.album ?? "–" },
    {
      key: "release",
      label: "Veröffentlicht",
      value: track.release_date ? dateFormat.format(new Date(track.release_date)) : "–",
    },
    {
      key: "playlists",
      label: "Playlists",
      value: track.playlist_count ? String(track.playlist_count) : "–",
    },
    { key: "added", label: "Hinzugefügt", value: dateFormat.format(new Date(track.created_at)) },
  ];
}

/** Schlüssel der Angaben, die sich zwischen zwei Tracks unterscheiden (beide gesetzt). */
export function differingFacts(a: MatchTrack, b: MatchTrack): Set<string> {
  const left = new Map(trackFacts(a).map((r) => [r.key, r.value]));
  const diff = new Set<string>();
  for (const row of trackFacts(b)) {
    if (row.key === "added" || row.key === "playlists") continue;
    if (row.key === "version") {
      if (a.detected_version !== b.detected_version) diff.add("version");
      continue;
    }
    if (row.key === "duration") {
      // Bis 2 s gilt die Dauer als gleich (wie in der Engine)
      if (a.duration_ms && b.duration_ms && Math.abs(a.duration_ms - b.duration_ms) > 2000) {
        diff.add("duration");
      }
      continue;
    }
    const other = left.get(row.key);
    if (other !== undefined && other !== "–" && row.value !== "–" && other !== row.value) {
      diff.add(row.key);
    }
  }
  return diff;
}

const FIELD_LABEL: Record<string, string> = {
  album: "Album",
  version: "Version",
  duration_ms: "Dauer",
  isrc: "ISRC",
  release_date: "Veröffentlichung",
  notes: "Notizen",
};

/** Kurzbeschreibung, was eine Zusammenführung verändert hat. */
export function mergeChanges(merge: TrackMerge): string {
  const c = merge.changes;
  const parts: string[] = [];
  if (c.sources) {
    parts.push(`${c.sources} ${c.sources === 1 ? "Quelle" : "Quellen"}`);
  }
  if (c.sources_demoted) parts.push(`${c.sources_demoted} als Vorschlag`);
  if (c.playlist_items) {
    parts.push(`${c.playlist_items} Playlist-${c.playlist_items === 1 ? "Eintrag" : "Einträge"}`);
  }
  if (c.tags_added) parts.push(`${c.tags_added} ${c.tags_added === 1 ? "Tag" : "Tags"}`);
  if (merge.filled_fields.length) {
    parts.push(`ergänzt: ${merge.filled_fields.map((f) => FIELD_LABEL[f] ?? f).join(", ")}`);
  }
  return parts.join(" · ") || "keine weiteren Daten";
}
