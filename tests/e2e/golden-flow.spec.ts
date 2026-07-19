import { expect, test } from "@playwright/test";

import {
  approveRuleAndOpenB03,
  captureBrowserErrors,
  completeB03WithActiveRule,
  createB01Revision,
  rollbackRule,
} from "./helpers";

test.use({ trace: "on" });

test.describe.serial("canonical B01 → B03 golden flow, five consecutive repetitions", () => {
  for (let iteration = 1; iteration <= 5; iteration += 1) {
    test(`golden ${iteration}/5`, async ({ page }, testInfo) => {
      const assertNoBrowserErrors = captureBrowserErrors(page);
      await page.setViewportSize({ width: 1920, height: 1080 });
      await createB01Revision(page);
      const { b01Url } = await approveRuleAndOpenB03(page);
      try {
        await completeB03WithActiveRule(page);
        await page.getByRole("tab", { name: "Письмо" }).click();
        await expect(page.getByText("подготовка выплат в онлайн-банке").first()).toBeVisible();
        await page.screenshot({
          path: testInfo.outputPath(`golden-${iteration}-b03.png`),
          fullPage: true,
        });
      } finally {
        await rollbackRule(page, b01Url);
      }
      assertNoBrowserErrors();
    });
  }
});
