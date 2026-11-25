import { describe, expect, it } from "vitest";
import { groupDraws, needWeights, ownerText, runMock } from "./draft";

describe("draft draws and mock", () => {
  it("groups draws into teams and owners by slot", () => {
    const rows = [
      { draw: 1, slot: 2, team_id: 5, owner_id: 9 }, { draw: 0, slot: 1, team_id: 4, owner_id: 4 },
      { draw: 1, slot: 1, team_id: 4, owner_id: 4 }, { draw: 0, slot: 2, team_id: 5, owner_id: 9 },
    ];
    expect(groupDraws(rows)).toEqual([[[4, 5], [4, 9]], [[4, 5], [4, 9]]]);
  });

  it("writes who ends up with a pick, likeliest first", () => {
    const ab = (t: number) => ({ 1: "NOP", 2: "ATL", 3: "MIL" }[t] ?? "?");
    const rows = [{ owner_id: 2, prob: 0.35 }, { owner_id: 1, prob: 0.649 }, { owner_id: 3, prob: 0.001 }];
    expect(ownerText(rows, ab)).toBe("NOP 65% · ATL 35%");
  });

  it("takes value, with fit deciding close calls", () => {
    const board = [{ pid: 1, ev: 10 }, { pid: 2, ev: 6 }, { pid: 3, ev: 5.8 }];
    const fit = new Map([["7:2", -1], ["7:3", 2]]);
    const m = runMock([7, 7, 8], [7, 7, 8], board, fit, 0.1);
    expect(m.map((x) => x.p.pid)).toEqual([1, 3, 2]);
    expect(needWeights({ shooting_adj: 35, defense_adj: 55 }).shooting).toBe(1.5);
  });
});
