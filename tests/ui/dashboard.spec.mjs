import { expect, test } from "@playwright/test";

const PASSWORD = process.env.UI_PASSWORD;
const BOOT_URL = process.env.BOOT_URL;
const SEEN_MAC = "52:54:00:10:20:30";
const NEW_MAC = "52:54:00:40:50:60";
const SHOTS = process.env.UI_OUTPUT || "test-results";

async function logIn(page) {
  await page.goto("/login");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("heading", { name: "ISOs" })).toBeVisible();
}

async function menuFor(request, mac) {
  const hyphens = mac.replaceAll(":", "-");
  const response = await request.get(`${BOOT_URL}/ipxe/menu?mac=${hyphens}&arch=x86_64&platform=efi`);
  return response.text();
}

test("sends logged-out visitors to the login page", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByLabel("Password")).toBeVisible();
});

test("refuses a wrong password, then accepts the right one", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.locator(".form-error")).toContainText("isn't right");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("heading", { name: "ISOs" })).toBeVisible();
});

test("lists ISOs with their family, architecture and warnings", async ({ page }) => {
  await logIn(page);
  const alpine = page.getByRole("row", { name: /alpine-test\.iso/ });
  await expect(alpine).toContainText("Alpine Linux");
  await expect(alpine).toContainText("x64");
  await expect(alpine).toContainText("Ready");
  const arm = page.getByRole("row", { name: /alpine-arm\.iso/ });
  await expect(arm).toContainText("ARM64");
  const generic = page.getByRole("row", { name: /memtest-tool\.iso/ });
  await expect(generic).toContainText("Generic");
  await expect(generic).toContainText("isn't a recognised OS family");
});

test("shows which network drivers a Windows ISO will load, and why one was skipped", async ({ page }) => {
  await logIn(page);
  await expect(page.getByRole("row", { name: /windows-test\.iso/ })).toContainText("Loads network drivers: rt25.inf");
  await expect(page.getByRole("row", { name: /alpine-test\.iso/ }).first()).not.toContainText("Loads network drivers");
  await expect(page.getByRole("status").filter({ hasText: "x64/messy" })).toContainText("not ok.sys");
});

test("shows a device that booted, and pins it through to the boot menu", async ({ page, request }) => {
  await menuFor(request, SEEN_MAC);
  await logIn(page);
  const row = page.getByRole("row", { name: new RegExp(SEEN_MAC) });
  await expect(row).toContainText("x64 UEFI");

  await row.getByLabel(`ISO for ${SEEN_MAC}`).selectOption({ label: "alpine-test.iso" });
  await row.getByLabel(`Pin type for ${SEEN_MAC}`).selectOption({ label: "Every boot" });
  await row.getByRole("button", { name: "Pin" }).click();
  await expect(row.getByRole("button", { name: "Unpin" })).toBeVisible();
  expect(await menuFor(request, SEEN_MAC)).toContain("pinned to alpine-test.iso");

  await page.reload();
  await expect(row.getByLabel(`ISO for ${SEEN_MAC}`)).toHaveValue(/alpine-test/);

  await row.getByRole("button", { name: "Unpin" }).click();
  await expect(row.getByRole("button", { name: "Unpin" })).toBeHidden();
  expect(await menuFor(request, SEEN_MAC)).not.toContain("pinned to");
});

test("a once pin clears itself in the UI when the device boots", async ({ page, request }) => {
  await menuFor(request, SEEN_MAC);
  await logIn(page);
  const row = page.getByRole("row", { name: new RegExp(SEEN_MAC) });
  await row.getByLabel(`ISO for ${SEEN_MAC}`).selectOption({ label: "alpine-test.iso" });
  await row.getByRole("button", { name: "Pin" }).click();
  await expect(row.getByRole("button", { name: "Unpin" })).toBeVisible();

  const pinned = await menuFor(request, SEEN_MAC);
  const url = pinned.match(/chain (http\S+pin=1)/)[1];
  const script = await (await request.get(url)).text();
  expect(script).toContain("kernel ");

  await expect(row.getByRole("button", { name: "Unpin" })).toBeHidden({ timeout: 15_000 });
  await expect(row).toContainText("Booted alpine-test.iso");
});

test("checks a typed MAC address, then pins a device it hasn't seen", async ({ page, request }) => {
  await logIn(page);
  const macField = page.getByLabel("MAC address");
  await macField.fill("not-a-mac");
  await page.getByRole("button", { name: "Pin device" }).click();
  await expect(page.locator("form.add-device .form-error")).toContainText("MAC address");

  await macField.fill(NEW_MAC);
  const form = page.locator("form.add-device");
  await form.getByLabel("ISO").selectOption({ label: "alpine-test.iso" });
  await page.getByRole("button", { name: "Pin device" }).click();
  await expect(macField).toHaveValue("");
  const row = page.getByRole("row", { name: new RegExp(NEW_MAC) });
  await expect(row).toContainText("Not seen yet");
  expect(await menuFor(request, NEW_MAC)).toContain("pinned to alpine-test.iso");
  await row.getByRole("button", { name: "Unpin" }).click();
  await expect(row.getByRole("button", { name: "Unpin" })).toBeHidden();
});

test("logs out", async ({ page }) => {
  await logIn(page);
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("fits a phone screen without sideways scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await logIn(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: `${SHOTS}/mobile.png`, fullPage: true });
});

test("captures the dashboard in both themes", async ({ page, request }) => {
  await menuFor(request, SEEN_MAC);
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.emulateMedia({ colorScheme: "light" });
  await logIn(page);
  await page.screenshot({ path: `${SHOTS}/light.png`, fullPage: true });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: `${SHOTS}/dark.png`, fullPage: true });
});

test("shows an outage, then recovers when the server comes back", async ({ page }) => {
  await logIn(page);
  await expect(page.getByText("Can't reach the Iso-Vault core")).toBeHidden();
  // The test runner watches for this line, then restarts the container.
  console.log("READY_FOR_RESTART");
  await expect(page.getByText("Can't reach the Iso-Vault core")).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText("Can't reach the Iso-Vault core")).toBeHidden({ timeout: 50_000 });
  await expect(page.getByRole("heading", { name: "ISOs" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "alpine-test.iso", exact: true })).toBeVisible();
});
