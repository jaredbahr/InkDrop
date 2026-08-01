const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const baseUrl = process.env.INKDROP_FIXTURE_URL || "http://127.0.0.1:8877/web/tests/fixtures/download-clients-settings.html";

async function chooseClient(page, label) {
  await page.getByRole("button", {name: "Add Download Client"}).click();
  await page.getByRole("radio", {name: label}).check();
  await page.getByRole("button", {name: "Continue"}).click();
}

async function fillBase(page, {name, url, username, secretLabel, secret}) {
  await page.getByLabel("Instance name").fill(name);
  await page.getByLabel("URL").fill(url);
  if (username !== undefined) await page.getByLabel("Username").fill(username);
  if (secretLabel) await page.getByLabel(secretLabel, {exact: true}).fill(secret);
}

async function addClient(page, label, values) {
  await chooseClient(page, label);
  await fillBase(page, values);
  if (values.enabled) await page.getByLabel("Enabled", {exact: true}).check();
  await page.getByRole("button", {name: "Add Client", exact: true}).click();
  await page.locator("dialog:not([open])").waitFor({state: "attached"});
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.INKDROP_CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  });
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(baseUrl, {waitUntil: "networkidle"});
    await page.locator("[data-download-client-manager]").waitFor();

    const add = page.getByRole("button", {name: "Add Download Client"});
    await add.focus(); await add.press("Enter");
    assert.deepEqual(await page.locator(".download-client-choice-group legend").allTextContents(), ["Torrent", "Usenet", "Soulseek"]);
    assert.equal(await page.getByRole("radio").count(), 8, "chooser should contain all eight accepted registry clients");
    await page.keyboard.press("Escape");
    assert.equal(await add.evaluate(node => document.activeElement === node), true, "Escape should close and return focus to Add");

    await addClient(page, "qBittorrent", {name: "qBit Main", url: "http://qbit-main:8080", username: "inkdrop", secretLabel: "Password", secret: "first-secret", enabled: true});
    await addClient(page, "qBittorrent", {name: "qBit Backup", url: "http://qbit-backup:8080", username: "inkdrop", secretLabel: "Password", secret: "second-secret", enabled: false});
    assert.equal(await page.locator('[data-client-type="qbittorrent"]').count(), 2, "multiple same-type cards should remain visible");
    assert.equal(await page.locator('[data-provider-id="qbittorrent"]').isHidden(), true, "migrated type should hide its duplicate legacy fallback");
    assert.equal(await page.locator('[data-provider-id="sabnzbd"]').isVisible(), true, "unmigrated legacy fallback should remain visible");

    await addClient(page, "NZBGet", {name: "NZBGet Comics", url: "http://nzbget:6789", username: "nzb", secretLabel: "API Key", secret: "nzb-key", enabled: false});
    await chooseClient(page, "uTorrent");
    assert.equal(await page.getByLabel("Username").count(), 1, "uTorrent should expose username");
    assert.equal(await page.getByLabel("Password", {exact: true}).count(), 1, "uTorrent should expose a masked password field");
    await page.getByRole("button", {name: "Cancel"}).click();
    await chooseClient(page, "rTorrent");
    assert.equal(await page.getByLabel("Label").count(), 1, "rTorrent should expose its client label");
    assert.equal(await page.getByLabel("API Key", {exact: true}).count(), 0, "rTorrent must not inherit NZBGet API key fields");
    await page.getByRole("button", {name: "Cancel"}).click();

    const mainCard = page.locator('[data-client-type="qbittorrent"]', {hasText: "qBit Main"});
    await mainCard.getByRole("button", {name: "Edit"}).click();
    const savedPassword = page.getByLabel("Password", {exact: true});
    assert.equal(await savedPassword.inputValue(), "", "saved secret must never echo into the editor");
    assert.match(await savedPassword.getAttribute("placeholder"), /Saved; leave blank to keep/i);
    await page.getByLabel("Instance name").fill("qBit Primary");
    await page.getByRole("button", {name: "Save Changes"}).click();
    assert.equal(await page.evaluate(() => window.fixtureState.secrets.dc_fixture_1.password), "first-secret", "blank edit should retain the saved secret");

    const primaryCard = page.locator('[data-client-type="qbittorrent"]', {hasText: "qBit Primary"});
    await primaryCard.getByRole("button", {name: "Edit"}).click();
    await page.getByLabel("Clear saved Password").check();
    await page.getByRole("button", {name: "Save Changes"}).click();
    assert.equal(await page.evaluate(() => window.fixtureState.secrets.dc_fixture_1?.password || ""), "", "explicit clear should remove the secret");
    assert.match(await primaryCard.innerText(), /Disabled/i, "clearing a secret should save the instance disabled");

    await chooseClient(page, "SLSKD");
    await page.getByLabel("Instance name").fill("");
    await page.getByRole("button", {name: "Add Client", exact: true}).click();
    assert.equal(await page.getByLabel("Instance name").getAttribute("aria-invalid"), "true", "first invalid control should be identified");
    assert.equal(await page.getByLabel("Instance name").evaluate(node => document.activeElement === node), true, "first invalid control should receive focus");
    await page.getByText("SLSKD Advanced Search Settings").click();
    assert.equal(await page.getByLabel("Probe Budget Seconds").count(), 1, "SLSKD advanced fields should include bounded help-backed controls");
    await page.getByRole("button", {name: "Cancel"}).click();

    await primaryCard.getByRole("button", {name: "Edit"}).click();
    await page.getByLabel("Priority").fill("5");
    await page.evaluate(() => window.fixtureState.forceConflict.add("dc_fixture_1"));
    await page.getByRole("button", {name: "Save Changes"}).click();
    assert.match(await page.locator(".download-client-inline-error").innerText(), /revision conflict/i, "409 revision conflicts should remain visible in the editor");
    await page.getByRole("button", {name: "Cancel"}).click();

    const backupCard = page.locator('[data-client-type="qbittorrent"]', {hasText: "qBit Backup"});
    await backupCard.getByRole("button", {name: "Enable"}).click();
    assert.match(await backupCard.innerText(), /Enabled/i);
    await backupCard.getByRole("button", {name: "Test"}).click();
    await page.getByText(/qBit Backup connected/i).waitFor();
    assert.match(await backupCard.innerText(), /Connected/);
    await page.getByRole("button", {name: "Test All Clients"}).click();
    await page.getByText(/Test All complete:/).waitFor();
    const testAllCalls = await page.evaluate(() => window.fixtureState.calls.filter(call => call.path === "/api/download-clients/test-all").length);
    assert.equal(testAllCalls, 1, "Test All should create exactly one run");

    await backupCard.getByRole("button", {name: "Delete"}).click();
    assert.equal(await page.getByRole("button", {name: "Cancel"}).evaluate(node => document.activeElement === node), true, "delete confirmation should focus Cancel");
    await page.getByRole("button", {name: "Cancel"}).click();
    assert.equal(await backupCard.count(), 1, "delete cancellation should preserve the instance");
    await page.evaluate(() => window.fixtureState.deleteGuard.add("dc_fixture_2"));
    await backupCard.getByRole("button", {name: "Delete"}).click();
    await page.getByRole("button", {name: "Delete Client"}).click();
    assert.match(await page.locator(".download-client-inline-error").innerText(), /still referenced/i, "409 delete guards should explain mappings/active work");
    await page.evaluate(() => window.fixtureState.deleteGuard.delete("dc_fixture_2"));
    await page.getByRole("button", {name: "Delete Client"}).click();
    await backupCard.waitFor({state: "detached"});

    assert.deepEqual(errors, [], `desktop errors: ${errors.join("; ")}`);
    await page.close();

    for (const width of [430, 600, 760]) {
      const mobile = await browser.newPage({viewport: {width, height: 900}});
      await mobile.goto(baseUrl, {waitUntil: "networkidle"});
      await mobile.getByRole("button", {name: "Add Download Client"}).click();
      await mobile.getByRole("radio", {name: "qBittorrent"}).check();
      await mobile.getByRole("button", {name: "Continue"}).click();
      await mobile.getByRole("button", {name: "Add Path Mapping"}).click();
      const overflow = await mobile.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 1, `${width}px UI overflowed by ${overflow}px`);
      const mappingColumns = await mobile.locator(".download-client-mapping-row").evaluate(node => getComputedStyle(node).gridTemplateColumns.split(" ").length);
      assert.equal(mappingColumns, 1, `${width}px path mapping should stack`);
      const shortTargets = await mobile.locator("dialog button").evaluateAll(nodes => nodes.filter(node => node.getBoundingClientRect().height < 44).map(node => node.textContent));
      assert.deepEqual(shortTargets, [], `${width}px dialog controls must keep 44px targets`);
      await mobile.keyboard.press("Escape");
      assert.equal(await mobile.getByRole("button", {name: "Add Download Client"}).evaluate(node => document.activeElement === node), true, `${width}px Escape should restore focus`);
      await mobile.close();
    }
    console.log("Download clients settings browser smoke passed.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
