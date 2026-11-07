/**
 * Smoke test: starts the stub API, builds the app with it, runs next start,
 * and tests that all pages render with data, empty, and error states.
 *
 * Usage: node smoke-test.mjs
 * Returns: exit code 0 if all tests pass, 1 if any fail.
 */

import { spawn } from "child_process";
import { existsSync, rmSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const webDir = path.resolve(__dirname, "..");
const projectRoot = path.resolve(__dirname, "../..");

let stubServer = null;
let nextApp = null;
let testsFailed = false;

const STUB_PORT = 8765;
const APP_PORT = 3002;
const STUB_URL = `http://127.0.0.1:${STUB_PORT}`;
const APP_URL = `http://127.0.0.1:${APP_PORT}`;

/**
 * Wait for a port to be listening
 */
async function waitForPort(port, maxWaitMs = 10000) {
  const startTime = Date.now();
  while (Date.now() - startTime < maxWaitMs) {
    try {
      await fetch(`http://127.0.0.1:${port}/`, { method: "HEAD" });
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  return false;
}

/**
 * Start the stub API server
 */
function startStubServer() {
  return new Promise((resolve, reject) => {
    console.log(`Starting stub API on port ${STUB_PORT}...`);
    stubServer = spawn("node", [path.join(__dirname, "stub-api.mjs"), String(STUB_PORT)], {
      stdio: "pipe",
      cwd: projectRoot,
    });

    let ready = false;
    stubServer.stdout.on("data", (data) => {
      const msg = data.toString();
      console.log(`[stub] ${msg.trim()}`);
      if (msg.includes("running on")) {
        ready = true;
        resolve();
      }
    });

    stubServer.stderr.on("data", (data) => {
      console.error(`[stub] ${data.toString()}`);
    });

    stubServer.on("error", reject);
    stubServer.on("exit", (code) => {
      if (code !== 0 && !ready) {
        reject(new Error(`Stub server exited with code ${code}`));
      }
    });

    setTimeout(() => {
      if (ready) return;
      reject(new Error("Stub server did not start in time"));
    }, 5000);
  });
}

/**
 * Build the app with the stub as NEXT_PUBLIC_API_URL
 */
function buildApp() {
  return new Promise((resolve, reject) => {
    console.log("\nBuilding app with stub API...");

    // Clean .next
    const nextDir = path.join(webDir, ".next");
    if (existsSync(nextDir)) {
      rmSync(nextDir, { recursive: true, force: true });
    }

    const build = spawn("npm", ["run", "build"], {
      stdio: "pipe",
      cwd: webDir,
      env: {
        ...process.env,
        NEXT_PUBLIC_API_URL: STUB_URL,
      },
    });

    let output = "";
    build.stdout.on("data", (data) => {
      output += data.toString();
      process.stdout.write(data);
    });

    build.stderr.on("data", (data) => {
      output += data.toString();
      process.stderr.write(data);
    });

    build.on("error", reject);
    build.on("exit", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`Build failed with code ${code}`));
      }
    });

    setTimeout(() => {
      reject(new Error("Build did not complete in time"));
    }, 60000);
  });
}

/**
 * Start next start
 */
function startApp() {
  return new Promise((resolve, reject) => {
    console.log(`\nStarting Next.js app on port ${APP_PORT}...`);
    nextApp = spawn("npm", ["run", "start"], {
      stdio: "pipe",
      cwd: webDir,
      env: {
        ...process.env,
        PORT: String(APP_PORT),
        NEXT_PUBLIC_API_URL: STUB_URL,
      },
    });

    let ready = false;
    nextApp.stdout.on("data", (data) => {
      const msg = data.toString();
      console.log(`[app] ${msg.trim()}`);
      if (msg.includes("ready")) {
        ready = true;
        resolve();
      }
    });

    nextApp.stderr.on("data", (data) => {
      console.error(`[app] ${data.toString()}`);
    });

    nextApp.on("error", reject);
    nextApp.on("exit", (code) => {
      if (code !== 0 && !ready) {
        reject(new Error(`Next.js exited with code ${code}`));
      }
    });

    setTimeout(() => {
      if (ready) return;
      reject(new Error("Next.js did not start in time"));
    }, 15000);
  });
}

/**
 * Test that a URL returns 200 and contains expected text
 */
async function testPage(name, path, expectedTexts = []) {
  console.log(`\nTesting ${name}...`);
  try {
    const response = await fetch(`${APP_URL}${path}`);
    if (response.status !== 200) {
      console.error(`  ✗ Expected 200, got ${response.status}`);
      testsFailed = true;
      return;
    }

    const html = await response.text();

    let allFound = true;
    for (const text of expectedTexts) {
      if (html.includes(text)) {
        console.log(`  ✓ Found: "${text}"`);
      } else {
        console.log(`  ✗ Missing: "${text}"`);
        allFound = false;
        testsFailed = true;
      }
    }

    if (allFound && expectedTexts.length === 0) {
      console.log(`  ✓ Page loaded (${html.length} bytes)`);
    }
  } catch (e) {
    console.error(`  ✗ Error: ${e.message}`);
    testsFailed = true;
  }
}

/**
 * Run all smoke tests
 */
async function runTests() {
  console.log("=== Chess Coach Smoke Test ===\n");

  try {
    // Start stub
    await startStubServer();
    await new Promise((r) => setTimeout(r, 1000));

    // Build
    await buildApp();

    // Start app
    await startApp();
    await new Promise((r) => setTimeout(r, 1000));

    // Test pages
    console.log("\n=== Running tests ===");

    // Landing page (should not show API errors with live stub up)
    await testPage("Landing", "/", [
      "Analyze my games", // Button text
    ]);

    // Progress page with deitythemenace (should show data)
    await testPage("Progress (data)", "/players/deitythemenace", [
      "deitythemenace", // Username
    ]);

    // Progress page with newplayer (should show syncing state)
    await testPage("Progress (syncing)", "/players/newplayer", [
      "newplayer", // Username
    ]);

    // Progress page with nosuchplayer (should show sync error)
    await testPage("Progress (error)", "/players/nosuchplayer", [
      "nosuchplayer", // Username
    ]);

    // Overview page with deitythemenace (should show data)
    await testPage("Overview (data)", "/players/deitythemenace/rapid", [
      "deitythemenace", // Username
      "Rapid", // Time control
    ]);

    // Overview page with newplayer (should show empty state)
    await testPage("Overview (empty)", "/players/newplayer/rapid", [
      "newplayer", // Username
    ]);

    // Not found page
    await testPage("Not found (404)", "/players/this-doesnt-exist-xyz", [
      "404", // Error indicator
    ]);

    console.log("\n=== Test Summary ===");
    if (testsFailed) {
      console.error("Some tests failed");
      process.exit(1);
    } else {
      console.log("All tests passed!");
      process.exit(0);
    }
  } catch (e) {
    console.error("\nFatal error:", e.message);
    process.exit(1);
  } finally {
    // Cleanup
    if (stubServer) {
      console.log("\nShutting down stub server...");
      stubServer.kill();
    }
    if (nextApp) {
      console.log("Shutting down Next.js...");
      nextApp.kill();
    }
  }
}

// Run tests
runTests();
