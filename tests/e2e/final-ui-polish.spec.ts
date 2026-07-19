import { expect, test, type Page } from "@playwright/test";

import { formatDate } from "../../apps/web/src/format";
import { captureBrowserErrors, completeB01V1, startFreshCase } from "./helpers";

type OperationName = "initial" | "revision" | "rule_proposal";
type OperationState = {
  run_id: string;
  operation: OperationName;
  status: string;
  mode: "live_ouroboros" | "deterministic_template";
  active: boolean;
  title: string;
  stage: string;
  stage_label: string;
  attempt_number: number;
  elapsed_from: string;
  result_hint: string;
  reason_code: string | null;
};

type WorkspacePayload = {
  operation_state: OperationState | null;
  runs: unknown[];
  campaign: { state: string };
  package: { package_id: string } | null;
  feedback: Array<{ feedback_id: string; package_id: string }>;
  context: { operation: OperationName; product: { product_id: string } } | null;
};

const TITLES: Record<OperationName, string> = {
  initial: "Ouroboros создаёт комплект",
  revision: "Ouroboros создаёт точечную версию",
  rule_proposal: "Ouroboros формирует проект правила",
};

function operationState(
  operation: OperationName,
  overrides: Partial<OperationState> = {},
): OperationState {
  return {
    run_id: `run_ui_${operation}`,
    operation,
    status: "RUNNING",
    mode: "live_ouroboros",
    active: true,
    title: TITLES[operation],
    stage: "running",
    stage_label: "Ouroboros выполняет задачу",
    attempt_number: 1,
    elapsed_from: new Date(Date.now() - 4_200).toISOString(),
    result_hint: "Результат появится здесь после сохранения.",
    reason_code: null,
    ...overrides,
  };
}

function campaignIdFrom(page: Page): string {
  const campaignId = new URL(page.url()).pathname.split("/").at(-1);
  if (!campaignId) throw new Error("campaign id is missing from the workspace URL");
  return campaignId;
}

function mutationHeaders(ordinal: number): Record<string, string> {
  return {
    "Idempotency-Key": `ui-polish-${ordinal}-${Date.now()}`,
    "X-CF-Actor": "ui_polish_test_editor",
    "X-CF-Actor-Role": "human",
  };
}

async function workspaceJson(page: Page, campaignId: string): Promise<WorkspacePayload> {
  const response = await page.request.get(`/api/v1/campaigns/${campaignId}/workspace`);
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as WorkspacePayload;
}

async function installOperationOverlay(
  page: Page,
  campaignId: string,
  state: { current: OperationState | null },
) {
  await page.route(`**/api/v1/campaigns/${campaignId}/workspace`, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as WorkspacePayload;
    payload.operation_state = state.current;
    if (state.current) {
      // The operation read model is enough to restore the UI. Avoid opening a fake SSE
      // stream in this providerless presentation test.
      payload.runs = [];
      if (payload.context) payload.context.operation = state.current.operation;
      if (
        state.current.active ||
        state.current.status === "FAILED" ||
        state.current.status === "CANCELLED"
      ) {
        payload.campaign.state = state.current.status;
      }
    }
    await route.fulfill({ response, json: payload });
  });
}

async function expectCancelInsideViewport(page: Page) {
  const cancel = page.getByRole("button", { name: /Отмена операции/ });
  await expect(cancel).toBeVisible();
  const box = await cancel.boundingBox();
  expect(box).not.toBeNull();
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport!.width);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport!.height);
}

test.describe.serial("final operation-aware UI polish", () => {
  test("timestamps use one explicit user-facing timezone", () => {
    expect(formatDate("2026-07-18T12:34:00Z")).toBe("18 июл., 15:34 МСК");
  });

  test("initial run survives queue, retry and reload before persisted success", async ({
    page,
  }, testInfo) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await startFreshCase(page, "B04");
    await page.getByRole("button", { name: "Проверить бриф" }).click();
    await expect(page.getByRole("button", { name: "Создать шаблонный комплект" })).toBeVisible();
    const campaignId = campaignIdFrom(page);
    const state = {
      current: operationState("initial", {
        status: "QUEUED",
        stage: "accepted",
        stage_label: "Запуск принят, задача ожидает выполнения",
      }),
    };
    await installOperationOverlay(page, campaignId, state);
    await page.reload();

    const progress = page.locator(".operation-status[role='status']");
    await expect(progress.getByRole("heading", { name: TITLES.initial })).toBeVisible();
    await expect(progress).toContainText("Запуск принят, задача ожидает выполнения");
    await expect(progress).toContainText("Результат появится здесь после сохранения.");
    await expect(progress).toContainText(/Прошло \d+ с/);
    await expect(progress).toContainText("Попытка 1 из 2");
    await expect(page.getByText("Артефакта ещё нет", { exact: true })).toHaveCount(0);
    await expectCancelInsideViewport(page);
    await page.screenshot({
      path: testInfo.outputPath("initial-running-desktop.png"),
      fullPage: true,
    });

    await page.setViewportSize({ width: 390, height: 844 });
    await expectCancelInsideViewport(page);
    expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(390);
    await page.screenshot({
      path: testInfo.outputPath("initial-running-narrow.png"),
      fullPage: true,
    });

    await page.emulateMedia({ reducedMotion: "reduce" });
    const animationDuration = await page
      .locator(".operation-spinner")
      .evaluate((node) => Number.parseFloat(getComputedStyle(node).animationDuration));
    expect(animationDuration).toBeLessThanOrEqual(0.01);
    await expect(progress).toContainText("Запуск принят, задача ожидает выполнения");

    state.current = operationState("initial", {
      run_id: state.current.run_id,
      attempt_number: 2,
      stage: "retry_scheduled",
      stage_label: "Временный сбой, готовим попытку 2 из 2",
    });
    await page.reload();
    await expect(page.locator(".operation-status")).toContainText("Попытка 2 из 2");
    await expect(page.locator(".operation-status")).toContainText(
      "Временный сбой, готовим попытку 2 из 2",
    );
    await expect(
      page.getByRole("status").filter({ hasText: "Временный сбой — повторяем запрос (2 из 2)" }),
    ).toBeVisible();
    await expect(page.locator(".operation-status code")).toHaveText(state.current.run_id);

    const generated = await page.request.post(`/api/v1/campaigns/${campaignId}/runs`, {
      data: { mode: "deterministic_template" },
      headers: mutationHeaders(1),
    });
    expect(generated.ok()).toBeTruthy();
    state.current = operationState("initial", {
      run_id: state.current.run_id,
      status: "COMPLETED",
      active: false,
      stage: "completed",
      stage_label: "Результат сохранён",
      attempt_number: 2,
    });
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.reload();
    await expect(page.getByText("Комплект v1", { exact: true })).toBeVisible();
    await expect(page.locator(".operation-status")).toHaveCount(0);
    await expect(
      page.getByText("Временный сбой устранён — запрос выполнен со второй попытки", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.locator(".email-preview-frame > article")).not.toContainText(
      "Все данные синтетические",
    );
    await expect(page.getByText(/Все данные синтетические · внешняя отправка отключена/)).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("final-campaign.png"), fullPage: true });
    assertNoBrowserErrors();
  });

  test("cancel and failure are terminal, factual and recoverable", async ({ page }, testInfo) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await startFreshCase(page, "B04");
    await page.getByRole("button", { name: "Проверить бриф" }).click();
    const campaignId = campaignIdFrom(page);
    const state = { current: operationState("initial") };
    await installOperationOverlay(page, campaignId, state);
    let cancelRequests = 0;
    await page.route(`**/api/v1/runs/${state.current.run_id}/cancel`, async (route) => {
      cancelRequests += 1;
      state.current = operationState("initial", {
        status: "CANCELLED",
        active: false,
        stage: "cancelled",
        stage_label: "Операция отменена",
        reason_code: "USER_CANCELLED",
      });
      await route.fulfill({ status: 200, json: { run_id: state.current.run_id, status: "CANCELLED" } });
    });
    await page.reload();
    await page.getByRole("button", { name: /Отмена операции/ }).click();
    await expect(page.getByRole("alert")).toContainText("Операция отменена");
    await expect(page.getByRole("alert")).toContainText(
      "Сохранённый результат не был объявлен готовым",
    );
    await expect(page.getByRole("alert")).toContainText("USER_CANCELLED");
    await expect(page.getByRole("button", { name: /Отмена операции/ })).toHaveCount(0);
    expect(cancelRequests).toBe(1);

    state.current = operationState("initial", {
      status: "FAILED",
      active: false,
      stage: "failed",
      stage_label: "Операция завершилась с ошибкой",
      attempt_number: 2,
      reason_code: "CONTROLLED_RETRY_EXHAUSTED",
    });
    await page.reload();
    const failure = page.getByRole("alert");
    await expect(failure).toContainText("Операция завершилась с ошибкой");
    await expect(failure).toContainText("Повторный запрос не выполнен");
    await expect(failure).toContainText("Автоматических попыток больше не будет");
    await expect(failure).toContainText("Безопасно запустите операцию ещё раз");
    await expect(failure).toContainText("CONTROLLED_RETRY_EXHAUSTED");
    await expect(page.getByRole("button", { name: "Создать шаблонный комплект" })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("initial-failure.png"), fullPage: true });
    assertNoBrowserErrors();
  });

  test("revision and rule proposal each stay one logical operation until saved", async ({
    page,
  }, testInfo) => {
    const assertNoBrowserErrors = captureBrowserErrors(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await completeB01V1(page);
    const campaignId = campaignIdFrom(page);
    const workspaceUrl = page.url();
    await page.getByRole("tab", { name: /^Изменения/ }).click();
    await page
      .getByLabel("Комментарий редактора")
      .fill("Добавьте в первый блок письма разрешённое понятие «подготовка выплат в онлайн-банке». Остальные области не меняйте.");
    await page.getByRole("button", { name: "Сохранить замечание" }).click();
    await expect(page.getByText("Замечание сохранено", { exact: true })).toBeVisible();
    const beforeRevision = await workspaceJson(page, campaignId);
    const feedback = beforeRevision.feedback.at(-1);
    expect(feedback).toBeTruthy();

    const state = { current: operationState("revision") };
    await installOperationOverlay(page, campaignId, state);
    await page.reload();
    const revisionProgress = page.locator(".operation-status");
    await expect(revisionProgress.getByRole("heading", { name: TITLES.revision })).toBeVisible();
    await expect(page.getByText("Комплект v1", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "v1 → v2" })).toHaveCount(0);
    await expectCancelInsideViewport(page);
    await page.screenshot({
      path: testInfo.outputPath("revision-running.png"),
      fullPage: true,
    });

    const revised = await page.request.post(`/api/v1/packages/${feedback!.package_id}/revision`, {
      data: { feedback_id: feedback!.feedback_id, mode: "deterministic_template" },
      headers: mutationHeaders(2),
    });
    expect(revised.ok()).toBeTruthy();
    state.current = null;
    await page.reload();
    await page.getByRole("tab", { name: /^Изменения/ }).click();
    await expect(page.getByText("Комплект v2", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "v1 → v2" })).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("revision-completed.png"),
      fullPage: true,
    });

    await page.getByRole("tab", { name: /^Правило/ }).click();
    await expect(page.getByRole("button", { name: "Предложить правило" })).toHaveCount(0);
    await page.getByRole("tab", { name: /^Изменения/ }).click();
    const proposalButton = page.getByRole("button", { name: "Предложить правило" });
    await expect(proposalButton).toHaveCount(1);
    let proposalRequests = 0;
    await page.route("**/api/v1/feedback/*/rule-proposals", async (route) => {
      proposalRequests += 1;
      state.current = operationState("rule_proposal");
      await route.fulfill({
        status: 201,
        json: {
          run_id: state.current.run_id,
          campaign_id: campaignId,
          operation: "rule_proposal",
          status: "RUNNING",
        },
      });
    });
    await proposalButton.click({ clickCount: 2 });
    const ruleProgress = page.locator(".operation-status");
    await expect(ruleProgress.getByRole("heading", { name: TITLES.rule_proposal })).toBeVisible();
    await expect(page.getByRole("tab", { name: /^Правило/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(page.getByRole("button", { name: "Предложить правило" })).toHaveCount(0);
    expect(proposalRequests).toBe(1);
    await page.reload();
    await expect(ruleProgress.getByRole("heading", { name: TITLES.rule_proposal })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("rule-running.png"), fullPage: true });

    const current = await workspaceJson(page, campaignId);
    const productId = current.context?.product.product_id;
    expect(productId).toBeTruthy();
    const proposed = await page.request.post(
      `/api/v1/feedback/${feedback!.feedback_id}/rule-proposals`,
      {
        data: {
          selected_scope: { product_ids: [productId], channel: "email", segment_ids: [] },
          mode: "deterministic_template",
        },
        headers: mutationHeaders(3),
      },
    );
    expect(proposed.ok()).toBeTruthy();
    state.current = operationState("rule_proposal", {
      status: "COMPLETED",
      active: false,
      stage: "completed",
      stage_label: "Результат сохранён",
    });
    await page.reload();
    await expect(page.getByRole("heading", { name: "Обязательное понятие" })).toBeVisible();
    await expect(page.locator(".rule-tests article")).toHaveCount(6);
    const rulePanel = page.locator(".rule-panel");
    await expect(rulePanel.getByText("Пульс Выплат", { exact: true })).toBeVisible();
    await expect(
      rulePanel.getByText("подготовка выплат в онлайн-банке", { exact: true }),
    ).toBeVisible();
    await expect(rulePanel.getByText("synthetic_payroll", { exact: true })).toBeVisible();
    await expect(rulePanel.getByText("payouts_via_online_bank", { exact: true })).toBeVisible();
    for (const leakedPhrase of ["matching synthetic case", "synthetic scope", "negative fixture"]) {
      await expect(page.getByText(new RegExp(leakedPhrase, "i"))).toHaveCount(0);
    }
    await expect(page.getByText(/Контекст изменился/).first()).toBeVisible();
    await expect(page.getByText("STALE_CONTEXT", { exact: true }).first()).toBeVisible();
    const approve = page.getByRole("button", {
      name: "Утвердить правило в тестовом режиме",
    });
    await expect(approve).toBeEnabled();
    await expect(page.getByText("Правило активно в тестовом режиме")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("rule-completed.png"), fullPage: true });

    await approve.click();
    const dialog = page.getByRole("dialog", { name: "Активировать ограниченное правило?" });
    await dialog
      .getByRole("button", { name: "Утвердить правило в тестовом режиме" })
      .click();
    await expect(page.getByText("Правило активно в тестовом режиме")).toBeVisible();
    await expect(page.getByRole("button", { name: "Создать похожий кейс B03" })).toHaveCount(0);
    const newCampaign = page.getByRole("button", { name: "Создать новую кампанию" });
    await expect(newCampaign).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("campaign-rule-active.png"), fullPage: true });
    await newCampaign.click();
    await expect(page.getByRole("heading", { name: "Соберите бриф кампании" })).toBeVisible();

    await page.goto(workspaceUrl);
    await page.getByRole("tab", { name: /^Правило/ }).click();
    await page.getByRole("button", { name: "Откатить" }).click();
    await page
      .getByRole("dialog", { name: "Откатить правило для будущих контекстов?" })
      .getByRole("button", { name: "Выполнить откат" })
      .click();
    await expect(page.getByText("Правило откачено")).toBeVisible();
    assertNoBrowserErrors();
  });
});
