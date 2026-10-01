/**
 * PostPilot importer, hosted by Google Apps Script — the server side.
 *
 * The same job as `postpilot ui`, without a terminal: review a weekly plan
 * against the live Sheet, then add rows exactly as a person would type them
 * (user columns only, `ID` blank). It is bound to the Sheet, deployed with
 * access "Only myself", and runs as the operator.
 *
 * What it must never do, for the same reasons as the local importer: write
 * `_State` or `_Log`, assign IDs, touch tool-owned columns, or publish. The
 * next scheduled `sync` takes the rows from there, exactly as it does for
 * hand-typed ones. Uploads go browser -> Drive directly (see Index.html), so a
 * large video never passes through Apps Script's request-size limits.
 */

var BRANDS_TAB = "_Brands";

function doGet() {
  return HtmlService.createHtmlOutputFromFile("Index")
    .setTitle("PostPilot Importer")
    .addMetaTag("viewport", "width=device-width, initial-scale=1");
}

/** Text of another project file, for the page to embed (the plan template). */
function fileText_(name) {
  return HtmlService.createHtmlOutputFromFile(name).getContent();
}

function timezone_() {
  return PropertiesService.getScriptProperties().getProperty("TIMEZONE") || "Asia/Karachi";
}

/** Wall-clock time in `tz` -> epoch ms; the one platform-specific piece Logic.js needs. */
function zonedToUtc_(y, m, d, hh, mm, ss, tz) {
  function p(n) { return (n < 10 ? "0" : "") + n; }
  var text = y + "-" + p(m) + "-" + p(d) + " " + p(hh) + ":" + p(mm) + ":" + p(ss);
  return Utilities.parseDate(text, tz, "yyyy-MM-dd HH:mm:ss").getTime();
}

/** One read of `_Brands` and of every active brand tab: brands + duplicate keys. */
function context_() {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  var tz = timezone_();
  var brandsSheet = book.getSheetByName(BRANDS_TAB);
  if (!brandsSheet) throw new Error("the Sheet has no _Brands tab");
  var values = brandsSheet.getDataRange().getDisplayValues();
  var brands = PP.parseBrands(values[0] || [], values.slice(1));

  var existing = {};
  var tabs = {};
  Object.keys(brands).forEach(function (slug) {
    var sheet = book.getSheetByName(slug);
    if (!sheet) return;
    var rows = sheet.getDataRange().getDisplayValues();
    var headers = (rows[0] || []).map(function (h) { return String(h).trim(); });
    tabs[slug] = { sheet: sheet, headers: headers };
    PP.existingKeys(brands[slug], headers, rows.slice(1), tz, zonedToUtc_, existing);
  });
  return { book: book, tz: tz, brands: brands, existing: existing, tabs: tabs };
}

function review_(ctx, posts, files) {
  return PP.reviewPlan(posts, ctx.brands, files, ctx.existing, ctx.tz, Date.now(), zonedToUtc_);
}

// --------------------------------------------------------------------------
// Called from the page with google.script.run
// --------------------------------------------------------------------------
function getStatus() {
  var out = { timezone: timezone_(), brands: [], sheet: "", user: "", error: "", template: "" };
  try {
    out.template = fileText_("PlanTemplate");
    var ctx = context_();
    out.sheet = ctx.book.getName();
    out.brands = Object.keys(ctx.brands).map(function (slug) {
      var b = ctx.brands[slug];
      return { slug: slug, name: b.name, platforms: b.enabled_platforms };
    });
    out.user = Session.getEffectiveUser().getEmail();
  } catch (e) {
    out.error = "cannot read the Sheet: " + e.message;
  }
  return out;
}

function reviewPlanText(text, files) {
  var parsed = PP.parsePlan(text);
  return { problems: parsed.problems, reviews: review_(context_(), parsed.posts, files) };
}

function reviewPosts(posts, files) {
  return { problems: [], reviews: review_(context_(), posts, files) };
}

/**
 * What the browser needs to upload straight to Drive: a token for the
 * operator (this deployment is "Only myself", so nobody else can call it)
 * and each brand's folder.
 */
function getUploadContext() {
  var ctx = context_();
  var folders = {};
  Object.keys(ctx.brands).forEach(function (slug) { folders[slug] = ctx.brands[slug].drive_folder_id; });
  return { token: ScriptApp.getOAuthToken(), folders: folders };
}

/**
 * Append the importable posts, one write per brand tab.
 *
 * `posts` carry their media already renamed to what is now in Drive. Every
 * post is reviewed again here against the Sheet as it is NOW, so a plan
 * imported twice, or a row someone typed meanwhile, is still caught.
 */
function appendPosts(posts) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var ctx = context_();
    var names = [];
    posts.forEach(function (p) { names = names.concat(p.media); });
    var reviews = review_(ctx, posts, names);

    var result = { rows_added: {}, skipped: [], errors: [] };
    var byBrand = {};
    reviews.forEach(function (r) {
      var p = r.post;
      var label = (p.brand + " " + p.date + " " + p.time).trim();
      if (!p.include) { result.skipped.push(label + ": removed in the preview"); return; }
      if (!r.importable) {
        result.skipped.push(label + ": " + (r.duplicate_of ? "already in the Sheet (" + r.duplicate_of + ")" : "has errors"));
        return;
      }
      (byBrand[p.brand] = byBrand[p.brand] || []).push(p);
    });

    Object.keys(byBrand).forEach(function (slug) {
      var tab = ctx.tabs[slug];
      if (!tab) { result.errors.push(slug + ": brand tab is missing — run `postpilot sheet init`"); return; }
      try {
        var rows = byBrand[slug].map(function (p) { return PP.rowFor(p, tab.headers); });
        var range = tab.sheet.getRange(tab.sheet.getLastRow() + 1, 1, rows.length, tab.headers.length);
        // Plain text, like the local importer's RAW append: "2026-09-28" and
        // "18:30" must stay the text `sync` parses, not become locale dates.
        range.setNumberFormat("@");
        range.setValues(rows);
        SpreadsheetApp.flush();
        result.rows_added[slug] = rows.length;
      } catch (e) {
        result.errors.push(slug + ": could not add rows to the Sheet — " + e.message);
      }
    });
    return result;
  } finally {
    lock.releaseLock();
  }
}
