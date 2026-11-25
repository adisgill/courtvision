import { describe, expect, it } from "vitest";
import { applyTrades, drawLottery, needWeights, runMock } from "./draft";

const ODDS = [140, 140, 140, 125, 105, 90, 75, 60, 45, 30, 20, 15, 10, 5];

describe("draft lottery and mock", () => {
  it("draws four lottery winners by the odds and keeps everyone else in order", () => {
    const seeded = Array.from({ length: 30 }, (_, i) => i + 1);
    let seed = 1;
    const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    let firstWorst = 0;
    for (let k = 0; k < 20000; k++) if (drawLottery(seeded, ODDS, rand)[0] === 1) firstWorst++;
    expect(Math.abs(firstWorst / 20000 - 0.14)).toBeLessThan(0.01);
    const one = drawLottery(seeded, ODDS, rand);
    expect([...one].sort((a, b) => a - b)).toEqual(seeded);
    expect(one.slice(14)).toEqual(seeded.slice(14));
    const rest = one.slice(4, 14);
    expect(rest).toEqual([...rest].sort((a, b) => a - b));
  });

  it("applies protections and swaps", () => {
    const order = [
      { team_id: 1, trade_owner_id: 9, kind: "convey", protected_through: 1 },
      { team_id: 2, trade_owner_id: 3, kind: "swap", protected_through: null },
    ];
    expect(applyTrades([1, 2, 3], order)).toEqual([1, 3, 2]); // 1 keeps #1; 3 swaps into #2
    expect(applyTrades([3, 1, 2], order)).toEqual([3, 9, 2]); // 1's #2 conveys; 2's #3 is worse
  });

  it("takes value, with fit deciding close calls", () => {
    const board = [{ pid: 1, ev: 10 }, { pid: 2, ev: 6 }, { pid: 3, ev: 5.8 }];
    const fit = new Map([["7:2", -1], ["7:3", 2]]);
    const m = runMock([7, 7, 8], [7, 7, 8], board, fit, 0.1);
    expect(m.map((x) => x.p.pid)).toEqual([1, 3, 2]);
    expect(needWeights({ shooting_adj: 35, defense_adj: 55 }).shooting).toBe(1.5);
  });
});
