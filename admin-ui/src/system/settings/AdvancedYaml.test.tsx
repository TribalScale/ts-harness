import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderPage } from "../../test/render";
import { AdvancedYaml } from "./AdvancedYaml";

describe("AdvancedYaml", () => {
  it("copies redacted YAML without secrets", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderPage(
      <AdvancedYaml
        draftYaml={"gateway:\n  token: ***\n"}
        liveYaml={"gateway:\n  token: super-secret-token\n"}
      />
    );
    await user.click(screen.getByRole("button", { name: "Copy redacted YAML" }));
    expect(writeText).toHaveBeenCalledWith("gateway:\n  token: ***\n");
    expect(writeText.mock.calls[0][0]).not.toContain("super-secret-token");
  });
});
