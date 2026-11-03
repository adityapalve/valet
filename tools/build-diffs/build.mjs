// Build valet/static/diffs.js from @pierre/diffs:  npm ci && node build.mjs
// Slimmed from ~11 MB to ~1 MB: only a few grammars, only Pierre's own themes, and the unused
// WASM / "highlights" backends stubbed out. Also writes THIRD_PARTY_LICENSES.txt for everything
// that ends up in the bundle.
import * as esbuild from "esbuild";
import fs from "node:fs";
import path from "node:path";

const here = path.dirname(new URL(import.meta.url).pathname);
const out = path.resolve(here, "../../valet/static");
const theming = path.join(here, "node_modules/@pierre/theming/dist");

const slim = {
  name: "valet-slim",
  setup(b) {
    b.onResolve({ filter: /^shiki\/langs$/ }, () => ({ path: path.join(here, "langs.js") }));
    b.onResolve({ filter: /^(@pierre\/highlights(\/.*)?|shiki\/wasm|shiki\/engine\/oniguruma)$/ },
      () => ({ path: path.join(here, "unused-backend.js") }));
    // Only Pierre's light/dark themes instead of the ~60 bundled Shiki themes.
    b.onResolve({ filter: /^@pierre\/theming\/themes$/ }, () => ({ path: "pierre-themes", namespace: "valet" }));
    b.onLoad({ filter: /.*/, namespace: "valet" }, () => ({
      resolveDir: here,
      contents: `
        import { createThemeCollection } from ${JSON.stringify(path.join(theming, "modules/createThemeCollection.js"))};
        import { createTheme } from ${JSON.stringify(path.join(theming, "modules/createTheme.js"))};
        import { pierreThemes } from ${JSON.stringify(path.join(theming, "collections/pierre.js"))};
        export const themes = createThemeCollection({ themes: [pierreThemes] });
        export const shikiThemes = createThemeCollection({ themes: [] });
        export { createTheme, pierreThemes };`,
    }));
  },
};

const result = await esbuild.build({
  entryPoints: [path.join(here, "entry.js")], bundle: true, format: "esm", minify: true,
  target: ["es2020"], outfile: path.join(out, "diffs.js"), plugins: [slim], metafile: true,
  legalComments: "none", logLevel: "warning",
});

// Licenses of every package that contributed code to the bundle.
const pkgs = new Map();
for (const input of Object.keys(Object.values(result.metafile.outputs)[0].inputs)) {
  const m = input.match(/^(.*node_modules\/(?:@[^/]+\/)?[^/]+)\//);
  if (m) pkgs.set(m[1], true);
}
let text = "Third-party code bundled in diffs.js (built by tools/build-diffs/build.mjs)\n";
for (const dir of [...pkgs.keys()].sort()) {
  const pj = JSON.parse(fs.readFileSync(path.join(here, dir, "package.json"), "utf8"));
  const lic = fs.readdirSync(path.join(here, dir)).find((f) => /^licen[cs]e/i.test(f));
  text += `\n${"=".repeat(78)}\n${pj.name}@${pj.version}  (${pj.license || "see below"})\n${"=".repeat(78)}\n`;
  text += lic ? fs.readFileSync(path.join(here, dir, lic), "utf8").trim() + "\n" : "(no license file in package)\n";
}
fs.writeFileSync(path.join(out, "THIRD_PARTY_LICENSES.txt"), text);
const kb = Math.round(fs.statSync(path.join(out, "diffs.js")).size / 1024);
console.log(`wrote valet/static/diffs.js (${kb} KB) from ${pkgs.size} packages, and THIRD_PARTY_LICENSES.txt`);
