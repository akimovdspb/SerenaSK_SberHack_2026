import { expect, test } from "@playwright/test";

import {
  captureBrowserErrors,
  completeB01V1,
  completeB03WithActiveRule,
  createB01Revision,
  rollbackRule,
  startB01,
  startFreshCase,
  validateReadyAndGenerate,
  approveRuleAndOpenB03,
} from "./helpers";

test.describe.serial("Gate 4 mandatory browser matrix", () => {
  test("1. full B01 happy flow reaches grounded QA-green v1", async ({ page }, testInfo) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await completeB01V1(page);
    await expect(page.getByLabel("Безопасный предпросмотр письма")).not.toContainText(
      "fact_payroll_",
    );
    await page.getByRole("tab", { name: /^Факты/ }).click();
    await expect(page.getByText("fact_payroll_setup", { exact: true })).toBeVisible();
    await page.getByRole("tab", { name: /^Привязки/ }).click();
    await expect(page.getByRole("heading", { name: "Утверждение → факт → источник" })).toBeVisible();
    await page.getByRole("tab", { name: /^Качество/ }).click();
    await expect(page.getByText("проверок: 22 · замечаний: 0 · вызовов модели: 0")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("b01-v1-qa.png"), fullPage: true });
    assertNoBrowserErrors();
  });

  test("2. needs-input remains deterministic and generation stays disabled", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startB01(page);
    await page.getByRole("button", { name: "Проверить бриф" }).click();
    await expect(page.getByText("Нужны ответы · вызовов модели: 0")).toBeVisible();
    await expect(page.getByText("Как подписать ссылку действия?")).toBeVisible();
    await expect(page.getByText("Какую разрешённую синтетическую ссылку использовать?")).toBeVisible();
    await expect(page.getByRole("button", { name: "Создать шаблонный комплект" })).toHaveCount(0);
    assertNoBrowserErrors();
  });

  test("3. contact blocker cannot be generated or approved", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startFreshCase(page, "B11");
    await page.getByRole("button", { name: "Проверить бриф" }).click();
    await expect(page.getByText("Заблокирован", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("CONTACT_CHANNELS_BLOCKED", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Утверждение недоступно" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Создать шаблонный комплект" })).toHaveCount(0);
    assertNoBrowserErrors();
  });

  test("4. consent policy shows an explicit SMS suppression", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startFreshCase(page, "B09");
    await validateReadyAndGenerate(page);
    await page.getByRole("tab", { name: "СМС" }).click();
    await expect(page.getByRole("heading", { name: "СМС подавлен" })).toBeVisible();
    await expect(page.getByText("Синтетический профиль не разрешает этот канал.")).toBeVisible();
    await page.getByRole("tab", { name: "Письмо" }).click();
    await expect(page.getByRole("heading", { name: "Счета Поток" })).toBeVisible();
    assertNoBrowserErrors();
  });

  test("5. feedback creates targeted v2 and protected-path diff", async ({ page }, testInfo) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await createB01Revision(page);
    await expect(page.getByText("2 изменено", { exact: true })).toBeVisible();
    await expect(page.getByText(/защищено/).first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("b01-targeted-diff.png"), fullPage: true });
    assertNoBrowserErrors();
  });

  test("6. tested rule needs E2E approval, applies to B03, then rolls back", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await createB01Revision(page);
    const { b01Url } = await approveRuleAndOpenB03(page);
    try {
      await completeB03WithActiveRule(page);
    } finally {
      await rollbackRule(page, b01Url);
    }
    assertNoBrowserErrors();
  });

  test("7. template and replay artifacts keep unmistakable mode badges", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startFreshCase(page, "B04");
    await validateReadyAndGenerate(page);
    await expect(page.getByText("Резервный шаблон", { exact: true }).first()).toBeVisible();
    const workspaceUrl = page.url();
    await page.route("**/api/v1/campaigns/*/workspace", async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as Record<string, unknown> & {
        package?: { mode?: string };
        package_history?: Array<{ mode?: string }>;
      };
      if (body.package) body.package.mode = "replay";
      body.package_history?.forEach((item) => {
        item.mode = "replay";
      });
      await route.fulfill({ response, json: body });
    });
    await page.goto(workspaceUrl);
    await expect(page.getByText("Повтор · сохранённый результат", { exact: true }).first()).toBeVisible();
    assertNoBrowserErrors();
  });

  test("8. evaluation dashboard exposes honest status and report link", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    const summariesResponse = await page.request.get("/api/v1/evaluation/runs");
    expect(summariesResponse.ok()).toBeTruthy();
    const summaries = (await summariesResponse.json()) as Array<{
      evaluation_id: string;
      frozen: boolean;
    }>;
    const frozen = summaries.find((item) => item.frozen);
    await page.goto("/evaluation");
    await expect(page.getByRole("heading", { name: "Измерения без подмены" })).toBeVisible();
    await expect(page.getByText("Измерено", { exact: true })).toBeVisible();
    if (frozen) {
      await expect(page.getByText("Срез не заморожен")).toHaveCount(0);
      await expect(page.getByLabel("Срез")).toHaveValue(frozen.evaluation_id);
      const report = page.getByRole("link", { name: /PDF отчёт/ });
      await expect(report).toHaveAttribute(
        "href",
        `/api/v1/evaluation/artifacts/${frozen.evaluation_id}/report.pdf`,
      );
      const response = await page.request.get(
        `/api/v1/evaluation/runs/${frozen.evaluation_id}`,
      );
      expect(response.ok()).toBeTruthy();
      expect((await response.json()).status).toBe("FROZEN");
    } else {
      await expect(page.getByText("Срез не заморожен")).toBeVisible();
      const report = page.getByRole("link", { name: /Публичный JSON текущего среза/ });
      await expect(report).toHaveAttribute(
        "href",
        "/api/v1/evaluation/runs/current_development_slice",
      );
      const response = await page.request.get(
        "/api/v1/evaluation/runs/current_development_slice",
      );
      expect(response.ok()).toBeTruthy();
      expect((await response.json()).status).toBe("NOT_FROZEN");
    }
    assertNoBrowserErrors();
  });

  test("9. test-only approval and separate ZIP export complete in browser", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startFreshCase(page, "B04");
    await validateReadyAndGenerate(page);
    await page.getByRole("button", { name: "Утвердить комплект в тестовом режиме" }).click();
    const dialog = page.getByRole("dialog", { name: /Утвердить комплект v1/ });
    await expect(dialog.locator(".confirmation-value")).toHaveText(/^[a-f0-9]{64}$/);
    await dialog.getByRole("button", { name: "Подтвердить точный хэш" }).click();
    await expect(page.getByText(/тестовое решение/)).toBeVisible();
    await page.getByRole("button", { name: "Экспорт" }).click();
    const link = page.getByRole("link", { name: "Скачать ZIP" });
    await expect(link).toBeVisible();
    const downloadPromise = page.waitForEvent("download");
    await link.click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/^export_.+\.zip$/);
    expect(await download.path()).not.toBeNull();
    assertNoBrowserErrors();
  });
});
