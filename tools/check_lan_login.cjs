// Home LAN mode (HTTPS) in a real browser: the login screen, wrong password, a successful sign-in to the workspace,
// sign-out, and the screen shown after the session ended. Runs the real Python server bound to this machine's
// LAN address (a non-loopback IPv4 address is required; the check is skipped when there is none).
// The server is started with HTTPS (the Home LAN default); the test trusts it without installing the CA.
process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
const { chromium } = require("playwright");
const assert = require("assert/strict");
const { spawn } = require("child_process");
const fs = require("fs"), os = require("os"), path = require("path");
const repo = path.resolve(__dirname, "..");
const python = process.env.SYMPORT_PYTHON || "python3";
const lan = Object.values(os.networkInterfaces()).flat().find((i) => i && i.family === "IPv4" && !i.internal);
if (!lan) { console.log("check_lan_login: skipped (no LAN interface)"); process.exit(0); }
const PORT = 8823, HOST = lan.address, PASSWORD = "lan-test-" + Math.random().toString(36).slice(2) + "-secret";
const root = fs.mkdtempSync(path.join(os.tmpdir(), "symport-lan-"));
for (const name of ["theme", "tools", "profiles", "catalog"]) fs.cpSync(path.join(repo, name), path.join(root, name), { recursive: true });
const server = spawn(python, ["-m", "symport.web", "--host", HOST, "--port", String(PORT), "--repo", root], {
  cwd: repo, stdio: ["ignore", "ignore", "inherit"],
  env: { ...process.env, PYTHONPATH: repo, SYMPORT_ACCESS_MODE: "lan", SYMPORT_BIND: HOST, SYMPORT_HTTP_PORT: String(PORT), SYMPORT_TLS: "on", SYMPORT_TLS_DIR: path.join(root, "tls"),
         SYMPORT_ADMIN_PASSWORD: PASSWORD, SYMPORT_DEVICE_ROOTS: "{}", SYMPORT_MGO_URL: "", SYMPORT_BRIDGE_URL: "" },
});
const base = `https://${HOST}:${PORT}`;
(async () => {
  let browser;
  try {
    for (let i = 0; i < 60; i++) { try { if ((await fetch(base + "/healthz")).ok) break; } catch { /* starting */ } await new Promise((r) => setTimeout(r, 250)); }
    assert.equal((await fetch(base + "/api/state")).status, 401, "API is closed before sign-in");
    const ca = await fetch(base + "/symport-ca.crt");
    assert.equal(ca.status, 200);
    assert((await ca.text()).includes("BEGIN CERTIFICATE"), "the local CA can be downloaded");
    browser = await chromium.launch({ headless: true, ...(process.env.SYMPORT_BROWSER_EXECUTABLE ? { executablePath: process.env.SYMPORT_BROWSER_EXECUTABLE } : {}), args: ["--no-sandbox", "--no-proxy-server"] });
    for (const [lang, labels] of [["en", { submit: "Sign in", wrong: "Wrong password." }], ["ru", { submit: "Войти", wrong: "Неверный пароль." }]]) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: lang, ignoreHTTPSErrors: true });
      const page = await context.newPage();
      page.setDefaultTimeout(15000);
      page.errors = [];
      page.on("pageerror", (e) => page.errors.push(e.message));
      await page.goto(base + "/");
      await page.getByRole("button", { name: labels.submit, exact: true }).waitFor();
      assert.equal(await page.locator("main").count(), 1);
      assert.equal(await page.locator("meta[name=symport-token]").count(), 0, "no token on the login screen");
      await page.getByLabel(/password|пароль/i).fill("definitely-wrong-password");
      await page.getByRole("button", { name: labels.submit, exact: true }).click();
      await page.getByText(labels.wrong).waitFor();
      assert.equal(await page.locator("body").innerText().then((t) => t.includes("definitely-wrong-password")), false);
      if (lang === "ru") {
        await page.getByLabel(/пароль/i).fill(PASSWORD);
        await page.getByRole("button", { name: labels.submit, exact: true }).click();
        await page.locator(".sidebar").waitFor();           // the workspace itself
        const cookies = await context.cookies();
        const session = cookies.find((c) => c.name === "symport_session");
        assert(session && session.httpOnly && session.sameSite === "Strict" && session.secure, "session cookie is HttpOnly + Secure + SameSite=Strict");
        assert.equal(await page.evaluate(() => document.cookie.includes("symport_session")), false, "cookie is invisible to scripts");
        assert.equal((await context.request.get(base + "/api/state")).status(), 200);
        await page.getByRole("button", { name: /Sign out|Выйти/ }).click();
        await page.getByRole("button", { name: "Войти", exact: true }).waitFor();
        assert.equal((await context.request.get(base + "/api/state")).status(), 401, "sign-out revokes the session");
        // a stale tab: the session is gone, so the next request returns to the login screen instead of looping
        await page.goto(base + "/");
        await page.getByRole("button", { name: "Войти", exact: true }).waitFor();
      }
      assert.deepEqual(page.errors, []);
      await context.close();
    }
    console.log("check_lan_login: ok");
  } finally {
    if (browser) await browser.close();
    server.kill();
    fs.rmSync(root, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); server.kill(); process.exit(1); });
