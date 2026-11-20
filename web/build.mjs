import { build } from "esbuild";
import { readFile, mkdir, writeFile } from "node:fs/promises";

const result = await build({ entryPoints: ["confirmation.js"], bundle: true,
  format: "esm", target: "es2022", minify: true, write: false, legalComments: "inline",
  drop: ["console", "debugger"] });
const template = await readFile("confirmation.html", "utf8");
const code = result.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
await mkdir("../tbank_mcp/ui", { recursive: true });
await writeFile("../tbank_mcp/ui/confirmation.html", template.replace("/* BUNDLED_APP */", () => code));
