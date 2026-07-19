import { expect, test, type Page } from "@playwright/test";

import { captureBrowserErrors } from "./helpers";

async function completeBrief(
  page: Page,
  values: { name: string; objective: string },
) {
  await page.getByLabel("Название кампании").fill(values.name);
  await page.getByLabel("Цель коммуникации").fill(values.objective);
  await page.getByLabel("Аудитория и событие").selectOption("segment_growth");
  await expect(page.getByLabel("Тон")).not.toHaveValue("");
  await page.getByRole("button", { name: /Продолжить/ }).click();
  await expect(page.getByRole("heading", { name: "Проверьте бриф" })).toBeVisible();
}

test.describe.serial("product-facing campaign authoring", () => {
  test("dashboard defaults to campaigns and isolates the B01–B15 basket", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Кампании и коммуникации" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Новая кампания" })).toBeVisible();
    await expect(page.getByText("B01", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Начать с примера" })).toBeVisible();

    await page.getByRole("tab", { name: /Тестовые сценарии/ }).click();
    await expect(page.getByRole("heading", { name: "Тестовые сценарии B01–B15" })).toBeVisible();
    await expect(page.getByText("B01", { exact: true })).toBeVisible();
    await expect(page.getByText("B15", { exact: true })).toBeVisible();
    assertNoBrowserErrors();
  });

  test("catalog product creates a normal ready campaign", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Новая кампания" }).click();

    await expect(page.getByRole("heading", { name: "Соберите бриф кампании" })).toBeVisible();
    const productOptions = await page.getByLabel("Продукт", { exact: true }).locator("option").allTextContents();
    expect(productOptions.slice(1).every((label) => !label.includes("версия"))).toBeTruthy();
    await page.getByLabel("Продукт", { exact: true }).selectOption("synthetic_term_plan");
    await page.getByRole("button", { name: /Продолжить/ }).click();
    await completeBrief(page, {
      name: "Планирование подключения",
      objective: "Помочь синтетической команде спланировать подключение продукта.",
    });
    await page.getByRole("button", { name: "Создать кампанию" }).click();

    await expect(page.getByText("Рабочее пространство кампании", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Планирование подключения" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Создать шаблонный комплект" })).toBeVisible();
    assertNoBrowserErrors();
  });

  test("custom product requires declarations and survives through grounded generation", async ({
    page,
  }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.goto("/campaigns/new");
    await page.getByRole("radio", { name: /Новый продукт/ }).check();

    await page.getByLabel("Точное название продукта").fill("Ритм Команды UI");
    await page.getByLabel("Подпись действия").fill("Собрать рабочий план");
    await page
      .getByLabel("Синтетическая HTTPS-ссылка (.test или .invalid)")
      .fill("https://team-rhythm-ui.example.test/start");
    await page.getByLabel("Название факта").fill("Стоимость в месяц");
    await page.getByLabel("Тип").selectOption("money");
    await page
      .getByLabel("Точная каноническая формулировка")
      .fill("Стоимость составляет 490 ₽ в месяц.");
    await page.getByLabel("Точное значение").fill("490");
    await page.getByLabel("Единица измерения").fill("RUB/month");

    const continueButton = page.getByRole("button", { name: /Продолжить/ });
    await expect(continueButton).toBeDisabled();
    await page
      .getByRole("checkbox", { name: /продукт и факты полностью синтетические/ })
      .check();
    await expect(continueButton).toBeDisabled();
    await page.getByRole("checkbox", { name: /в данных нет имён/ }).check();
    await expect(continueButton).toBeEnabled();
    await continueButton.click();

    await completeBrief(page, {
      name: "Возврат к командному ритму",
      objective: "Помочь синтетической команде собрать задачи после паузы.",
    });
    await page.getByRole("button", { name: "Создать кампанию" }).click();

    await expect(page.getByText("Рабочее пространство кампании", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Создать шаблонный комплект" }).click();
    await expect(page.getByText("Комплект v1", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Ритм Команды UI", exact: true })).toBeVisible();

    const campaignId = new URL(page.url()).pathname.split("/").at(-1);
    const response = await page.request.get(`/api/v1/campaigns/${campaignId}/workspace`);
    expect(response.ok()).toBeTruthy();
    const workspace = (await response.json()) as {
      context: { product: { exact_name: string; version: number }; source_manifest: unknown[] };
    };
    expect(workspace.context.product).toMatchObject({ exact_name: "Ритм Команды UI", version: 1 });
    expect(workspace.context.source_manifest.length).toBeGreaterThan(0);
    assertNoBrowserErrors();
  });

  test("editorial example prefills only an editable brief, never the saved draft", async ({
    page,
  }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    const catalogResponse = await page.request.get("/api/v1/authoring/catalog");
    expect(catalogResponse.ok()).toBeTruthy();
    expect(JSON.stringify(await catalogResponse.json())).not.toContain("saved_draft");

    await page.goto("/campaigns/new?reference=editorial_dq03");
    await expect(page.getByText(/Начато с редакционного примера/)).toBeVisible();
    await expect(page.getByText(/готовый текст примера не переносится/)).toBeVisible();
    await expect(page.getByLabel("Точное название продукта")).toHaveValue("Карта Высота");
    await expect(
      page.getByRole("checkbox", { name: /продукт и факты полностью синтетические/ }),
    ).not.toBeChecked();
    await expect(page.getByRole("checkbox", { name: /в данных нет имён/ })).not.toBeChecked();
    await expect(page.getByRole("button", { name: /Продолжить/ })).toBeDisabled();
    assertNoBrowserErrors();
  });

  test("wizard stays usable without horizontal overflow at 390×844", async ({ page }, testInfo) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/campaigns/new");
    await expect(page.getByRole("heading", { name: "Соберите бриф кампании" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Продолжить/ })).toBeVisible();
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
    const viewportWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth);
    await page.screenshot({ path: testInfo.outputPath("narrow-authoring-wizard.png"), fullPage: true });
    assertNoBrowserErrors();
  });
});
