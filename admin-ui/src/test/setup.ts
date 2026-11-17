import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, expect } from "vitest";

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

if (typeof Element !== "undefined" && typeof Element.prototype.scrollTo !== "function") {
  Element.prototype.scrollTo = function scrollTo() {};
}

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  localStorage.clear();
});

// Astryx >= 0.7.13 disables Buttons with aria-disabled="true" instead of the native `disabled`
// attribute (they stay focusable; clicks, Enter and Space are blocked). jest-dom's toBeDisabled()
// only sees the native attribute, so tests use toBeDisabledControl(), which accepts either.
expect.extend({
  toBeDisabledControl(received: Element) {
    // :disabled also covers controls inside a disabled <fieldset>, as jest-dom does.
    const native = received.matches(":disabled");
    const aria = received.getAttribute("aria-disabled") === "true";
    const pass = native || aria;
    return {
      pass,
      message: () =>
        pass
          ? `expected ${received.outerHTML.slice(0, 120)} to be enabled`
          : `expected ${received.outerHTML.slice(0, 120)} to be disabled (native or aria-disabled)`,
    };
  },
});

declare module "vitest" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  interface Assertion<T = any> {
    toBeDisabledControl(): T;
  }
  interface AsymmetricMatchersContaining {
    toBeDisabledControl(): unknown;
  }
}
