import { describe, expect, it } from "vitest";
import { isJwt, oidcTokenExchangeBody, pickJwt, rolesFromJwt, sessionToken } from "./session";

describe("isJwt", () => {
  it("accepts a three-segment token", () => {
    expect(isJwt("aaa.bbb.ccc")).toBe(true);
  });

  it("rejects opaque Auth0 or static gateway tokens", () => {
    expect(isJwt("hdfjk943we94222qsdisizxd")).toBe(false);
    expect(isJwt("")).toBe(false);
  });
});

describe("pickJwt", () => {
  it("prefers a JWT access token over an opaque access token", () => {
    expect(pickJwt("opaque-access", "aaa.bbb.ccc")).toBe("aaa.bbb.ccc");
  });

  it("returns the first JWT among candidates", () => {
    expect(pickJwt("aaa.bbb.ccc", "ddd.eee.fff")).toBe("aaa.bbb.ccc");
  });
});

describe("oidcTokenExchangeBody", () => {
  it("includes audience so Auth0 issues a JWT access token", () => {
    const body = oidcTokenExchangeBody({
      code: "c",
      redirectUri: "https://example/",
      clientId: "spa",
      codeVerifier: "v",
      audience: "ts-harness",
    });
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("audience")).toBe("ts-harness");
    expect(body.get("client_id")).toBe("spa");
  });
});

describe("rolesFromJwt", () => {
  function tokenWith(payload: Record<string, unknown>): string {
    const b64 = (obj: object) =>
      btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    return `${b64({ alg: "none" })}.${b64(payload)}.sig`;
  }

  it("reads Auth0 namespaced roles claims", () => {
    expect(rolesFromJwt(tokenWith({ "https://ts-harness/roles": ["admin"] }))).toEqual([
      "admin",
    ]);
  });
});

describe("sessionToken", () => {
  it("sends the ID token when the provider validates ID tokens", () => {
    expect(sessionToken("id", { access_token: "aaa.bbb.ccc", id_token: "ddd.eee.fff" })).toBe("ddd.eee.fff");
  });

  it("prefers a JWT access token otherwise", () => {
    expect(sessionToken("access", { access_token: "aaa.bbb.ccc", id_token: "ddd.eee.fff" })).toBe("aaa.bbb.ccc");
    expect(sessionToken(undefined, { access_token: "opaque", id_token: "ddd.eee.fff" })).toBe("ddd.eee.fff");
  });

  it("returns nothing for id mode without an ID token", () => {
    expect(sessionToken("id", { access_token: "aaa.bbb.ccc" })).toBe("");
  });
});
