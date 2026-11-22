/* Offline component contract test. No browser, libraries or network required. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.events = {}; this.dataset = {};
    this.files = []; this.value = ""; this.textContent = "";
  }
  append(...kids) { this.children.push(...kids); }
  replaceChildren(...kids) { this.children = kids; }
  setAttribute(k, v) { this[k] = v; }
  addEventListener(k, cb) { this.events[k] = cb; }
  async fire(k) { if (!this.disabled) await this.events[k]?.(); }
  querySelectorAll(tag) {
    return this.children.flatMap(e => [...(e.tag === tag ? [e] : []), ...e.querySelectorAll(tag)]);
  }
  click() { if (this.tag === "a") downloaded = this.download; else this.fire("click"); }
}
let confirmChange = false, blob, downloaded, catalogRevision = 0, store = [], version = 0, enabled = false;
let blocked = false;
const root = new Element("section");
const state = () => ({ revision: catalogRevision, can_upload: true, max_bytes: 16384,
  skills: store.length ? [{ name: "ad-check", description: "Local only", version, enabled }] : [] });
const actions = [];
const api = async (url, request) => {
  if (!request && url === "skill-library") return state();
  if (!request) {
    const v = Number(url.split("=")[1]) || version;
    return { name: "ad-check", current_version: version, document: { source: store[v-1], digest: "abc".repeat(20), version: v },
      enabled, local_preview: enabled ? { objective: "reach" } : null, revision: catalogRevision,
      versions: store.map((_,i)=>({version:i+1, created:"2025-01-01", digest:"abc".repeat(20)})), audit: [] };
  }
  assert.equal(request.method, "POST"); assert.equal(request.headers["X-CSRF-Token"], "test-only");
  const payload = JSON.parse(request.body); assert.equal(payload.revision, catalogRevision);
  if (blocked) throw Object.assign(new Error("资料库已变更"), { status:409 });
  actions.push(payload.action);
  if (payload.action === "upload") {
    assert.equal(payload.filename, "SKILL.md");
    const content=Buffer.from(payload.content_base64, "base64").toString("utf8");
    if (version && content===store[version-1]) return state();
    store.push(content); version = store.length; enabled=false;
  } else if (payload.action === "rollback") { version=payload.version;enabled=false; }
  else { enabled=payload.action === "enable"; }
  catalogRevision++;
  return state();
};
const g = { window: { confirm: () => confirmChange }, document: { createElement: tag => new Element(tag) },
  Blob, btoa, setTimeout: fn => fn(), URL: {createObjectURL: obj => { blob=obj;return "blob:local"; }, revokeObjectURL() {}} };
vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../src/jx_alert/admin_ui/skill-library.js"),"utf8"), g);
const find = (tag, text) => root.querySelectorAll(tag).find(e => !text || e.textContent.includes(text));
const tick = () => new Promise(r=>setImmediate(r));
const click = async label => {
  const b=find("button",label); assert.ok(b,label); await b.fire("click"); await tick();
};
(async () => {
  const panel=g.window.JxSkillLibrary.mount(root,{api,csrf:()=>"test-only"});
  await tick();
  const file=find("input");
  async function select(name, text) {
    const bin=new TextEncoder().encode(text);
    file.files=[{name,size:bin.byteLength,arrayBuffer:async()=>bin.buffer}];
    await file.fire("change");
  }
  const doc="---\r\nname: ad-check\r\ndescription: 核对\r\n---\r\n<b>literal</b>";
  assert.match(find("pre").textContent, /^---\nname: daily-review\ndescription: .+\n---\n#/);
  assert.equal(find("button","上传到资料库").disabled,true);
  await select("script.py",doc); await click("上传到资料库"); assert.deepEqual(actions,[]);
  await select("SKILL.md",doc); await click("上传到资料库");await click("ad-check · v1 · 停用");
  assert.equal(find("textarea").value,doc);
  await click("审阅完成");assert.deepEqual(actions,["upload"]);
  confirmChange=true; await click("审阅完成");assert.equal(enabled,true);assert.ok(find("p","未修改 JX 默认策略"));
  await select("SKILL.md",doc); await click("上传到资料库");
  assert.ok(find("p","原审阅状态不变"));assert.equal(enabled,true);assert.equal(store.length,1);
  await click("导出此版本");assert.equal(downloaded,"SKILL.md");assert.equal(await blob.text(),doc);
  await select("SKILL.md",doc+"二");await click("上传到资料库");await click("ad-check · v2 · 停用");
  find("select").value="1";await click("查看所选版本");await click("恢复为当前版本");
  assert.equal(version,1);assert.equal(enabled,false);
  await click("审阅完成");await click("停用此技能");assert.equal(enabled,false);
  blocked=true;await click("审阅完成");assert.equal(find("textarea"),undefined);
  assert.equal(find("button","上传到资料库").disabled,true);await panel.refresh();assert.ok(find("button","ad-check · v1 · 停用"));
  assert.deepEqual(actions,["upload","enable","upload","upload","rollback","enable","disable"]);
  // Large typed-array encoding must not depend on a browser's maximum argument
  // count. Archive validation belongs to the real server integration tests.
  const bundleRoot = new Element("section");
  let revision = 0;
  const big = Buffer.alloc(275 * 1024, 0x61);
  const name = "package-check";
  const fixtureAPI = async (url, req) => {
    if (req) {
      const fields = JSON.parse(req.body);
      assert.equal(fields.action, "upload_package");
      assert.equal(fields.revision, 0);
      assert.deepEqual(Buffer.from(fields.content_base64, "base64"), big);
      revision++;
    }
    if (url === "skill-library") return {can_upload:true, max_bytes:131072, max_archive_bytes:589824, revision,
      skills: revision ? [{name,version:1,enabled:false,description:"zip"}] : []};
    return {name,current_version:1,revision,enabled:false,local_preview:null,audit:[],
      versions:[{version:1,digest:"abc",created:"2026-01-01"}],
      document:{version:1,digest:"abc",source:"main",declared_version:"1.2.3"},
      references:{"references/a.md":"Aa","references/b.md":"<b>literal</b>"},
      missing_text_links:[["SKILL.md","../other/SKILL.md"]]};
  };
  g.window.JxSkillLibrary.mount(bundleRoot,{api:fixtureAPI,csrf:()=>"test-only"});
  await tick();
  const findIn = (tag,txt) => bundleRoot.querySelectorAll(tag).find(x => !txt || x.textContent.includes(txt));
  const input = findIn("input");
  input.files = [{ name:"fixture.zip", size:big.length, arrayBuffer: async()=>big }];
  await input.fire("change");
  await findIn("button","上传到资料库").fire("click"); await tick();
  await findIn("button","package-check").fire("click"); await tick();
  const ref = bundleRoot.querySelectorAll("select").find(x => x.id === "skill-library-reference");
  ref.value = "1"; await ref.fire("change");
  assert.equal(bundleRoot.querySelectorAll("textarea").at(-1).value, "<b>literal</b>");
  assert.ok(findIn("li","../other/SKILL.md"));
  assert.equal(findIn("a","导出完整版本").href, "/console/api/skill-library/package-check/package.zip?version=1");
  console.log("PASS: single-file lifecycle, ZIP encoding, inert references, missing-link notice and versioned export.");
})().catch(e=>{console.error(e);process.exitCode=1;});
