// Derives size-adjust / ascent / descent overrides for the local fallback fonts
// so the swap to Geist does not shift layout. Usage: node visual/measure-fallback.mjs
import { chromium } from "@playwright/test";
import fs from "node:fs";

const font = (file) => `data:font/woff2;base64,${fs.readFileSync(new URL(`../src/fonts/${file}`, import.meta.url)).toString("base64")}`;
const sample = "Staff platform engineer · Northwind Systems — Follow up with recruiter 2026-11-05 ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz 0123456789";
const browser = await chromium.launch();
const page = await browser.newPage();
await page.setContent(`<style>
@font-face{font-family:G;src:url(${font("geist-latin.woff2")})}
@font-face{font-family:M;src:url(${font("geist-mono-latin.woff2")})}
</style><span style="font-family:G">x</span><span style="font-family:M">x</span>`);
await page.evaluate(() => document.fonts.ready);
const result = await page.evaluate((text) => {
  const ctx = document.createElement("canvas").getContext("2d");
  const measure = (family) => {
    ctx.font = `100px ${family}`;
    const m = ctx.measureText(text);
    return { width: m.width, ascent: m.fontBoundingBoxAscent, descent: m.fontBoundingBoxDescent };
  };
  const out = {};
  for (const [web, local] of [["G", "Arial"], ["M", "Menlo"], ["M", "Courier New"]]) {
    const w = measure(web), l = measure(local);
    const size = w.width / l.width;
    out[`${web}->${local}`] = {
      sizeAdjust: `${(size * 100).toFixed(2)}%`,
      ascentOverride: `${((w.ascent / size)).toFixed(2)}%`,
      descentOverride: `${((w.descent / size)).toFixed(2)}%`,
    };
  }
  return out;
}, sample);
console.log(JSON.stringify(result, null, 2));
await browser.close();
