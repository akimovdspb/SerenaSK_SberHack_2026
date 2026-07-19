import { expect, test } from "@playwright/test";

import { captureBrowserErrors } from "./helpers";

test("focused 390×844 responsive smoke keeps navigation, focus and primary action usable", async ({
  page,
}, testInfo) => {
  const assertNoBrowserErrors = captureBrowserErrors(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Кампании и коммуникации" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Новая кампания" })).toBeVisible();
  await expect(page.locator(".mobile-nav")).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toBeVisible();
  const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
  const viewportWidth = await page.evaluate(() => window.innerWidth);
  expect(bodyWidth).toBeLessThanOrEqual(viewportWidth);
  await page.screenshot({ path: testInfo.outputPath("narrow-dashboard.png") });
  assertNoBrowserErrors();
});
