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

test("rejects viewer tokens without persisting them", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(
      JSON.stringify({ api_version: "1.0.0", role: "viewer" }),
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
    "viewer-token",
  );
  await userEvent.click(screen.getByRole("button", { name: "进入控制台" }));

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(
      "只接受 Admin token",
    ),
  );
  expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  expect(screen.queryByText("受保护内容")).not.toBeInTheDocument();
});
