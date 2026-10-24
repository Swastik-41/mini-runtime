// Browser test for the dashboard page, in headless Chromium with Playwright.
// It starts serve_fake.py (fake runtime, fake cgroups, no root needed).
//
//   NODE_PATH=$(npm root -g) node dashboard/tests/ui_test.js [screenshot.png]

"use strict";

const { chromium } = require("playwright");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const HERE = __dirname;
const PORT = 18000 + Math.floor(Math.random() * 1000);
const URL = `http://127.0.0.1:${PORT}/`;
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "dash-ui-"));
const env = { ...process.env, FAKE_RT_DIR: path.join(scratch, "rt"), FAKE_CG_ROOT: path.join(scratch, "cgroup") };

let server = null, external = null, failures = 0, passes = 0;

function startServer() {
  return new Promise((resolve, reject) => {
    server = spawn("python3", [path.join(HERE, "serve_fake.py"), String(PORT), scratch],
      { env, stdio: ["ignore", "pipe", "inherit"] });
    server.stdout.on("data", (d) => { if (String(d).includes("ready")) resolve(); });
    server.on("exit", (code) => { if (code) reject(new Error(`server exited ${code}`)); });
  });
}

function stopServer() {
  return new Promise((resolve) => {
    server.on("exit", resolve);
    server.kill("SIGTERM");
  });
}

async function check(name, fn) {
  try {
    await fn();
    passes++;
    console.log(`PASS  ${name}`);
  } catch (err) {
    failures++;
    console.log(`FAIL  ${name}\n      ${String(err.message).split("\n")[0]}`);
  }
}

function assert(cond, message) { if (!cond) throw new Error(message); }

async function run(page, name, fields = {}) {
  await page.fill('input[name="name"]', name);
  await page.fill('input[name="command"]', fields.command || "");
  if (fields.memory) await page.fill('input[name="memory"]', fields.memory);
  if (fields.cpu) await page.fill('input[name="cpu"]', String(fields.cpu));
  await page.click('button[type="submit"]');
  await page.waitForFunction((n) => document.getElementById("d-name").textContent === n, name);
}

const row = (page, name) => page.locator("#container-rows tr", { has: page.locator("td.cname", { hasText: new RegExp(`^${name}$`) }) });

(async () => {
  await startServer();
  const exe = fs.existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : undefined;
  const browser = await chromium.launch(exe ? { executablePath: exe } : {});
  const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: URL });
  const page = await context.newPage();

  const problems = [];
  page.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
  page.on("pageerror", (e) => problems.push(String(e)));
  page.on("dialog", (d) => d.accept());
  let statsRequests = [];
  page.on("request", (r) => { if (r.url().includes("/stats")) statsRequests.push({ url: r.url(), t: Date.now() }); });

  await page.goto(URL);

  await check("page loads, connects and shows the empty state", async () => {
    await page.waitForSelector("#conn.ok");
    await page.waitForFunction(() => !document.querySelector(".skeleton"));
    const text = await page.textContent("#containers-empty");
    assert(text.includes("No containers yet"), `empty text was "${text}"`);
  });

  await check("run a container and use its terminal", async () => {
    await run(page, "web1");
    await page.waitForFunction(() => document.querySelector("#terminal").textContent.includes("fake-shell ready"));
    await page.click("#terminal");
    await page.keyboard.type("hello\n");
    await page.waitForFunction(() => document.querySelector("#terminal").textContent.includes("got:hello"));
    await page.waitForFunction(() => document.getElementById("b-state").textContent === "Running");
    assert((await page.textContent("#b-terminal")) === "Terminal here", "terminal badge");
    assert((await page.textContent("#term-name")).includes("web1"), "terminal shows the container name");
  });

  await check("metrics: collecting first, then live CPU % of one core", async () => {
    await page.waitForFunction(() => document.getElementById("b-metrics").textContent === "Metrics live");
    await page.waitForFunction(() => /^\d+%$/.test(document.getElementById("t-cpu").textContent), null, { timeout: 8000 });
    assert((await page.textContent("#t-mem")).includes("MB"), "memory value");
    assert((await page.textContent("#sample-age")).startsWith("Last sample"), "sample age shown");
    assert((await page.textContent(".tiles")).includes("% of one CPU core"), "CPU label");
  });

  await check("HTML in a command and a file name is shown as text", async () => {
    await run(page, "html1", { command: "echo '<img src=x onerror=window.pwned=1><b>bold</b>'; sleep 30" });
    await page.waitForFunction(() => document.querySelector("#diff-list .path"));
    const meta = await page.textContent("#d-meta");
    assert(meta.includes("<b>bold</b>"), "command shown literally");
    assert(await page.evaluate(() => !document.querySelector("#d-meta b, #d-meta img, #diff-list b, #container-rows b")), "no injected elements");
    const paths = await page.locator("#diff-list .path").allTextContents();
    assert(paths.includes("/<b>bold</b>.txt"), `diff paths ${paths}`);
    assert(!(await page.evaluate(() => window.pwned)), "script ran");
  });

  await check("switching containers keeps one stats loop", async () => {
    for (let i = 0; i < 20; i++) await row(page, i % 2 ? "web1" : "html1").click();
    await page.waitForTimeout(500);
    statsRequests = [];
    await page.waitForTimeout(4000);
    const n = statsRequests.length;
    const other = statsRequests.filter((r) => !r.url.includes("/web1/")).length;
    assert(n >= 2 && n <= 6, `${n} stats requests in 4 s`);
    assert(other === 0, `${other} requests for a container that is not selected`);
  });

  await check("table search, filter and sort", async () => {
    await page.fill("#search", "html");
    assert((await page.locator("#container-rows tr").count()) === 1, "search");
    await page.fill("#search", "zzz");
    assert((await page.textContent("#containers-empty")).includes("No containers match"), "no-match text");
    await page.fill("#search", "");
    await page.selectOption("#sort", "name");
    const names = await page.locator("#container-rows td.cname").allTextContents();
    assert(names.join() === [...names].sort().join(), `sorted ${names}`);
    assert((await page.textContent("#updated")).startsWith("Updated"), "updated time");
  });

  await check("stop, then Start again with the same settings", async () => {
    await run(page, "again1", { memory: "64M", cpu: 30, command: "sleep 30" });
    await page.click("#d-stop");
    await page.waitForFunction(() => document.getElementById("b-state").textContent === "Exited 137");
    assert(await page.isEnabled("#d-start"), "Start enabled after exit");
    assert((await page.textContent("#d-notice")).includes("exit code 137"), "ended notice");
    assert((await page.textContent("#chart-mode")).includes("Final samples"), "final samples label");
    await page.click("#d-start");
    await page.waitForFunction(() => document.getElementById("b-state").textContent === "Running");
    assert((await page.textContent("#d-meta")).includes("memory 64M, CPU 30%"), "same options");
    await page.waitForFunction(() => /30%/.test(document.querySelector("#container-rows tr.selected").textContent));
  });

  await check("Edit and run copies the settings into the form", async () => {
    await page.click("#d-stop");
    await page.waitForFunction(() => document.getElementById("b-state").textContent === "Exited 137");
    await page.click("#d-edit");
    assert((await page.inputValue('input[name="memory"]')) === "64M", "memory copied");
    assert((await page.inputValue('input[name="cpu"]')) === "30", "cpu copied");
    assert((await page.inputValue('input[name="command"]')) === "sleep 30", "command copied");
  });

  await check("terminal toolbar: copy, clear and fullscreen", async () => {
    await row(page, "web1").click();
    await page.waitForFunction(() => document.querySelector("#terminal").textContent.includes("got:hello"));
    await page.click("#term-copy");
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    assert(copied.includes("got:hello"), "copied text");
    await page.click("#term-full");
    assert(await page.evaluate(() => document.getElementById("term-panel").classList.contains("fullscreen")), "fullscreen on");
    await page.click("#term-full");    // Esc goes to the shell while the terminal has focus
    assert(!(await page.evaluate(() => document.getElementById("term-panel").classList.contains("fullscreen"))), "button leaves");
    await page.click("#term-clear");
  });

  await check("a container started elsewhere says its terminal is not here", async () => {
    external = spawn(path.join(HERE, "fake_runtime.py"), ["run", "outside1", "--", "sleep", "60"], { env, stdio: "ignore" });
    await row(page, "outside1").waitFor();
    await row(page, "outside1").click();
    await page.waitForFunction(() => document.getElementById("b-terminal").textContent === "Terminal elsewhere");
    assert((await page.textContent("#d-notice")).includes("cannot re-attach"), "notice");
    assert(await page.isEnabled("#d-stop"), "can still stop it");
  });

  await check("bad input shows the server's message", async () => {
    await page.fill('input[name="name"]', "bad1");
    await page.fill('input[name="memory"]', "12");
    await page.click('button[type="submit"]');
    await page.waitForFunction(() => document.getElementById("run-error").textContent.includes("Memory"));
    await page.fill('input[name="memory"]', "100M");
  });

  await check("server restart: reconnects, resets the event log and asks for a reload", async () => {
    await row(page, "web1").click();
    const before = await page.locator("#event-rows tr").count();
    assert(before > 0, "events before restart");
    await stopServer();
    await page.waitForSelector("#conn.bad", { timeout: 8000 });
    assert((await page.textContent("#conn")) === "Reconnecting…", "reconnecting text");
    await startServer();
    await page.waitForSelector("#conn.ok", { timeout: 10000 });
    await page.waitForSelector("#restart-banner:not([hidden])");
    const after = await page.locator("#event-rows tr").count();
    assert(after < before, `event log not reset (${before} before, ${after} after)`);
    await page.waitForFunction(() => document.getElementById("b-terminal").textContent !== "Terminal here");
  });

  await check("reload after restart works and the page is usable", async () => {
    await page.click("#reload");
    await page.waitForSelector("#conn.ok");
    await run(page, "after1", { command: "sleep 30" });
    await page.waitForFunction(() => document.getElementById("b-state").textContent === "Running");
  });

  await check("narrow screen: no horizontal page scroll", async () => {
    await page.setViewportSize({ width: 390, height: 900 });
    await page.waitForTimeout(300);
    const { sw, iw } = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: window.innerWidth }));
    assert(sw <= iw, `scrollWidth ${sw} > ${iw}`);
    await page.setViewportSize({ width: 1400, height: 1000 });
  });

  await check("no console errors or CSP violations", async () => {
    const real = problems.filter((p) => !p.includes("Failed to load resource") && !p.includes("ERR_CONNECTION_REFUSED"));
    assert(real.length === 0, real.join(" | "));
  });

  if (process.argv[2]) {
    await row(page, "web1").click().catch(() => {});
    await run(page, "demo", { command: "" });
    await page.waitForTimeout(4000);
    await page.screenshot({ path: process.argv[2], fullPage: true });
  }

  await browser.close();
  if (external) external.kill("SIGKILL");
  await stopServer();
  spawnSync("rm", ["-rf", scratch]);
  console.log(`\n${passes} passed, ${failures} failed`);
  process.exit(failures ? 1 : 0);
})().catch(async (err) => {
  console.error(err);
  if (external) external.kill("SIGKILL");
  if (server) server.kill("SIGKILL");
  process.exit(2);
});
