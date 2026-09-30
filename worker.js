const TICKER = "AXON";
const LOCK_DATE = "2026-09-28";
const KV_KEY = "axon";
export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(savePrice(env));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/prices") return readPrice(env);
    return env.ASSETS.fetch(request);
  },
};
async function readPrice(env) {
  const saved = await env.PRICES.get(KV_KEY);
  if (!saved) {
    return Response.json(
      { error: "Price is not ready yet. It refreshes every 5 minutes." },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  return new Response(saved, {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "public, max-age=300",
    },
  });
}
async function savePrice(env) {
  try {
    const payload = await fetchPrices();
    await env.PRICES.put(KV_KEY, JSON.stringify(payload));
  } catch (error) {
    console.log(error instanceof Error ? error.message : "price refresh skipped");
  }
}
async function fetchPrices() {
  const period1 = midnightNewYork(LOCK_DATE);
  const period2 = Math.floor(Date.now() / 1000) + 86400;
  const url =
    "https://query1.finance.yahoo.com/v8/finance/chart/" +
    `${TICKER}?interval=1d&period1=${period1}&period2=${period2}`;
  const response = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
  if (!response.ok) throw new Error("price lookup failed");
  const body = await response.json();
  const chart = body.chart || {};
  if (chart.error) throw new Error(chart.error.description || "price lookup failed");
  const result = (chart.result || [])[0];
  if (!result) throw new Error("price lookup failed");
  const meta = result.meta || {};
  const timestamps = result.timestamp || [];
  const closes = ((result.indicators || {}).quote || [{}])[0].close || [];
  const byDate = new Map();
  for (let i = 0; i < timestamps.length; i += 1) {
    const close = closes[i];
    if (typeof close !== "number") continue;
    const tradedOn = nyDate(timestamps[i]);
    if (tradedOn < LOCK_DATE) continue;
    byDate.set(tradedOn, close);
  }
  const points = [...byDate.keys()].sort().map((date) => ({ date, close: byDate.get(date) }));
  const latest = latestQuote(meta);
  if (latest.price != null && latest.time) {
    const liveDay = nyDate(latest.time);
    if (liveDay >= LOCK_DATE) {
      const last = points[points.length - 1];
      if (last && last.date === liveDay) last.close = latest.price;
      else if (!last || last.date < liveDay) points.push({ date: liveDay, close: latest.price });
    }
  }
  return {
    ticker: meta.symbol || TICKER,
    currency: meta.currency || "USD",
    points,
    latest,
  };
}
function latestQuote(meta) {
  const candidates = [];
  for (const [priceKey, timeKey] of [
    ["preMarketPrice", "preMarketTime"],
    ["regularMarketPrice", "regularMarketTime"],
    ["postMarketPrice", "postMarketTime"],
  ]) {
    const price = meta[priceKey];
    const stamp = meta[timeKey];
    if (typeof price === "number" && typeof stamp === "number") candidates.push([stamp, price]);
  }
  if (!candidates.length) return { price: null, time: null };
  const [time, price] = candidates.reduce((best, item) => (item[0] > best[0] ? item : best));
  return { price, time };
}
function midnightNewYork(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  for (const offsetHours of [4, 5]) {
    const candidate = Date.UTC(year, month - 1, day, offsetHours, 0, 0);
    if (nyDate(candidate / 1000) === isoDate && nyHour(candidate) === 0) {
      return Math.floor(candidate / 1000);
    }
  }
  return Math.floor(Date.UTC(year, month - 1, day, 4, 0, 0) / 1000);
}
function nyHour(utcMs) {
  const hour = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    hourCycle: "h23",
  }).format(new Date(utcMs));
  return Number(hour);
}
function nyDate(unixSeconds) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(unixSeconds * 1000));
  const value = (type) => parts.find((part) => part.type === type).value;
  return `${value("year")}-${value("month")}-${value("day")}`;
}
