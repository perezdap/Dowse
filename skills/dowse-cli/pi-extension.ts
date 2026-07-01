/**
 * dowse Freshness — pi extension (CLI-only)
 *
 * Calls the existing `dowse hook session-start` command at session start so the
 * local `.dowse_index` stays fresh. Fail-open, identical semantics to the
 * Cursor sessionStart hook (see dowse/cursor_hooks.py: run_session_start_index).
 *
 * Why a hook, not an MCP tool call: the only deterministic job is
 * stale => reindex, and `dowse hook session-start` already implements it
 * (opt-in via `.dowse_index/`, incremental, fail-open). Lifting it into a pi
 * session_start event means agents reach for `dowse query` instead of grep
 * because the index is guaranteed current — the soft "prefer dowse" rule in
 * the dowse-cli skill then reliably holds.
 *
 * Install (one of):
 *   pi --extension skills/dowse-cli/pi-extension.ts
 *   cp skills/dowse-cli/pi-extension.ts ~/.pi/agent/extensions/
 *
 * Requires `dowse` on PATH (`pipx install dowse-context`). Fresh-index skips
 * require dowse-context >= 0.2.4 or an editable checkout with this hook change.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { accessSync, constants, statSync } from "node:fs";
import { delimiter, dirname, isAbsolute, join, relative, resolve } from "node:path";

const COMMAND = "dowse";
const ARGS = ["hook", "session-start"];
const TIMEOUT_MS = 120_000; // first run downloads MiniLM (~80 MB)

type HookPayload =
	| { status: "ok"; workspace: string; db_path: string; indexed_symbols: number }
	| { status: "skipped"; reason: string }
	| { status: "error"; reason: string; detail?: string };

function executableNames(command: string): string[] {
	if (process.platform !== "win32") return [command];
	const extensions = (process.env.PATHEXT || ".COM;.EXE;.BAT;.CMD")
		.split(";")
		.map((ext) => ext.trim().toLowerCase())
		.filter(Boolean);
	return [command, ...extensions.map((ext) => `${command}${ext}`)];
}

function canExecute(path: string): boolean {
	try {
		accessSync(path, constants.X_OK);
		return true;
	} catch {
		return false;
	}
}

function isDirectory(path: string): boolean {
	try {
		return statSync(path).isDirectory();
	} catch {
		return false;
	}
}

function isFile(path: string): boolean {
	try {
		return statSync(path).isFile();
	} catch {
		return false;
	}
}

function isInside(root: string, path: string): boolean {
	const rel = relative(resolve(root), resolve(path));
	return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function resolveFromPath(command: string, blockedRoot: string): string | null {
	for (const entry of (process.env.PATH || "").split(delimiter)) {
		if (!entry || !isAbsolute(entry) || isInside(blockedRoot, entry)) continue;
		for (const name of executableNames(command)) {
			const candidate = join(entry, name);
			if (canExecute(candidate)) return candidate;
		}
	}
	return null;
}

function findOptedInWorkspace(start: string): string | null {
	let current = resolve(start);
	while (true) {
		if (isDirectory(join(current, ".dowse_index")) || isFile(join(current, ".dowse.yaml"))) {
			return current;
		}
		const parent = dirname(current);
		if (parent === current) return null;
		current = parent;
	}
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

function isHookPayload(value: unknown): value is HookPayload {
	if (!isRecord(value) || typeof value.status !== "string") return false;
	if (value.status === "ok") {
		return (
			typeof value.workspace === "string" &&
			typeof value.db_path === "string" &&
			typeof value.indexed_symbols === "number"
		);
	}
	if (value.status === "skipped") {
		return typeof value.reason === "string";
	}
	if (value.status === "error") {
		return typeof value.reason === "string";
	}
	return false;
}

function parseHookPayload(stdout: string): HookPayload | null {
	try {
		const payload: unknown = JSON.parse(stdout.trim());
		return isHookPayload(payload) ? payload : null;
	} catch {
		return null;
	}
}

export default function (pi: ExtensionAPI): void {
	pi.on("session_start", async (event, ctx) => {
		// reload/new/resume/fork/startup all warrant a freshness check; it's a
		// no-op when there is no `.dowse_index/` in cwd or an ancestor.
		void event;

		const workspace = findOptedInWorkspace(ctx.cwd);
		if (!workspace) return;

		const commandPath = resolveFromPath(COMMAND, workspace);
		if (!commandPath) {
			if (ctx.hasUI) {
				ctx.ui.notify("dowse not on PATH; `pipx install dowse-context`", "error");
			}
			return;
		}

		let result: { stdout: string; stderr: string; code: number; killed: boolean };
		try {
			result = await pi.exec(commandPath, ARGS, { cwd: workspace, timeout: TIMEOUT_MS });
		} catch {
			// binary missing / spawn failure — fail open, but tell the user once.
			if (ctx.hasUI) {
				ctx.ui.notify("dowse not on PATH; `pipx install dowse-context`", "error");
			}
			return;
		}

		// Hooks must never block the editor session — always exit clean.
		const payload = parseHookPayload(result.stdout);

		if (!ctx.hasUI) return;

		// Only surface outcomes the user actually cares about; skipped is silent.
		if (result.killed) {
			ctx.ui.notify("dowse reindex timed out (continuing)", "warning");
			return;
		}
		if (payload?.status === "ok" && payload.indexed_symbols > 0) {
			ctx.ui.notify(`dowse: ${payload.indexed_symbols} symbols indexed`, "info");
		} else if (payload?.status === "error") {
			ctx.ui.notify(`dowse index failed: ${payload.reason}`, "warning");
		}
	});
}
