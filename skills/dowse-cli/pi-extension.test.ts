/**
 * Regression tests for pi-extension.ts session_start notifications.
 *
 * Run with Node >= 23.6 (native TS type stripping):
 *   node --test "skills/dowse-cli/*.test.ts"
 *
 * Pins the fix for the "dowse index failed: index_failed" dead-end warning:
 * error payloads must surface `detail` (the underlying exception) when
 * present, fall back to `reason` when not, and keep skipped/success silent
 * or informational as before.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { after, test } from "node:test";
import register, { failureMessage } from "./pi-extension.ts";

type Notification = { message: string; level: string };

const tempDirs: string[] = [];

after(() => {
	for (const dir of tempDirs) {
		rmSync(dir, { recursive: true, force: true });
	}
});

// Drives the real session_start handler with a stubbed pi API: a temp
// workspace opted in via `.dowse_index/`, a fake `dowse` binary on PATH so
// resolveFromPath succeeds, and canned hook stdout from pi.exec.
async function runSessionStart(payload: unknown): Promise<Notification[]> {
	const workspace = mkdtempSync(join(tmpdir(), "dowse-ext-ws-"));
	tempDirs.push(workspace);
	mkdirSync(join(workspace, ".dowse_index"));

	const bin = mkdtempSync(join(tmpdir(), "dowse-ext-bin-"));
	tempDirs.push(bin);
	const fakeName = process.platform === "win32" ? "dowse.cmd" : "dowse";
	writeFileSync(join(bin, fakeName), "");

	const notifications: Notification[] = [];
	let handler: ((event: unknown, ctx: unknown) => Promise<void>) | undefined;
	const pi = {
		on: (_name: string, fn: (event: unknown, ctx: unknown) => Promise<void>) => {
			handler = fn;
		},
		exec: async () => ({ stdout: JSON.stringify(payload), stderr: "", code: 0, killed: false }),
	};
	const ctx = {
		cwd: workspace,
		hasUI: true,
		ui: {
			notify: (message: string, level: string) => notifications.push({ message, level }),
		},
	};

	const savedPath = process.env.PATH ?? "";
	process.env.PATH = `${bin}${delimiter}${savedPath}`;
	try {
		register(pi as never);
		assert.ok(handler, "extension must register a session_start handler");
		await handler({}, ctx);
	} finally {
		process.env.PATH = savedPath;
	}
	return notifications;
}

test("error payload with detail surfaces the underlying exception", { concurrency: false }, async () => {
	const notifications = await runSessionStart({
		status: "error",
		reason: "index_failed",
		workspace: "w",
		detail: "zvec database is locked by another process",
	});
	assert.deepEqual(notifications, [
		{ message: "dowse index failed: zvec database is locked by another process", level: "warning" },
	]);
});

test("error payload without detail falls back to reason", { concurrency: false }, async () => {
	const notifications = await runSessionStart({ status: "error", reason: "index_failed" });
	assert.deepEqual(notifications, [{ message: "dowse index failed: index_failed", level: "warning" }]);
});

test("skipped outcomes stay silent", { concurrency: false }, async () => {
	for (const reason of ["index_fresh", "no_opted_in_workspace"]) {
		assert.deepEqual(await runSessionStart({ status: "skipped", reason }), []);
	}
});

test("successful reindex keeps the info notification", { concurrency: false }, async () => {
	const notifications = await runSessionStart({
		status: "ok",
		workspace: "w",
		db_path: "d",
		indexed_symbols: 12,
	});
	assert.deepEqual(notifications, [{ message: "dowse: 12 symbols indexed", level: "info" }]);
});

test("reindex with zero new symbols stays silent", { concurrency: false }, async () => {
	const notifications = await runSessionStart({
		status: "ok",
		workspace: "w",
		db_path: "d",
		indexed_symbols: 0,
	});
	assert.deepEqual(notifications, []);
});

test("failureMessage clips long details and collapses whitespace", () => {
	const clipped = failureMessage({ reason: "index_failed", detail: "x".repeat(500) });
	assert.equal(clipped.length, "dowse index failed: ".length + 200);
	assert.ok(clipped.endsWith("…"));

	const multiline = failureMessage({ reason: "index_failed", detail: " line one\n\tline two " });
	assert.equal(multiline, "dowse index failed: line one line two");

	assert.equal(failureMessage({ reason: "index_failed", detail: "   " }), "dowse index failed: index_failed");
});
