import { describe, expect, it } from "vitest";

import { formatTotalDuration, trackQueryString } from "./api";
import { coverage } from "./platforms";
import type { TrackSource } from "./types";

const source = (overrides: Partial<TrackSource>): TrackSource => ({
  id: "s",
  track_id: "t",
  platform: "spotify",
  external_id: "x",
  url: "https://open.spotify.com/track/x",
  link_status: "linked",
  availability: "unknown",
  match_method: "manual",
  match_score: null,
  last_checked_at: null,
  created_at: "",
  updated_at: "",
  ...overrides,
});

describe("Abdeckung", () => {
  it("unterscheidet verfügbar, fehlend und nicht mehr verfügbar", () => {
    const slots = coverage({
      sources: [
        source({ platform: "spotify" }),
        source({ platform: "youtube", availability: "unavailable" }),
        source({ platform: "soundcloud", link_status: "candidate" }),
      ],
    });
    expect(slots.map((s) => [s.platform.id, s.state])).toEqual([
      ["spotify", "on"],
      ["soundcloud", "off"],
      ["youtube", "gone"],
    ]);
  });
});

describe("Track-Abfrage", () => {
  it("wiederholt Listenparameter", () => {
    expect(
      trackQueryString({ platform: ["spotify", "youtube"], tag: ["a"], platform_count: 0, sort: "title" }),
    ).toBe("?platform=spotify&platform=youtube&platform_count=0&tag=a&sort=title");
    expect(trackQueryString({})).toBe("");
  });
});

describe("Gesamtdauer", () => {
  it("wechselt das Format mit der Länge", () => {
    expect(formatTotalDuration(225_000)).toBe("3:45");
    expect(formatTotalDuration(3_725_000)).toBe("1:02:05");
    expect(formatTotalDuration(11 * 3_600_000 + 4 * 60_000)).toBe("11 Std. 4 Min.");
  });
});
