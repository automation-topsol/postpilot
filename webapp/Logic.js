/**
 * The weekly plan, in JavaScript: parse the Markdown, review every post.
 *
 * A line-for-line port of `postpilot/plan.py` plus the parts of
 * `postpilot/sheets/parse.py` it relies on, so the hosted preview and the
 * Python `sync` agree about what is valid. `tests/test_webapp_parity.py` runs
 * both on the same plans and fails on any difference — change one, change
 * the other, and that test tells you if you forgot.
 *
 * Pure functions only, no Apps Script globals: the one thing that differs
 * between Apps Script and Node (turning a Pakistan wall-clock time into UTC)
 * is passed in as `zonedToUtc(y, m, d, hh, mm, ss, tzName) -> epoch ms`.
 */

var PP = (function () {
  var PLATFORMS = ["FB", "IG", "LI"];
  var LABEL = { FB: "Facebook", IG: "Instagram", LI: "LinkedIn" };
  var TYPES = ["image", "carousel", "reel", "text"];
  var MEDIA_COUNTS = { image: [1, 1], carousel: [2, 10], reel: [1, 1], text: [0, 0] };
  var TEXT_CAPABLE = { FB: true, LI: true };
  var LINK_CAPABLE = { FB: true, LI: true };
  var TRUEISH = ["true", "yes", "y", "1", "on", "✓", "x"];
  var FALSEISH = ["false", "no", "n", "0", "off", ""];

  var BRAND_HEADERS = [
    "ID", "Date", "Time", "Platforms", "Type", "Media", "Caption",
    "Caption (Facebook)", "Caption (Instagram)", "Caption (LinkedIn)", "Link", "Action",
    "Status", "Published URLs", "Error", "Attempts", "Last Run", "Notes",
  ];

  var SIMPLE_FIELDS = ["brand", "platforms", "type", "media", "link", "date", "time"];
  var CAPTION_KEYS = {
    "caption": "caption",
    "caption fb": "caption_facebook",
    "caption facebook": "caption_facebook",
    "caption ig": "caption_instagram",
    "caption instagram": "caption_instagram",
    "caption li": "caption_linkedin",
    "caption linkedin": "caption_linkedin",
  };
  var IMAGE_EXTS = ["jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp", "tif", "tiff"];
  var VIDEO_EXTS = ["mp4", "mov", "m4v", "webm", "avi", "mkv"];

  var HEADING = /^##\s+(.*)$/;
  var FIELD = new RegExp("^(" + SIMPLE_FIELDS.join("|") + ")\\s*:\\s*(.*)$", "i");
  var CAPTION = /^(caption(?:\s+(?:fb|facebook|ig|instagram|li|linkedin))?)\s*:\s*(.*)$/i;
  var DATE_IN_HEADING = /(\d{4}-\d{2}-\d{2})/;
  var TIME_IN_HEADING = /(\d{1,2}[:.]\d{2}(?:\s*[AaPp][Mm])?|\d{1,2}\s*[AaPp][Mm])/;
  var DRIVE_ID = /(?:\/d\/|\/file\/d\/|[?&]id=)([A-Za-z0-9_-]{20,})/;

  var MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  var MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december"];
  var WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  var WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

  // ------------------------------------------------------------------------
  // Small helpers that mirror Python behaviour
  // ------------------------------------------------------------------------
  function clean(value) {
    return String(value == null ? "" : value).replace(/ /g, " ").trim();
  }

  function fold(s) {
    return String(s).toLowerCase();
  }

  function pyRepr(s) {
    // Python's repr() of a str, which the error messages use: single quotes
    // unless the text contains one and no double quote.
    s = String(s);
    if (s.indexOf("'") >= 0 && s.indexOf('"') < 0) return '"' + s + '"';
    return "'" + s.replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
  }

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function parseBool(value, dflt) {
    var text = fold(clean(value));
    if (TRUEISH.indexOf(text) >= 0) return true;
    if (FALSEISH.indexOf(text) >= 0) return false;
    return dflt;
  }

  function headerIndex(headers, name) {
    var wanted = fold(name.trim());
    for (var i = 0; i < headers.length; i++) {
      if (fold(String(headers[i]).trim()) === wanted) return i;
    }
    return -1;
  }

  function parsePlatforms(value) {
    var platforms = [];
    var unknown = [];
    clean(value).split(/[,\s/|]+/).forEach(function (token) {
      if (!token) return;
      var code = token.toUpperCase();
      if (PLATFORMS.indexOf(code) < 0) {
        unknown.push(token);
        return;
      }
      if (platforms.indexOf(code) < 0) platforms.push(code);
    });
    return { platforms: platforms, unknown: unknown };
  }

  function parseMedia(value) {
    var items = [];
    clean(value).split(/[,\n]+/).forEach(function (raw) {
      var item = raw.trim();
      if (!item) return;
      var m = DRIVE_ID.exec(item);
      items.push(m ? m[1] : item);
    });
    return items;
  }

  function validDate(y, m, d) {
    if (m < 1 || m > 12 || d < 1) return false;
    var days = [31, (y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0)) ? 29 : 28,
      31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return d <= days[m - 1];
  }

  function monthOf(word) {
    var w = fold(word);
    var i = MONTHS.indexOf(w);
    if (i < 0) i = MONTH_NAMES.indexOf(w);
    return i < 0 ? 0 : i + 1;
  }

  /** Python's _DATE_FORMATS, tried in the same order. Returns [y, m, d] or null. */
  function strpDate(text) {
    var m;
    if ((m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(text)) || (m = /^(\d{4})\/(\d{1,2})\/(\d{1,2})$/.exec(text))) {
      var y = +m[1], mo = +m[2], d = +m[3];
      return validDate(y, mo, d) ? [y, mo, d] : null;
    }
    if ((m = /^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$/.exec(text))) {
      var mo2 = monthOf(m[2]);
      return mo2 && validDate(+m[3], mo2, +m[1]) ? [+m[3], mo2, +m[1]] : null;
    }
    if ((m = /^([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})$/.exec(text))) {
      var mo3 = monthOf(m[1]);
      return mo3 && validDate(+m[3], mo3, +m[2]) ? [+m[3], mo3, +m[2]] : null;
    }
    return null;
  }

  /** Python's _TIME_FORMATS on upper-cased, dot-stripped text. Returns [h, m, s] or null. */
  function strpTime(text) {
    var m;
    if ((m = /^(\d{1,2}):(\d{1,2})$/.exec(text))) {
      return +m[1] <= 23 && +m[2] <= 59 ? [+m[1], +m[2], 0] : null;
    }
    if ((m = /^(\d{1,2}):(\d{1,2}):(\d{1,2})$/.exec(text))) {
      return +m[1] <= 23 && +m[2] <= 59 && +m[3] <= 61 ? [+m[1], +m[2], Math.min(+m[3], 59)] : null;
    }
    // strptime turns a space in the format into \s+, so "6:30PM" needs the
    // no-space format and "6PM" matches neither "%I %p" form.
    if ((m = /^(\d{1,2}):(\d{1,2})\s*(AM|PM)$/.exec(text)) || (m = /^(\d{1,2})()\s+(AM|PM)$/.exec(text))) {
      var h = +m[1], mi = m[2] === "" ? 0 : +m[2];
      if (h < 1 || h > 12 || mi > 59) return null;
      return [(h % 12) + (m[3] === "PM" ? 12 : 0), mi, 0];
    }
    return null;
  }

  /** Mirrors parse_schedule: { at: epoch ms | null, error, warning }. */
  function parseSchedule(rawDate, rawTime, tz, zonedToUtc) {
    var dateText = clean(rawDate), timeText = clean(rawTime);
    if (!dateText) return { at: null, error: null, warning: null };

    var warning = null;
    var date = strpDate(dateText);

    var m;
    if (!date && (m = /^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$/.exec(dateText))) {
      var a = +m[1], b = +m[2], year = +m[3], day, month;
      if (year < 100) year += 2000;
      if (a > 12 && b <= 12) { day = a; month = b; }
      else if (b > 12 && a <= 12) { day = b; month = a; }
      else {
        day = a; month = b;
        warning = "read date " + pyRepr(dateText) + " as " + pad2(day) + "/" + pad2(month) +
          " (day-first); use YYYY-MM-DD to be sure";
      }
      if (!validDate(year, month, day)) {
        return { at: null, error: "unreadable date " + pyRepr(dateText), warning: null };
      }
      date = [year, month, day];
    }

    if (!date) {
      return { at: null, error: "unreadable date " + pyRepr(dateText) + " — use YYYY-MM-DD", warning: null };
    }

    var time = [0, 0, 0];
    if (timeText) {
      time = strpTime(timeText.toUpperCase().replace(/\./g, ""));
      if (!time) {
        return { at: null, error: "unreadable time " + pyRepr(timeText) + " — use HH:MM (24-hour)", warning: warning };
      }
    }

    return { at: zonedToUtc(date[0], date[1], date[2], time[0], time[1], time[2], tz), error: null, warning: warning };
  }

  // ------------------------------------------------------------------------
  // _Brands
  // ------------------------------------------------------------------------
  /** Active brands keyed by slug, from `_Brands` display values. */
  function parseBrands(headers, rows) {
    function cell(row, name) {
      var i = headerIndex(headers, name);
      return i >= 0 && i < row.length ? clean(row[i]) : "";
    }
    var brands = {};
    rows.forEach(function (row) {
      if (!row.some(function (c) { return clean(c); })) return;
      var slug = fold(cell(row, "Slug").trim());
      if (!/^[a-z0-9][a-z0-9-]*$/.test(slug)) return; // sync reports it; we just skip
      if (!parseBool(cell(row, "Active"), true)) return;
      brands[slug] = {
        name: cell(row, "Brand Name") || slug,
        slug: slug,
        enabled_platforms: parsePlatforms(cell(row, "Enabled Platforms")).platforms,
        facebook_page_id: cell(row, "Facebook Page ID"),
        instagram_user_id: cell(row, "Instagram User ID"),
        linkedin_org_urn: cell(row, "LinkedIn Org URN"),
        drive_folder_id: cell(row, "Drive Folder ID"),
        default_hashtags: cell(row, "Default Hashtags"),
      };
    });
    return brands;
  }

  function platformTarget(brand, platform) {
    return { FB: brand.facebook_page_id, IG: brand.instagram_user_id, LI: brand.linkedin_org_urn }[platform] || "";
  }

  // ------------------------------------------------------------------------
  // Brand-tab rows (parse_post + validate)
  // ------------------------------------------------------------------------
  function parsePost(brand, headers, row, tz, zonedToUtc) {
    function cell(name) {
      var i = headerIndex(headers, name);
      return i >= 0 && i < row.length ? clean(row[i]) : "";
    }
    if (!row.some(function (c) { return clean(c); })) return null;

    var sched = parseSchedule(cell("Date"), cell("Time"), tz, zonedToUtc);
    var rawType = cell("Type");
    var type = TYPES.indexOf(fold(rawType).trim()) >= 0 ? fold(rawType).trim() : null;
    var plat = parsePlatforms(cell("Platforms"));

    var post = {
      post_id: cell("ID"),
      brand_slug: brand.slug,
      scheduled_at: sched.at,
      raw_date: cell("Date"),
      platforms: plat.platforms,
      post_type: type,
      media: parseMedia(cell("Media")),
      caption: cell("Caption"),
      caption_facebook: cell("Caption (Facebook)"),
      caption_instagram: cell("Caption (Instagram)"),
      caption_linkedin: cell("Caption (LinkedIn)"),
      link: cell("Link"),
      issues: [],
      warnings: [],
    };
    if (sched.warning) post.warnings.push(sched.warning);

    var issues = post.issues;
    function issue(message, platform) { issues.push({ platform: platform || null, message: message }); }

    if (sched.error) issue(sched.error);
    if (post.scheduled_at === null && !post.raw_date.trim()) return post; // draft

    if (type === null) {
      issue(rawType ? "Type " + pyRepr(rawType) + " is not one of: " + TYPES.join(", ") : "Type is empty");
    }
    if (plat.unknown.length) issue("unknown platform(s): " + plat.unknown.join(", "));
    if (!post.platforms.length) issue("Platforms is empty");

    post.platforms.forEach(function (p) {
      if (brand.enabled_platforms.indexOf(p) < 0) {
        issue(LABEL[p] + " is not in this brand's Enabled Platforms", p);
      } else if (!platformTarget(brand, p)) {
        issue("brand setting: " + brand.name + " has no " + LABEL[p] + " ID/URN in _Brands " +
          "(nothing wrong with this row)", p);
      }
    });

    if (type !== null) {
      var low = MEDIA_COUNTS[type][0], high = MEDIA_COUNTS[type][1], count = post.media.length;
      if (count < low || count > high) {
        var expected = low === high ? "exactly " + low : low + "-" + high;
        issue(type + " needs " + expected + " media file(s), found " + count);
      }
      if (type === "text") {
        post.platforms.forEach(function (p) {
          if (!TEXT_CAPABLE[p]) issue(LABEL[p] + " cannot post without media", p);
        });
      }
    }

    if (!(post.caption || post.caption_facebook || post.caption_instagram || post.caption_linkedin)) {
      if (type === "text") issue("a text post needs a Caption");
      else post.warnings.push("no caption — posting media with no text");
    }

    if (post.link && !/^https?:\/\//.test(post.link)) {
      issue("Link " + pyRepr(post.link) + " must start with http:// or https://");
    }
    return post;
  }

  function captionFor(post, platform, brand) {
    var override = { FB: post.caption_facebook, IG: post.caption_instagram, LI: post.caption_linkedin }[platform];
    var text = (override || post.caption).trim();
    if (LINK_CAPABLE[platform] && post.link) text = (text + "\n\n" + post.link).trim();
    if (platform === "IG" && brand.default_hashtags && text.indexOf("#") < 0) {
      text = (text + "\n\n" + brand.default_hashtags.trim()).trim();
    }
    return text;
  }

  // ------------------------------------------------------------------------
  // The plan (plan.py)
  // ------------------------------------------------------------------------
  function newPost(heading, brand) {
    return {
      heading: heading || "", brand: brand || "", date: "", time: "", platforms: "", type: "",
      media: [], link: "", caption: "", caption_facebook: "", caption_instagram: "",
      caption_linkedin: "", include: true,
    };
  }

  function sheetValues(post) {
    return {
      "Date": post.date,
      "Time": post.time,
      "Platforms": post.platforms,
      "Type": fold(post.type),
      "Media": post.media.join(", "),
      "Caption": post.caption,
      "Caption (Facebook)": post.caption_facebook,
      "Caption (Instagram)": post.caption_instagram,
      "Caption (LinkedIn)": post.caption_linkedin,
      "Link": post.link,
    };
  }

  /** A row in the order of `headers` — the real tab's, which humans reorder. */
  function rowFor(post, headers) {
    var values = sheetValues(post);
    var row = headers.map(function () { return ""; });
    Object.keys(values).forEach(function (name) {
      var i = headerIndex(headers, name);
      if (i >= 0) row[i] = values[name];
    });
    return row;
  }

  function normaliseTime(text) {
    var cleaned = String(text).trim().toUpperCase().replace(/\./g, ":");
    var m;
    if ((m = /^(\d{1,2}):(\d{1,2})$/.exec(cleaned)) && +m[1] <= 23 && +m[2] <= 59) {
      return pad2(+m[1]) + ":" + pad2(+m[2]);
    }
    if ((m = /^(\d{1,2}):(\d{1,2})\s*(AM|PM)$/.exec(cleaned)) || (m = /^(\d{1,2})()\s*(AM|PM)$/.exec(cleaned))) {
      var h = +m[1], mi = m[2] === "" ? 0 : +m[2];
      if (h >= 1 && h <= 12 && mi <= 59) return pad2((h % 12) + (m[3] === "PM" ? 12 : 0)) + ":" + pad2(mi);
    }
    return text;
  }

  function parsePlan(text) {
    var problems = [];
    var defaultBrand = "";
    var posts = [];
    var current = null;
    var captionAttr = null;
    var captionLines = [];

    text = String(text).replace(/<!--[\s\S]*?-->/g, "");

    function closeCaption() {
      if (current !== null && captionAttr !== null) current[captionAttr] = captionLines.join("\n").trim();
      captionAttr = null;
      captionLines = [];
    }
    function closePost() {
      closeCaption();
      if (current !== null) posts.push(current);
      current = null;
    }

    var lines = text.split(/\r\n|\r|\n/);
    if (lines.length && lines[lines.length - 1] === "") lines.pop();

    lines.forEach(function (raw) {
      var line = raw.replace(/\s+$/, "");
      var m;

      if ((m = HEADING.exec(line))) {
        closePost();
        var heading = m[1].trim();
        current = newPost(heading, defaultBrand);
        var d = DATE_IN_HEADING.exec(heading);
        if (d) {
          current.date = d[1];
          var rest = heading.slice(d.index + d[0].length);
          var t = TIME_IN_HEADING.exec(rest);
          if (t) current.time = t[1].replace(/\./g, ":").trim();
        }
        return;
      }

      if (line.trim() === "---") {
        closePost();
        return;
      }

      if (current === null) {
        if ((m = FIELD.exec(line.trim())) && fold(m[1]) === "brand") defaultBrand = fold(m[2].trim());
        return;
      }

      if ((m = CAPTION.exec(line.trim()))) {
        closeCaption();
        captionAttr = CAPTION_KEYS[fold(m[1]).replace(/\s+/g, " ")];
        captionLines = m[2].trim() ? [m[2]] : [];
        return;
      }

      if (captionAttr !== null) {
        captionLines.push(line);
        return;
      }

      if ((m = FIELD.exec(line.trim()))) {
        var name = fold(m[1]), value = m[2].trim();
        if (name === "media") {
          current.media = value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        } else if (name === "brand") {
          current.brand = fold(value);
        } else {
          current[name] = value;
        }
      }
    });
    closePost();

    posts.forEach(function (p) { p.time = normaliseTime(p.time); });
    if (!posts.length) {
      problems.push("no posts found — each post starts with a '## YYYY-MM-DD HH:MM' heading");
    }
    return { posts: posts, problems: problems };
  }

  function mediaKind(name) {
    var base = String(name).split("/").pop();
    var dot = base.lastIndexOf(".");
    var ext = dot > 0 ? fold(base.slice(dot + 1)) : "";
    if (IMAGE_EXTS.indexOf(ext) >= 0) return "image";
    if (VIDEO_EXTS.indexOf(ext) >= 0) return "video";
    return "";
  }

  function weekdayMismatch(post) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(post.date);
    if (!m || !validDate(+m[1], +m[2], +m[3])) return "";
    var words = fold(post.heading).match(/[a-z]+/g) || [];
    var named = null;
    for (var i = 0; i < words.length; i++) {
      if (words[i].length >= 3 && WEEKDAYS.indexOf(words[i].slice(0, 3)) >= 0) { named = words[i].slice(0, 3); break; }
    }
    var jsDay = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).getUTCDay(); // 0 = Sunday
    var index = (jsDay + 6) % 7; // 0 = Monday, like Python
    if (named && named !== WEEKDAYS[index]) {
      return "heading says " + named.charAt(0).toUpperCase() + named.slice(1) + " but " + post.date +
        " is a " + WEEKDAY_NAMES[index] + " — the date is what counts";
    }
    return "";
  }

  function reviewPost(post, brands, available, tz, now, zonedToUtc) {
    var review = {
      post: post, errors: [], platform_errors: {}, warnings: [], captions: {},
      duplicate_of: "", scheduled_at: null,
    };
    var brand = brands[post.brand];
    if (!post.brand) {
      review.errors.push("no brand — add 'brand: <slug>' to the post or the top of the plan");
      return review;
    }
    if (!brand) {
      review.errors.push("unknown brand " + pyRepr(post.brand) + " — known: " + Object.keys(brands).sort().join(", "));
      return review;
    }
    if (!post.date) {
      review.errors.push("no date — put 'YYYY-MM-DD HH:MM' in the heading");
      return review;
    }

    var parsed = parsePost(brand, BRAND_HEADERS, rowFor(post, BRAND_HEADERS), tz, zonedToUtc);
    if (parsed === null) {
      review.errors.push("the post is empty");
      return review;
    }

    review.scheduled_at = parsed.scheduled_at;
    review.warnings = review.warnings.concat(parsed.warnings);
    parsed.issues.forEach(function (i) {
      if (i.platform === null) review.errors.push(i.message);
      else (review.platform_errors[i.platform] = review.platform_errors[i.platform] || []).push(i.message);
    });

    if (parsed.scheduled_at !== null && parsed.scheduled_at <= now) {
      review.errors.push("time is in the past — change it (a few minutes from now posts on the next run)");
    }

    post.media.forEach(function (name) {
      if (available.indexOf(name) < 0) review.errors.push("file " + pyRepr(name) + " is not in the files you dropped");
    });

    var kinds = post.media.map(mediaKind);
    if (kinds.indexOf("") >= 0) {
      review.errors.push("not an image or video: " + post.media.filter(function (n) { return !mediaKind(n); }).join(", "));
    }
    if ((parsed.post_type === "image" || parsed.post_type === "carousel") && kinds.indexOf("video") >= 0) {
      review.errors.push(parsed.post_type + " takes images only — use type 'reel' for video");
    }
    if (parsed.post_type === "reel" && kinds.indexOf("image") >= 0) {
      review.errors.push("reel takes one video, not an image");
    }

    parsed.platforms.forEach(function (p) { review.captions[p] = captionFor(parsed, p, brand); });

    var mismatch = weekdayMismatch(post);
    if (mismatch) review.warnings.push(mismatch);
    return review;
  }

  function importable(review) {
    if (review.errors.length || review.duplicate_of) return false;
    return Object.keys(review.captions).some(function (p) { return !review.platform_errors[p]; });
  }

  /** Full caption, not a prefix — adversarial defect #2. */
  function captionKey(brand, at, captions) {
    var text = captions.filter(Boolean).map(function (c) {
      return fold(c.split(/\s+/).filter(Boolean).join(" "));
    }).join(" ");
    return JSON.stringify([brand, at, text]);
  }

  function postKey(p, slug, at) {
    return captionKey(slug, at, [p.caption, p.caption_facebook, p.caption_instagram, p.caption_linkedin]);
  }

  /** Keys for rows already in a brand tab -> their label (ID or row number). */
  function existingKeys(brand, headers, rows, tz, zonedToUtc, into) {
    into = into || {};
    rows.forEach(function (row, index) {
      var p = parsePost(brand, headers, row, tz, zonedToUtc);
      if (p === null || p.scheduled_at === null) return;
      into[postKey(p, brand.slug, p.scheduled_at)] = p.post_id || "row " + (index + 2);
    });
    return into;
  }

  function reviewPlan(posts, brands, available, existing, tz, now, zonedToUtc) {
    var seen = {};
    return posts.map(function (post, index) {
      var review = reviewPost(post, brands, available, tz, now, zonedToUtc);
      if (review.scheduled_at !== null) {
        var key = postKey(post, post.brand, review.scheduled_at);
        if (existing[key]) review.duplicate_of = existing[key];
        else if (key in seen) review.duplicate_of = "post #" + (seen[key] + 1) + " in this plan";
        else seen[key] = index;
      }
      review.importable = importable(review);
      return review;
    });
  }

  return {
    BRAND_HEADERS: BRAND_HEADERS,
    parseBrands: parseBrands,
    parsePlan: parsePlan,
    reviewPlan: reviewPlan,
    existingKeys: existingKeys,
    rowFor: rowFor,
    mediaKind: mediaKind,
    newPost: newPost,
  };
})();

if (typeof module !== "undefined") module.exports = PP;
