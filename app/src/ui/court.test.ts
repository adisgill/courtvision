import { describe, expect, it } from "vitest";
import { hexOf, unitHex, zoneOf } from "./court";

// [x, y, q, r] from courtvision.app.court.hex_of (the Python the league baseline is built with)
const CASES: [number, number, number, number][] = [[156.0, 278.0, 0, 11], [-208.0, -47.0, -6, -2], [-161.0, -21.0, -5, -1], [-132.0, 290.0, -10, 11], [-160.0, 280.0, -11, 11], [151.0, 56.0, 4, 2], [185.0, 1.0, 6, 0], [41.0, 61.0, 0, 2], [-231.0, -33.0, -7, -1], [-203.0, 262.0, -12, 10], [-84.0, 182.0, -6, 7], [-34.0, 156.0, -4, 6], [61.0, 37.0, 1, 2], [-10.0, 116.0, -3, 5], [-118.0, 19.0, -5, 1], [-170.0, 221.0, -10, 9], [96.0, 117.0, 1, 5], [118.0, -37.0, 5, -2], [-234.0, 41.0, -9, 2], [-194.0, 198.0, -11, 8], [-24.0, 133.0, -3, 5], [-54.0, 82.0, -3, 3], [194.0, 41.0, 6, 2], [8.0, -16.0, 1, -1], [-40.0, 164.0, -5, 7], [-35.0, 182.0, -5, 7], [83.0, 133.0, 0, 5], [43.0, 276.0, -4, 11], [-164.0, 274.0, -11, 11], [119.0, 24.0, 4, 1], [129.0, 164.0, 1, 7], [229.0, 171.0, 4, 7], [143.0, 39.0, 4, 2], [-108.0, 56.0, -5, 2], [-90.0, 122.0, -6, 5], [74.0, 210.0, -1, 8], [75.0, 54.0, 2, 2], [98.0, 203.0, -1, 8], [185.0, 179.0, 3, 7], [-104.0, 28.0, -4, 1], [0.0, 0.0, 0, 0], [17.0, 25.0, 0, 1], [-17.0, 25.0, -1, 1], [29.0, 12.0, 1, 0], [220.0, 87.0, 6, 3], [-220.0, 87.0, -9, 3]];

describe("hexOf", () => {
  it("bins shots into the same hexagons as the Python baseline", () => {
    for (const [x, y, q, r] of CASES) expect(hexOf(x, y)).toEqual([q, r]);
  });
});

describe("zoneOf", () => {
  it("follows the cv_zone rule", () => {
    expect(zoneOf(3, 230, 40)).toBe("C3");
    expect(zoneOf(3, 0, 260)).toBe("ATB3");
    expect(zoneOf(2, 10, 10)).toBe("RA");
    expect(zoneOf(2, 60, 100)).toBe("PAINT");
    expect(zoneOf(2, 150, 150)).toBe("MID");
  });
});

describe("unitHex", () => {
  it("shrinks a small hexagon toward its zone and the zone toward league", () => {
    const league = [{ season: 2026, q: 0, rr: 0, lg_n: 1000, lg_fg: 0.6, lg_pps: 1.2, zone: "RA" }];
    const shots = Array.from({ length: 10 }, () => ({ season: 2026, x: 0, y: 0, made: 1, shot_value: 2 }));
    const [h] = unitHex(shots, league);
    expect(h.share).toBe(1);
    expect(h.fg).toBe(1);
    // 10 shots 0.8 points above league; zone: 8 / (10 + 150) per shot; hexagon: (8 + 60 * zone) / 70
    const zone = 8 / 160;
    expect(h.diff).toBeCloseTo((100 * (8 + 60 * zone)) / 70, 6);
  });
});
