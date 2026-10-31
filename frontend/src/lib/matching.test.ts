import { describe, expect, it } from "vitest";

import { blockerTexts, differingFacts, mergeChanges, reasonEffect, sortReasons } from "./matching";
import type { MatchReason, MatchTrack, TrackMerge } from "./types";

function track(overrides: Partial<MatchTrack> = {}): MatchTrack {
  return {
    id: "t",
    title: "Song",
    artist: "Artist",
    album: null,
    version: null,
    duration_ms: 200_000,
    isrc: null,
    release_date: null,
    notes: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    sources: [],
    tags: [],
    platforms: [],
    playlist_count: 0,
    detected_version: "Original",
    ...overrides,
  };
}

const r = (kind: MatchReason["kind"], effect = 0, text: string = kind): MatchReason => ({
  code: kind,
  text,
  effect,
  kind,
});

describe("Gründe", () => {
  it("formatiert Effekte", () => {
    expect(reasonEffect(r("plus", 55))).toBe("+55");
    expect(reasonEffect(r("minus", -25))).toBe("−25");
    expect(reasonEffect(r("cap", 40))).toBe("max. 40");
    expect(reasonEffect(r("info"))).toBe("");
  });

  it("sortiert Sperren und Obergrenzen nach vorn", () => {
    const sorted = sortReasons([r("info"), r("plus", 4), r("cap", 40), r("minus", -5), r("block")]);
    expect(sorted.map((x) => x.kind)).toEqual(["block", "cap", "minus", "plus", "info"]);
  });

  it("liefert Blocker-Texte", () => {
    expect(blockerTexts([r("block", 0, "Artist nicht eindeutig"), r("plus", 3)])).toEqual([
      "Artist nicht eindeutig",
    ]);
  });
});

describe("Gegenüberstellung", () => {
  it("markiert nur Angaben, die auf beiden Seiten gesetzt sind und abweichen", () => {
    const a = track({ duration_ms: 200_000, isrc: "GBAAA0000001", album: "LP" });
    const b = track({ duration_ms: 260_000, isrc: null, album: "LP", playlist_count: 3 });
    expect([...differingFacts(a, b)]).toEqual(["duration"]);
    expect(differingFacts(a, track({ duration_ms: 201_500 })).has("duration")).toBe(false);
    expect(differingFacts(a, track({ detected_version: "Live-Version" })).has("version")).toBe(true);
  });
});

describe("Verlauf", () => {
  it("fasst Änderungen zusammen", () => {
    const merge = {
      changes: { sources: 2, sources_demoted: 1, playlist_items: 1, tags_added: 0, import_items: 0 },
      filled_fields: ["isrc", "album"],
    } as unknown as TrackMerge;
    expect(mergeChanges(merge)).toBe("2 Quellen · 1 als Vorschlag · 1 Playlist-Eintrag · ergänzt: ISRC, Album");
  });
});
