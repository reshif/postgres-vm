import { createHash } from "node:crypto";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

async function htmlFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return htmlFiles(path);
    return entry.name.endsWith(".html") ? [path] : [];
  }));
  return nested.flat();
}

const hashes = new Set();
for (const file of await htmlFiles("out")) {
  const html = await readFile(file, "utf8");
  for (const match of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
    hashes.add(`'sha256-${createHash("sha256").update(match[1]).digest("base64")}'`);
  }
}

const policy = [
  "default-src 'self'",
  "img-src 'self' data:",
  "style-src 'self' 'unsafe-inline'",
  `script-src 'self' ${[...hashes].join(" ")}`,
  "connect-src 'self'",
  "font-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'"
].join("; ");
await writeFile("out/csp-hashes.conf", `set $memory_csp \"${policy}\";\n`);
