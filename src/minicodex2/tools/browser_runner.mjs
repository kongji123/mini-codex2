import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(path.join(process.cwd(), ".minicodex2", "browser_tooling", "node_modules", "playwright-core"));

async function main() {
  const [, , specPath, outputPath] = process.argv;
  if (!specPath || !outputPath) {
    throw new Error("usage: node browser_runner.mjs <spec.json> <result.json>");
  }
  const spec = JSON.parse(await fs.readFile(specPath, "utf8"));
  const consoleErrors = [];
  const consoleWarnings = [];
  const consoleEventDetails = [];
  const pageErrors = [];
  const failedRequests = [];
  const failedRequestKeys = new Set();
  const networkResponses = [];
  const networkResponseKeys = new Set();
  const actionResults = [];
  const result = {
    ok: true,
    requested_url: spec.url,
    final_url: "",
    title: "",
    html_excerpt: "",
    text_excerpt: "",
    console_errors: consoleErrors,
    console_warnings: consoleWarnings,
    console_event_details: consoleEventDetails,
    page_errors: pageErrors,
    failed_requests: failedRequests,
    network_responses: networkResponses,
    action_results: actionResults,
    storage_snapshot: { local_storage: {}, session_storage: {} },
    screenshot_path: spec.capture_screenshot ? spec.screenshot_path : "",
    error: "",
    stdout: "",
    stderr: "",
  };
  let browser;
  try {
    browser = await chromium.launch({
      headless: spec.headless !== false,
      executablePath: spec.browser_executable,
      args: ["--disable-dev-shm-usage"],
    });
    const context = await browser.newContext();
    const page = await context.newPage();
    const pushFailedRequest = (item) => {
      const key = JSON.stringify([
        item.source || "",
        item.url || "",
        item.method || "",
        item.resource_type || "",
        item.status || "",
        item.error || "",
      ]);
      if (failedRequestKeys.has(key)) {
        return;
      }
      failedRequestKeys.add(key);
      failedRequests.push(item);
    };
    const pushNetworkResponse = (item) => {
      const key = JSON.stringify([
        item.url || "",
        item.method || "",
        item.resource_type || "",
        item.status || "",
      ]);
      if (networkResponseKeys.has(key)) {
        return;
      }
      networkResponseKeys.add(key);
      networkResponses.push(item);
      if (networkResponses.length > 80) {
        networkResponses.shift();
      }
    };
    page.on("console", (msg) => {
      const text = msg.text();
      const location = msg.location ? msg.location() : {};
      const detail = {
        type: msg.type(),
        text,
        url: location?.url || "",
        line: location?.lineNumber ?? null,
        column: location?.columnNumber ?? null,
      };
      consoleEventDetails.push(detail);
      if (msg.type() === "error") {
        consoleErrors.push(text);
      } else if (msg.type() === "warning") {
        consoleWarnings.push(text);
      }
    });
    page.on("pageerror", (error) => {
      pageErrors.push(String(error));
    });
    page.on("requestfailed", (request) => {
      pushFailedRequest({
        source: "requestfailed",
        url: request.url(),
        method: request.method(),
        resource_type: request.resourceType(),
        error: request.failure()?.errorText || "request failed",
      });
    });
    page.on("response", (response) => {
      if (response.status() >= 400) {
        const request = response.request();
        pushFailedRequest({
          source: "response",
          url: response.url(),
          method: request.method(),
          resource_type: request.resourceType(),
          status: response.status(),
          status_text: response.statusText(),
        });
      }
    });
    page.on("requestfinished", async (request) => {
      const response = await request.response().catch(() => null);
      if (!response) {
        return;
      }
      const resourceType = request.resourceType();
      const method = request.method();
      const status = response.status();
      if (["document", "fetch", "xhr"].includes(resourceType)) {
        const item = {
          url: response.url(),
          method,
          resource_type: resourceType,
          status,
          status_text: response.statusText(),
        };
        if (status >= 300 || method !== "GET") {
          item.body_excerpt = (await response.text().catch(() => "")).slice(0, 1000);
        }
        pushNetworkResponse(item);
      }
      if (status < 400) {
        return;
      }
      pushFailedRequest({
        source: "requestfinished",
        url: response.url(),
        method: request.method(),
        resource_type: resourceType,
        status,
        status_text: response.statusText(),
      });
    });
    await page.goto(spec.url, {
      waitUntil: spec.wait_until || "domcontentloaded",
      timeout: spec.timeout_ms || 45000,
    });
    const locatorFor = (rawSelector) => {
      const value = String(rawSelector || "");
      if (value.startsWith("text=")) {
        return page.getByText(value.slice(5), { exact: false });
      }
      return page.locator(value);
    };
    for (const [index, rawAction] of (spec.actions || []).entries()) {
      const action = rawAction || {};
      const type = String(action.type || "").trim();
      const selector = String(action.selector || "");
      const timeout = Number(action.timeout_ms || spec.timeout_ms || 45000);
      if (!type) {
        continue;
      }
      const actionResult = { index, type, selector, ok: true };
      try {
        actionResult.before_url = page.url();
        if (type === "wait_for" || type === "wait_for_selector") {
          if (!selector) {
            await page.waitForTimeout(Number(action.value || action.timeout_ms || 1000));
          } else {
            await locatorFor(selector).waitFor({
              state: action.state || "visible",
              timeout,
            });
          }
        } else if (type === "hover") {
          await locatorFor(selector).hover({ timeout });
        } else if (type === "click") {
          await locatorFor(selector).click({ timeout });
        } else if (type === "fill") {
          await locatorFor(selector).fill(String(action.value || ""), { timeout });
        } else if (type === "press") {
          await locatorFor(selector).press(String(action.key || "Enter"), { timeout });
        } else if (type === "check") {
          await locatorFor(selector).check({ timeout });
        } else if (type === "uncheck") {
          await locatorFor(selector).uncheck({ timeout });
        } else if (type === "select") {
          await locatorFor(selector).selectOption(String(action.value || ""), { timeout });
        } else if (type === "upload_files") {
          const values = Array.isArray(action.values)
            ? action.values.map((item) => String(item))
            : [String(action.value || "")].filter(Boolean);
          await locatorFor(selector).setInputFiles(values, { timeout });
        } else if (type === "wait_for_url_contains") {
          const expected = String(action.value || "");
          await page.waitForURL((url) => url.toString().includes(expected), { timeout });
        } else if (type === "assert_url_contains") {
          const expected = String(action.value || "");
          if (!page.url().includes(expected)) {
            throw new Error(`expected current URL to contain "${expected}", got "${page.url()}"`);
          }
        } else if (type === "assert_selector") {
          if (!selector) {
            throw new Error("assert_selector requires selector");
          }
          await locatorFor(selector).waitFor({
            state: action.state || "visible",
            timeout,
          });
        } else if (type === "wait_for_text") {
          const expected = String(action.value || "");
          await page.getByText(expected, { exact: false }).first().waitFor({ timeout });
        } else if (type === "click_text") {
          const expected = String(action.value || "");
          await page.getByText(expected, { exact: false }).first().click({ timeout });
        } else if (type === "assert_text") {
          const expected = String(action.value || "");
          const actual = selector
            ? await locatorFor(selector).innerText({ timeout })
            : await page.locator("body").innerText({ timeout });
          if (!actual.includes(expected)) {
            throw new Error(`expected text "${expected}" was not found`);
          }
          actionResult.text = actual.slice(0, 500);
        } else if (type === "wait_for_load_state") {
          await page.waitForLoadState(String(action.value || "networkidle"), { timeout });
        } else if (type === "wait_for_timeout") {
          await page.waitForTimeout(Number(action.value || action.timeout_ms || 1000));
        } else if (type === "extract_text") {
          actionResult.text = await (selector ? locatorFor(selector) : page.locator("body")).innerText({ timeout });
        } else if (type === "screenshot") {
          const actionPath = String(action.path || spec.screenshot_path || "");
          if (!actionPath) {
            throw new Error("screenshot action requires path");
          }
          await page.screenshot({ path: actionPath, fullPage: true });
          actionResult.path = actionPath;
        } else if (type === "media_diagnostics") {
          const rawSelector = selector || "video,audio";
          actionResult.selector = rawSelector;
          actionResult.media = await page.evaluate((mediaSelector) => {
            const mediaNodes = Array.from(document.querySelectorAll(mediaSelector));
            const videoProbe = document.createElement("video");
            const audioProbe = document.createElement("audio");
            const mediaErrorName = (code) => ({
              1: "MEDIA_ERR_ABORTED",
              2: "MEDIA_ERR_NETWORK",
              3: "MEDIA_ERR_DECODE",
              4: "MEDIA_ERR_SRC_NOT_SUPPORTED",
            })[code] || "";
            return {
              support: {
                video_mp4_avc1: videoProbe.canPlayType('video/mp4; codecs="avc1.42E01E"'),
                video_mp4_hvc1: videoProbe.canPlayType('video/mp4; codecs="hvc1"'),
                video_webm_vp9: videoProbe.canPlayType('video/webm; codecs="vp9"'),
                audio_mp4_aac: audioProbe.canPlayType('audio/mp4; codecs="mp4a.40.2"'),
              },
              elements: mediaNodes.slice(0, 20).map((node, elementIndex) => ({
                index: elementIndex,
                tag: node.tagName.toLowerCase(),
                src: node.getAttribute("src") || "",
                current_src: node.currentSrc || "",
                ready_state: node.readyState,
                network_state: node.networkState,
                paused: node.paused,
                ended: node.ended,
                muted: node.muted,
                controls: node.controls,
                duration: Number.isFinite(node.duration) ? node.duration : null,
                current_time: Number.isFinite(node.currentTime) ? node.currentTime : null,
                error: node.error ? {
                  code: node.error.code,
                  name: mediaErrorName(node.error.code),
                  message: node.error.message || "",
                } : null,
                bounding_box: (() => {
                  const rect = node.getBoundingClientRect();
                  return {
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                  };
                })(),
              })),
            };
          }, rawSelector);
        } else {
          throw new Error(`unsupported browser action: ${type}`);
        }
      } catch (error) {
        actionResult.ok = false;
        actionResult.error = String(error);
        result.ok = false;
      }
      actionResult.after_url = page.url();
      if (!actionResult.ok) {
        actionResult.text_excerpt = (await page.locator("body").innerText().catch(() => "")).slice(0, 1200);
        actionResult.storage_snapshot = await page.evaluate(() => ({
          local_storage_keys: Object.keys(window.localStorage).slice(0, 40),
          session_storage_keys: Object.keys(window.sessionStorage).slice(0, 40),
        })).catch(() => ({ local_storage_keys: [], session_storage_keys: [] }));
      }
      actionResults.push(actionResult);
      if (!actionResult.ok) {
        break;
      }
    }
    if (spec.capture_screenshot && spec.screenshot_path) {
      await page.screenshot({ path: spec.screenshot_path, fullPage: true });
    }
    result.final_url = page.url();
    result.title = await page.title();
    result.html_excerpt = (await page.content()).slice(0, 4000);
    result.text_excerpt = (await page.locator("body").innerText().catch(() => "")).slice(0, 2000);
    result.storage_snapshot = await page.evaluate(() => {
      const take = (storage) => {
        const out = {};
        for (let i = 0; i < storage.length && i < 40; i += 1) {
          const key = storage.key(i);
          if (!key) continue;
          const value = storage.getItem(key) || "";
          out[key] = value.length > 300 ? `${value.slice(0, 300)}…` : value;
        }
        return out;
      };
      return {
        local_storage: take(window.localStorage),
        session_storage: take(window.sessionStorage),
      };
    }).catch(() => ({ local_storage: {}, session_storage: {} }));
  } catch (error) {
    result.ok = false;
    result.error = String(error);
  } finally {
    if (browser) {
      await browser.close().catch(() => {});
    }
    await fs.mkdir(path.dirname(outputPath), { recursive: true });
    await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
  }
}

main().catch(async (error) => {
  const [, , , outputPath] = process.argv;
  if (outputPath) {
    const fallback = {
      ok: false,
      error: String(error),
      requested_url: "",
      final_url: "",
      title: "",
      html_excerpt: "",
      text_excerpt: "",
      console_errors: [],
      console_warnings: [],
      console_event_details: [],
      page_errors: [],
      failed_requests: [],
      network_responses: [],
      action_results: [],
      storage_snapshot: { local_storage: {}, session_storage: {} },
      screenshot_path: "",
      stdout: "",
      stderr: "",
    };
    await fs.mkdir(path.dirname(outputPath), { recursive: true });
    await fs.writeFile(outputPath, JSON.stringify(fallback, null, 2), "utf8");
  }
  process.exitCode = 1;
});
