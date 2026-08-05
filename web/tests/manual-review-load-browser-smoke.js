const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.env.INKDROP_MANUAL_REVIEW_BROWSER_URL;
const username = process.env.INKDROP_MANUAL_REVIEW_BROWSER_USERNAME;
const password = process.env.INKDROP_MANUAL_REVIEW_BROWSER_PASSWORD;
if (!baseUrl || !username || !password) throw new Error("authenticated Manual Review fixture environment is required");

const reviewPayload = series => ({
  ok: true,
  items: [{
    review_id: `fixture-${series}`,
    series,
    title: `${series} #1`,
    query: series,
    reason: "Needs Decision",
    review_group: "Needs Decision",
    source: "fixture",
    can_approve: false,
  }],
  slskd_probe: {},
  pack_state: null,
});

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.INKDROP_CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  });
  try {
    const context = await browser.newContext({viewport: {width: 1280, height: 900}});
    const page = await context.newPage();
    const pageErrors = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    await page.goto(baseUrl, {waitUntil: "domcontentloaded"});
    await page.getByRole("textbox", {name: "Username"}).fill(username);
    await page.getByRole("textbox", {name: "Password"}).fill(password);
    await page.getByRole("button", {name: "Sign in"}).click();
    await page.locator("main").waitFor({state: "visible"});
    await page.waitForTimeout(300);

    const manualRoute = "**/api/manual-review";
    const duplicateRoute = "**/api/inkdrop-state/series?series_filter=duplicate_titles*";
    await page.route(duplicateRoute, route => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ok: true, rows: []}),
    }));

    await page.route(manualRoute, route => route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ok: false, error: "fixture request failed"}),
    }));
    const failed = await page.evaluate(() => window.loadManualReview(null, {timeoutMs: 2000}));
    assert.match(failed.error, /fixture request failed/, "HTTP failure did not reach Manual Review error handling");
    assert.match(await page.locator("#manualReview").innerText(), /Could not load Manual Review/, "HTTP failure rendered a false empty state");
    assert.doesNotMatch(await page.locator("#manualReview").innerText(), /No review rows match/, "HTTP failure was presented as valid empty data");
    await page.unroute(manualRoute);

    let manualRequests = 0;
    await page.route(manualRoute, async route => {
      manualRequests += 1;
      const series = manualRequests === 1 ? "Older Result" : "Current Result";
      if (manualRequests === 1) await new Promise(resolve => setTimeout(resolve, 450));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(reviewPayload(series)),
      });
    });
    const older = page.evaluate(() => window.loadManualReview("Older Result", {timeoutMs: 3000}));
    await page.waitForTimeout(50);
    const current = await page.evaluate(() => window.loadManualReview("Current Result", {timeoutMs: 3000}));
    const stale = await older;
    assert.equal(current.visibleCount, 1, "current Manual Review response did not render");
    assert.equal(stale.stale, true, "older response was not marked stale");
    const finalText = await page.locator("#manualReview").innerText();
    assert.match(finalText, /Current Result/, "latest Manual Review response was not retained");
    assert.doesNotMatch(finalText, /Older Result/, "older response replaced current Manual Review content");
    await page.unroute(manualRoute);

    await page.route(manualRoute, async route => {
      await new Promise(resolve => setTimeout(resolve, 2500));
      try {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(reviewPayload("Too Late")),
        });
      } catch (_closedRequest) {
        // The page is expected to abort this request at its deadline.
      }
    });
    const startedAt = Date.now();
    const timedOut = await page.evaluate(() => window.loadManualReview(null, {timeoutMs: 1000}));
    assert.match(timedOut.error, /did not respond within 1s/, "hung Manual Review request did not report its deadline");
    assert.ok(Date.now() - startedAt < 2200, "hung Manual Review request exceeded its UI deadline");
    assert.match(await page.locator("#manualReview").innerText(), /Could not load Manual Review/, "timeout left stale content visible");
    await page.unroute(manualRoute);

    let duplicateRequests = 0;
    await page.unroute(duplicateRoute);
    await page.route(duplicateRoute, async route => {
      duplicateRequests += 1;
      await new Promise(resolve => setTimeout(resolve, 150));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ok: true, rows: []}),
      });
    });
    await page.evaluate(() => {
      duplicateSeriesWorkloadCache = new Map();
      duplicateSeriesWorkloadCacheAt = 0;
      duplicateSeriesWorkloadFetchInFlight = null;
    });
    await page.evaluate(() => Promise.all([
      getDuplicateSeriesWorkloadMap({timeoutMs: 2000}),
      getDuplicateSeriesWorkloadMap({timeoutMs: 2000}),
    ]));
    await page.evaluate(() => getDuplicateSeriesWorkloadMap({timeoutMs: 2000}));
    assert.equal(duplicateRequests, 1, "empty duplicate-series result was refetched or concurrent requests were not coalesced");

    assert.deepEqual(pageErrors, [], `browser page errors: ${pageErrors.join(" | ")}`);
    console.log("PASS manual-review-load-browser-smoke");
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
