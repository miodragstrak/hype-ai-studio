import { chromium } from "playwright-core";
import path from "node:path";

const root = path.resolve("..");
const artifacts = path.join(root, "test-artifacts", "manual-smoke");
const browser = await chromium.launch({ executablePath: "/usr/bin/google-chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.setDefaultTimeout(10000);
const consoleErrors = [];
const failedRequests = [];
page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
page.on("requestfailed", request => failedRequests.push(`${request.method()} ${request.url()} ${request.failure()?.errorText}`));
page.on("response", response => { if (response.status() >= 400) failedRequests.push(`${response.status()} ${response.request().method()} ${response.url()}`); });

await page.goto("http://127.0.0.1:5174/projects", { waitUntil: "networkidle" });
await page.getByRole("link", { name: "New project" }).click();
await page.getByLabel("Project title").fill("001C Browser Smoke");
await page.getByLabel("Creative brief").fill("A deterministic local producer workflow verification film.");
await page.getByRole("button", { name: "Create project" }).click();
await page.getByText("Project details").waitFor();

await page.getByRole("link", { name: /Assets/ }).click();
const audioPanel = page.locator(".upload-panel").first();
await audioPanel.getByLabel("AUDIO file").setInputFiles(path.join(artifacts, "smoke-song.wav"));
await audioPanel.getByLabel("Source / owner").fill("Hype QA");
await audioPanel.getByLabel("Usage confirmed").check();
await audioPanel.getByRole("button", { name: "Upload" }).click();
await page.getByText("smoke-song.wav").waitFor();

await page.getByRole("link", { name: /Shots/ }).click();
await page.getByLabel("Title").fill("Opening performance");
await page.getByLabel("Generation prompt").fill("Artist performs under crisp white stage lights");
await page.getByLabel("Duration").fill("1");
await page.getByRole("button", { name: "Add shot" }).click();
await page.getByRole("button", { name: "Generate variant" }).click();
await page.getByRole("button", { name: "Generation active" }).waitFor();
await page.getByRole("button", { name: "Generate another" }).waitFor({ timeout: 20000 });
await page.getByRole("button", { name: "Generate another" }).click();
await page.getByRole("button", { name: "Generation active" }).waitFor();
await page.getByRole("button", { name: "Generate another" }).waitFor({ timeout: 20000 });

await page.getByRole("link", { name: /Review/ }).click();
await page.locator("article").nth(1).waitFor({ timeout: 10000 });
const variants = page.locator("article");
const variantCount = await variants.count();
await variants.nth(0).getByRole("button", { name: "Select" }).click();
await variants.nth(1).getByRole("button", { name: "Select" }).click();
await variants.nth(0).getByRole("button", { name: "Reject" }).click();
await page.getByText("Rejected").waitFor();
await page.getByText("Selected").last().waitFor();
await page.reload({ waitUntil: "networkidle" });
await page.getByText("Selected").last().waitFor();
await page.screenshot({ path: path.join(artifacts, "review-desktop.png"), fullPage: true });

await page.getByRole("link", { name: /Render/ }).click();
const renderButton = page.getByRole("button", { name: "Submit final render" });
if (await renderButton.isDisabled()) throw new Error("render unexpectedly blocked");
await renderButton.click();
await page.getByText("Final video ready").waitFor({ timeout: 30000 });
const finalVideo = page.locator(".completed-render video");
await finalVideo.waitFor();
const finalUrl = await finalVideo.getAttribute("src");
const mediaResponse = await page.request.get(finalUrl);
if (!mediaResponse.ok() || !mediaResponse.headers()["content-type"]?.includes("video/mp4")) throw new Error("final media unavailable");
const downloadUrl = await page.getByRole("link", { name: "Download MP4" }).getAttribute("href");
const downloadResponse = await page.request.get(downloadUrl);
if (!downloadResponse.ok() || !downloadResponse.headers()["content-disposition"]?.includes("attachment")) throw new Error("download unavailable");
await page.screenshot({ path: path.join(artifacts, "render-desktop.png"), fullPage: true });
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({ path: path.join(artifacts, "render-mobile.png"), fullPage: true });

console.log(JSON.stringify({ url: page.url(), projectTitle: "001C Browser Smoke", variants: variantCount, selectedPersisted: true, finalUrl, finalBytes: (await mediaResponse.body()).length, consoleErrors, failedRequests }, null, 2));
await browser.close();
if (consoleErrors.length || failedRequests.length) process.exitCode = 1;
