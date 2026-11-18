// One-off: evaluates the prototype's SEED block and writes JSON for seed.py.
// Usage: node backend/scripts/dump_seed.mjs reference/prototype-original.html backend/app/seed_data.json
import { readFileSync, writeFileSync } from "node:fs";
import vm from "node:vm";

const [src, out] = process.argv.slice(2);
const lines = readFileSync(src, "utf8").split("\n");
const start = lines.findIndex(l => l.startsWith("const USE_MOCK"));
const end = lines.findIndex(l => l.startsWith("/* ---------- mock backend"));
const code = lines.slice(start, end).join("\n").replace(/^const /gm, "var ") + "\n;this.__OUT = { SEED, ORG, ME };";
const ctx = {};
vm.createContext(ctx);
vm.runInContext(code, ctx);
// Summary-only cases get placeholder gap/event rows at seed time; mark them synthetic.
ctx.__OUT.SEED.otherCases.forEach(c => { c.synthetic_placeholder_rows = true; });
ctx.__OUT.dumped_on = new Date().toISOString().slice(0, 10);
writeFileSync(out, JSON.stringify(ctx.__OUT, null, 2));
console.log("wrote", out, Object.keys(ctx.__OUT.SEED).join(","));
