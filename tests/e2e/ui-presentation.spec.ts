import { expect, test } from "@playwright/test";

import { captureBrowserErrors, startFreshCase, validateReadyAndGenerate } from "./helpers";

const LEGACY_ENGLISH_LABELS = [
  "CONTROLLED CONTENT OPS",
  "CASES / CONTROL BOARD",
  "CAMPAIGN WORKSPACE",
  "EVALUATION / EVIDENCE",
  "DIAGNOSTICS / PUBLIC SAFE",
  "MESSAGE METRICS",
  "FACT CARD",
  "TARGETED FEEDBACK",
  "SYNTHETIC",
  "NO SEND",
  "Communication Factory",
  "Live Ouroboros",
  "Chaos-кейсы",
  "Deterministic QA",
  "Синтетический preview",
  "Утвердить test-only",
];

test.describe.serial("presentation, accessibility and responsive contract", () => {
  test("russian navigation, single main, working skip link and no legacy labels", async ({
    page,
  }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Кампании и коммуникации" })).toBeVisible();

    const nav = page.getByRole("navigation", { name: "Основная навигация" });
    for (const label of ["Кампании", "Результаты", "Диагностика"]) {
      await expect(nav.getByRole("link", { name: label })).toBeVisible();
    }
    const workspaceNav = nav.locator(".nav-item.is-disabled");
    await expect(workspaceNav).toContainText("Кампания");
    await expect(workspaceNav).toContainText("сначала откройте кампанию");
    await expect(workspaceNav).toHaveAttribute("aria-disabled", "true");
    await expect(page.locator("main")).toHaveCount(1);
    await expect(page.locator("main#main-content")).toHaveCount(1);
    await expect(
      page.getByRole("heading", { name: "Начать с примера" }),
    ).toBeVisible();

    await page.keyboard.press("Tab");
    const skipLink = page.locator(".skip-link");
    await expect(skipLink).toBeFocused();
    await page.keyboard.press("Enter");
    await expect
      .poll(() => page.evaluate(() => document.activeElement?.id ?? window.location.hash))
      .toContain("main-content");

    for (const route of ["/", "/results", "/evaluation", "/diagnostics"]) {
      await page.goto(route);
      await expect(page.locator(".page").first()).toBeVisible();
      for (const label of LEGACY_ENGLISH_LABELS) {
        await expect(page.getByText(label, { exact: true })).toHaveCount(0);
      }
    }
    assertNoBrowserErrors();
  });

  test("synthetic/no-send boundaries stay visible on desktop and narrow widths", async ({
    page,
  }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto("/");
    const topbarBadges = page.locator(".boundary-badges");
    await expect(topbarBadges.getByText("Синтетические данные")).toBeVisible();
    await expect(topbarBadges.getByText("Отправка отключена")).toBeVisible();

    await page.setViewportSize({ width: 390, height: 844 });
    const strip = page.locator(".safety-strip");
    await expect(strip.getByText("Синтетические данные")).toBeVisible();
    await expect(strip.getByText("Отправка отключена")).toBeVisible();

    await page.setViewportSize({ width: 1024, height: 768 });
    await expect(strip.getByText("Синтетические данные")).toBeVisible();
    assertNoBrowserErrors();
  });

  test("no page-level horizontal overflow at 390×844 on all four screens", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 390, height: 844 });
    for (const route of ["/", "/results", "/evaluation", "/diagnostics"]) {
      await page.goto(route);
      await expect(page.locator(".page").first()).toBeVisible();
      const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
      const viewportWidth = await page.evaluate(() => window.innerWidth);
      expect(bodyWidth, `horizontal overflow on ${route}`).toBeLessThanOrEqual(viewportWidth);
    }
    await startFreshCase(page, "B04");
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
    const viewportWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyWidth, "horizontal overflow on workspace").toBeLessThanOrEqual(viewportWidth);
    assertNoBrowserErrors();
  });

  test("results page exposes exactly ten confirmed live cases and their outputs", async ({
    page,
  }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto("/results");
    await expect(page.getByRole("heading", { name: "10 успешных живых кейсов" })).toBeVisible();
    await expect(page.locator(".result-case")).toHaveCount(10);
    await expect(page.getByText("B05", { exact: true })).toHaveCount(0);
    await expect(page.getByText("B08", { exact: true })).toHaveCount(0);

    const b01 = page.locator(".result-case", { hasText: "B01" });
    await b01.locator("summary").click();
    await expect(b01.getByRole("heading", { name: "СМС" })).toBeVisible();
    await expect(b01.getByRole("heading", { name: "Письмо" })).toBeVisible();
    // The results page renders the frozen historical evidence package. Keep its
    // original product name instead of relabelling past provider output.
    await expect(b01).toContainText("Проект Команда+");
    await expect(b01.getByText("100/100", { exact: true })).toBeVisible();

    await page.setViewportSize({ width: 390, height: 844 });
    await expect(b01).toBeVisible();
    assertNoBrowserErrors();
  });
  test("1920×1080 first frame keeps current state, primary action and readable proof text", async ({
    page,
  }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto("/");
    const primary = page.getByRole("button", { name: "Новая кампания" });
    await expect(primary).toBeVisible();
    const box = await primary.boundingBox();
    expect(box, "primary action must be inside the first 1080p frame").not.toBeNull();
    expect(box!.y + box!.height).toBeLessThanOrEqual(1080);

    await page.getByRole("tab", { name: /Тестовые сценарии/ }).click();
    const tableFontSize = await page
      .locator(".data-table td")
      .first()
      .evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(tableFontSize).toBeGreaterThanOrEqual(13);
    const badgeFontSize = await page
      .locator(".badge")
      .first()
      .evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
    expect(badgeFontSize).toBeGreaterThanOrEqual(12);
    assertNoBrowserErrors();
  });

  test("workspace tabs expose tablist semantics with arrow-key navigation", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startFreshCase(page, "B04");
    await validateReadyAndGenerate(page);

    const tablist = page.getByRole("tablist", { name: "Артефакты кампании" });
    await expect(tablist).toBeVisible();
    const emailTab = page.getByRole("tab", { name: "Письмо" });
    await expect(emailTab).toHaveAttribute("aria-selected", "true");
    await expect(emailTab).toHaveAttribute("aria-controls", "artifact-panel");
    await expect(page.locator("#artifact-panel[role='tabpanel']")).toBeVisible();

    await emailTab.focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "СМС" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await page.keyboard.press("Home");
    await expect(emailTab).toHaveAttribute("aria-selected", "true");
    assertNoBrowserErrors();
  });

  test("confirm dialog closes on Escape and restores focus to its opener", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startFreshCase(page, "B04");
    await validateReadyAndGenerate(page);

    const approve = page.getByRole("button", { name: "Утвердить комплект в тестовом режиме" });
    await approve.click();
    const dialog = page.getByRole("dialog", { name: /Утвердить комплект v1/ });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Отмена" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(approve).toBeFocused();
    assertNoBrowserErrors();
  });

  test("disabled controls expose a visible reason linked through aria-describedby", async ({
    page,
  }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startFreshCase(page, "B11");
    await page.getByRole("button", { name: "Проверить бриф" }).click();
    const blockedButton = page.getByRole("button", { name: "Утверждение недоступно" });
    await expect(blockedButton).toBeDisabled();
    const describedBy = await blockedButton.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    await expect(page.locator(`#${describedBy}`)).toBeVisible();
    await expect(page.locator(`#${describedBy}`)).toContainText("управляемым отказом");
    assertNoBrowserErrors();
  });

  test("api failure shows an alert with recovery that actually refetches", async ({ page }) => {
    let failuresRemaining = 2;
    await page.route("**/api/v1/dashboard", async (route) => {
      if (failuresRemaining > 0) {
        failuresRemaining -= 1;
        await route.fulfill({ status: 503, json: { detail: "Временный сбой backend" } });
        return;
      }
      await route.fallback();
    });
    await page.goto("/");
    const alert = page.locator(".state-error[role='alert']");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("Не удалось получить данные");
    await alert.getByRole("button", { name: "Повторить" }).click();
    await expect(page.getByRole("heading", { name: "Кампании и коммуникации" })).toBeVisible();
  });

  test("structured validation errors stay readable instead of leaking object coercion", async ({
    page,
  }) => {
    await page.route("**/api/v1/dashboard", async (route) => {
      await route.fulfill({
        status: 422,
        json: {
          detail: [
            {
              type: "missing",
              loc: ["header", "X-CF-Actor"],
              msg: "Field required",
            },
          ],
        },
      });
    });
    await page.goto("/");
    const alert = page.locator(".state-error[role='alert']");
    await expect(alert).toContainText("Не заполнено обязательное поле: заголовок X-CF-Actor");
    await expect(alert).not.toContainText("[object Object]");
  });

  test("unknown status value falls back to a neutral badge with the raw value", async ({
    page,
  }) => {
    await page.route("**/api/v1/dashboard", async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as {
        business_cases?: Array<{ actual_status?: string | null }>;
      };
      if (body.business_cases?.length) {
        body.business_cases[0].actual_status = "SOME_FUTURE_STATE";
      }
      await route.fulfill({ response, json: body });
    });
    await page.goto("/");
    await page.getByRole("tab", { name: /Тестовые сценарии/ }).click();
    const unknownBadge = page.getByText("SOME_FUTURE_STATE", { exact: true }).first();
    await expect(unknownBadge).toBeVisible();
    await expect(unknownBadge).toHaveClass(/badge-neutral/);
  });

  test("mode badge keeps the raw execution mode identifier reachable", async ({ page }) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await startFreshCase(page, "B04");
    await validateReadyAndGenerate(page);
    const badge = page.locator(".mode-deterministic_template").first();
    await expect(badge).toBeVisible();
    await expect(badge).toHaveAttribute("title", /deterministic_template/);
    await expect(
      page.locator(".workspace-identifiers .mode-badge code", {
        hasText: "deterministic_template",
      }),
    ).toBeVisible();
    assertNoBrowserErrors();
  });
});
