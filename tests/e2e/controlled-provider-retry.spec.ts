import { expect, test, type Page } from "@playwright/test";

import { captureBrowserErrors } from "./helpers";

const expected = process.env.RETRY_E2E_EXPECTED ?? "";

async function createReadyCampaign(page: Page, suffix: string) {
  const create = await page.request.post("/api/v1/campaigns", {
    data: { case_id: "B04" },
    headers: { "Idempotency-Key": `retry-e2e-create-${suffix}` },
  });
  expect(create.status()).toBe(201);
  const campaign = (await create.json()) as { campaign_id: string };
  const validate = await page.request.post(
    `/api/v1/campaigns/${campaign.campaign_id}/validate`,
    { headers: { "Idempotency-Key": `retry-e2e-validate-${suffix}` } },
  );
  expect(validate.ok()).toBeTruthy();
  return campaign.campaign_id;
}

async function waitForTerminalRun(page: Page, campaignId: string) {
  let run: { status?: string; terminal_at?: string | null } = {};
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`/api/v1/campaigns/${campaignId}/workspace`);
        const workspace = (await response.json()) as { runs: Array<typeof run> };
        run = workspace.runs.at(-1) ?? {};
        return run.terminal_at;
      },
      { timeout: 10_000 },
    )
    .not.toBeNull();
  return run;
}

test("enabled retry profile shows a Russian recovered-success status", async ({ page }, testInfo) => {
  test.skip(expected !== "success", "success fault profile only");
  const assertNoBrowserErrors = captureBrowserErrors(page);
  await page.setViewportSize({ width: 1920, height: 1080 });
  const campaignId = await createReadyCampaign(page, "success-0001");
  await page.goto(`/campaigns/${campaignId}`);
  await page.getByRole("button", { name: "Создать через Ouroboros" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Временный сбой — повторяем запрос (2 из 2)" }),
  ).toBeVisible();
  const run = await waitForTerminalRun(page, campaignId);
  expect(run.status).toBe("COMPLETED");

  await expect(
    page.getByText("Временный сбой устранён — запрос выполнен со второй попытки"),
  ).toBeVisible();
  await expect(page.getByText("Комплект v1", { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("retry-success.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(390);
  assertNoBrowserErrors();
});

test("enabled retry profile stops after attempt two without an infinite spinner", async ({
  page,
}, testInfo) => {
  test.skip(expected !== "failure", "failure fault profile only");
  const assertNoBrowserErrors = captureBrowserErrors(page);
  await page.setViewportSize({ width: 1920, height: 1080 });
  const campaignId = await createReadyCampaign(page, "failure-0001");
  await page.goto(`/campaigns/${campaignId}`);
  await page.getByRole("button", { name: "Создать через Ouroboros" }).click();
  const run = await waitForTerminalRun(page, campaignId);
  expect(run.status).toBe("FAILED");

  const terminal = page.getByRole("alert").filter({ hasText: "Повторный запрос не выполнен" });
  await expect(terminal).toBeVisible();
  await expect(terminal).toContainText("Автоматических попыток больше не будет");
  const manualRestart = page.getByRole("button", { name: "Создать через Ouroboros" });
  await expect(manualRestart).toBeVisible();
  await expect(manualRestart).toBeEnabled();
  await expect(page.getByText("Поток событий подключён", { exact: true })).toHaveCount(0);
  await page.waitForTimeout(300);
  await expect(terminal).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("retry-terminal-failure.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(390);
  assertNoBrowserErrors();
});
