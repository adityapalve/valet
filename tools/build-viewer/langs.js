// Replaces "shiki/langs" (~200 grammars) with the few that agents' scratch code uses.
// Anything else is rendered as plain text by the viewer.
export const bundledLanguages = {
  python: () => import("shiki/langs/python.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
  shellscript: () => import("shiki/langs/shellscript.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  r: () => import("shiki/langs/r.mjs"),
};
