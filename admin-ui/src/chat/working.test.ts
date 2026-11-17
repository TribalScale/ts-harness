import { expect, it } from "vitest";
import { WORKING_ACTIVITY, isWorkingActivity } from "./working";

it("treats every working status as in progress", () => {
  for (const status of WORKING_ACTIVITY) {
    expect(isWorkingActivity(status)).toBe(true);
  }
});

it("leaves waiting and finished programs quiet", () => {
  for (const status of ["blocked", "failed", "succeeded", "cancelled", ""]) {
    expect(isWorkingActivity(status)).toBe(false);
  }
});
