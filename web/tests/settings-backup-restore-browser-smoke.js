const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.env.INKDROP_SETTINGS_BROWSER_URL;
if (!baseUrl) throw new Error("INKDROP_SETTINGS_BROWSER_URL is required");

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.INKDROP_CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
  });
  try {
    const page = await browser.newPage({ viewport: { width: 430, height: 850 }, acceptDownloads: true });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => typeof window.appendSettingsBackupRestore === "function");
    await page.evaluate(() => {
      document.body.replaceChildren();
      window.appendSettingsBackupRestore(document.body);
    });
    const panel = page.locator('[data-settings-backup-restore="true"]');
    await panel.waitFor({ state: "visible" });
    assert.equal(await panel.getAttribute("role"), null);
    const status = panel.locator('[role="status"]');
    assert.equal(await status.getAttribute("aria-live"), "polite");
    const downloadPromise = page.waitForEvent("download");
    await panel.getByRole("button", { name: "Download settings backup" }).click();
    const download = await downloadPromise;
    const downloadPath = await download.path();
    assert.ok(download.suggestedFilename().endsWith(".json"));
    await panel.locator('input[type="file"]').setInputFiles(downloadPath);
    await status.filter({ hasText: "Preview:" }).waitFor();
    const apply = panel.getByRole("button", { name: "Apply previewed restore" });
    assert.equal(await apply.isEnabled(), true);
    page.once("dialog", dialog => dialog.accept());
    await apply.click();
    await status.filter({ hasText: "Restore complete:" }).waitFor();
    assert.ok((await status.innerText()).includes("No restart is required"));
    assert.equal((await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)), true, "mobile Settings should not overflow");
    for (const viewport of [{width: 430, height: 850}, {width: 1280, height: 900}]) {
      await page.setViewportSize(viewport);
      await page.evaluate(() => {
        document.body.replaceChildren();
        const runtime = document.createElement("section");
        runtime.dataset.fixture = "general-runtime";
        document.body.appendChild(runtime);
        window.appendGeneralSettingsLens(runtime, {listener: {host_port: 8796, container_port: 8796}});
        const fallback = document.createElement("section");
        fallback.dataset.fixture = "general-fallback";
        document.body.appendChild(fallback);
        window.appendSettingsReadonlyPresetArea(fallback, {key: "other"});
      });
      assert.equal(await page.getByText("Analytics", {exact: true}).count(), 0, `${viewport.width}px General settings must not render an Analytics heading`);
      assert.equal(await page.getByText(/Send Anonymous Usage Data/i).count(), 0, `${viewport.width}px General settings must not render anonymous usage controls`);
      assert.equal(await page.locator('[data-setting-key="privacy.analytics"], [data-settings-readonly-row="general-analytics"]').count(), 0, `${viewport.width}px General settings must not retain analytics placeholder rows`);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true, `${viewport.width}px General settings should not overflow`);
    }
    console.log("Settings backup/restore browser smoke passed.");
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exit(1);
});
