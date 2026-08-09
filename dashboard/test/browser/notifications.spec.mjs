import { expect, test } from "@playwright/test";

async function setState(request, value) {
  const response = await request.post(`/__test/state?value=${value}`);
  expect(response.ok()).toBeTruthy();
}

async function installNotificationFake(context) {
  await context.addInitScript(() => {
    window.__notifications = [];
    window.__focusCalls = 0;
    window.focus = () => { window.__focusCalls += 1; };

    class FakeNotification {
      static get permission() {
        const forced = new URL(window.location.href).searchParams.get("permission");
        if (forced) return forced;
        return window.localStorage.getItem("__test-notification-permission") || "default";
      }

      static async requestPermission() {
        window.localStorage.setItem("__test-notification-permission", "granted");
        return "granted";
      }

      constructor(title, options) {
        this.title = title;
        this.options = options;
        this.closed = false;
        window.__notifications.push(this);
      }

      close() {
        this.closed = true;
      }
    }

    window.Notification = FakeNotification;
    window.__clickNotification = (index) => window.__notifications[index]?.onclick?.();
  });
}

test.beforeEach(async ({ context, request }) => {
  await setState(request, "running");
  await installNotificationFake(context);
});

test("permission, cross-tab dedup, and Hub notification clicks use one browser path", async ({ context, page }) => {
  await page.goto("/");
  const toggle = page.getByRole("button", { name: /Enable browser notifications/ });
  await expect(toggle).toBeVisible();
  await toggle.click();
  await expect(page.getByRole("button", { name: /Disable dashboard notifications/ })).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.__notifications.length)).toBe(1);

  const second = await context.newPage();
  await second.goto("/");
  await expect(second.getByText("Notifications on")).toBeVisible();

  await setState(page.request, "validated");
  await expect.poll(async () => {
    const firstCount = await page.evaluate(() => window.__notifications.length);
    const secondCount = await second.evaluate(() => window.__notifications.length);
    return firstCount + secondCount;
  }).toBe(2);

  const deliveryPage = await page.evaluate(() => window.__notifications.length) === 2 ? page : second;
  const notificationIndex = await deliveryPage.evaluate(() => window.__notifications.length - 1);
  const delivered = await deliveryPage.evaluate((index) => {
    const item = window.__notifications[index];
    return { title: item.title, body: item.options.body };
  }, notificationIndex);
  expect(delivered.title).toBe("mergetrain · api");
  expect(delivered.body).toMatch(/awaiting deploy approval/);

  await deliveryPage.evaluate((index) => window.__clickNotification(index), notificationIndex);
  await expect.poll(() => deliveryPage.evaluate(() => window.location.hash)).toBe("#repo=%2Fwork%2Fapi");
  await expect.poll(() => deliveryPage.evaluate(() => window.__focusCalls)).toBe(1);
});

test("a failed live snapshot is visibly degraded, alerts once, and recovers", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Enable browser notifications/ }).click();
  await setState(page.request, "error");

  await expect(page.locator(".live.degraded")).toContainText("DEGRADED");
  await expect(page.getByRole("alert").filter({ hasText: "Live state unavailable" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.__notifications.length)).toBe(2);
  const alert = await page.evaluate(() => {
    const item = window.__notifications.at(-1);
    return { body: item.options.body, title: item.title };
  });
  expect(alert.body).toMatch(/last known state may be stale/);
  expect(JSON.stringify(alert)).not.toMatch(/snapshot temporarily unavailable/);

  await setState(page.request, "running");
  await expect(page.locator(".live")).toContainText("CONNECTED");
  await expect(page.getByText("Live state unavailable")).toHaveCount(0);
});

test("an initial snapshot failure renders a retrying error instead of stale live data", async ({ page }) => {
  await setState(page.request, "error");
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("Local train state unavailable");
  await expect(page.getByRole("alert")).toContainText("Retrying automatically");
  await expect(page.getByText("CONNECTED", { exact: true })).toHaveCount(0);
});

test("denied browser permission is explicit and non-interactive", async ({ page }) => {
  await page.goto("/?permission=denied");
  const blocked = page.getByRole("button", { name: /Allow site notifications/ });
  await expect(blocked).toBeVisible();
  await expect(blocked).toBeDisabled();
  await expect(blocked).toContainText("Notifications blocked");
});
