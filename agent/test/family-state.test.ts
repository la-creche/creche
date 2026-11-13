// The order of the pulls of the family state (decision CH5a). A pull
// starts only after the pull before it settles, so the facade applies the
// pulls in the order they started.

import { describe, expect, it } from "vitest";

import { inOrder } from "../src/family-state.js";
import type { StatePull } from "../src/family-state.js";

describe("inOrder", () => {
  it("starts a pull only after the pull before it settles", async () => {
    const events: string[] = [];
    // The first call is slow and the second is fast.
    const delays = [30, 0];
    const pull = inOrder(async (): Promise<StatePull> => {
      const call = events.length / 2;
      events.push(`start ${call}`);
      await new Promise((resolve) => setTimeout(resolve, delays[call]));
      events.push(`end ${call}`);

      return { ok: false, fault: "creds_missing" };
    });

    await Promise.all([pull(), pull()]);

    expect(events).toEqual(["start 0", "end 0", "start 1", "end 1"]);
  });
});
