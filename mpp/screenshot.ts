import crypto from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const pngMagic = Buffer.from("89504e470d0a1a0a", "hex");

export async function screenshot(): Promise<{ pngBase64: string; sha256: string }> {
  const chrome = process.env.MPP_CHROME_BIN ?? "";
  if (!chrome) throw new Error("MPP_CHROME_BIN is required for the controlled screenshot task");
  const directory = mkdtempSync(join(tmpdir(), "checkoutgym-mpp-"));
  const output = join(directory, "alpha.png");
  const profile = join(directory, "chrome-profile");
  try {
    await execFileAsync(chrome, [
      "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
      "--disable-extensions", "--disable-sync", `--user-data-dir=${profile}`,
      "--window-size=800,600", `--screenshot=${output}`,
      new URL("./fixtures/alpha.html", import.meta.url).href,
    ], { timeout: 15000 });
    const png = readFileSync(output);
    if (!png.subarray(0, 8).equals(pngMagic)) throw new Error("browser did not produce a PNG");
    return {
      pngBase64: png.toString("base64"),
      sha256: crypto.createHash("sha256").update(png).digest("hex"),
    };
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}
