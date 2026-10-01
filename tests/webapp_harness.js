// Runs webapp/Logic.js on a JSON case from stdin; see test_webapp_parity.py.
const PP = require("../webapp/Logic.js");

function tzOffsetMs(utcMs, tz) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(new Date(utcMs)).map(p => [p.type, p.value]),
  );
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  return asUtc - utcMs;
}

function zonedToUtc(y, m, d, hh, mm, ss, tz) {
  const wall = Date.UTC(y, m - 1, d, hh, mm, ss);
  const first = wall - tzOffsetMs(wall, tz);
  return wall - tzOffsetMs(first, tz);
}

let input = "";
process.stdin.on("data", c => (input += c));
process.stdin.on("end", () => {
  const c = JSON.parse(input);
  const brands = PP.parseBrands(c.brands.headers, c.brands.rows);
  const existing = {};
  for (const [slug, tab] of Object.entries(c.tabs)) {
    if (brands[slug]) PP.existingKeys(brands[slug], tab.headers, tab.rows, c.tz, zonedToUtc, existing);
  }
  const parsed = PP.parsePlan(c.plan);
  const reviews = PP.reviewPlan(parsed.posts, brands, c.files, existing, c.tz, c.now_ms, zonedToUtc);
  process.stdout.write(JSON.stringify({ brands, problems: parsed.problems, reviews }));
});
