import { expect, type Page } from "@playwright/test";

export function captureBrowserErrors(page: Page) {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  return () => {
    expect(consoleErrors, "browser console errors").toEqual([]);
    expect(pageErrors, "uncaught page errors").toEqual([]);
  };
}

export async function startFreshCase(page: Page, caseId: string) {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Кампании и коммуникации" })).toBeVisible();
  await page.getByRole("tab", { name: /Тестовые сценарии/ }).click();
  const row = page.getByRole("row").filter({ has: page.getByText(caseId, { exact: true }) });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: /Новый прогон/ }).click();
  await expect(page.getByText("Рабочее пространство кампании", { exact: true })).toBeVisible();
}

export async function startB01(page: Page) {
  await startFreshCase(page, "B01");
}

export async function validateReadyAndGenerate(page: Page) {
  await page.getByRole("button", { name: "Проверить бриф" }).click();
  const generate = page.getByRole("button", { name: "Создать шаблонный комплект" });
  await expect(generate).toBeVisible();
  await generate.click();
  await expect(page.getByText("Комплект v1", { exact: true })).toBeVisible();
}

export async function completeB01V1(page: Page) {
  await startB01(page);
  await page.getByRole("button", { name: "Проверить бриф" }).click();
  await expect(page.getByText("Нужны ответы · вызовов модели: 0")).toBeVisible();
  await page.getByLabel("Подпись ссылки").fill("Собрать первый реестр");
  await page.getByLabel("Разрешённый URL").fill("https://pulse-pay.example.test/start");
  await page.getByRole("button", { name: "Сохранить ответы" }).click();
  const generate = page.getByRole("button", { name: "Создать шаблонный комплект" });
  await expect(generate).toBeVisible();
  await generate.click();
  await expect(page.getByText("Комплект v1", { exact: true })).toBeVisible();
  await expect(page.getByText("Резервный шаблон", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("подготовка выплат в онлайн-банке")).toHaveCount(0);
}

export async function createB01Revision(page: Page) {
  await completeB01V1(page);
  await page.getByRole("tab", { name: /^Изменения/ }).click();
  await page
    .getByLabel("Комментарий редактора")
    .fill("Добавьте в первый блок письма разрешённое понятие «подготовка выплат в онлайн-банке». Остальные факты, СМС, ссылку и название продукта не меняйте.");
  await page.getByRole("button", { name: "Сохранить замечание" }).click();
  await expect(page.getByText("Замечание сохранено", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Создать точечную версию 2" }).click();
  await expect(page.getByText("Комплект v2", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "v1 → v2" })).toBeVisible();
  await expect(page.getByText("/email/plain_text", { exact: true })).toBeVisible();
  await expect(page.getByText("/email/sections/0/body", { exact: true })).toBeVisible();
}

export async function approveRuleAndOpenB03(page: Page) {
  const b01Url = page.url();
  await page.getByRole("tab", { name: /^Изменения/ }).click();
  await page.getByRole("button", { name: "Предложить правило" }).click();
  await expect(page.getByRole("heading", { name: "Обязательное понятие" })).toBeVisible();
  await expect(page.locator(".rule-tests article")).toHaveCount(6);
  await page.getByRole("button", { name: "Утвердить правило в тестовом режиме" }).click();
  const dialog = page.getByRole("dialog", { name: "Активировать ограниченное правило?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Утвердить правило в тестовом режиме" }).click();
  await expect(page.getByText("Правило активно в тестовом режиме")).toBeVisible();
  await expect(page.getByRole("button", { name: "Создать новую кампанию" })).toBeVisible();
  // B03 remains a regression fixture, not an ordinary product-facing shortcut.
  const response = await page.request.post("/api/v1/campaigns", {
    data: { case_id: "B03" },
    headers: {
      "Idempotency-Key": `e2e-b03-${Date.now()}`,
      "X-CF-Actor": "playwright_editor",
      "X-CF-Actor-Role": "human",
    },
  });
  expect(response.ok()).toBeTruthy();
  const campaign = (await response.json()) as { campaign_id: string };
  await page.goto(`/campaigns/${campaign.campaign_id}`);
  await expect(page.getByText("Рабочее пространство кампании", { exact: true })).toBeVisible();
  return { b01Url };
}

export async function completeB03WithActiveRule(page: Page) {
  await validateReadyAndGenerate(page);
  await expect(page.locator(".applied-rules code")).toHaveText(/^rulev_/);
  await page.getByRole("tab", { name: "Письмо" }).click();
  await expect(page.getByText("подготовка выплат в онлайн-банке").first()).toBeVisible();
  await page.getByRole("tab", { name: "СМС" }).click();
  await expect(page.locator(".sms-bubble")).not.toContainText("подготовка выплат в онлайн-банке");
}

export async function rollbackRule(page: Page, b01Url: string) {
  await page.goto(b01Url);
  await page.getByRole("tab", { name: /^Правило/ }).click();
  await page.getByRole("button", { name: "Откатить" }).click();
  const dialog = page.getByRole("dialog", { name: "Откатить правило для будущих контекстов?" });
  await dialog.getByRole("button", { name: "Выполнить откат" }).click();
  await expect(page.getByText("Правило откачено")).toBeVisible();
}
