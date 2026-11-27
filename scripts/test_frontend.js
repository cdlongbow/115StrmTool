const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.resolve(__dirname, "../combined/web/index.html"), "utf8");
const scripts = Array.from(html.matchAll(/<script>([\s\S]*?)<\/script>/g), match => match[1]);
const appScript = scripts[2].replace(/\nloadConfig\(\);\s*startPolling\(updateDashboard[\s\S]*$/, "\n");

class MockClassList {
  constructor() {
    this.values = new Set();
  }
  add(...names) {
    names.forEach(name => this.values.add(name));
  }
  remove(...names) {
    names.forEach(name => this.values.delete(name));
  }
  contains(name) {
    return this.values.has(name);
  }
}

class MockElement {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.className = "";
    this.classList = new MockClassList();
    this._innerHTML = "";
    this._textContent = "";
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    this.children = [];
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set textContent(value) {
    this._textContent = String(value);
    this._innerHTML = "";
  }
  get textContent() {
    return this._textContent;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  querySelectorAll(selector) {
    if (selector.includes(".path-entry") || selector.includes(".browser-item")) {
      return this.children;
    }
    return [];
  }
  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }
  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter(item => item !== handler);
  }
  emit(type, event = {}) {
    (this.listeners[type] || []).forEach(handler => handler(event));
  }
  click() {}
  remove() {}
}

function response(status, payload, text) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: new Headers(),
    json: async () => payload,
    text: async () => text === undefined ? JSON.stringify(payload) : text,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return {promise, resolve, reject};
}

function makeContext({fetchImpl, promptImpl = () => null} = {}) {
  const elements = new Map();
  const storage = new Map();
  const document = new MockElement("document");
  document.hidden = false;
  document.body = new MockElement("body");
  document.getElementById = id => {
    if (!elements.has(id)) {
      elements.set(id, new MockElement());
    }
    return elements.get(id);
  };
  document.createElement = tagName => new MockElement(tagName);
  document.querySelectorAll = selector => {
    if (selector === ".nav a") {
      return [];
    }
    if (selector.includes(".browser-item")) {
      return document.getElementById("browserList").children;
    }
    if (selector.includes(".path-entry")) {
      return document.getElementById("p115_pathMappings").children;
    }
    return [];
  };
  document.querySelector = () => null;
  document.getElementById("qrcodeAppType").value = "alipaymini";

  const windowListeners = {};
  const sandbox = {
    console,
    Headers,
    URL,
    URLSearchParams,
    Promise,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    document,
    location: {search: "", pathname: "/", hash: ""},
    history: {replaceState() {}},
    localStorage: {
      getItem: key => storage.has(key) ? storage.get(key) : null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    navigator: {clipboard: {writeText: async () => {}}},
    prompt: promptImpl,
    alert() {},
    confirm: () => true,
    fetch: fetchImpl || (async () => response(200, {})),
    addEventListener(type, handler) {
      (windowListeners[type] ||= []).push(handler);
    },
    removeEventListener(type, handler) {
      windowListeners[type] = (windowListeners[type] || []).filter(item => item !== handler);
    },
    emit(type, event = {}) {
      (windowListeners[type] || []).forEach(handler => handler(event));
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  const context = vm.createContext(sandbox);
  return {context, sandbox, document, elements, storage};
}

function run(context, code) {
  return vm.runInContext(code, context);
}

function makeAppContext(options) {
  const state = makeContext(options);
  run(state.context, scripts[1]);
  run(state.context, appScript);
  return state;
}

function getElement(state, id) {
  return state.document.getElementById(id);
}

async function flush() {
  await new Promise(resolve => setImmediate(resolve));
}

test("401 prompts for config token and retries the original request", async () => {
  const calls = [];
  let promptMessage = "";
  const state = makeContext({
    fetchImpl: async (input, init) => {
      calls.push({input, init});
      return response(calls.length === 1 ? 401 : 200, {ok: true});
    },
    promptImpl: message => {
      promptMessage = message;
      return "  secret-token  ";
    },
  });
  run(state.context, scripts[0]);
  const result = await state.sandbox.fetch("/api/status");
  assert.equal(result.status, 200);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].init.headers.get("X-Admin-Token"), "secret-token");
  assert.equal(state.storage.get("adminToken"), "secret-token");
  assert.equal(state.sandbox.__admin401Shown, false);
  assert.match(promptMessage, /config\.json/);
  assert.doesNotMatch(promptMessage, /控制台/);
});

test("401 cancellation resets the prompt lock", async () => {
  let calls = 0;
  const state = makeContext({
    fetchImpl: async () => {
      calls += 1;
      return response(401, {error: "unauthorized"});
    },
    promptImpl: () => null,
  });
  run(state.context, scripts[0]);
  const result = await state.sandbox.fetch("/api/status");
  assert.equal(result.status, 401);
  assert.equal(calls, 1);
  assert.equal(state.sandbox.__admin401Shown, false);
});

test("apiFetch renders error, detail and 422 validation messages", async () => {
  const cases = [
    [{error: "backend exploded"}, "backend exploded"],
    [{detail: "missing resource"}, "missing resource"],
    [{detail: [{loc: ["body", "name"], msg: "field required"}]}, "body.name: field required"],
  ];
  for (const [payload, expected] of cases) {
    const state = makeAppContext({
      fetchImpl: async () => response(422, payload),
    });
    await assert.rejects(
      () => state.sandbox.apiFetch("/api/example"),
      error => error.message.includes("HTTP 422") && error.message.includes(expected),
    );
  }
});

test("Emby restart button performs a single restart request", async () => {
  const urls = [];
  const state = makeAppContext({
    fetchImpl: async url => {
      urls.push(url);
      return response(200, {status: "ok"});
    },
  });
  await state.sandbox.restartEmby();
  assert.deepEqual(urls, ["/admin/api/emby/restart"]);
});

test("WebDAV omits an unchanged password and refills the mask after save", async () => {
  const bodies = [];
  const state = makeAppContext({
    fetchImpl: async (_url, init) => {
      bodies.push(JSON.parse(init.body));
      return response(200, {password: "********"});
    },
  });
  state.sandbox.loadWebdavConfig({
    enabled: true,
    url: "https://dav.test",
    username: "user",
    password: "********",
    remote_subdir: "backup",
    keep_count: 10,
  });
  await state.sandbox.saveWebdavConfig();
  assert.equal(Object.hasOwn(bodies[0], "password"), false);
  assert.equal(getElement(state,"wd_password").value, "********");
  assert.equal(run(state.context, "_webdavPasswordDirty"), false);

  getElement(state,"wd_password").value = "new-secret";
  state.sandbox.markWebdavPasswordDirty();
  await state.sandbox.saveWebdavConfig();
  assert.equal(bodies[1].password, "new-secret");
  assert.equal(getElement(state,"wd_password").value, "********");
  assert.equal(run(state.context, "_webdavPasswordDirty"), false);
});

test("WebDAV restore reports restart failures after a successful restore", async () => {
  const state = makeAppContext({
    fetchImpl: async () => response(200, {
      ok: true,
      restart: {p115: true, emby: false},
    }),
  });
  run(state.context, "loadConfig = function() {}; refreshBackupStatus = function() {};");
  await state.sandbox.restoreRemoteBackup("backup.zip");
  const toast = getElement(state, "toast");
  assert.match(toast.textContent, /配置已恢复/);
  assert.match(toast.textContent, /服务重启失败/);
  assert.match(toast.textContent, /Emby/);
  assert.match(toast.className, /error/);
});

test("WebDAV restore confirms service restart when all callbacks succeed", async () => {
  const state = makeAppContext({
    fetchImpl: async () => response(200, {
      ok: true,
      restart: {p115: true, emby: true},
    }),
  });
  run(state.context, "loadConfig = function() {}; refreshBackupStatus = function() {};");
  await state.sandbox.restoreRemoteBackup("backup.zip");
  assert.equal(getElement(state, "toast").textContent, "恢复成功，服务已重启");
});

test("sync start errors preserve the active progress monitor", async () => {
  const urls = [];
  const state = makeAppContext({
    fetchImpl: async url => {
      urls.push(url);
      return response(200, {status: "error", message: "同步正在进行中"});
    },
  });
  run(state.context, "_syncProgressTimer = {stop(){ globalThis.__syncMonitorStopped = true; }}");
  await state.sandbox.startFullSync();
  assert.deepEqual(urls, ["/api/sync/start"]);
  assert.equal(state.sandbox.__syncMonitorStopped, undefined);
});

test("sync network errors preserve the active progress monitor", async () => {
  const state = makeAppContext({
    fetchImpl: async () => {
      throw new Error("network down");
    },
  });
  run(state.context, "_syncProgressTimer = {stop(){ globalThis.__syncMonitorStopped = true; }}");
  await state.sandbox.startFullSync();
  assert.equal(state.sandbox.__syncMonitorStopped, undefined);
});

test("sync polling starts only after a successful start response", async t => {
  const urls = [];
  const state = makeAppContext({
    fetchImpl: async url => {
      urls.push(url);
      if (url === "/api/sync/start") {
        return response(200, {status: "started", message: "started"});
      }
      return response(200, {phase: "scanning", current: 1, total: 2, message: "scanning"});
    },
  });
  t.after(() => state.sandbox.stopPolling(run(state.context, "_syncProgressTimer")));
  await state.sandbox.startFullSync();
  await flush();
  assert.deepEqual(urls.slice(0, 2), ["/api/sync/start", "/api/sync/progress"]);
});

test("poll handle exposes its stopped state", async () => {
  const state = makeAppContext();
  const handle = state.sandbox.startPolling(async () => ({}), 1000);
  assert.equal(handle.stopped, false);
  handle.stop();
  assert.equal(handle.stopped, true);
});

test("poll handle marks itself stopped after repeated failures", async () => {
  const state = makeAppContext();
  let calls = 0;
  const handle = state.sandbox.startPolling(
    async () => {
      calls += 1;
      throw new Error("offline");
    },
    0,
    null,
    {maxFailures: 2},
  );
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.ok(calls >= 2);
  assert.equal(handle.stopped, true);
});

test("sync monitor recovers after its handle self-stops", async t => {
  const urls = [];
  const state = makeAppContext({
    fetchImpl: async url => {
      urls.push(url);
      return response(200, {phase: "scanning", current: 1, total: 2, message: "scanning"});
    },
  });
  run(state.context, "_syncProgressTimer = {stopped: true, stop(){}};");
  await state.sandbox.ensureSyncProgressPoll();
  await flush();
  const timer = run(state.context, "_syncProgressTimer");
  assert.ok(timer);
  assert.equal(timer.stopped, false);
  assert.ok(urls.includes("/api/sync/progress"));
  t.after(() => state.sandbox.stopPolling(timer));
});

test("loadConfig adopts an active sync task after page refresh", async t => {
  const urls = [];
  const config = {
    media_diag: false,
    emby: {},
    p115: {paths: []},
    checkin: {},
    webdav: {},
  };
  const state = makeAppContext({
    fetchImpl: async url => {
      urls.push(url);
      if (url === "/admin/api/config") {
        return response(200, config);
      }
      if (url === "/admin/api/autostart") {
        return response(200, {enabled: false});
      }
      if (url === "/admin/api/status") {
        return response(200, {
          emby: {running: false, enabled: false},
          p115: {running: false, enabled: false},
          version: "dev",
        });
      }
      if (url === "/admin/api/p115/status") {
        return response(200, {stats: {total_files: 0}, client_ready: false});
      }
      if (url === "/api/sync/progress") {
        return response(200, {phase: "scanning", current: 1, total: 2, message: "scanning"});
      }
      return response(200, {});
    },
  });
  state.sandbox.loadConfig();
  await flush();
  assert.ok(urls.includes("/api/sync/progress"));
  const timer = run(state.context, "_syncProgressTimer");
  assert.ok(timer);
  assert.equal(timer.stopped, false);
  t.after(() => state.sandbox.stopPolling(timer));
});

test("closing QR login ignores a late QR response", async () => {
  const qr = deferred();
  const urls = [];
  const state = makeAppContext({
    fetchImpl: async url => {
      urls.push(url);
      return qr.promise;
    },
  });
  const pending = state.sandbox.showQrcodeLogin();
  state.sandbox.closeQrcode();
  qr.resolve(response(200, {qrcode: "data:image/png;base64,abc", payload: {uid: "1"}}));
  await pending;
  await flush();
  assert.deepEqual(urls, ["/api/qrcode?app=alipaymini"]);
  assert.equal(run(state.context, "_qrcodeTimer"), null);
});

test("reopening QR login clears the previous cookie", async () => {
  const qr = deferred();
  const state = makeAppContext({fetchImpl: async () => qr.promise});
  getElement(state,"qrcodeCookie").value = "old-cookie";
  getElement(state,"qrcodeCopyBtn").style.display = "inline-block";
  const pending = state.sandbox.showQrcodeLogin();
  assert.equal(getElement(state,"qrcodeCookie").value, "");
  assert.equal(getElement(state,"qrcodeCopyBtn").style.display, "none");
  state.sandbox.closeQrcode();
  qr.resolve(response(200, {qrcode: "data:image/png;base64,abc", payload: {}}));
  await pending;
});

test("QR error status stops polling", async () => {
  const state = makeAppContext({
    fetchImpl: async url => {
      if (url.startsWith("/api/qrcode?")) {
        return response(200, {qrcode: "data:image/png;base64,abc", payload: {uid: "1"}});
      }
      return response(200, {status: "error", message: "login failed"});
    },
  });
  await state.sandbox.showQrcodeLogin();
  await flush();
  assert.equal(run(state.context, "_qrcodeTimer"), null);
  assert.equal(getElement(state, "qrcodeStatus").textContent, "login failed");
});

test("a late QR check from an old session keeps the new timer", async t => {
  const oldCheck = deferred();
  let qrCalls = 0;
  let checkCalls = 0;
  const state = makeAppContext({
    fetchImpl: async url => {
      if (url.startsWith("/api/qrcode?")) {
        qrCalls += 1;
        return response(200, {qrcode: "data:image/png;base64,abc", payload: {uid: String(qrCalls)}});
      }
      checkCalls += 1;
      if (checkCalls === 1) {
        return oldCheck.promise;
      }
      return response(200, {status: "waiting"});
    },
  });
  t.after(() => state.sandbox.closeQrcode());
  await state.sandbox.showQrcodeLogin();
  await flush();
  await state.sandbox.showQrcodeLogin();
  await flush();
  const currentTimer = run(state.context, "_qrcodeTimer");
  assert.ok(currentTimer);
  oldCheck.resolve(response(200, {status: "success", cookie: "old-cookie"}));
  await flush();
  assert.equal(run(state.context, "_qrcodeTimer"), currentTimer);
});

test("path mapping redraw preserves typed values and stable ids", () => {

  const state = makeAppContext();
  run(state.context, "p115Paths = [{id: 1, from: '/old', to: 'D:/old', enabled: true}]; _pathMappingSeq = 1;");
  state.sandbox.renderPathMappings(false);
  const row = new MockElement();
  row.dataset.id = "1";
  const from = new MockElement("input");
  const to = new MockElement("input");
  const enabled = new MockElement("input");
  from.value = "/typed";
  to.value = "D:/typed";
  enabled.checked = false;
  const fields = {".path-from": from, ".path-to": to, ".path-cb": enabled};
  row.querySelector = selector => fields[selector];
  getElement(state,"p115_pathMappings").children = [row];
  state.sandbox.addPathMapping();
  const mappings = run(state.context, "p115Paths");
  assert.equal(mappings.length, 2);
  assert.equal(mappings[0].id, 1);
  assert.equal(mappings[0].from, "/typed");
  assert.equal(mappings[0].to, "D:/typed");
  assert.equal(mappings[0].enabled, false);
  assert.equal(mappings[1].id, 2);
});

test("directory browser accepts only the latest async result", async () => {
  const first = deferred();
  const second = deferred();
  const state = makeAppContext({
    fetchImpl: async url => url.includes("pid=1") ? first.promise : second.promise,
  });
  getElement(state,"browserModal").style.display = "flex";
  const firstLoad = state.sandbox.loadBrowserDir("1", "/a");
  const secondLoad = state.sandbox.loadBrowserDir("2", "/b");
  second.resolve(response(200, {path: "/b", items: [{id: "2", name: "B", is_dir: true}]}));
  await secondLoad;
  first.resolve(response(200, {path: "/a", items: [{id: "1", name: "A", is_dir: true}]}));
  await firstLoad;
  assert.equal(getElement(state,"browserPath").textContent, "/b");
  assert.equal(run(state.context, "_browsePath"), "/b");
  assert.match(getElement(state,"browserList").innerHTML, /B/);
  assert.doesNotMatch(getElement(state,"browserList").innerHTML, /A/);
});

test("directory errors are rendered as plain text", async () => {
  const state = makeAppContext({
    fetchImpl: async () => {
      throw new Error("<img src=x onerror=alert(1)>");
    },
  });
  getElement(state,"browserModal").style.display = "flex";
  await state.sandbox.loadBrowserDir("1", "/a");
  assert.match(getElement(state,"browserList").textContent, /<img src=x/);
  assert.doesNotMatch(getElement(state,"browserList").innerHTML, /<img/);
});

test("overview polling survives repeated failures and resumes on visibility and online", async t => {
  const state = makeAppContext();
  let calls = 0;
  const handle = state.sandbox.startPolling(
    async () => {
      calls += 1;
      throw new Error("offline");
    },
    0,
    null,
    {maxFailures: 0},
  );
  t.after(() => handle.stop());
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.ok(calls > 10, `expected more than 10 attempts, got ${calls}`);
  const beforeVisibility = calls;
  state.document.hidden = true;
  state.document.emit("visibilitychange");
  state.document.hidden = false;
  state.document.emit("visibilitychange");
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(calls > beforeVisibility);
  const beforeOnline = calls;
  state.sandbox.emit("online");
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(calls > beforeOnline);
});

test("STRM next page follows page times page size", async () => {
  for (const [total, expected] of [[20, false], [21, true]]) {
    const state = makeAppContext({
      fetchImpl: async () => response(200, {
        items: [{file_name: "movie.strm", file_size: 1, file_type: "strm", created_at: ""}],
        total,
        page: 1,
        page_size: 20,
      }),
    });
    await state.sandbox.listStrmFiles(1);
    assert.equal(getElement(state,"strmTable").innerHTML.includes("下一页"), expected);
  }
});
