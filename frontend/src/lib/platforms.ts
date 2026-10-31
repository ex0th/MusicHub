import type { Platform, Track, TrackSource } from "./types";

export interface PlatformMeta {
  id: Platform;
  label: string;
  /** Kürzel für den Abdeckungsstreifen */
  short: string;
}

/** Feste Reihenfolge überall in der UI: Spotify, SoundCloud, YouTube. */
export const PLATFORMS: readonly PlatformMeta[] = [
  { id: "spotify", label: "Spotify", short: "Sp" },
  { id: "soundcloud", label: "SoundCloud", short: "SC" },
  { id: "youtube", label: "YouTube", short: "YT" },
];

export const platformLabel = (id: Platform): string =>
  PLATFORMS.find((p) => p.id === id)?.label ?? id;

export type SlotState = "on" | "off" | "gone";

export interface CoverageSlot {
  platform: PlatformMeta;
  state: SlotState;
  source: TrackSource | null;
}

/**
 * Wie ist ein Track auf einer Plattform vertreten?
 *  on   – bestätigte Quelle, nicht als unverfügbar markiert
 *  gone – bestätigte Quelle, aber als unverfügbar markiert
 *  off  – keine bestätigte Quelle
 */
export function coverage(track: Pick<Track, "sources">): CoverageSlot[] {
  return PLATFORMS.map((platform) => {
    const source =
      track.sources.find((s) => s.platform === platform.id && s.link_status === "linked") ?? null;
    let state: SlotState = "off";
    if (source) state = source.availability === "unavailable" ? "gone" : "on";
    return { platform, state, source };
  });
}

export const SLOT_TEXT: Record<SlotState, string> = {
  on: "verfügbar",
  off: "nicht verknüpft",
  gone: "nicht mehr verfügbar",
};
