"use strict";

const assert = require("node:assert/strict");
const { spawn, execFileSync } = require("node:child_process");
const { once } = require("node:events");
const { mkdtempSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const path = require("node:path");
const { createInterface } = require("node:readline");
const { test } = require("node:test");
const { chromium } = require("playwright");

test("Owner console renders and uploads a disabled Skill in Chromium", { timeout: 60_000 }, async (t) => {
  const cwd = mkdtempSync(path.join(tmpdir(), "jx-owner-smoke-"));
  const server = spawn("python", [path.join(__dirname, "serve_owner_fixture.py")], {
    cwd, env: { PATH: process.env.PATH, HOME: cwd, PYTHONUTF8: "1" }, stdio: ["ignore", "pipe", "inherit"],
  });
  const closed = once(server, "close");
  const lines = createInterface({ input: server.stdout });
  t.after(async () => {
    lines.close();
    server.kill();
    await closed;
    rmSync(cwd, { recursive: true, force: true });
  });
  const [ready] = await Promise.race([
    once(lines, "line", { signal: AbortSignal.timeout(12_000) }),
    closed.then(([code]) => { throw new Error(`Fixture exited before startup (status ${code})`); }),
  ]);
  const { origin } = JSON.parse(ready);
  assert.match(origin, /^https:\/\/127\.0\.0\.1:\d+$/);
  const browser = await chromium.launch();
  t.after(() => browser.close());

  const source = "---\nname: browser-fixture\ndescription: 仅用于离线检验。\n---\n# 演示技能\n<b>纯文本</b>\n";
  for (const width of [1280, 390]) {
    await t.test(`${width}px`, async () => {
      // Trust the ephemeral loopback certificate only in this disposable
      // context, and abort any browser request outside the fixture origin.
      const context = await browser.newContext({
        viewport: { width, height: 900 }, ignoreHTTPSErrors: true,
        serviceWorkers: "block", locale: "zh-CN",
      });
      const blocked = [];
      await context.route("**/*", async (route) => {
        const url = new URL(route.request().url());
        if (url.origin === origin) await route.continue();
        else {
          blocked.push(url.origin);
          await route.abort();
        }
      });
      const page = await context.newPage();
      page.setDefaultTimeout(5_000);
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      page.on("response", response => {
        const { pathname } = new URL(response.url());
        if (response.status() >= 400 && pathname.startsWith("/console/")) {
          errors.push(`${response.status()} ${pathname}`);
        }
      });

      async function layout(label) {
        const { viewport, clientWidth, scrollWidth } = await page.evaluate(async () => {
          await document.fonts.ready;
          const { clientWidth, scrollWidth } = document.documentElement;
          return { viewport: window.innerWidth, clientWidth, scrollWidth };
        });
        assert.equal(viewport, width);
        assert.ok(scrollWidth <= clientWidth + 1, `${label}: document ${scrollWidth}px exceeds ${clientWidth}px`);
      }

      try {
        await page.goto(`${origin}/console/`);
        await page.waitForURL(`${origin}/console/login`);
        await layout("login");
        await page.getByLabel("所有者口令").fill("test-fixture-only-password");
        await page.getByRole("button", { name: "登录", exact: true }).press("Enter");
        await page.waitForURL(`${origin}/console/`);
        await page.getByRole("heading", { name: "经营概览" }).waitFor();
        await page.getByRole("heading", { name: "离线示例店铺" }).waitFor();
        await layout("overview");

        const menu = page.getByRole("button", { name: "切换工作区" });
        const nav = page.locator("#workspace-navigation");
        const settings = page.getByRole("navigation", { name: "工作区" })
          .getByRole("button", { name: "分析设置" });
        if (width === 390) {
          assert.equal(await nav.isVisible(), false);
          await menu.press("Enter");
          await settings.waitFor();
          await settings.press("Escape");
          assert.equal(await menu.getAttribute("aria-expanded"), "false");
          assert.equal(await nav.isVisible(), false);
          assert.equal(await menu.evaluate(element => element === document.activeElement), true);
          await menu.press("Enter");
        }
        await settings.waitFor();
        await settings.press("Enter");
        const heading = page.getByRole("heading", { name: "分析设置" });
        await heading.waitFor();
        // On mobile the selected button is still in the DOM, but its collapsed
        // navigation is no longer exposed to role-based locators.
        assert.equal(await nav.locator('[data-view="analysis-settings"]').getAttribute("aria-current"), "page");
        if (width === 390) {
          assert.equal(await nav.isVisible(), false);
          assert.equal(await heading.evaluate(element => element === document.activeElement), true);
        }

        const library = page.getByRole("region", { name: "自定义 Skills 资料库" });
        const record = library.getByRole("button", { name: "browser-fixture · v1 · 停用" });
        if (width === 1280) {
          await library.getByText("还没有上传的技能。").waitFor();
          const file = library.getByLabel("选择本地技能文档");
          const upload = library.getByRole("button", { name: "上传到资料库" });
          assert.equal(await upload.isDisabled(), true);
          await file.setInputFiles({ name: "SKILL.md", mimeType: "text/markdown", buffer: Buffer.from(source) });
          assert.equal(await upload.isEnabled(), true);
          const receipt = page.waitForResponse(response =>
            new URL(response.url()).pathname === "/console/api/skill-library"
            && response.request().method() === "POST");
          await upload.press("Enter");
          assert.equal((await receipt).status(), 200);
          await library.getByText("已入库。新版本保持停用，点击名称可审阅。", { exact: true }).waitFor();
        }
        // The mobile browser session is fresh; it sees the persisted version
        // written by the desktop browser, not a simulated client-side receipt.
        await record.waitFor();
        await record.press("Enter");
        const preview = library.getByLabel("原始文档（仅显示纯文本）");
        await preview.waitFor();
        assert.equal(await preview.inputValue(), source);
        assert.equal(await preview.evaluate(element => element.readOnly), true);
        await library.getByText("资料库状态：停用。模型应用状态：未应用。").waitFor();
        await library.getByText("第一次上传？查看文件格式与示例").press("Enter");
        await library.locator("pre").waitFor();
        await layout("Skills detail");

        // Preserve the original single-file path above. Exercise real owner-only
        // ZIP import/export on the desktop and reopen the same references on mobile.
        const fixture = path.resolve(__dirname, "../fixtures/owner-skills/portable-review");
        if (width === 1280) {
          const zip = path.join(cwd, "portable-review.zip");
          execFileSync("python", ["-m", "zipfile", "-c", zip, fixture]);
          await library.getByLabel("选择本地技能文档").setInputFiles(zip);
          await library.getByRole("button", { name: "上传到资料库" }).click();
        }
        await library.getByRole("button", { name: "portable-review · v1 · 停用" }).click();
        const docs = library.getByLabel("查看包内参考文档");
        await docs.selectOption({ label: "references/nested/清单.txt" });
        const ref = library.getByLabel("参考文档原文（只读）");
        assert.match(await ref.inputValue(), /<b>本行是纯文本/);
        assert.equal(await ref.evaluate(element => element.readOnly), true);
        await library.getByText(/这些本地文档链接不在此版本中/).waitFor();
        await layout("Skills package");
        if (width === 1280) {
          const download = page.waitForEvent("download");
          await library.getByRole("link", { name: "导出完整版本 ZIP（含参考文档）" }).click();
          const result = await download;
          assert.equal(result.suggestedFilename(), "portable-review-v1.zip");
          const saved = path.join(cwd, "download.zip");
          await result.saveAs(saved);
          execFileSync("python", ["-c", [
            "from pathlib import Path; import sys, zipfile",
            "p=Path(sys.argv[2]); z=zipfile.ZipFile(sys.argv[1])",
            "assert {k:z.read(k) for k in z.namelist()} == {f.relative_to(p.parent).as_posix():f.read_bytes() for f in p.rglob('*') if f.is_file()}",
          ].join("\n"), saved, fixture]);
        }

        assert.deepEqual(errors, []);
        assert.deepEqual(blocked, []);
      } finally {
        await context.close();
      }
    });
  }
});
