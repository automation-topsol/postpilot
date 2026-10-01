// Runs webapp/Code.js against fake Apps Script services; see test_webapp_server.py.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.join(__dirname, "..", "webapp");
const input = JSON.parse(fs.readFileSync(0, "utf8"));

// ---- fakes --------------------------------------------------------------
function fakeSheet(name, values) {
  const formats = {};
  return {
    name,
    values,
    formats,
    getDataRange: () => ({ getDisplayValues: () => values.map(r => r.map(String)) }),
    getLastRow: () => {
      for (let i = values.length; i > 0; i--) if (values[i - 1].some(c => String(c) !== "")) return i;
      return 0;
    },
    getRange: (row, col, rows, cols) => ({
      setNumberFormat: f => { for (let r = 0; r < rows; r++) formats[row + r] = f; },
      setValues: data => {
        if (data.length !== rows || data.some(d => d.length !== cols)) throw new Error("shape mismatch");
        data.forEach((d, r) => {
          while (values.length < row + r) values.push([]);
          values[row + r - 1] = d.slice();
        });
      },
    }),
  };
}

const sheets = Object.fromEntries(Object.entries(input.sheets).map(([n, v]) => [n, fakeSheet(n, v)]));
const book = { getName: () => "Fake Calendar", getSheetByName: n => sheets[n] || null };

const context = {
  console,
  SpreadsheetApp: { getActiveSpreadsheet: () => book, flush: () => {} },
  PropertiesService: { getScriptProperties: () => ({ getProperty: () => null }) },
  Session: { getEffectiveUser: () => ({ getEmail: () => "operator@example.com" }) },
  ScriptApp: { getOAuthToken: () => "fake-token" },
  LockService: { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) },
  HtmlService: { createHtmlOutputFromFile: n => ({ getContent: () => `<${n}>` }) },
  Utilities: {
    // Asia/Karachi is UTC+5 with no DST; the parity test covers real zones.
    parseDate: (text, tz) => {
      if (tz !== "Asia/Karachi") throw new Error("fake only knows Asia/Karachi");
      const [d, t] = text.split(" ");
      const [y, m, day] = d.split("-").map(Number);
      const [hh, mm, ss] = t.split(":").map(Number);
      return new Date(Date.UTC(y, m - 1, day, hh - 5, mm, ss));
    },
  },
};
vm.createContext(context);
for (const file of ["Logic.js", "Code.js"]) {
  vm.runInContext(fs.readFileSync(path.join(root, file), "utf8"), context, { filename: file });
}

vm.runInContext(`Date.now = () => ${input.now_ms};`, context);

const out = {};
out.status = context.getStatus();
out.review = context.reviewPlanText(input.plan, input.files);
const ready = out.review.reviews.filter(r => r.importable).map(r => r.post);
out.first = context.appendPosts(ready);
out.second = context.appendPosts(ready); // importing the same plan twice
out.upload = context.getUploadContext();
out.sheets = Object.fromEntries(Object.entries(sheets).map(([n, s]) => [n, { values: s.values, formats: s.formats }]));
process.stdout.write(JSON.stringify(out));
