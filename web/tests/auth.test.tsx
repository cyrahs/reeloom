import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TOKEN_STORAGE_KEY } from "../src/api";
import { AuthGate } from "../src/auth";

test("stores only a validated admin token and removes it on logout", async () => {
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ api_version: "1.0.0", role: "admin" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

  render(
    <AuthGate>
      <p>受保护内容</p>
    </AuthGate>,
  );
  await userEvent.type(
    screen.getByLabelText("Admin Bearer token"),
    "secret-admin-token",
  );
  await userEvent.click(screen.getByRole("button", { name: "进入控制台" }));

  await screen.findByText("受保护内容");
  expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBe(
    "secret-admin-token",
  );
  expect(document.body).not.toHaveTextContent("secret-admin-token");
  expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
    headers: { Authorization: "Bearer secret-admin-token" },
  });
});

test.each([
  [
    "server rejection",
    401,
    { error: { code: "unauthorized" } },
    "Token 无效或已过期",
  ],
  [
    "non-Admin session",
    200,
    { api_version: "1.0.0", role: "viewer" },
    "无法验证 Token",
  ],
] as const)("rejects %s without persisting the token", async (
  _case,
  status,
  payload,
  message,
) => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  render(
    <AuthGate>
      <p>受保护内容</p>
    </AuthGate>,
  );
  await userEvent.type(
    screen.getByLabelText("Admin Bearer token"),
    "invalid-token",
  );
  await userEvent.click(screen.getByRole("button", { name: "进入控制台" }));

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(message),
  );
  expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  expect(screen.queryByText("受保护内容")).not.toBeInTheDocument();
});
