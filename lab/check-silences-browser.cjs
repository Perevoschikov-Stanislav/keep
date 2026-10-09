/* Browser journeys for the silence registry. Run only against the isolated localhost lab. */
const fs = require("fs");
const path = require("path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "..");
const run = fs.readFileSync(path.join(root, ".lab-work/silences-verification/CURRENT"), "utf8").trim();
const { chromium } = require(path.join(run, "browser/node_modules/playwright"));
const origin = "http://localhost:8012";
const results = [];
const pageErrors = [];

function check(label, condition) {
  results.push({ check: label, passed: !!condition });
  assert.ok(condition, label);
  console.log("PASS", label);
}

async function eventually(label, callback, timeout = 20000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const result = await callback();
    if (result) { check(label, true); return result; }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  check(label, false);
}

async function api(context, endpoint, method = "GET", body, status = 200) {
  const response = await context.request.fetch(origin + "/v2" + endpoint, {
    method, ...(body === undefined ? {} : { data: body }),
  });
  assert.equal(response.status(), status, method + " " + endpoint);
  const raw = await response.text();
  return raw ? JSON.parse(raw) : null;
}

async function session(browser, actor, viewport = { width: 1440, height: 1000 }) {
  const context = await browser.newContext({ viewport, timezoneId: "UTC" });
  await context.addCookies([{ name: "silence_lab_actor", value: actor, url: origin }]);
  const page = await context.newPage();
  page.on("pageerror", (error) => pageErrors.push({ actor, url: page.url(),
    message: error.message.slice(0, 500), stack: error.stack?.slice(0, 1500) }));
  await page.goto(origin + "/silences");
  await page.getByRole("heading", { name: "Silences Registry" }).waitFor();
  check(actor + " direct link survives OAUTH2PROXY login", new URL(page.url()).pathname === "/silences");
  return { context, page };
}

async function newRule(page, comment, fingerprints = "task22-cedar-a") {
  await page.getByRole("button", { name: "New Silence Rule" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Fingerprints (one per line or comma-separated)").fill(fingerprints);
  await dialog.getByLabel("Team Scope").selectOption("cedar");
  await dialog.getByLabel("Reason / Comment").fill(comment);
  return dialog;
}

async function submit(context, dialog, page) {
  const response = page.waitForResponse((r) => new URL(r.url()).pathname === "/v2/silences" && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "Apply Silence" }).click();
  const created = await response;
  assert.equal(created.status(), 201);
  await dialog.waitFor({ state: "hidden" });
  return (await created.json()).result;
}

async function cancel(context, rule) {
  const current = await api(context, "/silences/" + rule.id);
  if (["cancelled", "expired"].includes(current.state)) return;
  await api(context, "/silences/" + rule.id + "/cancel", "POST", {
    schema_version: 1, client_request_id: crypto.randomUUID(), expected_revision: current.revision,
    reason: "Task22 browser cleanup", correlation_id: null,
  });
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  let currentPage;
  try {
    const { context, page } = await session(browser, "admin");
    currentPage = page;
    for (const old of (await api(context, "/silences")).items) {
      if (old.comment.startsWith("Task22 browser") && ["active", "scheduled"].includes(old.state)) await cancel(context, old);
    }
    await page.reload();
    check("Maintenance navigation replaced by Silences", await page.getByRole("link", { name: "Maintenance", exact: true }).count() === 0);
    await page.goto(origin + "/maintenance");
    await page.getByRole("heading", { name: "Silences Registry" }).waitFor();
    check("old Maintenance URL redirects", new URL(page.url()).pathname === "/silences");
    await page.screenshot({ path: path.join(run, "screenshots/silences-desktop.png") });

    let dialog = await newRule(page, "Task22 browser immediate");
    const immediate = await submit(context, dialog, page);
    check("UI immediate create accepted by real API", immediate.state === "active" && immediate.team_id === "cedar");
    const row = page.getByRole("row").filter({ hasText: "Task22 browser immediate" });
    await row.getByRole("button", { name: "+4h" }).click();
    await eventually("UI quick extension increments revision", async () => (await api(context, "/silences/" + immediate.id)).revision > immediate.revision);
    await row.locator('button[title="Edit rule"]').click();
    dialog = page.getByRole("dialog");
    await dialog.getByLabel("Reason / Comment").fill("Task22 browser edited");
    await dialog.getByRole("button", { name: "Save Changes" }).click();
    await dialog.waitFor({ state: "hidden" });
    await eventually("UI edit saves reason", async () => (await api(context, "/silences/" + immediate.id)).comment === "Task22 browser edited");
    const edited = page.getByRole("row").filter({ hasText: "Task22 browser edited" });
    await edited.locator('button[title="Cancel silence"]').click();
    await page.getByRole("button", { name: "Confirm Cancel" }).click();
    await eventually("UI cancellation persisted", async () => (await api(context, "/silences/" + immediate.id)).state === "cancelled");

    dialog = await newRule(page, "Task22 browser indefinite");
    await dialog.getByRole("button", { name: "Indefinite", exact: true }).click();
    const forever = await submit(context, dialog, page);
    check("UI indefinite rule has no deadline", forever.ends_at === null);
    check("quick extend cannot shorten indefinite rule", await page.getByRole("row").filter({ hasText: "Task22 browser indefinite" }).getByRole("button", { name: "+4h" }).isDisabled());
    await cancel(context, forever);

    dialog = await newRule(page, "Task22 browser schedule");
    await dialog.getByLabel("Start", { exact: true }).selectOption("scheduled");
    const future = new Date(Date.now() + 86400000);
    const localDate = future.toISOString().slice(0, 16).replace("T", " ");
    await dialog.getByPlaceholder("Start date and time").fill(localDate);
    await dialog.getByPlaceholder("Start date and time").press("Tab");
    const scheduled = await submit(context, dialog, page);
    check("UI scheduled rule retains UTC start", scheduled.state === "scheduled" && scheduled.starts_at.startsWith(future.toISOString().slice(0, 10)));
    await page.getByRole("tab", { name: "Scheduled", exact: true }).click();
    await page.getByRole("row").filter({ hasText: "Task22 browser schedule" }).waitFor();
    await cancel(context, scheduled);
    await page.getByRole("tab", { name: "Active", exact: true }).click();

    dialog = await newRule(page, "Task22 browser retry");
    const commands = [];
    let dropped = false;
    await page.route("**/v2/silences", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      commands.push(route.request().postDataJSON());
      if (!dropped) { dropped = true; await route.fetch(); await route.abort("failed"); }
      else await route.continue();
    });
    await dialog.getByRole("button", { name: "Apply Silence" }).click();
    await eventually("uncertain network response leaves retry form open", async () => dropped && await dialog.getByRole("button", { name: "Apply Silence" }).isEnabled());
    const replay = await submit(context, dialog, page);
    await page.unroute("**/v2/silences");
    check("UI retries identical nonce and dates after server commit", commands.length === 2 && JSON.stringify(commands[0]) === JSON.stringify(commands[1]));
    await cancel(context, replay);

    await page.route("**/v2/silences?**", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: { message: "Task22 storage unavailable" } }) }));
    await page.reload();
    await page.getByText(/Failed to load silences:/).waitFor();
    check("registry exposes load failure", true);
    await page.unroute("**/v2/silences?**");
    await page.reload();
    await page.getByRole("button", { name: "New Silence Rule" }).waitFor();

    for (const suffix of ["a", "b"]) {
      await api(context, "/alerts/event", "POST", { fingerprint: "task22-ui-" + suffix,
        name: "Task22Browser" + suffix.toUpperCase(), source: ["task22-browser"],
        zone: "zone-cedar", status: "firing", severity: "critical" }, 202);
    }
    await eventually("browser alert fixtures ingested", async () => (await api(context, "/alerts")).filter((a) => a.fingerprint.startsWith("task22-ui-")).length === 2);
    await page.goto(origin + "/alerts/feed");
    const alertRow = page.getByRole("row").filter({ hasText: "Task22BrowserA" });
    await alertRow.locator('button[aria-haspopup="menu"]').click();
    await page.getByRole("menuitem", { name: "Silence", exact: true }).click();
    dialog = page.getByRole("dialog");
    await dialog.getByLabel("Reason / Comment").fill("Task22 browser from alert");
    const fromAlert = await submit(context, dialog, page);
    check("alert menu creates canonical fingerprint silence", fromAlert.selector.fingerprints.join() === "task22-ui-a" && fromAlert.team_id === "cedar");
    await cancel(context, fromAlert);
    await page.reload();
    for (const name of ["Task22BrowserA", "Task22BrowserB"]) await page.getByRole("row").filter({ hasText: name }).getByRole("checkbox").check();
    await page.getByRole("button", { name: "Silence 2 alert(s)", exact: true }).click();
    dialog = page.getByRole("dialog");
    await dialog.getByLabel("Reason / Comment").fill("Task22 browser bulk");
    const bulk = await submit(context, dialog, page);
    check("bulk action creates one rule covering both canonical targets", bulk.selector.fingerprints.join() === "task22-ui-a,task22-ui-b");
    await cancel(context, bulk);

    const incidentName = "Task22 browser incident " + crypto.randomUUID().slice(0, 8);
    const incident = await api(context, "/incidents", "POST", { team_id: "cedar",
      user_generated_name: incidentName, user_summary: incidentName,
      assignee: null, severity: "critical", same_incident_in_the_past_id: null }, 202);
    await page.goto(origin + "/incidents/" + incident.id + "/alerts");
    await page.getByText("No alerts yet", { exact: true }).waitFor();
    check("paid AI control absent from empty incident", await page.getByRole("button", { name: "Try AI Correlation" }).count() === 0);
    await api(context, "/incidents/" + incident.id + "/alerts", "POST", ["task22-ui-a"], 202);
    await page.goto(origin + "/incidents");
    const incidentRow = page.getByRole("row").filter({ hasText: incidentName });
    await incidentRow.locator('button[aria-haspopup="menu"]').click();
    await page.getByRole("menuitem", { name: "Silence", exact: true }).click();
    dialog = page.getByRole("dialog");
    await dialog.getByLabel("Reason / Comment").fill("Task22 browser from incident");
    const fromIncident = await submit(context, dialog, page);
    check("incident menu creates canonical incident silence", fromIncident.selector.incident_ids.join() === incident.id);
    await cancel(context, fromIncident);
    await page.goto(origin + "/silences");

    const viewer = await session(browser, "viewer");
    check("viewer create control disabled", await viewer.page.getByRole("button", { name: "New Silence Rule" }).isDisabled());
    check("viewer API scope remains read only", (await api(viewer.context, "/auth/users/me/permissions")).role === "viewer");
    await viewer.page.screenshot({ path: path.join(run, "screenshots/silences-viewer.png") });
    await viewer.context.close();

    const responder = await session(browser, "cedar");
    await responder.page.getByLabel("Team filter").waitFor();
    const teamOptions = await responder.page.getByLabel("Team filter").locator("option").allTextContents();
    check("responder sees no foreign team in UI", teamOptions.includes("Team: cedar") && !teamOptions.includes("Team: quartz"));
    await responder.page.getByRole("button", { name: "New Silence Rule" }).click();
    check("responder form limits team scope", (await responder.page.getByLabel("Team Scope").locator("option").allTextContents()).join() === "Team: cedar");
    await responder.context.close();

    await page.setViewportSize({ width: 375, height: 812 });
    await page.screenshot({ path: path.join(run, "screenshots/silences-mobile.png"), fullPage: true });
    check("mobile registry controls remain available", await page.getByRole("button", { name: "New Silence Rule" }).isVisible());
    check("browser has no uncaught application error", pageErrors.length === 0);
    await context.close();
  } catch (error) {
    if (currentPage && !currentPage.isClosed()) {
      await currentPage.screenshot({ path: path.join(run, "screenshots/failure.png"), fullPage: true }).catch(() => {});
      fs.writeFileSync(path.join(run, "browser-failure.txt"), error.message + "\n" + (await currentPage.locator("body").innerText()).slice(0, 6000));
    }
    throw error;
  } finally {
    fs.writeFileSync(path.join(run, "browser-result.json"), JSON.stringify({ results, pageErrors }, null, 2));
    await browser.close();
  }
  console.log("browser:", results.length, "checks passed");
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
